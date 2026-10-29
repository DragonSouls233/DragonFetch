# -*- coding: utf-8 -*-
"""九色（91PORNY）地址页解析与线路管理。

地址页：``https://dizhi222.pages.dev/``。它的正文现在是**空的**：

    <div id="main"></div>
    <script src="https://lib.kingslord.com/dizhi/publish.js?20221020"></script>

真正的线路清单被写进那个外链 JS（一行混淆脚本，字符串数组里塞着
``91tanhua301.sbs`` … ``91tanhua350.sbs``、``jiuse.icu``、``91porny.com``）。
所以解析要两段：先抽 HTML 里的 ``<a>``，抽不到再顺着 ``<script src>`` 去读 JS
（实测 2026-10：HTML 里 0 条，JS 里 50 条 91tanhua + jiuse.icu）。

流程：
  1. 抓地址页（默认直连，失败且允许时用代理兜底）；
  2. HTML 抽链接；为空则读页面引用 JS，正则抽「站点域名」；
  3. 逐条轻量探测（HTTP + 站点指纹校验），按可用性 + 延迟排序；
  4. 结果写 ``DATA/jiuse/state.json``，并在 SQLite 记一条检测历史；
  5. 全部失败时**回退上次缓存**，界面照常可用（只是标记「缓存」）；
  6. 每条线路记 ``direct`` 标记：true = 本机直连能开（不用代理下载）。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List

from . import client

NAV_URL = "https://dizhi222.pages.dev/"

# 站点指纹：命中任一即认为「这是九色站点，而不是随便一个 200 页面」
SITE_MARKERS = ("九色", "91porny", "91porney", "91tanhua", "91自拍", "91视频")

_URL_RE = re.compile(r'https?://[^\s"\'<>)]+', re.I)
_HREF_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_SCRIPT_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)
_TAG_RE = re.compile(r"<[^>]+>")

# 站点域名特征：地址页 JS 里出现的字符串数组，只认这几类才算「线路」
SITE_HOST_HINTS = ("91tanhua", "jiuse.", "91porny", "91porney")
_HOST_RE = re.compile(r"(?:https?://)?(?:www\.)?((?:[a-z0-9][a-z0-9-]*\.)+[a-z]{2,6})", re.I)

# 明显不是「线路」的链接
_SKIP_HOSTS = ("pages.dev", "github.io", "t.me", "telegram", "twitter.com",
               "x.com", "mailto", "javascript:", "google", "cloudflare",
               "kingslord", "alibaba", "googletagmanager", "jsdelivr",
               "dizhi9", "gstatic", "w3.org")


# 已实测不可达的兜底域名（**留档，默认不纳入** ``_fallback_domains()``）。
# 需要时可手动把这里的条目并回默认序列；不要不经复测就直接启用。
# 实测 2026-10-29 23:48（系统代理 127.0.0.1:10808 生效）：
#   * 91porney.com          → 「未能解析此远程名称」（DNS 失效 / 域名废弃）
#   * 91tanhua301~350.sbs   → HEAD/GET 全部操作超时（抽测 301/320/325/350 四个
#     编号均超时，判定整段不可达）
_DEPRECATED_FALLBACK_DOMAINS: tuple = (
    "91porney.com",
    *[f"91tanhua{i}.sbs" for i in range(301, 351)],
)


def _fallback_domains() -> List[str]:
    """静态兜底域名清单（地址页 HTML + 外链 JS 都抽不到线路时直接用）。

    九色地址页正文经常是空壳、外链 JS（``lib.kingslord.com/dizhi/publish.js``）
    在当前/用户网络下也可能取不到 ⇒ 纯靠网络解析会永远拿不到候选。这里给一份
    **实测可达**的域名直接去探测，至少让界面有几条可探线路，并诚实报告成败。

    实测留档（2026-10-29 23:48，系统代理 127.0.0.1:10808 生效）
    --------------------------------------------------------------------
    基线可用：baidu HTTP 200、google HTTP 200、ip.sb HTTP 204（证明代理正常）。
    探测方法：逐域名 HEAD（8s 超时）+ GET（10s 超时）。

    逐条结论：
      * ``jiuse.icu``    → HEAD 超时，但 **GET HTTP 200**（可达；HEAD 超时是
        站点对 HEAD 的处理问题，GET 才是真实可用性）
      * ``91porny.com``  → HEAD 返回 204（WAF 对 HEAD 的假象），**GET HTTP 200**
        （可达）
      * ``91porney.com`` → **未能解析此远程名称**（DNS 失效 / 域名废弃）
      * ``91tanhua301.sbs`` / ``91tanhua320.sbs`` / ``91tanhua325.sbs`` /
        ``91tanhua350.sbs`` → HEAD 与 GET **全部操作超时**（抽测 4 个不同编号，
        判定 91tanhua301~350.sbs 整段不可达）

    为何只保留 2 条（用户长期偏好）
    --------------------------------------------------------------------
    「实测拿不到数据的源别塞进序列（只会拖慢每次刮削）」。原 53 条里
    50 条 sbs + 91porney 全是 0 命中死源，逐条探测会把每次「地址刷新」
    拖成几十秒到几分钟的假死。已实测不可达的域名从「每次必试」序列里
    **降级移除**，留档在 ``_DEPRECATED_FALLBACK_DOMAINS``（需要时可手动并入）。
    返回格式不变：纯主机名列表，由 ``refresh()`` 拼上 ``https://`` 后探测。
    """
    return ["jiuse.icu", "91porny.com"]



def _data_dir() -> Path:
    """数据目录：优先 DF_DATA_ROOT（前端注入根 DATA），否则模块内 DATA。"""
    import os

    root = (os.environ.get("DF_DATA_ROOT") or "").strip()
    if root:
        p = Path(root) / "jiuse"
    else:
        p = Path(__file__).resolve().parent.parent / "DATA"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def _state_path() -> Path:
    return _data_dir() / "state.json"


def _load_state() -> dict:
    try:
        p = _state_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"addresses": [], "active": "", "updated_at": 0, "source": NAV_URL}


def _save_state(data: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def parse_addresses(html: str) -> List[Dict[str, str]]:
    """从地址页 HTML 抽出候选线路：``[{url, host, label}]``（按出现顺序去重）。"""
    cands: List[Dict[str, str]] = []
    seen = set()

    def add(url: str, text: str = "") -> None:
        url = (url or "").strip().rstrip(".,;，。")
        if not url.lower().startswith("http"):
            return
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        if any(s in host for s in _SKIP_HOSTS):
            return
        if host in seen:
            return
        seen.add(host)
        cands.append({"url": url.split("?")[0].rstrip("/"), "host": host,
                      "label": (text or host).strip()[:40]})

    # ① 锚点优先（地址页是人工排的 <a> 列表，顺序有意义）
    for m in _HREF_RE.finditer(html or ""):
        href, inner = m.group(1), _TAG_RE.sub("", m.group(2) or "")
        add(href, inner)

    # ② 兜底：正文里裸 URL（有些地址页把地址写成纯文本）
    for m in _URL_RE.finditer(html or ""):
        add(m.group(0), "")

    # 站点域名优先（91tanhua 系），其余排在后面
    cands.sort(key=lambda c: (0 if "91" in c["host"] else 1))
    return cands


def asset_urls(html: str, base_url: str = "") -> List[str]:
    """页面引用的外部 JS（地址页把线路清单放在外链脚本里）。"""
    out: List[str] = []
    for src in _SCRIPT_RE.findall(html or ""):
        src = (src or "").strip()
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif not src.lower().startswith("http"):
            src = urllib.parse.urljoin(base_url or NAV_URL, src)
        if src not in out:
            out.append(src)
    return out


def parse_domains(text: str) -> List[Dict[str, str]]:
    """从 JS/纯文本里抽「站点域名」候选（``jiuse.icu`` 排在 91tanhua 系前面）。

    JS 是混淆过的，但域名是以明文字符串存在数组里的，所以直接正则抓
    ``xxx.yyy`` 再按 ``SITE_HOST_HINTS`` 过滤即可，不需要执行 JS。
    """
    cands: List[Dict[str, str]] = []
    seen = set()
    for host in _HOST_RE.findall(text or ""):
        h = host.lower().strip(".")
        if not any(k in h for k in SITE_HOST_HINTS):
            continue
        if any(s in h for s in _SKIP_HOSTS):
            continue
        if h in seen:
            continue
        seen.add(h)
        cands.append({"url": "https://" + h, "host": h, "label": h})
    # 主站（jiuse.icu / 91porny.com）比 91tanhua 系更稳，排前面
    cands.sort(key=lambda c: (1 if "91tanhua" in c["host"] else 0))
    return cands


def _looks_like_site(html: str) -> bool:
    low = (html or "").lower()
    return any(m.lower() in low for m in SITE_MARKERS) or "<video" in low


def check_one(url: str, timeout: float = 8.0, proxy: str = "") -> dict:
    """探测单条线路并校验站点指纹。``direct`` 标记是否走的直连。

    只发**一次**请求就够：``client.fetch`` 内部已经做了多通道兜底，
    再单独 ``check`` + ``fetch`` 会让每条线路白跑两遍。
    """
    import time

    t0 = time.time()
    try:
        html, via = client.fetch(url, timeout=timeout, proxy=proxy or None)
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "ok": False, "http": 0,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "via": "", "size": 0, "match": False, "direct": not proxy,
                "error": str(exc)[:200]}
    ok = _looks_like_site(html)
    return {"url": url, "ok": ok, "http": 200,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "via": via, "size": len(html), "match": ok, "direct": not proxy,
            "error": "" if ok else "页面可访问，但没识别到九色站点特征"}


def _probe_many(cands: List[Dict[str, str]], timeout: float, proxy: str,
                workers: int = 4) -> List[dict]:
    """并行探测一批候选（顺序按候选表返回），避免串行超时叠加。"""
    from concurrent.futures import ThreadPoolExecutor

    items = cands or []
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        futs = [ex.submit(check_one, c["url"], timeout, proxy) for c in items]
        out = []
        for c, f in zip(items, futs):
            try:
                r = f.result()
            except Exception as exc:  # noqa: BLE001
                r = {"url": c["url"], "ok": False, "elapsed_ms": 0,
                     "error": str(exc)[:120], "direct": not proxy}
            r.update({"host": c["host"], "label": c["label"]})
            out.append(r)
        return out


def fetch_nav_html(nav_url: str, timeout: float = 12.0,
                   proxy: str = "", allow_proxy_fallback: bool = False,
                   prefer_proxy: bool = False):
    """抓地址页：默认先直连；``prefer_proxy`` 或直连失败时用代理。

    返回 ``(html, via, used_proxy)``。``prefer_proxy`` 用于「上次就知道这个
    地址页直连不通」的情况，省掉一次白等的直连超时。
    """
    if prefer_proxy and proxy and allow_proxy_fallback:
        html, via = client.fetch(nav_url, timeout=timeout, proxy=proxy)
        return html, via, True
    try:
        html, via = client.fetch(nav_url, timeout=timeout)
        return html, via, False
    except Exception as first:  # noqa: BLE001
        if not (allow_proxy_fallback and proxy):
            raise
        html, via = client.fetch(nav_url, timeout=timeout, proxy=proxy)
        return html, f"{via}（直连失败：{type(first).__name__}）", True


def refresh(nav_url: str = "", timeout: float = 8.0,
            max_check: int = 8, progress=None,
            proxy: str = "", allow_proxy_fallback: bool = False,
            prefer_proxy: bool = False) -> dict:
    """抓地址页 → 抽候选 → 并行探测 → 排序 → 落盘/入库。"""
    nav_url = (nav_url or NAV_URL).strip()
    say = progress or (lambda *a, **k: None)

    say(0, 0, f"读取地址页 {nav_url}")
    try:
        html, via, nav_via_proxy = fetch_nav_html(
            nav_url, max(timeout, 10), proxy, allow_proxy_fallback, prefer_proxy)
    except Exception as exc:  # noqa: BLE001
        prev = _load_state()
        prev["error"] = f"地址页读取失败：{exc}"
        return {"ok": False, "error": prev["error"],
                "addresses": prev.get("addresses", []),
                "via": "", "cached": True, "nav_url": nav_url}

    cands = parse_addresses(html)
    say(0, len(cands), f"地址页解析出 {len(cands)} 条候选")

    # 地址页正文是空壳时，顺着 <script src> 去读 JS（线路清单在里面）
    if not cands:
        for asset in asset_urls(html, nav_url):
            say(0, 0, f"地址页无正文，改读脚本 {asset[:60]}")
            try:
                js, _js_via, js_via_proxy = fetch_nav_html(
                    asset, max(timeout, 10), proxy, allow_proxy_fallback,
                    prefer_proxy or nav_via_proxy)
            except Exception:  # noqa: BLE001
                continue
            cands = parse_domains(js)
            via = f"{via} + js"
            nav_via_proxy = bool(nav_via_proxy or js_via_proxy)
            if cands:
                break
        say(0, len(cands), f"脚本里解析出 {len(cands)} 条线路")

    if not cands:
        # 外部源兜底：地址页（HTML + 外链 JS）全抽不到时，用一份静态清单直接
        # 探测。九色地址页常因外链 JS 取不到而整页空壳，纯靠网络解析会永远拿
        # 不到候选；这里至少给界面几条可探线路，并诚实报告成败。
        fb = _fallback_domains()
        cands = [{"url": "https://" + h, "host": h, "label": h} for h in fb]
        say(0, len(cands), f"地址页无候选，改用静态兜底域名 {len(cands)} 条")

    targets = cands[:max(max_check, 1)]
    say(0, len(targets), f"并行探测 {len(targets)} 条线路…")

    # 地址页本身就是「直连失败、走代理才读到」的 → 站点大概率同样只能代理。
    # 这时直接从代理通道探测，省掉每条线路一次直连超时（否则 6 条能拖 2 分钟）。
    probe_via_proxy = bool(proxy and allow_proxy_fallback and nav_via_proxy)
    results = _probe_many(targets, timeout, proxy if probe_via_proxy else "")

    if probe_via_proxy:
        # 只给「最优的那条」补一次直连判定：决定这个模块下载要不要挂代理。
        # 其余线路一律按「仅代理」记录，避免每条都白等一个直连超时。
        for r in results:
            r.setdefault("direct", False)
            if r.get("ok") and not r.get("direct"):
                r["note"] = "仅代理可达"
        winner = next((r for r in results if r.get("ok")), None)
        if winner is not None:
            d = check_one(winner["url"], min(timeout, 4.0), "")
            winner["direct"] = bool(d.get("ok"))
            if winner["direct"]:
                winner.pop("note", None)
    elif not any(r.get("ok") for r in results) and proxy and allow_proxy_fallback:
        say(0, len(targets), "直连都不通，改用代理通道复测…")
        results = _probe_many(targets, timeout, proxy)
        for r in results:
            if r.get("ok"):
                r["note"] = "仅代理可达"

    good = [r for r in results if r.get("ok")]
    bad = [r for r in results if not r.get("ok")]
    good.sort(key=lambda r: (0 if r.get("direct") else 1,
                             r.get("elapsed_ms", 99999)))
    ordered = good + bad

    state = _load_state()
    merged: Dict[str, dict] = {}
    for item in list(state.get("addresses") or []) + ordered:
        url = (item.get("url") or "").rstrip("/")
        if not url:
            continue
        old = merged.get(url, {})
        merged[url] = {**old, **item, "url": url}
    state["addresses"] = list(merged.values())
    state["updated_at"] = int(time.time())
    state["source"] = nav_url
    state["via"] = via
    if good:
        state["active"] = good[0]["url"]
        state["active_direct"] = bool(good[0].get("direct"))
    state["nav_via_proxy"] = bool(nav_via_proxy)
    state.pop("error", None)
    _save_state(state)

    try:
        from . import db
        db.log_check(nav_url, via, ordered[:12])
    except Exception:
        pass

    active = state.get("active", "")
    active_item = next((r for r in good if r.get("url", "").rstrip("/") == active), {})
    return {"ok": bool(good), "addresses": state["addresses"],
            "active": active, "via": via, "cached": False,
            "nav_url": nav_url, "good": len(good), "bad": len(bad),
            "checked": len(targets),
            "direct": bool(active_item.get("direct")),
            "active_direct": bool(active_item.get("direct")),
            "nav_via_proxy": bool(nav_via_proxy),
            "nav_url_reachable": True,
            "error": "" if good else "候选线路均不可用（可能是网络不通或地址页失效，已保留上次缓存）"}


def current() -> dict:
    """当前状态（不联网）。"""
    st = _load_state()
    st.setdefault("nav_url", NAV_URL)
    return st


def set_active(url: str) -> None:
    st = _load_state()
    st["active"] = (url or "").strip()
    st["active_at"] = int(time.time())
    _save_state(st)


def addresses(only_ok: bool = False) -> List[dict]:
    items = list(_load_state().get("addresses") or [])
    if only_ok:
        items = [i for i in items if i.get("ok")]
    return items
