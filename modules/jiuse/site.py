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

# 封面：懒加载站常把真实地址放在 data-src / data-original，src 多为占位图
_IMG_DATA_SRC_RE = re.compile(
    r'<img[^>]+?(?:data-src|data-original|data-lazy-src)=["\']([^"\']+)["\']', re.I)
_IMG_SRC_RE = re.compile(r'<img[^>]+?src=["\']([^"\']+)["\']', re.I)

_VIDEO_HINTS = ("/vod/view/", "/video/viewhd/", "/v/", "/video",
                "view_video", "voddetail", "vodplay",
                "/archives/", "/post/", "/watch")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# 91 系真实卡片：<div class="video-elem"> 里
#   ① <a class="display" href="/vod/view/XXX">  内含 <div class="img" style="background-image:url(...)">
#   ② <a class="title" href="/vod/view/XXX">标题</a>
#   ③ <small class="layer">00:03:45</small>
#   ④ 作者: <a href="/dashen/XXX">  **或**  <a href="/author/XXX">（站点两种写法混用）
# 注意封面**不是 <img>**，而是 background-image，所以原来基于 <img> 的取法必然全空。
_ELEM_SPLIT_RE = re.compile(r'<div class="video-elem">', re.I)
_BG_URL_RE = re.compile(
    r'background-image\s*:\s*url\(\s*[\'"]?([^\'")]+)', re.I)
_TITLE_A_RE = re.compile(
    r'<a[^>]+class="[^"]*\btitle\b[^"]*"[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.I | re.S)
_ANY_A_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_DUR_RE = re.compile(r'<small[^>]+class="[^"]*\blayer\b[^"]*"[^>]*>(.*?)</small>',
                     re.I | re.S)
# 作者只认 /dashen/ 会漏掉一半卡片（实测首页 24 条里 12 条是 /author/），
# 于是「演员/作者」列永远是空的 —— 这就是九色"拿不到演员"的根因。
_AUTHOR_RE = re.compile(
    r'href=["\']/(?:dashen|author|u)/([^"\'/]+)["\'][^>]*>(.*?)</a>', re.I | re.S)


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


def _parse_cards(html: str, base_url: str) -> List[dict]:
    """按 91 系 ``div.video-elem`` 卡片解析（封面走 background-image）。"""
    out: List[dict] = []
    parts = _ELEM_SPLIT_RE.split(html or "")
    for seg in parts[1:]:            # parts[0] 是首张卡片之前的杂项
        href = title = ""
        m = _TITLE_A_RE.search(seg)
        if m:
            href, raw = m.group(1), m.group(2)
            title = re.sub(r"\s+", " ", _TAG_RE.sub("", raw)).strip()
        if not href:
            # 卡片内第一个视频详情链接（display 层）
            for am in _ANY_A_RE.finditer(seg):
                if any(h in am.group(1).lower()
                       for h in ("/vod/view/", "/video/viewhd/")):
                    href = am.group(1)
                    title = re.sub(r"\s+", " ", _TAG_RE.sub("", am.group(2))).strip()
                    break
        if not href:
            continue
        low = href.lower()
        if not any(h in low for h in ("/vod/view/", "/video/viewhd/", "/video/")):
            continue
        url = _abs(base_url, href).split("?")[0]

        cover = ""
        mb = _BG_URL_RE.search(seg)          # ① background-image: url(...)
        if mb:
            cand = mb.group(1).strip()
            if cand and not cand.startswith("data:"):
                cover = _abs(base_url, cand)
        if not cover:                        # ② 退回落懒加载/src 的 <img>
            ms = _IMG_DATA_SRC_RE.search(seg) or _IMG_SRC_RE.search(seg)
            if ms:
                cand = ms.group(1).strip()
                if cand and not cand.startswith("data:") \
                        and not cand.lower().endswith(".svg"):
                    cover = _abs(base_url, cand)

        dur = ""
        md = _DUR_RE.search(seg)
        if md:
            dur = re.sub(r"\s+", " ", _TAG_RE.sub("", md.group(1))).strip()[:12]
        author = ""
        ma = _AUTHOR_RE.search(seg)
        if ma:
            # 优先用链接文字（站点有时 slug 是拼音、文字才是中文名）
            author = re.sub(r"\s+", " ", _TAG_RE.sub("", ma.group(2) or "")).strip() \
                or (ma.group(1) or "").strip()
        if not title:
            title = url.rstrip("/").split("/")[-1]
        out.append({"title": title[:120], "url": url, "cover": cover,
                    "duration": dur, "author": author})
    return out


def _parse_anchors(html: str, base_url: str) -> List[dict]:
    """兜底：通用 ``<a>`` 扫描（搜索页/结构不同的页面）。"""
    out: List[dict] = []
    for m in _ANCHOR_RE.finditer(html or ""):
        href = (m.group(1) or "").strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        low = href.lower()
        if not any(h in low for h in _VIDEO_HINTS):
            if not re.search(r"/\d{3,}\.html?$", low):
                continue
        url = _abs(base_url, href).split("?")[0]
        text = re.sub(r"\s+", " ", _TAG_RE.sub("", m.group(2) or "")).strip()
        inner = m.group(2) or ""
        # 窗口不越过下一个 <a>，否则会把相邻条目的图串过来
        nxt = html.find("<a", m.end())
        win_end = min(m.end() + 400, nxt if nxt != -1 else len(html))
        block = html[m.start():win_end]
        img = _IMG_RE.search(inner) or _IMG_RE.search(block)
        title = text or (img.group(1) if img else "") or url.rstrip("/").split("/")[-1]
        cover = ""
        m_img = (_IMG_DATA_SRC_RE.search(inner) or _IMG_SRC_RE.search(inner)
                 or _IMG_DATA_SRC_RE.search(block) or _IMG_SRC_RE.search(block))
        if m_img:
            cand = (m_img.group(1) or "").strip()
            if cand and not cand.startswith("data:") and not cand.lower().endswith(".svg"):
                cover = _abs(base_url, cand)
        out.append({"title": title[:120], "url": url, "cover": cover,
                    "duration": "", "author": ""})
    return out


def parse_list(html: str, base_url: str) -> List[dict]:
    """从列表页抽视频条目（按 URL 去重）。

    优先走 91 系 ``video-elem`` 卡片（能拿到标题/封面/时长/作者），
    抽不到再回落到通用 ``<a>`` 扫描，保证别的页面形态也能用。
    """
    items = _parse_cards(html, base_url) or _parse_anchors(html, base_url)
    out: List[dict] = []
    seen = set()
    for it in items:
        u = it.get("url") or ""
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(it)
    return out


def probe_page(url: str, timeout: float = 15.0) -> dict:
    """打开一个页面：返回标题、识别到的条目数、以及列表（首页/列表页用）。"""
    html, via = client.fetch(url, timeout=timeout)
    items = parse_list(html, url)
    return {"url": url, "title": page_title(html), "via": via,
            "size": len(html), "items": items, "count": len(items)}


def open_home(base_url: str, timeout: float = 15.0) -> dict:
    return probe_page(base_url.rstrip("/") + "/", timeout=timeout)
