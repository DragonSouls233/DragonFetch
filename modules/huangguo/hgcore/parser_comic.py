# -*- coding: utf-8 -*-
"""漫剧站列表页解析：hg-comic-card-grid / hg-comic-feed-card。

「黄果漫剧」(huangguoac.com) 与「黄果AI短剧」(huangguoai.com) 同属一套前端框架，
但**列表卡片不是同一套 class**，不能共用 parser_home：

    站点            列表容器                          卡片
    huangguoai     div.hg-card-grid[.is-active]     div.hg-drama-card
    huangguoac     div.hg-comic-card-grid[.is-active] div.hg-comic-feed-card

已验证的卡片结构::

    <div class="hg-comic-feed-card" data-track-id="2628" data-track-title="标题">
      <a href="/detail/2628/" style="display:contents">
        <div class="hg-comic-feed-card__cover">
          <img alt="标题" data-src="真实封面" src="占位图">
          <div class="hg-comic-feed-card__copy"><h3>标题</h3></div>
          <span class="hg-comic-feed-card__episode">更新至1集</span>
        </div>
      </a>
      <div class="hg-comic-feed-card__meta">
        <a href="/tag/naodong/">脑洞</a> ...
      </div>
    </div>

注意（踩过的坑）:
    页面里同时存在搜索联想区的公共 ``hg-drama-card``（每页都一样），
    以及 ``<template data-panel-cards="hot|random">`` 里的隐藏面板。
    因此**必须先锁定 is-active 容器**，否则会把「首项永远相同」的污染卡片
    当成列表内容（与 parser_home 处理 hg-card-grid 的原因一致）。

分页（来自 data-jump-tpl="/ai-manju/{page}/"）:
    第 1 页 -> 原地址；第 N 页 -> {url}/{N}/（与 parser_home.page_url 同规则）
"""

import re
from typing import List, Tuple

from bs4 import BeautifulSoup

from .models import Drama
from .utils import parse_score

# 真实列表容器（排除 <template> 内模板与搜索联想区的公共卡片）
_GRID_SELECTOR = "div.hg-comic-card-grid.is-active"
_GRID_ANY = "div.hg-comic-card-grid"

# 卡片选择器
_CARD_SELECTOR = "div.hg-comic-feed-card"


def _list_container(soup):
    """定位真实列表容器。

    优先级:
      1. div.hg-comic-card-grid.is-active  —— 列表页 / 标签页（当前激活面板）
      2. 第一个「非隐藏且不在 <template> 内」的 div.hg-comic-card-grid —— 搜索页
      3. None（调用方退回全文解析）
    """
    c = soup.select_one(_GRID_SELECTOR)
    if c is not None:
        return c

    for g in soup.select(_GRID_ANY):
        if "hidden" in g.attrs:
            continue
        if any(p.name == "template" for p in g.parents):
            continue
        return g
    return None


def parse_list_meta(html: str) -> Tuple[int, int]:
    """读取列表页的 (总条数, 总页数)；缺失返回 (0, 0)。

    - 总条数来自 div.hg-comic-card-grid[data-panel-total]（如 45）
    - 总页数来自分页条 [data-pages]
    """
    soup = BeautifulSoup(html, "html.parser")
    total = pages = 0

    grid = soup.select_one("div.hg-comic-card-grid[data-panel-total]")
    if grid:
        try:
            total = int(grid.get("data-panel-total") or 0)
        except ValueError:
            total = 0

    jump = soup.select_one("[data-pages]")
    if jump:
        try:
            pages = int(jump.get("data-pages") or 0)
        except ValueError:
            pages = 0
    return total, pages


def page_url(url: str, page: int) -> str:
    """按分页模板生成第 N 页 URL（page<=1 返回原地址）。"""
    if page <= 1:
        return url
    return f"{url.rstrip('/')}/{page}/"


def parse_home(html: str) -> List[Drama]:
    """解析列表页 HTML，返回去重后的剧集列表（保持页序）。"""
    soup = BeautifulSoup(html, "html.parser")
    container = _list_container(soup)
    scope = container if container is not None else soup

    seen = {}
    order: List[Drama] = []

    for card in scope.select(_CARD_SELECTOR):
        drama = _parse_card(card)
        if not drama:
            continue
        if drama.did in seen:
            old = seen[drama.did]
            if not old.title and drama.title:
                old.title = drama.title
            if not old.cover and drama.cover:
                old.cover = drama.cover
            continue
        seen[drama.did] = drama
        order.append(drama)

    return order


def _parse_card(card) -> "Drama | None":
    """解析单个 hg-comic-feed-card 节点。"""
    # ---- ID：优先 data-track-id，其次卡片内 /detail/{id}/ 链接
    did = None
    raw_id = card.get("data-track-id")
    if raw_id:
        try:
            did = int(str(raw_id).strip())
        except ValueError:
            did = None
    if did is None:
        link = card.select_one('a[href*="/detail/"]')
        m = re.search(r"/detail/(\d+)/", (link.get("href") if link else "") or "")
        if not m:
            return None
        did = int(m.group(1))

    # ---- 封面：懒加载地址在 data-src
    img = card.select_one("img[data-src], img")
    cover = ""
    if img:
        cover = (img.get("data-src") or img.get("src") or "").strip()
        if "placeholder" in cover:
            cover = ""

    # ---- 标题：data-track-title -> h3 -> img alt
    title = (card.get("data-track-title") or "").strip()
    if not title:
        h3 = card.select_one("h3")
        if h3:
            title = h3.get_text(strip=True)
    if not title and img:
        title = (img.get("alt") or "").strip()

    # ---- 集数文案（"更新至4集"）；卡片不带评分，留空由详情页补
    ep_el = card.select_one(".hg-comic-feed-card__episode")
    episode_text = ep_el.get_text(strip=True) if ep_el else ""
    score = parse_score(episode_text) if ep_el else ""

    # ---- 标签（hg-comic-feed-card__meta 内）
    tags = [t.get_text(strip=True)
            for t in card.select(".hg-comic-feed-card__meta a")
            if t.get_text(strip=True)]

    return Drama(
        did=did,
        title=title or f"未命名{did}",
        cover=cover,
        score=score,
        episode_text=episode_text,
        desc="",
        tags=tags,
    )


def parse_search(html: str) -> List[Drama]:
    """解析搜索结果页（卡片结构一致，直接复用）。"""
    return parse_home(html)


__all__ = ["parse_home", "parse_search", "parse_list_meta", "page_url"]
