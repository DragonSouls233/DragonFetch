# -*- coding: utf-8 -*-
"""九色站点解析：列表页 → 视频条目；单条 → 交给共享 yt-dlp 下载。

站点（91 系）结构各家不同、且常有 Cloudflare 校验，因此这里的策略是
**保守解析 + 明确降级**：
  * 列表页：抓 `<a>`，按路径特征（/v/、/video、voddetail、纯数字.html）识别条目，
    尽量从同块的 `<img alt/title>` 取标题；
  * 单条：优先用共享 ``bin/yt-dlp.exe`` 解析，失败则返回页面标题，
    并提示「用浏览器打开」（模块的主职责是拿到最新地址）。
"""
from __future__ import annotations

import re
from typing import List

from . import client

_TAG_RE = re.compile(r"<[^>]+>")
_ANCHOR_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_IMG_RE = re.compile(r'<img[^>]+(?:alt|title)=["\']([^"\']{2,80})["\']', re.I)

_VIDEO_HINTS = ("/v/", "/video", "view_video", "voddetail", "vodplay",
                "/archives/", "/post/", "/watch")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _abs(base: str, href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return base.rstrip("/") + "/" + href.lstrip("./")


def page_title(html: str) -> str:
    m = _TITLE_RE.search(html or "")
    if not m:
        return ""
    return re.sub(r"\s+", " ", _TAG_RE.sub("", m.group(1))).strip()[:120]


def parse_list(html: str, base_url: str) -> List[dict]:
    """从列表页抽视频条目：``[{title, url}]``（按 URL 去重）。"""
    out: List[dict] = []
    seen = set()
    for m in _ANCHOR_RE.finditer(html or ""):
        href = (m.group(1) or "").strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        low = href.lower()
        if not any(h in low for h in _VIDEO_HINTS):
            # 纯数字 .html 也是常见详情页
            if not re.search(r"/\d{3,}\.html?$", low):
                continue
        url = _abs(base_url, href).split("?")[0]
        if url in seen:
            continue
        seen.add(url)
        text = re.sub(r"\s+", " ", _TAG_RE.sub("", m.group(2) or "")).strip()
        block_end = min(len(html), m.end() + 400)
        img = _IMG_RE.search(html[m.start():block_end])
        title = text or (img.group(1) if img else "") or url.rstrip("/").split("/")[-1]
        out.append({"title": title[:120], "url": url})
    return out


def probe_page(url: str, timeout: float = 15.0) -> dict:
    """打开一个页面：返回标题、识别到的条目数、以及列表（首页/列表页用）。"""
    html, via = client.fetch(url, timeout=timeout)
    items = parse_list(html, url)
    return {"url": url, "title": page_title(html), "via": via,
            "size": len(html), "items": items, "count": len(items)}


def open_home(base_url: str, timeout: float = 15.0) -> dict:
    return probe_page(base_url.rstrip("/") + "/", timeout=timeout)
