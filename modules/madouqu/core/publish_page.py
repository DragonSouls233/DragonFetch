# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""麻豆地址发布页：读发布页 → 提域名 → 校验 → 持久化。

为什么需要它
------------
麻豆区站点域名换得很勤（实测 2026-10：内置的 .sbs/.club/.cc/.net/.org 里
多数早已 TLS 失败），站点导航上自己挂着两个**发布页**：

    https://chinaurl.github.io/            ← 「发布页1」（备用）
    https://bitbucket.org/fabuye/madouqu/  ← 「发布页2」（永久）

链路实测（2026-10-28）
----------------------
  * chinaurl.github.io 的 HTML 是混淆壳，只做一件事：调 api.country.is 判断
    访问者是不是中国内地 IP，是就 `location.href = 'https://fabuye.site'`。
  * bitbucket 仓库的 README 也是同一句话：最新网址 → https://fabuye.site。
  * 真正的域名表在 **https://fabuye.site/config.js** 里，明文可解析：
        const domainConfig = [{ id:'md', name:'麻豆区',
                                urls:['https://madouqu.shop', ...] }, ...]
    同一份 config.js 里还有 国产BT / 淘磁力 / 小酒楼 等**别的站**，必须按
    「麻豆区」这一组过滤，不能无脑全收。

因此本模块的做法（**照搬黄果 ``hgcore/mirror_refresh.py`` 的成熟方案**）：
  1. 读发布页（直连优先，直连读不到才用系统代理——代理**只用于读发布页**）；
  2. 从 config.js 的「麻豆区」分组 / 发布页正文里提取候选域名；
  3. 用**直连**逐个做内容校验（页面必须含 ``entry-title`` 这类真站点标记），
     发布页/中转页会卡在这一步 —— 保证写进列表的一定是能解析出列表的地址；
  4. 结果写 ``DATA/madouqu/mirrors.json``，供爬虫 ``fallback_urls`` 优先使用。

离线可测：``sync(fetch_fn=...)`` 可注入假实现，不发网络请求。
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 站点自己在导航里挂的两个发布页（顺序即优先级）
PUBLISH_PAGES = [
    "https://chinaurl.github.io/",
    "https://bitbucket.org/fabuye/madouqu/",
]

# 发布页最终都指向这个落地页；域名表在它旁边的 config.js（明文 JS）
ENTRY_PAGES = ["https://fabuye.site"]
CONFIG_SOURCES = ["https://fabuye.site/config.js"]

# 两个发布页都是「壳」：chinaurl 的 HTML 混淆后只留一句
# ``location.href = 'https://fabuye.site'``；bitbucket 是 SPA 空壳，正文在
# 仓库 README 里。README 走 API 拿纯文本，是**能自动读到落地页**的唯一稳定入口
# （落地页哪天改名，README 会第一时间跟着改，config.js 的地址也就跟着更新）。
EXTRA_SOURCES = [
    "https://api.bitbucket.org/2.0/repositories/fabuye/madouqu/src/master/README.md",
]

# 真站点的内容标记：麻豆区列表页一定有 entry-title 卡片 / madouqu 字样。
# 发布页、跳转壳、停放页都不含它 —— 这就是「能下载」和「能打开」的分界线。
MARKERS = ("entry-title", "madouqu")

# 静态兜底：发布页全挂也不能一个域名都没有（实测可用性见 crawler 注释）
STATIC_FALLBACK = [
    "https://madouqu.shop",
    "https://madouqu2.sbs",
    "https://madouqu2.shop",
    "https://madouqu.com",
]

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

MAX_CANDIDATES = 12

_URL_RE = re.compile(r"https?://([A-Za-z0-9][A-Za-z0-9.\-]*)(?::\d+)?")
# config.js 的「麻豆区」分组：id:'md' 或 name 里带麻豆
_GROUP_RE = re.compile(
    r"\{[^{}]*?(?:id\s*:\s*['\"]md['\"]|name\s*:\s*['\"][^'\"]*麻豆[^'\"]*['\"])"
    r"[^{}]*?\}", re.S)
_URLS_BLOCK_RE = re.compile(r"urls\s*:\s*\[(.*?)\]", re.S)
_QUOTED_RE = re.compile(r"['\"](https?://[^'\"]+)['\"]")


# ---------------------------------------------------------------- 工具
def state_file() -> Path:
    """域名缓存文件：``DATA/madouqu/mirrors.json``（与库同目录）。"""
    root = os.environ.get("DF_DATA_ROOT") or ""
    if root:
        base = Path(root)
    else:  # 从 modules/madouqu/core/publish_page.py 回溯到仓库根
        base = Path(__file__).resolve().parents[3] / "DATA"
    d = base / "madouqu"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d / "mirrors.json"


def _norm(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


def _host(url: str) -> str:
    m = re.match(r"https?://([^/:]+)", _norm(url))
    return (m.group(1) if m else "").lower()


def is_madou_host(host: str) -> bool:
    """只认麻豆自己的域名。

    config.js 里同时还挂着 国产BT(gcbt*)/淘磁力(taocili*)/小酒楼(xiaojiulou*)，
    它们是**别的站**，混进来会让爬虫拿着 GCBT 的域名去搜麻豆，永远 0 条。
    """
    h = (host or "").lower()
    return "madou" in h


# ---------------------------------------------------------------- 抓取
def fetch_text(url: str, timeout: float = 10.0, use_proxy: bool = False) -> str:
    """抓页面文本。

    ``use_proxy=False``（默认）刻意 ``trust_env=False``：域名校验一律走直连，
    避免"经系统代理假性可达"把本机根本下不了的域名写进列表。

    ``use_proxy=True`` 只用于**读发布页**：用户开着全局代理时浏览器能进发布页，
    程序若强制直连就永远读不到最新域名。
    """
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s = requests.Session()
    s.trust_env = bool(use_proxy)
    r = s.get(url, headers={"User-Agent": USER_AGENT},
              timeout=timeout, verify=False, allow_redirects=True)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    # 站点多为 UTF-8；bitbucket API 的 README 需要显式按 utf-8 解码
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text or ""


# ---------------------------------------------------------------- 提取
def parse_config_js(text: str) -> List[str]:
    """从 fabuye.site/config.js 里抽出「麻豆区」这一组的域名（保序去重）。"""
    out: List[str] = []
    for grp in _GROUP_RE.finditer(text or ""):
        block = grp.group(0)
        mb = _URLS_BLOCK_RE.search(block)
        if not mb:
            continue
        for m in _QUOTED_RE.finditer(mb.group(1)):
            u = _norm(m.group(1))
            if u and is_madou_host(_host(u)) and u not in out:
                out.append(u)
    return out


def extract_candidates(html: str, page_url: str = "") -> List[str]:
    """兜底：从发布页正文/README 里挑出麻豆域名（发布页结构变了也能用）。"""
    out: List[str] = []
    page_host = _host(page_url)
    for m in _URL_RE.finditer(html or ""):
        host = m.group(1).lower().strip(".")
        if not is_madou_host(host) or host == page_host:
            continue
        u = _norm("https://" + host)
        if u not in out:
            out.append(u)
    return out


def extract_entry_pages(html: str) -> List[str]:
    """从发布页里找出「落地页」（fabuye.site 这类中转页），便于跟着它改址。"""
    out: List[str] = []
    for m in _URL_RE.finditer(html or ""):
        host = m.group(1).lower()
        if "fabuye" in host and "bitbucket" not in host:
            u = _norm("https://" + host)
            if u not in out:
                out.append(u)
    return out


# ---------------------------------------------------------------- 校验
def validate(url: str, timeout: float = 8.0, fetch_fn=None) -> Tuple[bool, str]:
    """直连打开候选域名，用内容标记判断「这是不是真站点」。

    返回 ``(是否可用, 说明)``。发布页/中转页会返回 ``False``（无 entry-title）。
    ``fetch_fn`` 仅供离线测试注入；默认走**直连**（``use_proxy=False``）。
    """
    try:
        if fetch_fn is not None:
            html = fetch_fn(_norm(url) + "/") or ""
        else:
            html = fetch_text(_norm(url) + "/", timeout=timeout, use_proxy=False)
    except Exception as exc:                          # noqa: BLE001
        return False, type(exc).__name__
    if not html:
        return False, "空响应"
    hits = sum(html.count(m) for m in MARKERS)
    if hits <= 0:
        return False, "无列表卡片（发布页/中转页）"
    return True, f"命中标记 {hits}"


# ---------------------------------------------------------------- 缓存
def load_cache() -> List[str]:
    p = state_file()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                 # noqa: BLE001
        return []
    urls = data.get("verified") if isinstance(data, dict) else None
    return [_norm(u) for u in (urls or []) if _norm(u)]


def fallback_urls() -> List[str]:
    """给爬虫用的完整候选列表：发布页缓存 → 静态表（去重保序）。"""
    out: List[str] = []
    for u in load_cache() + STATIC_FALLBACK:
        n = _norm(u)
        if n and n not in out:
            out.append(n)
    return out


# ---------------------------------------------------------------- 主流程
def sync(timeout: float = 10.0, allow_proxy: bool = True, save: bool = True,
         fetch_fn=None, workers: int = 6, log=None) -> Dict:
    """读发布页 → 提候选 → 直连校验 → 落盘。返回统计字典。"""
    res: Dict = {
        "pages": list(PUBLISH_PAGES), "page_used": "", "config_used": "",
        "entry_pages": [], "discovered": [], "verified": [], "dead": [],
        "errors": [], "via_proxy": False, "primary": "",
    }

    def say(msg: str) -> None:
        if log:
            log(msg)

    def fetch(u: str, **kw) -> str:
        # 注入 fetch_fn（离线测试）时只传 url，保持测试替身签名简单
        if fetch_fn is not None:
            return fetch_fn(u)
        return fetch_text(u, timeout=timeout, **kw)

    entry_pages = list(ENTRY_PAGES)
    config_sources = list(CONFIG_SOURCES)

    # ① 读发布页：一是拿「落地页」最新域名（跟着改址），二是兜底提麻豆域名
    page_domains: List[str] = []
    for page in PUBLISH_PAGES:
        html = ""
        for use_proxy in ((False, True) if allow_proxy else (False,)):
            try:
                html = fetch(page, use_proxy=use_proxy)
                if html.strip():
                    res["via_proxy"] = use_proxy
                    break
            except Exception as exc:                  # noqa: BLE001
                res["errors"].append(f"{page} -> {type(exc).__name__}")
        if not html.strip():
            continue
        res["page_used"] = page
        found_entry = extract_entry_pages(html)
        for u in found_entry:
            if u not in entry_pages:
                entry_pages.append(u)
                config_sources.append(u.rstrip("/") + "/config.js")
        got = extract_candidates(html, page)
        if got:
            page_domains.extend(got)
        say(f"发布页 {page} → 落地页 {found_entry or '未识别'}，域名 {len(got)} 个")

    # ② 读 config.js：真正的域名表
    conf_domains: List[str] = []
    for src in config_sources:
        try:
            text = fetch(src, use_proxy=False)
        except Exception as exc:                      # noqa: BLE001
            res["errors"].append(f"{src} -> {type(exc).__name__}")
            continue
        got = parse_config_js(text)
        if got:
            conf_domains = got
            res["config_used"] = src
            say(f"config.js {len(got)} 个麻豆域名：{', '.join(got)}")
            break

    # ②.5 两个发布页都是壳 → 再读一次仓库 README（纯文本），
    #      顺手把落地页最新域名补进 config 候选，落地页换域名时能自愈
    for src in EXTRA_SOURCES:
        try:
            text = fetch(src, use_proxy=False)
        except Exception as exc:                      # noqa: BLE001
            res["errors"].append(f"{src} -> {type(exc).__name__}")
            continue
        found_entry = extract_entry_pages(text)
        for u in found_entry:
            if u not in entry_pages:
                entry_pages.append(u)
                cf = u.rstrip("/") + "/config.js"
                if cf not in config_sources:
                    config_sources.append(cf)
                    try:
                        got = parse_config_js(fetch(cf, use_proxy=False))
                    except Exception:                 # noqa: BLE001
                        got = []
                    if got:
                        conf_domains = got
                        res["config_used"] = cf
                        say(f"README 指向的 {cf} 有 {len(got)} 个麻豆域名")
        got2 = extract_candidates(text, src)
        page_domains.extend(got2)
        res["entry_pages"] = entry_pages

    # ③ 候选 = config.js → 发布页正文 → 缓存/静态表
    cands: List[str] = []
    for u in conf_domains + page_domains + load_cache() + STATIC_FALLBACK:
        n = _norm(u)
        if n and is_madou_host(_host(n)) and n not in cands:
            cands.append(n)
    cands = cands[:MAX_CANDIDATES]
    res["discovered"] = conf_domains or page_domains
    res["entry_pages"] = entry_pages

    if not cands:
        res["errors"].append("发布页与静态表都没有候选域名")
        return res

    # ④ 并发做直连内容校验
    results: Dict[str, Tuple[bool, str]] = {}
    vtimeout = min(max(timeout, 8.0), 12.0)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(cands)))) as ex:
        futs = {ex.submit(validate, u, vtimeout, fetch_fn): u for u in cands}
        for f, u in futs.items():
            try:
                results[u] = f.result()
            except Exception as exc:                  # noqa: BLE001
                results[u] = (False, type(exc).__name__)

    # ④.5 并发校验时偶发 TLS/超时（实测同一域名上一次 2s 通、下一次 8s 超时），
    #      通过数不足 2 个时再给"没通过的那批"一次机会 —— 否则一次抖动
    #      就会把明明能用的域名从列表里删掉。
    dead_now = [u for u in cands if not results.get(u, (False,))[0]]
    if len([u for u in cands if results.get(u, (False,))[0]]) < 2 and dead_now:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(dead_now)))) as ex:
            futs2 = {ex.submit(validate, u, vtimeout + 4.0, fetch_fn): u
                     for u in dead_now}
            for f, u in futs2.items():
                try:
                    ok2, why2 = f.result()
                except Exception:                     # noqa: BLE001
                    continue
                if ok2:
                    results[u] = (ok2, why2)
                    say(f"重试通过 {u}")

    verified = [u for u in cands if results.get(u, (False,))[0]]
    dead = [u for u in cands if not results.get(u, (False,))[0]]
    res["verified"] = verified
    res["dead"] = dead
    for u in dead:
        say(f"剔除 {u}（{results.get(u, (False, ''))[1]}）")
    for u in verified:
        say(f"可用 {u}（{results.get(u, (False, ''))[1]}）")

    # ⑤ 落盘：只写校验通过的，且**保留原有条目顺序**（用户当前用的域名排前面）
    if save and verified:
        payload = {
            "verified": verified,
            "dead": dead,
            "page_used": res["page_used"],
            "config_used": res["config_used"],
            "entry_pages": entry_pages,
            "updated_at": int(time.time()),
            "updated_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            state_file().write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            res["errors"].append(f"写缓存失败：{exc}")

    res["primary"] = verified[0] if verified else ""
    return res


def refresh_crawler(crawler, timeout: float = 10.0, log=None) -> Dict:
    """把发布页结果直接套到爬虫实例上（``fallback_urls`` 换成新表）。"""
    res = sync(timeout=timeout, log=log)
    if res.get("verified"):
        try:
            crawler.fallback_urls = list(res["verified"])
        except Exception:                             # noqa: BLE001
            pass
    return res
