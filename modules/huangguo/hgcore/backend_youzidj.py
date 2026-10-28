# -*- coding: utf-8 -*-
"""柚子短剧（youzidj.com）后端：实现 site_api 约定的同一组函数。

站点的 id 是 **slug**（/dramas/{slug}/season-1/），不是数字，所以
site_api/detail_url_for 这类按数字拼地址的地方要用本后端自己的 detail_url_at。

播放：/dramas/{slug}/season-1-episode-{n}/ 页面里的 JSON-LD 给出
    contentUrl = https://youzidj.com/media/{work}/{ep}/master.m3u8
但该地址**带凭证**：直接 GET 会 403
（{"error":"播放凭证已过期，请重新开始播放"}）。
浏览器里真正放流的是同前缀下带 token 的 DASH 分片
（…/delivery-v2/{token}/1248/init.mp4），token 由前端 JS 动态申请。
所以这里返回 master.m3u8 原地址（供浏览器态嗅探用），yt-dlp 直取会 403。
"""

from typing import Dict, List
from urllib.parse import quote

from . import config
from .http_client import fetch_text
from .models import Drama
from .parser_youzidj import (
    detail_url, episode_url, parse_content_url, parse_detail, parse_list,
    parse_list_meta, parse_play_srcs,
)

_URL_SEARCH = "/search/?q={kw}"


def _base() -> str:
    return (config.BASE_URL or "").rstrip("/")


def _cat_url(path: str) -> str:
    return _base() + path


def list_dramas() -> List[Drama]:
    """首页列表（约 50 张卡片）。"""
    return parse_list(fetch_text(config.URL_HOME))


def list_category(url: str) -> List[Drama]:
    return parse_list(fetch_text(url))


def list_category_meta(url: str):
    return parse_list_meta(fetch_text(url))


def list_category_all(url: str, max_pages: int = 0, on_page=None) -> List[Drama]:
    """分类/标签页列表（本站无 URL 分页，一次取完）。"""
    items = parse_list(fetch_text(url))
    if on_page:
        on_page(1, 1)
    return items


def list_ranks() -> List[Drama]:
    return []


def list_topics() -> list:
    """标签页列表（/tags/{slug}/），当专题用。"""
    try:
        html = fetch_text(_cat_url("/tags/"))
    except Exception:                            # noqa: BLE001
        return []
    from bs4 import BeautifulSoup

    from .models import Topic

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a[href*='/tags/']"):
        href = str(a.get("href") or "")
        if "/tags/" not in href:
            continue
        slug = href.rstrip("/").split("/tags/")[-1]
        if not slug:
            continue
        out.append(Topic(slug=slug,
                         title=a.get_text(" ", strip=True)[:40]))
    return out


def list_topic_dramas(slug: str) -> List[Drama]:
    return parse_list(fetch_text(_cat_url(f"/tags/{slug}/")))


def search_dramas(keyword: str) -> List[Drama]:
    return parse_list(fetch_text(_cat_url(_URL_SEARCH.format(kw=quote(keyword)))))


def detail_url_at(base_url: str, did) -> str:
    """详情页 URL（did 是 slug）。"""
    return detail_url(base_url or _base(), did)


def load_detail(did) -> dict:
    html = fetch_text(detail_url_at(_base(), did), referer=config.URL_HOME)
    return parse_detail(html)


def parse_detail_html(html: str) -> dict:
    return parse_detail(html)


def play_page_url_at(base_url: str, did, ep: int) -> str:
    return episode_url(base_url or _base(), did, ep)


def play_page_url(did, ep: int) -> str:
    return play_page_url_at(_base(), did, ep)


def get_play_url_at(base_url: str, did, ep: int) -> str:
    """取播放地址。

    先按站点约定直接请求 master.m3u8（部分情况下可用），403 时改为
    抓播放页里的 JSON-LD 地址返回（带凭证时需要浏览器态放流）。
    """
    base = (base_url or _base()).rstrip("/")
    html = fetch_text(play_page_url_at(base, did, ep),
                      referer=detail_url_at(base, did))
    url = parse_content_url(html)
    if not url:
        srcs: Dict[int, str] = parse_play_srcs(html)
        url = srcs.get(ep) or (srcs[min(srcs)] if srcs else "")
    if not url:
        return ""
    try:
        probe = fetch_text(url, referer=play_page_url_at(base, did, ep), timeout=20)
        if probe.strip().startswith("#EXTM3U"):
            return url
    except Exception:                            # noqa: BLE001 - 403 属正常情况
        pass
    return url


def get_play_url(did, ep: int) -> str:
    return get_play_url_at(_base(), did, ep)


def get_play_urls_batch(did, eps: List[int]) -> Dict[int, str]:
    return {ep: get_play_url(did, ep) for ep in eps}
