# -*- coding: utf-8 -*-
"""huangguo.me 后端：实现 site_api 约定的同一组函数。

site_api 会按 config.ACTIVE_SITE_ID 把调用分发到这里。所有列表/详情/取址逻辑
都基于 huangguo.me 的结构（poster-card + chapter API）。

付费说明：该站为金币/会员付费站，通常仅前 1-2 集免费，其余章节 API 返回空
videoUrl（needUnlock=true）。本后端如实返回空串，不做任何付费绕过。
"""

from typing import Dict, List
from urllib.parse import quote

from . import config
from .http_client import fetch_text
from .models import Drama
from .parser_huangguome import (
    list_page_url, page_url as _page_url, pager_template, parse_detail,
    parse_home, parse_list_meta, parse_page_links, resolve_play_url,
    resolve_play_url_at,
)
from urllib.parse import urljoin

# 分集 -> chapterId 映射缓存（按 did/slug），避免每次取址都重新抓详情
_CHAPTERS: Dict[str, Dict[int, int]] = {}
# 分集 -> 是否锁定（付费）缓存
_LOCKED: Dict[str, set] = {}


def list_dramas() -> List[Drama]:
    return parse_home(fetch_text(config.URL_HOME))


def list_category(url: str) -> List[Drama]:
    return parse_home(fetch_text(url))


def list_category_meta(url: str):
    """获取分类页的 (总页数, 当前页)。"""
    return parse_list_meta(fetch_text(url))


def list_category_all(url: str, max_pages: int = 0,
                      on_page=None) -> List[Drama]:
    """翻页拉取分类页全部剧集（去重，保持页序）。

    huangguome 每页 25 部。分页 URL 用页面上真实链接推导的模板拼接
    （部分频道路径含额外段，如 /list/x/latest/page/2/，猜不得），
    推不出模板时才回退到 page_url 猜测。上限保护 30 页。
    """
    first = fetch_text(url)
    pages, _cur = parse_list_meta(first)
    tpl = pager_template(first)              # 形如 /list/x/latest/page/{page}/
    links = parse_page_links(first)
    if pages <= 0:
        pages = max(links) if links else 1
    if max_pages > 0:
        pages = min(pages, max_pages)
    pages = min(pages, 30)

    seen = set()
    order: List[Drama] = []

    def _merge(dramas: List[Drama]) -> None:
        for d in dramas:
            if d.did in seen:
                continue
            seen.add(d.did)
            order.append(d)

    _merge(parse_home(first))
    if on_page:
        on_page(1, pages)

    for p in range(2, pages + 1):
        # 优先：真实模板 > 页面上该页的显式链接 > 猜测模板
        target = (list_page_url(tpl, config.BASE_URL, p)
                  or (urljoin(config.BASE_URL + "/", links[p]) if p in links else "")
                  or _page_url(url, p))
        try:
            html = fetch_text(target)
            _merge(parse_home(html))
        except Exception:                    # noqa: BLE001 - 单页失败不中断
            pass
        if on_page:
            on_page(p, pages)
    return order


def list_ranks() -> List[Drama]:
    # huangguo.me 无独立排行榜页，回退到首页
    return list_dramas()


def list_topics() -> list:
    return []


def list_topic_dramas(slug: str) -> List[Drama]:
    return []


def search_dramas(keyword: str) -> List[Drama]:
    try:
        url = f"{config.BASE_URL}/search/video/{quote(keyword)}/"
        return parse_home(fetch_text(url))
    except Exception:                          # noqa: BLE001
        return []


def load_detail_at(base_url: str, did) -> dict:
    """按**指定站点根**获取剧集详情，并刷新 chapters / 锁定集缓存。

    并发安全：不读全局 config，供「任务自带站点」的取址流程调用。
    """
    base = (base_url or config.BASE_URL).rstrip("/")
    html = fetch_text(f"{base}/detail/{did}/", referer=base + "/")
    detail = parse_detail(html)
    _CHAPTERS[str(did)] = detail.get("chapters", {})
    _LOCKED[str(did)] = set(detail.get("locked_eps", []))
    return detail


def load_detail(did) -> dict:
    """获取剧集详情（标题、简介、封面、集数、chapters 映射、锁定集）。"""
    return load_detail_at(config.BASE_URL, did)


def parse_detail_html(html: str) -> dict:
    """仅解析详情 HTML（供并发按站点抓取时复用，免去全局 URL 依赖）。"""
    return parse_detail(html)


def get_play_url_at(base_url: str, did, ep: int) -> str:
    """按指定站点根实时获取某一集播放地址（并发安全，不依赖全局站点）。"""
    base = (base_url or config.BASE_URL).rstrip("/")
    did = str(did)
    chapters = _CHAPTERS.get(did)
    if not chapters or ep not in chapters:
        # 重新加载详情以补全映射
        try:
            load_detail_at(base, did)
        except Exception:                      # noqa: BLE001
            pass
        chapters = _CHAPTERS.get(did, {})

    chapter_id = chapters.get(ep)
    if not chapter_id:
        return ""
    return resolve_play_url_at(base, chapter_id)


def get_play_url(did, ep: int) -> str:
    """实时获取某一集的播放地址（m3u8，当前站点）。

    返回空串表示未取到：多为金币/会员付费内容（needUnlock），或 chapterId 缺失。
    """
    return get_play_url_at(config.BASE_URL, did, ep)
