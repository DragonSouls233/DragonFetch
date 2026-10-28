# -*- coding: utf-8 -*-
"""黄桃短剧（aihuangdou.com）后端：实现 site_api 约定的同一组函数。

与黄果后端的关系：**同一套门面、不同解析器**。上层（UI / 任务队列）只认
site_api 的函数签名，因此新增站点不需要改任何 UI 代码。

黄桃站与 91成人短剧虽然前端模板不同，但**后端 API 是同一套**：
    GET {base}/videos/{did}/episodes/{ep}/playback
        -> {"data": {"n": 2, "src": "https://...m3u8?auth_key=..."}}
所以取址优先打这个接口（比解析播放页 HTML 轻一半）；接口不通时再退回
播放页内嵌的 ninePlayData JSON。
"""

import json
from typing import Dict, List
from urllib.parse import quote

from . import config
from .http_client import fetch_text
from .models import Drama
from .parser_huangdou import (
    episode_url, parse_detail, parse_list, parse_list_meta, parse_play_srcs,
    parse_ranks,
)

# 站内播放页固定在 /video/{did}/ 下，第 1 集无后缀、其余 /video/{did}/{ep}/
_URL_DETAIL = "/drama/{did}/"
_URL_SEARCH = "/search/?q={kw}"


def _base() -> str:
    return (config.BASE_URL or "").rstrip("/")


def _cat_url(path: str) -> str:
    return _base() + path


# ---------------------------------------------------------------- 列表

def list_dramas() -> List[Drama]:
    """首页剧集列表。"""
    return parse_list(fetch_text(config.URL_HOME))


def list_category(url: str) -> List[Drama]:
    """分类列表页（AI短剧 / AI漫剧 …）。"""
    return parse_list(fetch_text(url))


def list_category_meta(url: str):
    """分类页 (总条数, 总页数)。"""
    return parse_list_meta(fetch_text(url))


def list_category_all(url: str, max_pages: int = 0, on_page=None) -> List[Drama]:
    """翻页拉取分类页全部剧集（去重，保持页序）。

    分页规则与黄果树一致：/ai-duanju/{page}/（页面上 data-pages 给出总页数）。
    """
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
        except Exception:                        # noqa: BLE001 - 单页失败不中断
            pass
        if on_page:
            on_page(p, pages)
    return order


def _page_url(url: str, page: int) -> str:
    if page <= 1:
        return url
    return url.rstrip("/") + f"/{page}/"


def list_ranks() -> List[Drama]:
    """排行榜（/rankings/ 是 li.rank-row 结构）。"""
    return parse_ranks(fetch_text(_cat_url("/rankings/")))


def list_topics() -> list:
    """本站没有独立专题页。"""
    return []


def list_topic_dramas(slug: str) -> List[Drama]:
    return []


def search_dramas(keyword: str) -> List[Drama]:
    """站内搜索：/search/?q=关键词。"""
    url = _cat_url(_URL_SEARCH.format(kw=quote(keyword)))
    return parse_list(fetch_text(url))


# ---------------------------------------------------------------- 详情 / 播放

def detail_url_at(base_url: str, did) -> str:
    base = (base_url or _base()).rstrip("/")
    return base + _URL_DETAIL.format(did=did)


def load_detail(did) -> dict:
    """获取剧集详情（标题/简介/封面/集数）。"""
    html = fetch_text(detail_url_at(_base(), did), referer=config.URL_HOME)
    return parse_detail(html)


def parse_detail_html(html: str) -> dict:
    """仅解析详情 HTML（供并发按站点抓取时复用）。"""
    return parse_detail(html)


def play_page_url_at(base_url: str, did, ep: int) -> str:
    """按指定站点根构造某集播放页 URL（不读全局站点，队列并发安全）。"""
    return episode_url(base_url or _base(), did, ep)


def play_page_url(did, ep: int) -> str:
    return play_page_url_at(_base(), did, ep)


def playback_api_at(base_url: str, did, ep: int) -> str:
    base = (base_url or _base()).rstrip("/")
    return f"{base}/videos/{did}/episodes/{max(1, int(ep))}/playback"


def get_play_url_at(base_url: str, did, ep: int) -> str:
    """实时取某一集的 m3u8 地址。

    顺序：① playback 接口（轻、带 auth_key 时效）→ ② 播放页 ninePlayData。
    每次都重新请求：auth_key 有时效，不能跨调用缓存。
    """
    base = (base_url or _base()).rstrip("/")
    ep = max(1, int(ep))

    try:
        raw = fetch_text(playback_api_at(base, did, ep),
                         referer=play_page_url_at(base, did, ep), timeout=20)
        data = json.loads(raw)
        src = str((data.get("data") or {}).get("src") or "").strip()
        if src.startswith("http"):
            return src.replace("\\u0026", "&")
    except Exception:                            # noqa: BLE001 - 接口不可用时走退路
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
    """实时获取某一集的 m3u8 地址（当前站点）。"""
    return get_play_url_at(_base(), did, ep)


def get_play_urls_batch(did, eps: List[int]) -> Dict[int, str]:
    result: Dict[int, str] = {}
    for ep in eps:
        try:
            result[ep] = get_play_url(did, ep)
        except Exception:                        # noqa: BLE001
            result[ep] = ""
    return result
