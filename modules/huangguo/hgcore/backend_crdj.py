# -*- coding: utf-8 -*-
"""91成人短剧（91crdjai.com）后端：实现 site_api 约定的同一组函数。

与黄桃后端共用同一套取址接口 /videos/{did}/episodes/{ep}/playback，
差别只在页面结构（a.card / .ep-grid / playInitialData）。

站点特色：
  * 详情链接带 slug（/duanju/6016-xxx/），但服务端接受纯 id 并 302 到
    slug 版，所以库里只存 did 就够。
  * 分页是 /duanju/page/{n}/（**不是** /duanju/{n}/，后者是剧 id）。
  * 详情页顶部有「.addr-row / .addr-label」——那是发布页给的备用线路。
"""

import json
from typing import Dict, List
from urllib.parse import quote

from . import config
from .http_client import fetch_text
from .models import Drama
from .parser_crdj import (
    episode_url, parse_detail, parse_list, parse_list_meta, parse_play_srcs,
    playback_url,
)

_URL_DETAIL = "/duanju/{did}/"
_URL_SEARCH = "/search/?q={kw}"


def _base() -> str:
    return (config.BASE_URL or "").rstrip("/")


def _cat_url(path: str) -> str:
    return _base() + path


# ---------------------------------------------------------------- 列表

def list_dramas() -> List[Drama]:
    """首页 / 短剧列表。"""
    return parse_list(fetch_text(config.URL_HOME))


def list_category(url: str) -> List[Drama]:
    return parse_list(fetch_text(url))


def list_category_meta(url: str):
    return parse_list_meta(fetch_text(url))


def list_category_all(url: str, max_pages: int = 0, on_page=None) -> List[Drama]:
    """翻页拉取全部剧集（/duanju/page/{n}/）。"""
    first = fetch_text(url)
    _total, pages = parse_list_meta(first)
    if pages <= 0:
        pages = 1
    if max_pages > 0:
        pages = min(pages, max_pages)
    pages = min(pages, 60)

    seen, order = set(), []

    def _merge(items: List[Drama]) -> None:
        for d in items:
            if d.did in seen:
                continue
            seen.add(d.did)
            order.append(d)

    _merge(parse_list(first))
    if on_page:
        on_page(1, pages)
    for p in range(2, pages + 1):
        try:
            _merge(parse_list(fetch_text(_page_url(url, p))))
        except Exception:                        # noqa: BLE001
            pass
        if on_page:
            on_page(p, pages)
    return order


def _page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    return url.rstrip("/") + f"/page/{page}/"


def list_ranks() -> List[Drama]:
    """本站没有独立榜单页（列表页自带热度排序）。"""
    return []


def list_topics() -> list:
    return []


def list_topic_dramas(slug: str) -> List[Drama]:
    return []


def search_dramas(keyword: str) -> List[Drama]:
    url = _cat_url(_URL_SEARCH.format(kw=quote(keyword)))
    return parse_list(fetch_text(url))


# ---------------------------------------------------------------- 详情 / 播放

def detail_url_at(base_url: str, did) -> str:
    base = (base_url or _base()).rstrip("/")
    return base + _URL_DETAIL.format(did=did)


def load_detail(did) -> dict:
    html = fetch_text(detail_url_at(_base(), did), referer=config.URL_HOME)
    return parse_detail(html)


def parse_detail_html(html: str) -> dict:
    return parse_detail(html)


def play_page_url_at(base_url: str, did, ep: int) -> str:
    """播放页 URL：/duanju/{did}/{ep}/（纯 id 会被 302 到 slug 版）。"""
    return episode_url(base_url or _base(), did, ep)


def play_page_url(did, ep: int) -> str:
    return play_page_url_at(_base(), did, ep)


def get_play_url_at(base_url: str, did, ep: int) -> str:
    """实时取某一集的 m3u8（playback 接口优先，播放页 JSON 兜底）。"""
    base = (base_url or _base()).rstrip("/")
    ep = max(1, int(ep))

    try:
        raw = fetch_text(playback_url(base, did, ep),
                         referer=play_page_url_at(base, did, ep), timeout=20)
        data = json.loads(raw)
        src = str((data.get("data") or {}).get("src") or "").strip()
        if src.startswith("http"):
            return src.replace("\\u0026", "&")
    except Exception:                            # noqa: BLE001
        pass

    html = fetch_text(play_page_url_at(base, did, ep),
                      referer=detail_url_at(base, did))
    srcs: Dict[int, str] = parse_play_srcs(html)
    if ep in srcs:
        return srcs[ep]
    if srcs:
        return srcs[min(srcs)]
    return ""


def get_play_url(did, ep: int) -> str:
    return get_play_url_at(_base(), did, ep)


def get_play_urls_batch(did, eps: List[int]) -> Dict[int, str]:
    result: Dict[int, str] = {}
    for ep in eps:
        try:
            result[ep] = get_play_url(did, ep)
        except Exception:                        # noqa: BLE001
            result[ep] = ""
    return result
