# -*- coding: utf-8 -*-
"""统一地址同步中心：糖心 / 黄果 / 麻豆 / 九色 四个国内模块的「地址」只在这里管。

为什么需要它
------------
这四个模块的域名都会变，各自有发布页/地址页。原来每个页面各有一个「刷新域名」
按钮，用户得记住去哪一页点、点完哪个模块生效。更麻烦的是「能不能不用代理」：
地址页常在墙外，但**站点本身往往可直连**——只要把域名同步下来并以直连方式
校验一次，下载就可以完全不挂代理。这件事必须由一处统一做，否则每个模块各写
一份判定逻辑，迟早互相打架。

四个模块的地址来源（都实测对过）
--------------------------------
  糖心    daohang.pro/app.js 的 CONFIG.links → 糖心那条（scienceUrl 科学线路优先）
  黄果    自带发布页（hgcore.mirror_refresh）：抓页面 → 提候选 → 直连内容校验
  麻豆    站点自带的备用域名表（madouqu.sbs/club/cc/net/org）+ 上次成功地址
  九色    dizhi222.pages.dev 地址页：正文是空壳，线路在它引用的 publish.js 里

同步一次做三件事
----------------
  1. **取址**：拿候选地址。**先验已有地址**，直连能过就直接沿用（最快路径，
     不联网抓发布页）；过不了才去读发布页/地址页（这一步允许用代理）。
  2. **校验**：候选并行用**直连**（``trust_env=False``，绝不偷偷走环境代理）打开，
     并校验站点指纹——「200 但不是这个站」在域名被停放时很常见。
  3. **落地**：直连能过 → 写该模块的 ``route/base_url`` 并把它的
     ``proxy_enabled`` 关掉（这就是「不开代理也能下载」的来源）；直连过不了 →
     保留代理开关，状态里如实标「需代理」，绝不让模块看起来能用、实际下不了。

结果落盘 ``DATA/app/navsync.json``，启动自检、页面、状态栏都读它；同步失败
只更新 ``error``，不动既有配置（宁可继续用上次的可用地址）。
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence

import requests
import urllib3

from . import paths
from .logbus import LOG
from .settings import settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

STATE_FILE = "navsync.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 顺序 = 界面顺序 = 同步顺序
ORDER = ("tangxin", "huangguo", "madouqu", "jiuse")

TARGETS: Dict[str, Dict[str, str]] = {
    "tangxin": {
        "label": "糖心 Vlog",
        "source": "导航页 daohang.pro",
        "note": "科学线路优先，实测可直连",
        "page": "tangxin",
    },
    "huangguo": {
        "label": "黄果短剧",
        "source": "官方发布页",
        "note": "候选域名逐个直连校验内容指纹",
        "page": "huangguo",
    },
    "madouqu": {
        "label": "麻豆磁力",
        "source": "站点备用域名",
        "note": "主域失效时按备用表轮询",
        "page": "madouqu",
    },
    "jiuse": {
        "label": "九色视频",
        "source": "地址页 dizhi222",
        "note": "地址页是 JS 空壳，线路在 publish.js 里",
        "page": "jiuse",
    },
}

MARKERS = {
    "tangxin": ("糖心", "tangxin", "vlog"),
    "huangguo": ("黄果", "huangguo", "短剧", "huang"),
    "madouqu": ("麻豆", "madouqu", "mdx", "传媒"),
    "jiuse": ("九色", "91porny", "91tanhua", "91自拍"),
}

_LOCK = threading.RLock()


# ---------------------------------------------------------------------- #
# 状态读写
# ---------------------------------------------------------------------- #
def _state_path():
    return paths.APP_DATA / STATE_FILE


def state() -> Dict[str, Any]:
    """读同步状态（不联网）。缺文件返回空结构。"""
    try:
        p = _state_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("targets", {})
                return data
    except Exception:  # noqa: BLE001
        pass
    return {"at": 0, "targets": {}, "ok": 0, "total": len(ORDER)}


def _save_state(data: Dict[str, Any]) -> None:
    with _LOCK:
        try:
            _state_path().parent.mkdir(parents=True, exist_ok=True)
            _state_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            LOG.warn(f"地址同步状态写入失败：{exc}")


def cached(key: str) -> Dict[str, Any]:
    return dict(state().get("targets", {}).get(key) or {})


def _put(key: str, item: Dict[str, Any]) -> None:
    """写单个模块结果（并发安全）。"""
    with _LOCK:
        data = state()
        data.setdefault("targets", {})[key] = item
        data["ok"] = sum(1 for v in data["targets"].values() if v.get("ok"))
        data["total"] = len(ORDER)
        data["at"] = int(time.time())
        _save_state(data)


# ---------------------------------------------------------------------- #
# 直连探测
# ---------------------------------------------------------------------- #
def _direct_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False          # 关键：不读 HTTP(S)_PROXY，保证是「真直连」
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    return s


def probe(url: str, timeout: float = 8.0,
          markers: Sequence[str] = (),
          min_size: int = 0,
          proxy: str = "") -> Dict[str, Any]:
    """打开一个地址并校验；``proxy`` 为空即**直连**（``trust_env=False``）。

    ``markers``：站点指纹，一个都命中不了就判不通过——域名被停放/抢注时
    页面照样返回 200，只看状态码会把「假站点」当成可用。
    ``min_size``：正文最小字节数（空壳页/停放页通常很小）。
    """
    t0 = time.time()
    out: Dict[str, Any] = {"ok": False, "code": 0, "ms": 0, "size": 0,
                           "match": True, "error": "", "url": url,
                           "direct": not proxy}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = _direct_session().get(url, timeout=timeout, verify=False,
                                  proxies=proxies, allow_redirects=True)
        out["code"] = r.status_code
        out["size"] = len(r.content or b"")
        text = r.text or ""
        if r.status_code >= 400:
            out["error"] = f"HTTP {r.status_code}"
        elif min_size and out["size"] < min_size:
            out["error"] = f"页面过小（{out['size']}B）"
        elif markers:
            low = text.lower()
            hit = any(str(m).lower() in low for m in markers)
            out["match"] = hit
            if hit:
                out["ok"] = True
            else:
                out["error"] = "页面特征不符"
        else:
            out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    out["ms"] = int((time.time() - t0) * 1000)
    return out


def probe_direct(url: str, timeout: float = 8.0,
                 markers: Sequence[str] = (),
                 min_size: int = 0) -> Dict[str, Any]:
    """直连探测（界面「直连测试」按钮用的就是它）。"""
    return probe(url, timeout, markers, min_size, "")


def probe_many(cands: Sequence[str], timeout: float,
               markers: Sequence[str] = (),
               workers: int = 4,
               proxy: str = "") -> Dict[str, Dict[str, Any]]:
    """并行探测一批候选（把「逐个超时叠加」压成「一个超时」）。"""
    urls: List[str] = []
    for u in cands:
        u = (u or "").strip().rstrip("/")
        if u and u not in urls:
            urls.append(u)
    if not urls:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as ex:
        futs = {ex.submit(probe, u, timeout, markers, 0, proxy): u for u in urls}
        return {futs[f]: f.result() for f in futs}


def _pick(cands: Sequence[str], timeout: float, markers: Sequence[str] = (),
          progress: Optional[Callable] = None,
          proxy: str = "") -> Dict[str, Any]:
    """并行探测后**按候选顺序**返回第一个通过的；直连全挂时可选用代理复测。"""
    urls: List[str] = []
    for u in cands:
        u = (u or "").strip().rstrip("/")
        if u and u not in urls:
            urls.append(u)
    if not urls:
        return {"ok": False, "error": "没有候选地址", "tried": []}
    if progress:
        progress(0, 0, f"直连校验 {len(urls)} 个候选…")
    got = probe_many(urls, timeout, markers)
    for u in urls:
        r = got.get(u) or {}
        if r.get("ok"):
            r["tried"] = urls
            return r

    if proxy:
        # 直连都不通时再走代理复测：能开就至少「可用（需代理）」，而不是判死
        if progress:
            progress(0, 0, "直连都不通，改用代理复测…")
        got2 = probe_many(urls, timeout, markers, proxy=proxy)
        for u in urls:
            r = got2.get(u) or {}
            if r.get("ok"):
                r["tried"] = urls
                return r

    last = got.get(urls[0]) or {"ok": False, "error": "候选地址都不可用"}
    last.setdefault("error", "候选地址都不可用")
    last["tried"] = urls
    return last


def _fast_path(cur: str, markers: Sequence[str], timeout: float,
               progress: Optional[Callable] = None) -> Dict[str, Any]:
    """最快路径：上次的地址直连还能开就直接沿用，不联网抓任何发布页。"""
    cur = (cur or "").strip().rstrip("/")
    if not cur:
        return {"ok": False, "error": "没有上次地址"}
    if progress:
        progress(0, 0, f"校验上次地址 {cur}")
    return probe_direct(cur, timeout=min(timeout, 6.0), markers=markers)


# ---------------------------------------------------------------------- #
# 各模块：取址 + 校验 + 落地
# ---------------------------------------------------------------------- #
def _proxy_for_nav() -> str:
    """读发布页/地址页时允许用的代理（默认允许：这些页常在墙外）。"""
    s = settings()
    if not s.get("navsync.use_proxy_for_nav", True):
        return ""
    return s.proxy_url if s.get("proxy.enabled", True) else ""


def _apply_tangxin(url: str, direct: bool, label: str = "") -> None:
    s = settings()
    s.set("tangxin.route_url", url)
    s.set("tangxin.route_label", label or url)
    s.set("tangxin.proxy_enabled", not direct)
    s.save()
    try:
        from . import backend as bk

        ns = bk.tangxin()                     # 内部会 apply_tangxin_settings
        ns.config.set_origin(url)
    except Exception as exc:  # noqa: BLE001
        LOG.warn(f"糖心线路落地失败（下次切页会重试）：{exc}")


def _sync_tangxin(timeout: float, progress=None) -> Dict[str, Any]:
    from . import nav as navmod

    s = settings()
    markers = MARKERS["tangxin"]
    cur = (s.get("tangxin.route_url") or "").strip()

    # ① 最快路径：上次线路直连还通，就不去打扰导航页
    fast = _fast_path(cur, markers, timeout, progress)
    if fast.get("ok"):
        _apply_tangxin(cur.rstrip("/"), True,
                       s.get("tangxin.route_label") or "上次可用线路")
        return {"ok": True, "url": cur.rstrip("/"), "direct": True,
                "latency_ms": fast.get("ms", 0), "via": "沿用上次线路",
                "note": "直连校验通过"}

    # ② 读导航页（允许代理）拿最新入口
    res = navmod.refresh(use_proxy=bool(_proxy_for_nav()), timeout=int(timeout))
    entries = [e for e in (res.get("entries") or []) if e.get("kind") == "tangxin"]
    if not entries:
        return {"ok": False, "url": cur,
                "error": res.get("error") or "导航页里没有糖心条目（已保留上次线路）",
                "direct": False, "cached": bool(res.get("stale"))}

    sci: List[str] = []
    main: List[str] = []
    for e in entries:
        u = (e.get("science_base_url") or "").strip()
        if u and u not in sci:
            sci.append(u)
        u2 = (e.get("base_url") or "").strip()
        if u2 and u2 not in main:
            main.append(u2)
    ordered = sci + [u for u in main if u not in sci]

    picked = _pick(ordered, timeout, markers, progress)
    if not picked.get("ok"):
        # 有的线路只有繁体首页（/zh-tw）才给正常页面
        alt = [u.rstrip("/") + "/zh-tw" for u in ordered]
        picked = _pick(alt, timeout, markers, progress)
    if not picked.get("ok"):
        return {"ok": False, "url": ordered[0] if ordered else cur,
                "error": picked.get("error") or "糖心线路直连不通",
                "via": res.get("via", ""), "direct": False,
                "tried": picked.get("tried", [])}

    url = picked["url"].rstrip("/")
    for suffix in ("/zh-tw", "/zh-tw/"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    is_sci = url in [x.rstrip("/") for x in sci]
    _apply_tangxin(url, True, "科学线路" if is_sci else "主站")
    return {"ok": True, "url": url, "direct": True,
            "latency_ms": picked.get("ms", 0), "via": res.get("via", ""),
            "note": "直连校验通过" + ("（科学线路）" if is_sci else "（主站）")}


def _sync_huangguo(timeout: float, progress=None) -> Dict[str, Any]:
    from . import backend as bk

    ns = bk.huangguo()
    s = settings()
    markers = MARKERS["huangguo"]
    cur = (getattr(ns.config, "BASE_URL", "") or "").strip()

    fast = _fast_path(cur, markers, timeout, progress)
    if fast.get("ok"):
        s.set("huangguo.proxy_enabled", False)
        s.save()
        try:
            ns.config.BASE_URL = cur.rstrip("/")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "url": cur.rstrip("/"), "direct": True,
                "latency_ms": fast.get("ms", 0), "via": "沿用上次镜像",
                "note": "直连校验通过"}

    site_id = (s.get("huangguo.site_id") or "").strip() or "huangguoai"
    if progress:
        progress(0, 0, "读取黄果发布页…")
    r = ns.mirror_refresh.refresh(site_id=site_id, timeout=int(timeout),
                                  save=True, allow_proxy=True, workers=8)
    verified = list(r.get("verified") or [])
    url = (r.get("switched_to") or getattr(ns.config, "BASE_URL", "") or "").rstrip("/")
    if not verified:
        return {"ok": False, "url": url,
                "error": "；".join((r.get("errors") or ["没有直连可用的镜像"])[-2:]),
                "direct": False, "note": "发布页没有直连镜像（已保留原域名）"}
    # mirror_refresh 的 verified 全部通过「直连内容校验」，可直接视为直连可用
    s.set("huangguo.proxy_enabled", False)
    s.save()
    picked = probe_direct(url or verified[0], timeout=min(timeout, 6.0),
                          markers=markers)
    return {"ok": True, "url": url or verified[0], "direct": True,
            "latency_ms": picked.get("ms", 0),
            "via": r.get("publish_page_used", ""),
            "note": f"发布页发现 {len(r.get('discovered') or [])} 个候选，"
                    f"直连校验通过 {len(verified)} 个"}


def _sync_madouqu(timeout: float, progress=None) -> Dict[str, Any]:
    from . import backend as bk

    s = settings()
    markers = MARKERS["madouqu"]
    cur = (s.get("madouqu.base_url") or "").strip()
    # 麻豆的域名常年全挂（实测 2026-10：sbs/club/cc/net/org 直连与代理都是 TLS 失败），
    # 探测超时压短一点，免得一个死站把整次同步拖到二十几秒。
    t = min(timeout, 5.0)

    fast = _fast_path(cur, markers, t, progress)
    if fast.get("ok"):
        s.set("madouqu.proxy_enabled", False)
        s.save()
        return {"ok": True, "url": cur.rstrip("/"), "direct": True,
                "latency_ms": fast.get("ms", 0), "via": "沿用上次域名",
                "note": "直连校验通过"}

    cands: List[str] = []
    if cur:
        cands.append(cur)
    # 发布页优先：chinaurl.github.io / bitbucket.org/fabuye/madouqu
    #   → https://fabuye.site/config.js 的「麻豆区」分组。
    # 站点换域名时只有这条链路是权威的；静态备用表里大半是早就作废的旧域名。
    publish_note = ""
    try:
        from madouqu.core import publish_page as _pub

        if progress:
            progress(0, 0, "读取麻豆发布页…")
        pub = _pub.sync(timeout=min(timeout, 10))
        for u in pub.get("verified") or []:
            if u not in cands:
                cands.append(u)
        publish_note = ("发布页 " + (pub.get("config_used") or "未取到域名表")
                        + f"，可用 {len(pub.get('verified') or [])} 个")
    except Exception as exc:  # noqa: BLE001
        logger_note = f"发布页读取失败：{exc}"
        publish_note = logger_note
    try:
        crawler = bk.madouqu().crawler.MadouquCrawler({"use_selenium": False})
        cands.extend(u for u in (crawler.fallback_urls or []) if u not in cands)
    except Exception:  # noqa: BLE001
        # 实测 2026-10-28：发布页给的 madouqu.shop / madouqu2.sbs /
        # madouqu2.shop 直连 200、107KB、含 entry-title（真站点）；
        # 老的 .sbs/.club/.cc/.net/.org 多数 TLS/CONNECT 失败。
        cands.extend(["https://madouqu.shop", "https://madouqu2.sbs",
                      "https://madouqu2.shop", "https://madouqu.com"])
    # 导航页里如果出现麻豆条目，也算候选
    try:
        from . import nav as navmod

        for e in navmod.find_entries("madouqu"):
            for u in (e.get("base_url"), e.get("science_base_url")):
                if u:
                    cands.append(u)
    except Exception:  # noqa: BLE001
        pass

    picked = _pick(cands[:6], t, markers, progress, proxy=_proxy_for_nav())
    if not picked.get("ok"):
        return {"ok": False, "url": cur, "direct": False,
                "error": picked.get("error") or "所有候选域名都不可用",
                "tried": picked.get("tried", []),
                "note": "沿用上次地址，代理开关保持不变"}
    url = picked["url"].rstrip("/")
    direct = bool(picked.get("direct", True))
    s.set("madouqu.base_url", url)
    s.set("madouqu.proxy_enabled", not direct)
    s.save()
    return {"ok": True, "url": url, "direct": direct,
            "latency_ms": picked.get("ms", 0),
            "via": publish_note or ("备用域名表" if direct else "备用域名表 · 代理"),
            "note": (f"命中直连域名（校验 {len(picked.get('tried') or [])} 个）"
                     if direct else "直连都不通，仅代理可达")}


def _sync_jiuse(timeout: float, progress=None) -> Dict[str, Any]:
    from . import backend as bk

    s = settings()
    ns = bk.jiuse()
    markers = MARKERS["jiuse"]
    cur = (s.get("jiuse.active_url") or "").strip()

    fast = _fast_path(cur, markers, timeout, progress)
    if fast.get("ok"):
        s.set("jiuse.proxy_enabled", False)
        s.set("jiuse.direct_only", True)
        s.save()
        return {"ok": True, "url": cur.rstrip("/"), "direct": True,
                "latency_ms": fast.get("ms", 0), "via": "沿用上次线路",
                "note": "直连校验通过"}

    nav_url = (s.get("jiuse.nav_url") or "https://dizhi222.pages.dev/").strip()
    proxy = _proxy_for_nav()
    # 上次就知道这个地址页直连不通 → 这次直接用代理读，省掉一个直连超时
    prev = ns.nav.current() if hasattr(ns.nav, "current") else {}
    prefer_proxy = bool(proxy and prev.get("nav_via_proxy"))
    res = ns.nav.refresh(nav_url, float(min(timeout, 6)),
                         int(s.get("jiuse.check_limit") or 6),
                         progress or (lambda *a, **k: None),
                         proxy=proxy, allow_proxy_fallback=bool(proxy),
                         prefer_proxy=prefer_proxy)
    if not res.get("ok"):
        return {"ok": False, "url": res.get("active", "") or cur, "direct": False,
                "error": res.get("error") or "地址页没有可用线路",
                "cached": bool(res.get("cached")), "note": "沿用上次线路"}
    url = (res.get("active") or "").rstrip("/")
    direct = bool(res.get("active_direct") or res.get("direct"))
    s.set("jiuse.active_url", url)
    s.set("jiuse.direct_only", direct)
    s.set("jiuse.proxy_enabled", not direct)
    s.save()
    try:
        ns.nav.set_active(url)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "url": url, "direct": direct, "latency_ms": 0,
            "via": res.get("via", ""),
            "note": (f"可用 {res.get('good', 0)}/{res.get('checked', 0)} 条"
                     + ("（直连）" if direct else "（仅代理可达）"))}


_SYNCERS = {
    "tangxin": _sync_tangxin,
    "huangguo": _sync_huangguo,
    "madouqu": _sync_madouqu,
    "jiuse": _sync_jiuse,
}


# ---------------------------------------------------------------------- #
# 对外接口
# ---------------------------------------------------------------------- #
def sync_one(key: str, timeout: Optional[float] = None,
             progress: Optional[Callable] = None) -> Dict[str, Any]:
    """同步单个模块，返回并落盘一条结果。异常不外抛（写进 error）。"""
    if key not in _SYNCERS:
        return {"ok": False, "error": f"未知模块 {key}"}
    meta = TARGETS[key]
    s = settings()
    timeout = float(timeout or s.get("navsync.timeout", 8) or 8)
    t0 = time.time()
    try:
        raw = _SYNCERS[key](timeout, progress) or {}
    except Exception as exc:  # noqa: BLE001
        raw = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    item = {
        "key": key,
        "label": meta["label"],
        "source": meta["source"],
        "page": meta["page"],
        "ok": bool(raw.get("ok")),
        "url": raw.get("url", "") or "",
        "direct": bool(raw.get("direct")),
        "proxy_needed": not bool(raw.get("direct")),
        "latency_ms": int(raw.get("latency_ms") or 0),
        "via": raw.get("via", "") or "",
        "error": raw.get("error", "") or "",
        "note": raw.get("note", "") or "",
        "cached": bool(raw.get("cached")),
        "at": int(time.time()),
        "cost_ms": int((time.time() - t0) * 1000),
    }
    _put(key, item)
    if item["ok"]:
        LOG.info(f"地址同步 · {meta['label']}：{item['url']}"
                 + ("（直连可用）" if item["direct"] else "（需代理）"))
    else:
        LOG.warn(f"地址同步 · {meta['label']}：{item['error'] or '失败'}"
                 + (f"（{item['url']}）" if item["url"] else ""))
    return item


def _timeout_item(key: str, deadline: float) -> Dict[str, Any]:
    meta = TARGETS[key]
    prev = cached(key)
    item = {
        "key": key, "label": meta["label"], "source": meta["source"],
        "page": meta["page"], "ok": False,
        "url": prev.get("url", ""), "direct": False, "proxy_needed": True,
        "latency_ms": 0, "via": "", 
        "error": f"同步超时（>{int(deadline)}s），已保留上次地址",
        "note": "超时", "cached": True, "at": int(time.time()),
        "cost_ms": int(deadline * 1000),
    }
    _put(key, item)
    return item


def sync_all(keys: Optional[List[str]] = None,
             progress: Optional[Callable] = None,
             parallel: bool = True) -> Dict[str, Any]:
    """同步多个模块（默认全部 4 个）。

    ``progress`` 形如 ``(pct, total, text)``；总耗时受
    ``navsync.deadline``（默认 60s）约束，超时的目标如实标「超时」而不卡界面。
    """
    todo = [k for k in (keys or list(ORDER)) if k in _SYNCERS]
    total = len(todo)
    deadline = float(settings().get("navsync.deadline", 60) or 60)
    t0 = time.time()
    results: Dict[str, Dict[str, Any]] = {}
    res_lock = threading.Lock()
    done = {"n": 0}

    def say(text: str) -> None:
        if not progress:
            return
        pct = int(done["n"] * 100 / max(1, total))
        try:
            progress(pct, total, text)
        except TypeError:
            try:
                progress(pct, text)
            except TypeError:
                progress(text)

    def worker(key: str) -> None:
        say(f"同步 {TARGETS[key]['label']}…")
        try:
            item = sync_one(key)
        except Exception as exc:  # noqa: BLE001 - sync_one 已兜底，这里再兜一层
            item = {"ok": False, "label": TARGETS[key]["label"],
                    "error": f"{type(exc).__name__}: {exc}", "url": ""}
        with res_lock:
            results[key] = item
            done["n"] += 1
        say(f"{TARGETS[key]['label']}：{'已就绪' if item.get('ok') else '失败'}")

    if parallel and total > 1:
        # 用 daemon 线程而不是线程池：某个站卡住时不会拖住程序退出
        threads = [threading.Thread(target=worker, args=(k,), daemon=True)
                   for k in todo]
        for th in threads:
            th.start()
        for th in threads:
            left = deadline - (time.time() - t0)
            th.join(timeout=max(0.1, left))
    else:
        for k in todo:
            worker(k)

    for k in todo:
        if k not in results:
            results[k] = _timeout_item(k, deadline)

    ok = sum(1 for v in results.values() if v.get("ok"))
    direct = sum(1 for v in results.values() if v.get("ok") and v.get("direct"))
    out = {"at": int(time.time()), "ok": ok, "direct": direct, "total": total,
           "cost_ms": int((time.time() - t0) * 1000), "targets": results}
    LOG.info(f"地址同步完成：{ok}/{total} 可用，其中直连 {direct} 个"
             f"（{out['cost_ms']} ms）")
    return out


def summary() -> List[Dict[str, Any]]:
    """给界面用的行数据：静态元信息 + 上次同步结果。"""
    rows = []
    st = state()
    for key in ORDER:
        meta = TARGETS[key]
        item = dict(st.get("targets", {}).get(key) or {})
        rows.append({
            "key": key,
            "label": meta["label"],
            "source": item.get("source") or meta["source"],
            "note": meta["note"],
            "page": meta["page"],
            "url": item.get("url", ""),
            "ok": bool(item.get("ok")),
            "direct": bool(item.get("direct")),
            "proxy_needed": bool(item.get("proxy_needed", not item.get("direct"))),
            "latency_ms": int(item.get("latency_ms") or 0),
            "error": item.get("error", ""),
            "hint": item.get("note", ""),
            "at": int(item.get("at") or 0),
            "synced": bool(item),
        })
    return rows


def last_run_text() -> str:
    """状态栏/侧栏一行摘要（要短，侧栏只有 200px 宽）。"""
    st = state()
    if not st.get("at"):
        return "地址同步：未执行"
    ok = sum(1 for v in st.get("targets", {}).values() if v.get("ok"))
    direct = sum(1 for v in st.get("targets", {}).values()
                 if v.get("ok") and v.get("direct"))
    when = time.strftime("%m-%d %H:%M", time.localtime(st["at"]))
    return f"地址同步 {ok}/{len(ORDER)} · 直连 {direct} · {when}"


def manual_set(key: str, url: str, timeout: Optional[float] = None) -> Dict[str, Any]:
    """手动指定地址：先直连校验，通过才落地（避免手填一个下不了的域名）。"""
    if key not in TARGETS:
        return {"ok": False, "error": f"未知模块 {key}"}
    url = (url or "").strip().rstrip("/")
    if not url:
        return {"ok": False, "error": "地址为空"}
    if not url.lower().startswith("http"):
        url = "https://" + url
    s = settings()
    timeout = float(timeout or s.get("navsync.timeout", 8) or 8)
    r = probe_direct(url, timeout=timeout, markers=MARKERS.get(key, ()))
    item = {
        "key": key, "label": TARGETS[key]["label"], "source": "手动指定",
        "page": TARGETS[key]["page"], "ok": bool(r.get("ok")), "url": url,
        "direct": bool(r.get("ok")), "proxy_needed": not bool(r.get("ok")),
        "latency_ms": int(r.get("ms") or 0), "via": "手动",
        "error": "" if r.get("ok") else (r.get("error") or "直连校验未通过"),
        "note": "手动指定 · 直连校验通过" if r.get("ok") else "手动指定 · 直连不通",
        "cached": False, "at": int(time.time()), "cost_ms": int(r.get("ms") or 0),
    }
    if item["ok"]:
        if key == "tangxin":
            _apply_tangxin(url, True, "手动")
        elif key == "huangguo":
            try:
                from . import backend as bk

                ns = bk.huangguo()
                ns.config.BASE_URL = url
                ns.config.ACTIVE_SITE["base_url"] = url
                ns.config.ACTIVE_SITE["home_url"] = url + "/"
                ns.config._rebuild_urls()  # noqa: SLF001 - 就是要它重建 URL 表
                ns.settings.save({"base_url": url})
            except Exception as exc:  # noqa: BLE001
                LOG.warn(f"黄果域名落地失败：{exc}")
            s.set("huangguo.proxy_enabled", False)
        elif key == "madouqu":
            s.set("madouqu.base_url", url)
            s.set("madouqu.proxy_enabled", False)
        elif key == "jiuse":
            s.set("jiuse.active_url", url)
            s.set("jiuse.direct_only", True)
            s.set("jiuse.proxy_enabled", False)
            try:
                from . import backend as bk

                bk.jiuse().nav.set_active(url)
            except Exception:  # noqa: BLE001
                pass
        s.save()
    _put(key, item)
    return item


def auto_on_start() -> bool:
    return bool(settings().get("navsync.auto_on_start", True))
