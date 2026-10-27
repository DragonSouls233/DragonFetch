# -*- coding: utf-8 -*-
"""首页 / 列表页解析：提取剧集卡片列表。

卡片结构（已验证）:
    <div class="hg-drama-card">
      <a class="hg-drama-card__cover-link" href="/video/{id}/" style="display:contents">
        <div class="hg-drama-card__cover">
          <img src="placeholder" data-src="真实封面" alt="标题" ...>
          <span class="hg-drama-card__badge">黄果原创</span>
          <span class="hg-drama-card__score">9.9分</span>
          <span class="hg-drama-card__episode" data-ep-base="更新至4集">...</span>
        </div>
      </a>
      <div class="hg-drama-card__body">
        <a href="/video/{id}/">标题</a>
        <p class="hg-drama-card__desc">简介</p>
        <div class="hg-drama-card__tags"><a class="hg-tag">现代</a>...</div>
      </div>
    </div>

注意：站点前端曾把卡片链接从 /detail/{id}/ 切换到 /video/{id}/（二者内容等价，
/detail/ 仍作为别名可用）。_parse_card / parse_ranks 同时兼容两种链接。
"""

import re
from typing import List, Tuple

from bs4 import BeautifulSoup

from .models import Drama
from .utils import parse_episode_count, parse_score, strip_html

# 真实列表容器（排除 <template> 内的隐藏模板与搜索联想区的公共卡片）
_GRID_SELECTOR = "div.hg-card-grid.is-active"
# 兜底：无 is-active 时取带 data-channel-panel 的网格
_GRID_FALLBACK = "div.hg-card-grid[data-channel-panel]"


def _list_container(soup):
    """定位真实列表容器。

    站点列表页包含三类 hg-drama-card：
      1. <template> 内的 JS 模板（隐藏，占多数）
      2. 搜索联想区 hg-search-suggest__guess（每页相同的公共卡片）
      3. 真实列表 hg-card-grid（带 is-active / data-channel-panel）
    必须只取第 3 类，否则会出现「各 TAB 首项相同」的污染。
    """
    c = soup.select_one(_GRID_SELECTOR) or soup.select_one(_GRID_FALLBACK)
    if c is not None:
        return c
    # 极端兜底：按 data-panel-total 找
    c = soup.select_one("div.hg-card-grid[data-panel-total]")
    return c


def parse_list_meta(html: str) -> Tuple[int, int]:
    """读取列表页的 (总条数, 总页数)；缺失返回 (0, 0)。"""
    soup = BeautifulSoup(html, "html.parser")
    total = pages = 0
    grid = soup.select_one("div.hg-card-grid[data-panel-total]")
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
    """按 huangguoai 分页规则生成第 N 页 URL。

    规则（来自 data-jump-tpl="/ai-duanju/{page}/"）:
        第 1 页 -> 原地址（/ai-duanju/）
        第 N 页 -> /ai-duanju/{N}/
    """
    if page <= 1:
        return url
    u = url.rstrip("/")
    return f"{u}/{page}/"


def parse_home(html: str) -> List[Drama]:
    """解析首页 / 列表页 HTML，返回去重后的剧集列表。

    只解析真实列表容器内的卡片，避免模板与搜索联想区污染。
    首页（无 hg-card-grid）退回解析全文卡片。
    """
    soup = BeautifulSoup(html, "html.parser")
    container = _list_container(soup)
    scope = container if container is not None else soup

    seen = {}
    order = []

    for card in scope.select("div.hg-drama-card"):
        drama = _parse_card(card)
        if not drama:
            continue
        if drama.did in seen:
            # 同一部剧可能在多个板块出现，保留信息更完整的那个
            old = seen[drama.did]
            if not old.desc and drama.desc:
                old.desc = drama.desc
            if not old.title and drama.title:
                old.title = drama.title
            continue
        seen[drama.did] = drama
        order.append(drama)

    return order


def _parse_card(card) -> "Drama | None":
    """解析单个卡片节点。"""
    # ---- ID：卡片内第一个 /detail/{id}/ 或 /video/{id}/ 链接
    # 站点前端曾把卡片链接从 /detail/{id}/ 切换到 /video/{id}/（两者内容等价，
    # /detail/ 仍作为别名可用）。两种都认，避免「列表解析为 0」导致首页/分类全部读取不到。
    link = card.select_one('a[href*="/detail/"], a[href*="/video/"]')
    if not link:
        return None
    m = re.search(r"/(?:detail|video)/(\d+)/", link.get("href", ""))
    if not m:
        return None
    did = int(m.group(1))

    # ---- 封面：懒加载地址在 data-src
    cover = ""
    img = card.select_one("img[data-src]")
    if img:
        cover = img.get("data-src") or img.get("src") or ""

    # ---- 标题：优先 body 里的链接文本，其次 img alt
    # 同样的 /detail|video 兼容（标题链接也可能已切到 /video/）
    title = ""
    body_links = card.select(
        'div.hg-drama-card__body a[href*="/detail/"], '
        'div.hg-drama-card__body a[href*="/video/"]')
    for a in body_links:
        txt = a.get_text(strip=True)
        if txt:
            title = txt
            break
    if not title and img:
        title = img.get("alt", "").strip()

    # ---- 评分 / 集数
    score_el = card.select_one(".hg-drama-card__score")
    score = parse_score(score_el.get_text(strip=True)) if score_el else ""

    ep_el = card.select_one(".hg-drama-card__episode")
    episode_text = ""
    if ep_el:
        # data-ep-base 是完整文案，如 "更新至4集"；否则退回文本
        episode_text = ep_el.get("data-ep-base") or ep_el.get_text(strip=True)

    # ---- 简介 / 标签
    desc_el = card.select_one(".hg-drama-card__desc")
    desc = strip_html(desc_el.get_text()) if desc_el else ""

    tags = [t.get_text(strip=True) for t in card.select("a.hg-tag") if t.get_text(strip=True)]

    return Drama(
        did=did,
        title=title or f"未命名{did}",
        cover=cover,
        score=score,
        episode_text=episode_text,
        desc=desc,
        tags=tags,
    )


def parse_search(html: str) -> List[Drama]:
    """解析搜索结果页。结构与首页卡片一致，直接复用。"""
    return parse_home(html)


def parse_ranks(html: str) -> List[Drama]:
    """解析排行榜页（hg-rank-item 结构）。

    结构:
        <div class="hg-rank-item" data-rank-id="117">
          <span class="hg-rank-num">01</span>
          <a class="hg-rank-item__cover" href="/video/117/"><img data-src alt></a>
          <h2 class="hg-rank-item__title"><a href="/video/117/">标题</a></h2>
          <div class="hg-rank-item__tags">... · 9.9分</div>
          <p class="hg-rank-item__desc">简介</p>
        </div>
    排行页不提供集数，详情加载后补全。
    """
    soup = BeautifulSoup(html, "html.parser")
    result: List[Drama] = []
    seen = set()

    for item in soup.select("div.hg-rank-item"):
        link = item.select_one('a[href*="/detail/"], a[href*="/video/"]')
        if not link:
            continue
        m = re.search(r"/(?:detail|video)/(\d+)/", link.get("href", ""))
        if not m:
            continue
        did = int(m.group(1))
        if did in seen:
            continue
        seen.add(did)

        img = item.select_one("img[data-src]")
        cover = (img.get("data-src") or img.get("src") or "") if img else ""

        title_el = item.select_one(".hg-rank-item__title")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title and img:
            title = img.get("alt", "").strip()

        # 评分藏在 tags 文本里（"... · 9.9分"）
        tags_el = item.select_one(".hg-rank-item__tags")
        score = parse_score(tags_el.get_text(strip=True)) if tags_el else ""

        desc_el = item.select_one(".hg-rank-item__desc")
        desc = strip_html(desc_el.get_text()) if desc_el else ""

        tags = [t.get_text(strip=True) for t in item.select(".hg-rank-item__tags a")]

        result.append(Drama(did=did, title=title or f"未命名{did}", cover=cover,
                            score=score, desc=desc, tags=tags))

    # 兜底：排名页同时嵌有少量普通卡片
    for d in parse_home(html):
        if d.did not in seen:
            seen.add(d.did)
            result.append(d)
    return result


def parse_topics(html: str) -> list:
    """解析专题列表页（hg-topic-card 结构），返回 List[Topic]。

    结构:
        <a class="hg-topic-card" href="/topics/hot-aiduanju/">
          <img data-src alt="专题标题">
          <p class="hg-topic-card__meta"><span>28部剧情</span></p>
          <h3 class="hg-topic-card__title">标题</h3>
        </a>
    """
    from .models import Topic

    soup = BeautifulSoup(html, "html.parser")
    result = []
    seen = set()

    for card in soup.select("a.hg-topic-card"):
        href = card.get("href", "")
        m = re.search(r"/topics/([a-z0-9-]+)/", href)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)

        img = card.select_one("img[data-src]")
        cover = (img.get("data-src") or img.get("src") or "") if img else ""

        title_el = card.select_one(".hg-topic-card__title")
        title = title_el.get_text(strip=True) if title_el else (img.get("alt", "") if img else "")

        meta_el = card.select_one(".hg-topic-card__meta")
        count_text = meta_el.get_text(strip=True) if meta_el else ""

        result.append(Topic(slug=slug, title=title, cover=cover, count_text=count_text))

    return result
