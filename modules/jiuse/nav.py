# -*- coding: utf-8 -*-
"""九色（91PORNY）地址页解析与线路管理。

地址页：``https://dizhi222.pages.dev/`` —— 一个静态页，正文就是若干条
``<a href="https://www.91tanhua###.sbs">`` 形式的「最新地址」。

流程：
  1. 直连抓地址页（不代理），正则/BeautifulSoup 双通道抽链接；
  2. 过滤出真正的站点域名（91tanhua 系 / 同源短域名），去重；
  3. 逐条轻量探测（HTTP + 标题校验），按可用性 + 延迟排序；
  4. 结果写 ``DATA/jiuse/state.json``，并在 SQLite 记一条检测历史。
  5. 全部失败时**回退上次缓存**，界面照常可用（只是标记「缓存」）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List

from . import client

NAV_URL = "https://dizhi222.pages.dev/"

# 站点指纹：命中任一即认为「这是九色站点，而不是随便一个 200 页面」
SITE_MARKERS = ("九色", "91porny", "91porney", "91tanhua", "91自拍", "91视频")

_URL_RE = re.compile(r'https?://[^\s"\'<>)]+', re.I)
_HREF_RE = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# 明显不是「线路」的链接
_SKIP_HOSTS = ("pages.dev", "github.io", "t.me", "telegram", "twitter.com",
               "x.com", "mailto", "javascript:", "google", "cloudflare")


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


def _looks_like_site(html: str) -> bool:
    low = (html or "").lower()
    return any(m.lower() in low for m in SITE_MARKERS) or "<video" in low


def check_one(url: str, timeout: float = 8.0) -> dict:
    """探测单条线路并校验站点指纹。"""
    res = client.check(url, timeout=timeout)
    if not res.get("ok"):
        return res
    try:
        html, via = client.fetch(url, timeout=timeout)
        res["match"] = _looks_like_site(html)
        res["via"] = via
        res["size"] = len(html)
        if not res["match"]:
            res["ok"] = False
            res["error"] = "页面可访问，但没识别到九色站点特征"
    except Exception as exc:  # noqa: BLE001
        res["match"] = False
        res["error"] = str(exc)[:160]
    return res


def refresh(nav_url: str = "", timeout: float = 8.0,
            max_check: int = 8, progress=None) -> dict:
    """抓地址页 → 抽候选 → 逐条探测 → 排序 → 落盘/入库。"""
    nav_url = (nav_url or NAV_URL).strip()
    say = progress or (lambda *a, **k: None)

    say(0, 0, f"读取地址页 {nav_url}")
    try:
        html, via = client.fetch(nav_url, timeout=max(timeout, 12))
    except Exception as exc:  # noqa: BLE001
        prev = _load_state()
        prev["error"] = f"地址页读取失败：{exc}"
        return {"ok": False, "error": prev["error"], "addresses": prev.get("addresses", []),
                "via": "", "cached": True, "nav_url": nav_url}

    cands = parse_addresses(html)
    say(0, len(cands), f"地址页解析出 {len(cands)} 条候选")
    if not cands:
        prev = _load_state()
        return {"ok": False, "error": "地址页里没有解析到任何线路",
                "addresses": prev.get("addresses", []), "via": via,
                "cached": True, "nav_url": nav_url}

    results: List[dict] = []
    targets = cands[:max(max_check, 1)]
    for i, c in enumerate(targets, start=1):
        say(i, len(targets), f"探测 {c['host']}")
        r = check_one(c["url"], timeout=timeout)
        r.update({"host": c["host"], "label": c["label"]})
        results.append(r)

    good = [r for r in results if r.get("ok")]
    bad = [r for r in results if not r.get("ok")]
    good.sort(key=lambda r: r.get("elapsed_ms", 99999))
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
    state.pop("error", None)
    _save_state(state)

    try:
        from . import db
        db.log_check(nav_url, via, ordered[:12])
    except Exception:
        pass

    return {"ok": bool(good), "addresses": state["addresses"],
            "active": state.get("active", ""), "via": via, "cached": False,
            "nav_url": nav_url, "good": len(good), "bad": len(bad),
            "checked": len(targets),
            "error": "" if good else "候选线路都不可用（已保留缓存）"}


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
