# -*- coding: utf-8 -*-
"""huangguoac.com（黄果漫剧）后端：实现 site_api 约定的同一组函数。

与 huangguoai 的关系（已逐页实测）:
    相同: 详情页（hg-web-detail__ep-grid / data-ep-id）、播放页内嵌 epPlaySrcs、
          排行榜（hg-rank-item）、专题（hg-topic-card）、封面 AES-128-CBC 加密、
          分页模板 {url}/{page}/、搜索路径 /search/video/{kw}/
    不同: **列表卡片**——本站用 hg-comic-card-grid / hg-comic-feed-card，
          短剧站用 hg-card-grid / hg-drama-card。因此列表解析走 parser_comic，
          其余（详情 / 播放 / 排行 / 专题）复用同一套解析器。

注意: 本站详情页的 og:image 是站点通用占位图（/static/web/images/home_og.jpg），
真实封面在 .hg-web-detail__poster img —— 该回退已收在 parser_detail 里。
"""

from typing import Dict, List
from urllib.parse import quote

from . import config
from .http_client import fetch_text
from .models import Drama
from .parser_comic import page_url as _page_url, parse_home, parse_list_meta
from .parser_comic import parse_search as _parse_search
from .parser_detail import parse_detail
from .parser_home import parse_ranks, parse_topics
from .parser_play import parse_play_srcs, pick_episode

# 自动翻页的上限保护（每页 20 条，60 页 = 1200 条）
_MAX_PAGES = 60


def list_dramas() -> List[Drama]:
    """获取首页剧集列表。"""
    html = fetch_text(config.URL_HOME)
    return parse_home(html)


def list_category(url: str) -> List[Drama]:
    """获取分类列表页的剧集（漫剧 / 里番动漫 / 日漫 / 韩漫 共用结构）。"""
    html = fetch_text(url)
    return parse_home(html)


def list_category_meta(url: str):
    """获取分类页的 (总条数, 总页数)。"""
    html = fetch_text(url)
    return parse_list_meta(html)


def list_category_all(url: str, max_pages: int = 0,
                      on_page=None) -> List[Drama]:
    """翻页拉取分类页全部剧集（去重，保持页序）。

    max_pages=0 表示按 data-pages 自动决定上限；上限保护 _MAX_PAGES。
    on_page(done, total) 可选回调，用于 UI 进度展示。
    """
    first = fetch_text(url)
    _total, pages = parse_list_meta(first)
    if pages <= 0:
        pages = 1
    if max_pages > 0:
        pages = min(pages, max_pages)
    pages = min(pages, _MAX_PAGES)

    seen = {}
    order: List[Drama] = []

    def _merge(dramas: List[Drama]) -> None:
        for d in dramas:
            if d.did in seen:
                continue
            seen[d.did] = True
            order.append(d)

    _merge(parse_home(first))
    if on_page:
        on_page(1, pages)

    for p in range(2, pages + 1):
        try:
            html = fetch_text(_page_url(url, p))
            _merge(parse_home(html))
        except Exception:                    # noqa: BLE001 - 单页失败不中断
            pass
        if on_page:
            on_page(p, pages)
    return order


def list_ranks() -> List[Drama]:
    """获取排行榜（热播榜）。"""
    html = fetch_text(config.category_url("/ranks/"))
    return parse_ranks(html)


def list_topics() -> list:
    """获取专题列表。"""
    html = fetch_text(config.category_url("/topics/"))
    return parse_topics(html)


def list_topic_dramas(slug: str) -> List[Drama]:
    """获取某个专题下的剧集列表（专题页内是普通卡片结构）。"""
    html = fetch_text(config.URL_TOPIC.format(slug=slug),
                      referer=config.category_url("/topics/"))
    return parse_home(html)


def search_dramas(keyword: str) -> List[Drama]:
    """按关键词搜索剧集。"""
    url = config.URL_SEARCH.format(kw=quote(keyword))
    html = fetch_text(url)
    return _parse_search(html)


# ---------------------------------------------------------------- 详情 / 播放

def load_detail(did: int) -> dict:
    """获取剧集详情（标题、简介、封面、可下载集数）。"""
    url = config.URL_DETAIL.format(did=did)
    html = fetch_text(url, referer=config.URL_HOME)
    return parse_detail(html)


def parse_detail_html(html: str) -> dict:
    """仅解析详情 HTML（供并发按站点抓取时复用，免去全局 URL 依赖）。"""
    return parse_detail(html)


def play_page_url_at(base_url: str, did, ep: int) -> str:
    """按**指定站点根**构造某一集的播放页 URL（第 1 集无 ep- 后缀）。

    不读全局 config：并发场景下（用户在队列运行中切换站点）必须用任务
    自己固化的站点根，否则会把 A 站的 did 拼到 B 站域名上，整片 404。
    """
    base = (base_url or config.BASE_URL).rstrip("/")
    if ep <= 1:
        return f"{base}/video/{did}/"
    return f"{base}/video/{did}/ep-{ep}/"


def play_page_url(did: int, ep: int) -> str:
    """构造当前站点的播放页 URL（保留旧签名）。"""
    return play_page_url_at(config.BASE_URL, did, ep)


def get_play_url_at(base_url: str, did, ep: int) -> str:
    """按指定站点根实时获取某一集的 m3u8 地址（并发安全，不依赖全局站点）。"""
    base = (base_url or config.BASE_URL).rstrip("/")
    url = play_page_url_at(base, did, ep)
    referer = f"{base}/detail/{did}/"
    html = fetch_text(url, referer=referer)
    srcs: Dict[int, str] = parse_play_srcs(html)

    picked = pick_episode(srcs, ep)
    if picked:
        return picked

    if srcs:
        return srcs[min(srcs)]
    return ""


def get_play_url(did: int, ep: int) -> str:
    """实时获取某一集的 m3u8 地址（当前站点）。"""
    return get_play_url_at(config.BASE_URL, did, ep)


def get_play_urls_batch(did: int, eps: List[int]) -> Dict[int, str]:
    """批量取多集地址（逐集请求，保证 auth_key 新鲜）。"""
    result: Dict[int, str] = {}
    for ep in eps:
        try:
            result[ep] = get_play_url(did, ep)
        except Exception:                        # noqa: BLE001
            result[ep] = ""
    return result
