# -*- coding: utf-8 -*-
"""AI污短剧（aiwu.ai-wu.cc）后端：实现 site_api 约定的同一组函数。

站点是 maccms（苹果CMS）模板，内容来源 from="huangguo"，vod id 与封面
路径 /upload/huangguo/{vid}.jpg 一一对应 —— 也就是说它的片源就是黄果那批，
**与黄果高度重合**（跨站去重会把这些标成「重复」）。

播放直链：player_data.url = "{vid}|{ep}"（encrypt=0），
真正的直链由站点解析器下发，但站点的 /api.php/provide/vod/ 已返回
"closed"、/static/player/ 目录也不存在（实测 404），所以**取址会返回空串**，
该站只能浏览、不能直接下载。
"""

import re
from typing import Dict, List
from urllib.parse import quote

from . import config
from .http_client import fetch_text
from .models import Drama
from .parser_aiwu import (
    episode_url, parse_detail, parse_list, parse_list_meta, parse_play_srcs,
    parse_vod_ref,
)

_URL_DETAIL = "/detail/{did}/"
_URL_SEARCH = "/search/{kw}/"


def _base() -> str:
    return (config.BASE_URL or "").rstrip("/")


def _cat_url(path: str) -> str:
    return _base() + path


def list_dramas() -> List[Drama]:
    """首页 /browse/ 列表。"""
    return parse_list(fetch_text(_cat_url("/browse/")))


def list_category(url: str) -> List[Drama]:
    return parse_list(fetch_text(url))


def list_category_meta(url: str):
    return parse_list_meta(fetch_text(url))


def list_category_all(url: str, max_pages: int = 0, on_page=None) -> List[Drama]:
    """分类页列表（本站没有可用分页，一次取完）。"""
    items = parse_list(fetch_text(url))
    if on_page:
        on_page(1, 1)
    return items


def list_ranks() -> List[Drama]:
    return []


def list_topics() -> list:
    return []


def list_topic_dramas(slug: str) -> List[Drama]:
    return []


def search_dramas(keyword: str) -> List[Drama]:
    return parse_list(fetch_text(_cat_url(_URL_SEARCH.format(kw=quote(keyword)))))


def detail_url_at(base_url: str, did) -> str:
    base = (base_url or _base()).rstrip("/")
    return base + _URL_DETAIL.format(did=did)


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
    """取 m3u8。

    站点把直链交给解析器下发，而解析接口（/api.php/provide/vod/）返回
    "closed"、静态播放器目录 404，所以这里通常取不到；能取到就直接用。
    """
    base = (base_url or _base()).rstrip("/")
    html = fetch_text(play_page_url_at(base, did, ep),
                      referer=detail_url_at(base, did))
    srcs: Dict[int, str] = parse_play_srcs(html)
    if srcs:
        return srcs.get(ep) or srcs[min(srcs)]
    return ""


def get_play_url(did, ep: int) -> str:
    return get_play_url_at(_base(), did, ep)


def get_play_urls_batch(did, eps: List[int]) -> Dict[int, str]:
    return {ep: get_play_url(did, ep) for ep in eps}


def vod_ref_at(base_url: str, did, ep: int) -> dict:
    """取该集在 maccms 里的 vod 引用（用于跨站比对同源片源）。"""
    base = (base_url or _base()).rstrip("/")
    html = fetch_text(play_page_url_at(base, did, ep),
                      referer=detail_url_at(base, did))
    return parse_vod_ref(html)
