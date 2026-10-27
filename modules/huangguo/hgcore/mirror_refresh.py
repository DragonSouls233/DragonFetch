# -*- coding: utf-8 -*-
"""从官方「回家的路」发布页刷新备用域名：读取 → 提取 → 校验 → 清理 → 持久化。

用户诉求：读发布页更新域名，并把**不能用的删掉**。

设计要点
--------
* 发布页 / 中转页（huangguoai.ai、huangguo10.com、huangguo8.com、*.pages.dev、
  *.gitlab.io、/go-home/）本身**不是剧集站**：HTTP 200 但页面里没有
  hg-drama-card，拿它当 base_url 会把列表解析成 0 条。它们只能用来「读出最新的
  入口域名」，因此单独放在站点定义的 ``publish_pages`` 里。
* 校验分两层，缺一不可：
    1) **网络层**：请求成功且有内容；
    2) **内容层**：页面必须含该站点的 marker（huangguoai 是 ``hg-drama-card``）。
  发布页会卡在第 2 层 —— 这样写进「镜像列表」的一定是**真能下载的站点地址**。
* 现有镜像（内置 + 已保存）同样跑一遍校验，**不可达 / 非本站的直接剔除**。
* 结果写入设置 ``site_mirrors``（可在设置面板「域名管理」里看到、管理）。
* 当前 BASE_URL 若已失效，自动切换到校验通过的第一个。

离线可测：``refresh(fetch_fn=...)`` 可注入假实现，不发任何网络请求。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from . import applog, config, settings

# 发布页 / 社交 / 商务等**非剧集站**域名：提取候选时排除。
# （注意：这里排除的只是「不作为镜像」，不影响它们作为 publish_pages 被读取。）
_NEVER_MIRROR_HOSTS = {
    # 黄果自身的发布页 / 中转页 / 主域
    "huangguoai.com", "huangguoai.ai", "huangguo10.com", "huangguo8.com",
    "huangguoai.pages.dev", "huangguoai.gitlab.io",
    # 社交 / 邮箱 / 商务 / 代码托管
    "t.me", "telegram.me", "x.com", "twitter.com", "gmail.com",
    "google.com", "facebook.com", "github.com", "gitlab.com", "gitlab.io",
    "qq.com", "163.com", "weixin.qq.com", "outlook.com",
    # 发布页里夹带的统计 / 图床 / CDN（实测会被误提取，纯噪声）
    "googletagmanager.com", "google-analytics.com", "gstatic.com",
    "googlesyndication.com", "doubleclick.net", "jsdelivr.net",
    "cloudflare.com", "unpkg.com", "bootcdn.net", "baidu.com",
    "hm.baidu.com", "cnzz.com", "51.la",
}

# 只认这些后缀的域名，避免把 "1.2" / "v2.0" 之类的文本误当域名。
_OK_TLD = re.compile(r"\.(cc|com|net|org|io|ai|dev|xyz|top|cyou|info|app|me|cn|tv|site|online|club)$", re.I)

_URL_RE = re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9.\-]*)(?::\d+)?")
_BARE_RE = re.compile(r"(?<![A-Za-z0-9.\-/])((?:[a-z0-9][a-z0-9\-]{0,30}\.)+[a-z]{2,10})(?![A-Za-z0-9.\-])")

MAX_CANDIDATES = 24          # 单次校验上限，防止发布页噪声拖慢
_PREFER_HTTPS = "https://"


# ---------------------------------------------------------------- 工具

def _norm(url: str) -> str:
    """规整基址：补协议、去尾斜杠。"""
    u = (url or "").strip()
    if not u:
        return ""
    if not u.lower().startswith(("http://", "https://")):
        u = _PREFER_HTTPS + u
    return u.rstrip("/")


def _host(url: str) -> str:
    return config._site_domain_of(url)


# ---------------------------------------------------------------- 提取

def extract_candidates(html: str, page_url: str = "") -> List[str]:
    """从发布页 HTML 提取「疑似站点入口」的域名（小写、去重、保序）。

    宽松提取 + 后续内容校验：宁可多提（由 validate 把关），不可漏提。
    """
    if not html:
        return []
    page_host = _host(page_url)
    out: List[str] = []
    seen = set()

    def add(host: str) -> None:
        h = (host or "").lower().strip().strip(".")
        if not h or h in seen:
            return
        if h in _NEVER_MIRROR_HOSTS:
            return
        if page_host and (h == page_host or h.endswith("." + page_host)):
            return
        if "." not in h or not _OK_TLD.search(h):
            return
        if h.startswith("www."):          # 统一去掉 www 前缀
            h = h[4:]
            if h in seen:
                return
        seen.add(h)
        out.append(h)

    for m in _URL_RE.finditer(html):
        add(m.group(1))
    for m in _BARE_RE.finditer(html):
        add(m.group(1))
    return out


# ---------------------------------------------------------------- 校验

def _default_fetch(url: str, timeout: int = 8, use_proxy: bool = False) -> str:
    """抓取页面文本。

    ``use_proxy=False``（默认）刻意 ``trust_env=False``：**不跟随系统/环境代理**。
      备用域名是「国内直连」入口，若经系统代理探测会假性可达，掩盖
      「直连被墙」这一真实情况，最终选中用户根本连不上的域名。

    ``use_proxy=True``：**跟随系统代理**（读 HTTP(S)_PROXY / 注册表 IE 设置）。
      只用于一种兜底场景：**直连连发布页都打不开**时。用户开了全局代理，
      浏览器能进发布页，但程序若一律强制直连，就永远读不到最新域名
      —— 本参数正是为此存在的「读发布页」通道。

    关键约束：代理只用于**读发布页**；镜像**校验一律走直连**（见 refresh），
    因此哪怕开着代理刷新，写进列表的也一定是本机能直连下载的域名。
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s = requests.Session()
    s.trust_env = bool(use_proxy)
    r = s.get(
        _norm(url) + "/",
        headers={"User-Agent": config.USER_AGENT, "Referer": _norm(url) + "/"},
        timeout=timeout,
        verify=False,
        allow_redirects=True,
    )
    return r.text or ""


def _content_ok(html: str, site: dict) -> Tuple[bool, int, str]:
    """内容层判定：页面是否属于该站点的真站点（含 marker）。

    抽出来单独一个函数，是因为「直连校验」与「代理兜底校验」都要用，
    必须保证两边判定口径完全一致，否则会出现「直连判死、代理判活」的错觉。
    """
    if not html:
        return False, 0, "空响应"
    # 强标记门槛：必须出现「卡片容器」，否则只是发布页/中转页/空壳，
    # 写进 mirrors 会让程序把列表解析成 0 条（用户看到「打不开」）。
    strong = _strong_markers_of(site)
    s_hits = sum(html.count(m) for m in strong) if strong else 0
    if strong and s_hits <= 0:
        return False, s_hits, "无剧集卡片(是发布页而非真站点)"
    markers = _markers_of(site)
    hits = sum(html.count(m) for m in markers) if markers else len(html)
    if markers and hits <= 0:
        return False, hits, "非本站页面"
    return True, hits, ""


def _markers_of(site: dict) -> List[str]:
    return list(site.get("markers") or [])


def _strong_markers_of(site: dict) -> List[str]:
    """「必须是真站点」才允许出现的强标记。

    ⚠ 为什么需要它（实测踩坑）：站点 ``markers`` 是**识别用**的宽松条件，
    「至少命中一个即认为该站」。但发布页 / 中转页里也会残留同样的字样
    （实测 hgai5.com：``hg-drama-card`` 为 0，``video_list`` 却出现 1 次，
    标题是「黄果短剧 · 官方最新地址入口」），于是发布页被误判为真站点 →
    写进 mirrors → 程序拿它当 base_url → 列表解析成 0 条，用户看到「打不开」。

    这里改用只出现在**真实剧集列表页**的标记做准入门槛：卡片容器
    （hg-drama-card / poster-card / hg-comic-feed-card）至少命中一个。
    """
    strong = list(site.get("strong_markers") or [])
    if strong:
        return strong
    # 未显式声明时，从 markers 里挑出「卡片容器」类标记作为强标记，
    # 排除 video_list / lifan-dongman 这类泛用字样。
    generic = ("video_list", "lifan-dongman", "hanman")
    return [m for m in (site.get("markers") or []) if m not in generic]


def validate_mirror(url: str, site: dict, timeout: int = 8,
                    fetch_fn: Optional[Callable[[str], str]] = None) -> Tuple[bool, int, str]:
    """内容层校验某个基址是否为「该站点的真站点」。

    返回 ``(是否可用, marker 命中数, 错误说明)``。
    """
    fetch = fetch_fn or (lambda u: _default_fetch(u, timeout))
    try:
        html = fetch(url)
    except Exception as exc:                       # noqa: BLE001
        return False, 0, type(exc).__name__
    return _content_ok(html, site)


# ---------------------------------------------------------------- 主流程

def refresh(site_id: Optional[str] = None,
            timeout: int = 8,
            save: bool = True,
            fetch_fn: Optional[Callable[[str], str]] = None,
            workers: int = 10,
            allow_proxy: bool = True,
            log: Optional[Callable[[str], None]] = None) -> Dict:
    """读取发布页 → 提取候选 → 校验 → 合并去重 → 剔除失效 → 持久化。

    返回统计字典（键见下方 ``res`` 初始化）。``fetch_fn`` 可注入，便于离线测试。

    allow_proxy=True（默认）时，若**直连读不到发布页**，会用系统代理再试一次
    —— 用户开着全局代理时浏览器能进发布页、程序却全是直连请求，就属此场景。
    注意代理**只用于读发布页**：域名校验始终走直连，保证写入的地址本机可下载。
    """
    res: Dict = {
        "site_id": "", "publish_pages": [], "publish_page_used": "",
        "discovered": [], "verified": [], "kept": [], "removed": [],
        "added": [], "mirrors": [], "switched_to": "",
        "fetched_via_proxy": False, "proxy_only": [], "errors": [],
    }

    def say(msg: str) -> None:
        if log:
            log(msg)
        else:
            applog.info(msg)

    site = config.get_site(site_id) if site_id else config.ACTIVE_SITE
    if not site:
        res["errors"].append("未找到站点定义")
        return res
    res["site_id"] = site["id"]

    # 1) 依次尝试发布页，第一个能给出候选的就用它
    def _fetch_page(page: str) -> Tuple[str, bool]:
        """抓发布页：直连优先，直连失败/为空时回退系统代理。

        返回 ``(html, 是否经代理)``。注入 ``fetch_fn``（离线测试）时不走代理回退。
        """
        if fetch_fn is not None:
            return fetch_fn(page), False
        first_err: Optional[Exception] = None
        try:
            html = _default_fetch(page, timeout, use_proxy=False)
            if (html or "").strip():
                return html, False
        except Exception as exc:                   # noqa: BLE001
            first_err = exc
        if not allow_proxy:
            if first_err:
                raise first_err
            return "", False
        # 直连拿不到内容 → 试系统代理（用户可能开着全局代理）
        return _default_fetch(page, timeout, use_proxy=True), True

    discovered: List[str] = []
    for page in config.publish_pages(site["id"]):
        res["publish_pages"].append(page)
        try:
            html, via_proxy = _fetch_page(page)
        except Exception as exc:                   # noqa: BLE001
            res["errors"].append(f"{page} -> {type(exc).__name__}")
            continue
        cands = extract_candidates(html, page)
        if cands:
            discovered = cands
            res["publish_page_used"] = page
            res["fetched_via_proxy"] = via_proxy
            if via_proxy:
                say("直连读不到发布页，已改用系统代理读取：%s" % page)
            break
    res["discovered"] = discovered

    # 2) 候选集合 = 新发现 + 内置镜像 + 已保存镜像（保序去重）
    old = []
    for u in list(site.get("mirrors") or []) + \
            list(config.SITE_MIRRORS.get(site["id"]) or []):
        n = _norm(u)
        if n and n not in old:
            old.append(n)
    cand_list: List[str] = []
    for u in discovered + old:
        n = _norm(u)
        if n and n not in cand_list:
            cand_list.append(n)
    cand_list = cand_list[:MAX_CANDIDATES]

    # 3) 并发做内容校验
    results: Dict[str, Tuple[bool, int, str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(cand_list) or 1))) as ex:
        futs = {ex.submit(validate_mirror, u, site, timeout, fetch_fn): u
                for u in cand_list}
        for fut, u in futs.items():
            try:
                results[u] = fut.result()
            except Exception as exc:               # noqa: BLE001
                results[u] = (False, 0, type(exc).__name__)

    verified = [u for u in cand_list if results.get(u, (False,))[0]]
    dead = [u for u in cand_list if not results.get(u, (False,))[0]]
    res["verified"] = verified
    res["removed"] = dead
    old_norm = set(old)
    res["kept"] = [u for u in verified if u in old_norm]
    res["added"] = [u for u in verified if u not in old_norm]
    res["mirrors"] = verified

    for u in dead:
        say("镜像校验失败(已剔除)：%s（%s）" % (u, results.get(u, (False, 0, ""))[2]))
    for u in res["added"]:
        say("发现新的可用镜像：%s" % u)

    # 3.5) 直连一个都不通时：用系统代理再校验一遍，**只作诊断上报、绝不入列表**。
    #      写进 site_mirrors 的地址必须是本机能直连下载的，否则会「看着能用、下不了」。
    if not verified and allow_proxy and fetch_fn is None:
        for u in cand_list:
            try:
                html = _default_fetch(u, timeout, use_proxy=True)
            except Exception:                      # noqa: BLE001
                continue
            ok, _hits, _why = _content_ok(html, site)
            if ok:
                res["proxy_only"].append(u)
        if res["proxy_only"]:
            say("注意：以下域名本机直连不通、只有经代理才能打开，已排除：%s"
                % "、".join(res["proxy_only"]))

    if not verified:
        res["errors"].append("没有任何校验通过的镜像，保持原配置不变")
        return res

    # 4) 持久化（写「最近一次校验通过的可用域名」，便于设置面板查看与管理）
    if save:
        try:
            config.SITE_MIRRORS = {**config.SITE_MIRRORS, site["id"]: verified}
            settings.save({"site_mirrors": config.SITE_MIRRORS})
        except Exception as exc:                   # noqa: BLE001
            res["errors"].append("保存镜像失败：%s" % exc)

    # 5) 当前生效域名已失效 → 切到第一个可用的
    cur = _norm(config.BASE_URL)
    if cur not in verified:
        target = verified[0]
        config.BASE_URL = target
        config.ACTIVE_SITE["base_url"] = target
        config.ACTIVE_SITE["home_url"] = target + "/"
        config._rebuild_urls()
        res["switched_to"] = target
        if save:
            try:
                settings.save({"base_url": target})
            except Exception as exc:               # noqa: BLE001
                res["errors"].append("保存 base_url 失败：%s" % exc)
        say("当前域名不可用，已切换为：%s" % target)

    return res
