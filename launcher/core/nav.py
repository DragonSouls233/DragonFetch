# -*- coding: utf-8 -*-
"""导航页（发布页）服务：**先用代理读导航页，拿到最新入口，再直连使用**。

背景：``https://daohang.pro`` 是糖心等站点的「回家的路」。它的首页是纯 JS
渲染的空壳，真正的链接表在同目录 ``app.js`` 的 ``CONFIG.links`` 里，结构为：

    {name:{zhCN:"糖心Vlog",zhTW:"..."},
     description:{zhCN:"糖心Vlog全集在线观看",...},
     url:"https://tangxinvlog.app?ref=daohang.pro",
     scienceUrl:"https://tangxin01.top/?ref=daohang.pro"}

导航页本身在境内需要代理才能访问；解析出来的目标站点通常可直连，所以流程是
「代理读导航 → 记下入口 → 之后按直连方式使用」。结果缓存到
``DATA/app/nav.json``，代理不可用时也能用上次的成功结果。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any, Dict, List

import requests
import urllib3

from . import paths
from .logbus import LOG
from .settings import settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULT_URL = "https://daohang.pro/app.js"
CACHE_FILE = "nav.json"


# ------------------------------------------------------------------ 解析
def _clean_url(url: str) -> str:
    """去掉推广参数（?ref=daohang.pro 等），只保留站点本身。"""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        if not parts.scheme:
            return url.strip()
        return f"{parts.scheme}://{parts.netloc}"
    except Exception:
        return url.strip()


def parse_links(text: str) -> List[Dict[str, Any]]:
    """从 app.js / 页面 HTML 里提取导航条目（容错：缺字段也能出结果）。"""
    entries: List[Dict[str, Any]] = []
    seen = set()
    for m in re.finditer(r'name:\s*\{\s*zhCN:\s*"([^"]*)"', text):
        name = m.group(1).strip()
        seg = text[m.end():m.end() + 900]
        u = re.search(r'url:\s*"([^"]+)"', seg)
        sci = re.search(r'scienceUrl:\s*"([^"]+)"', seg)
        desc = re.search(r'description:\s*\{\s*zhCN:\s*"([^"]*)"', seg)
        if not u:
            continue
        url = u.group(1).strip()
        if url in seen:
            continue
        seen.add(url)
        entries.append({
            "name": name,
            "desc": (desc.group(1).strip() if desc else ""),
            "url": url,
            "base_url": _clean_url(url),
            "science_url": (sci.group(1).strip() if sci else ""),
            "science_base_url": (_clean_url(sci.group(1)) if sci else ""),
            "kind": classify(name, url),
        })
    return entries


def classify(name: str, url: str = "") -> str:
    """给条目归类，前端据此决定用哪个模块打开。"""
    text = f"{name} {url}".lower()
    if "糖心" in text or "tangxin" in text:
        return "tangxin"
    if "黄视频" in text or "huang.video" in text:
        return "huangvideo"
    if "madou" in text or "麻豆" in text:
        return "madouqu"
    if "九色" in text or "91porny" in text:
        return "jiuse"
    return "other"


# ------------------------------------------------------------------ 抓取
def _fetch(url: str, proxies: Dict[str, str] | None, timeout: int = 20):
    resp = requests.get(url, headers={"User-Agent": UA, "Referer": "https://daohang.pro/"},
                        timeout=timeout, verify=False, proxies=proxies)
    resp.raise_for_status()
    try:
        text = resp.content.decode("utf-8")
    except UnicodeDecodeError:
        text = resp.text or ""
    return text, resp.url


def refresh(use_proxy: bool = True, url: str = "", timeout: int = 20) -> Dict[str, Any]:
    """读取导航页并缓存。返回 {ok, entries, via, error, at}。"""
    s = settings()
    target = (url or s.get("nav.url") or DEFAULT_URL).strip()
    proxy_map = s.proxies(enabled=True) if use_proxy else None
    errors: List[str] = []
    text, via = "", ""

    attempts = []
    if proxy_map:
        attempts.append(("proxy", proxy_map, target))
        attempts.append(("proxy", proxy_map, "https://daohang.pro/"))
    attempts.append(("direct", None, target))
    attempts.append(("direct", None, "https://daohang.pro/"))

    for mode, proxies, u in attempts:
        try:
            body, final = _fetch(u, proxies, timeout=timeout)
            if "name:" in body and "url:" in body:
                text, via = body, f"{mode} · {final}"
                break
            errors.append(f"{mode} {u} 未获取到链接表")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{mode} {u}: {type(exc).__name__}")

    if not text:
        cached = cached_entries()
        return {
            "ok": False,
            "entries": cached,
            "via": "",
            "error": "；".join(errors[-3:]) or "导航页不可达",
            "at": s.get("nav.updated_at", 0),
            "stale": True,
        }

    entries = parse_links(text)
    now = int(time.time())
    payload = {"at": now, "via": via, "url": target, "entries": entries}
    try:
        (paths.APP_DATA).mkdir(parents=True, exist_ok=True)
        (paths.APP_DATA / CACHE_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        LOG.warn("导航页缓存写入失败")

    s.set("nav.updated_at", now)
    s.save()
    LOG.info(f"导航页已更新：{len(entries)} 条（{via}）")
    return {"ok": True, "entries": entries, "via": via, "error": "", "at": now}


def cached_entries() -> List[Dict[str, Any]]:
    try:
        p = paths.APP_DATA / CACHE_FILE
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return list(data.get("entries") or [])
    except Exception:
        pass
    return []


def cached_payload() -> Dict[str, Any]:
    try:
        p = paths.APP_DATA / CACHE_FILE
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"at": 0, "entries": [], "via": ""}


def find_entries(kind: str) -> List[Dict[str, Any]]:
    return [e for e in cached_entries() if e.get("kind") == kind]
