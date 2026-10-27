# -*- coding: utf-8 -*-
"""站点自动发现：并发探测候选主页，识别可达站点并切换生效。

每次启动时：
  1. 并发探测注册表中的所有候选地址（跨多个「黄果」站点）
  2. 用各站点的 markers 识别页面真正归属哪个站点（避免误判）
  3. 按优先级选择第一个可达站点（huangguoai 优先，go-home 优先于根域）
  4. 写入 config 生效 + 记录到 SQLite（DB 记录 active_site / 健康检查历史）

结果可在设置面板「重新检测」时再次触发。
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

from . import applog, config, site_db, sites
from .http_client import fetch_text


def probe(url: str, site_id: str, timeout: int = 8) -> Tuple[bool, int, int, str, str]:
    """探测单个候选 URL。

    返回: (是否可用, HTTP 状态码, 耗时 ms, HTML, 识别到的 site_id)
    """
    start = time.time()
    try:
        html = fetch_text(url, referer=url, timeout=timeout)
        latency = int((time.time() - start) * 1000)
        # 用该候选所属站点的 markers 判断；再用全局 identify 兜底识别
        site = sites.get_site(site_id)
        ok = bool(site and any(m in html for m in site.get("markers", [])))
        if not ok:
            real = sites.identify(html)
            ok = real is not None
            if real:
                site_id = real["id"]
        return ok, 200, latency, html, site_id
    except Exception as exc:                          # noqa: BLE001
        latency = int((time.time() - start) * 1000)
        code = getattr(getattr(exc, "response", None), "status_code", 0) or 0
        return False, code, latency, "", site_id


def _detail_score(html: str) -> int:
    """粗略评估页面是否像「剧集列表页」：/detail/ 或 /video/ 链接越多越像首页。

    站点前端把卡片链接从 /detail/{id}/ 切换到了 /video/{id}/（二者内容等价），
    故两种都计入，否则所有候选项评分恒为 0，会误判「不像列表页」而退回首页。
    """
    if not html:
        return 0
    return html.count("/detail/") + html.count("/video/")


def discover(timeout: int = 8,
             prefer_site_id: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """并发探测所有候选，返回 (site_id, home_url) 或 None。

    选择规则（依次）:
      1. 若 prefer_site_id 对应的站点有可达候选，优先选它（保留用户当前选择）
      2. 否则按站点注册表优先级（列表靠前者优先）
      3. 同站点内：更像列表页的候选优先（/detail/ 链接更多），再按定义顺序
      4. 若最终候选不像列表页（detail 链接过少，如发布页），退回站点规范首页
    """
    # (url, site_id, order)  —— order 用于同站点内稳定排序
    plan: List[Tuple[str, str, int]] = []
    seen_urls = set()
    order = 0

    def add(url: str, site_id: str) -> None:
        key = (url or "").strip().rstrip("/")
        if not key or key in seen_urls:
            return
        seen_urls.add(key)
        plan.append((key + "/", site_id, order))

    for site in sites.all_sites():
        for url in site.get("candidates", []):
            add(url, site["id"])
            order += 1
        # 已保存的镜像（含「从发布页刷新」刚写回的那些）也参与探测。
        # 否则站点一改址，内置 candidates 全成死地址 → 永远识别不到，
        # 只能靠人去改代码；而发布页刷新写回的域名本就在 SITE_MIRRORS 里。
        for url in list(site.get("mirrors") or []) + \
                list(config.SITE_MIRRORS.get(site["id"]) or []):
            add(url, site["id"])
            order += 1

    results = {}   # url -> (ok, real_sid, detail_score)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(probe, url, sid, timeout): (url, sid)
                   for url, sid, _ in plan}
        for fut in as_completed(futures):
            url, sid = futures[fut]
            try:
                ok, code, latency, html, real_sid = fut.result()
            except Exception:                         # noqa: BLE001
                ok, code, latency, html, real_sid = False, 0, 0, "", sid
            site_db.log_check(url, ok, code, latency, real_sid)
            results[url] = (ok, real_sid, _detail_score(html))

    # 可达候选，按「实际识别出的站点」归类
    reachable = [item for item in plan if results.get(item[0], (False,))[0]]
    if not reachable:
        applog.bug("站点识别：所有候选主页均不可达（可能是站点改址/网络故障），候选数=%d", len(plan))
        return None

    def sort_key(item):
        url, planned_sid, order = item
        _ok, real_sid, score = results.get(url, (False, planned_sid, 0))
        real_sid = real_sid or planned_sid
        prefer_rank = 0 if (prefer_site_id and real_sid == prefer_site_id) else 1
        return (prefer_rank, sites.priority_of(real_sid), -score, order)

    reachable.sort(key=sort_key)
    url, planned_sid, _ = reachable[0]
    _ok, real_sid, score = results.get(url, (False, planned_sid, 0))
    real_sid = real_sid or planned_sid

    site = sites.get_site(real_sid)
    if site and score < 5:
        # 探测页面不像剧集列表（如「最新地址发布页」）→ 用站点规范首页
        return real_sid, site.get("home_url") or url
    return real_sid, url


def apply_discovery(timeout: int = 8, prefer_current: bool = True,
                    auto_refresh: bool = True) -> Optional[str]:
    """执行发现流程并通过 config / DB 生效，返回最终生效的 site_id。

    prefer_current=True 时，若当前站点仍可达则保持不变（避免覆盖用户选择），
    仅在当前站点不可达时切换到其它可达站点。

    auto_refresh=True（默认）时，若**内置候选 + 已保存镜像全部不可达**，
    则先自动读一次官方发布页刷新最新域名，再重试识别 —— 这就是「站点改址了，
    程序自己找到新地址」的闭环，无需人去改代码。全部失败返回 None（config 保持原值）。
    """
    site_db.init_db()
    prefer = config.ACTIVE_SITE_ID if prefer_current else None
    found = discover(timeout=timeout, prefer_site_id=prefer)

    if not found and auto_refresh:
        try:
            from . import mirror_refresh
            applog.info("站点识别：所有候选项均不可达，尝试读取发布页获取最新域名…")
            res = mirror_refresh.refresh(save=True)
            verified = res.get("verified") or []
            if verified:
                applog.info("发布页刷新得到 %d 个可用域名，重新识别站点（%s）",
                            len(verified), "、".join(verified[:3]))
                found = discover(timeout=timeout, prefer_site_id=prefer)
            else:
                hint = ("（另有 %d 个域名仅经代理可达，不能用于直连下载）"
                        % len(res.get("proxy_only") or [])) if res.get("proxy_only") else ""
                applog.bug("站点识别：发布页刷新仍未取得可用域名%s：%s",
                           hint, res.get("errors"))
        except Exception as exc:                   # noqa: BLE001
            applog.bug("站点识别：发布页刷新异常：%s", exc)

    if not found:
        return None

    site_id, home_url = found
    site = sites.get_site(site_id)
    if not site:
        applog.bug("站点识别：识别到未知站点 id=%s，已忽略", site_id)
        return None

    # 应用：保留用户原有代理/并发/分片不变，仅切换站点
    config.set_active_site(site)
    # 自动跳过被屏蔽的域名：从规范域名 + 镜像候选里挑一个可达的作为当前 BASE_URL
    config.resolve_active_base_url()
    home_url = config.BASE_URL + "/"
    config.ACTIVE_SITE["home_url"] = home_url
    config._rebuild_urls()
    applog.info("站点识别完成：%s（%s）", site["name"], home_url)

    # 持久化
    site_db.set_active_site(site_id)
    site_db.set_kv("active_home", home_url)
    site_db.set_kv("last_discovery", str(int(time.time())))
    return site_id


def current_from_db() -> str:
    """读取 DB 中上次生效的站点 id。"""
    return site_db.get_active_site()
