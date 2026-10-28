# -*- coding: utf-8 -*-
"""柚子短剧（youzidj.com）页面解析（SvelteKit 应用，Tailwind 风格模板）。

实测结构（2026-10-28）：

1) 列表（首页 / 标签页 / 分类页）
    <article class="drama-card pomelo-drama-card home-drama-card"
             data-home-work="5c147ff4-…">
      <a class="home-drama-card-link" aria-label="查看详情：我的岳母苏沛真"
         href="https://youzidj.com/dramas/my-mother-in-law-su-peizhen/season-1/">
        <span class="home-card-episodes-long">更新至32集</span>
        <span class="home-card-heat">899</span>
        <p class="drama-card-title" title="我的岳母苏沛真">我的岳母苏沛真</p>
      </a>
      <div class="drama-card-tags"><a class="drama-card-tag">情感</a>…</div>
    </article>
   ⚠ 站点 id 是 **slug**（不是数字）：/dramas/{slug}/season-N/。
     库里用 slug 当 did，详情/播放地址都由 slug 拼出来。

2) 详情 /dramas/{slug}/season-1/
    <h1>我的岳母苏沛真 · 第1季</h1>
    <a href="…/season-1-episode-1/">立即观看</a>
    <a href="…/season-1-episode-2/">02</a> …

3) 播放 /dramas/{slug}/season-1-episode-1/
    <script type="application/ld+json">{"contentUrl":
       "https://youzidj.com/media/{work_uuid}/{ep_uuid}/master.m3u8"}</script>
   ⚠ 这个 m3u8 **带凭证**：直接 requests 取会 403
   （{"error":"播放凭证已过期，请重新开始播放"}）。
   浏览器里同一地址能放流，实际走的是 /media/…/delivery-v2/{token}/…/init.mp4
   （DASH 分片）。所以播放取址要么复现凭证，要么用浏览器嗅探到的带 token 地址。
"""

import json
import re
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup

from .models import Drama
from .utils import parse_episode_count, strip_html

_SLUG_RE = re.compile(r"/dramas/([a-z0-9\-]+)/")
_EP_RE = re.compile(r"/dramas/[a-z0-9\-]+/season-\d+-episode-(\d+)/")


def _slug_of(href: str) -> str:
    m = _SLUG_RE.search(href or "")
    return m.group(1) if m else ""


def _cover_of(card) -> str:
    """封面：picture 里的 data-image-src / srcset（懒加载 + 多尺寸）。"""
    img = card.select_one("img[data-image-src]")
    if img is not None:
        url = str(img.get("data-image-src") or "").strip()
        if url:
            return url
    src = card.select_one("source[data-image-srcset]")
    if src is not None:
        first = str(src.get("data-image-srcset") or "").split(",")[0].strip().split(" ")[0]
        if first:
            return first
    img2 = card.select_one("img")
    if img2 is not None:
        return str(img2.get("src") or "").strip()
    return ""


def _parse_card(card) -> Drama | None:
    link = card.select_one("a.home-drama-card-link[href]") or card.select_one(
        "a[href*='/dramas/']")
    if link is None:
        return None
    href = str(link.get("href") or "")
    slug = _slug_of(href)
    if not slug:
        return None

    title = ""
    t = card.select_one(".drama-card-title")
    if t is not None:
        title = str(t.get("title") or "").strip() or strip_html(
            t.get_text(" ", strip=True))
    if not title:
        aria = str(link.get("aria-label") or "")
        if "：" in aria:
            title = aria.split("：", 1)[1].strip()

    ep_text = ""
    ep_el = card.select_one(".home-card-episodes-long") or card.select_one(
        ".home-card-episodes")
    if ep_el is not None:
        ep_text = strip_html(ep_el.get_text(" ", strip=True))

    heat = ""
    heat_el = card.select_one(".home-card-heat")
    if heat_el is not None:
        heat = strip_html(heat_el.get_text(" ", strip=True))

    tags = [strip_html(a.get_text(" ", strip=True))
            for a in card.select(".drama-card-tag")]
    tags = [x for x in tags if x]

    return Drama(did=slug, title=title, cover=_cover_of(card),
                 episode_text=ep_text, score="", desc=heat,
                 category="柚子", tags=tags)


def parse_list(html: str) -> List[Drama]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("article.drama-card") or soup.select("a[href*='/dramas/']")
    seen, order = set(), []
    for card in cards:
        d = _parse_card(card)
        if d is None or d.did in seen:
            continue
        seen.add(d.did)
        order.append(d)
    return order


def parse_list_meta(html: str) -> Tuple[int, int]:
    """站点分页是前端路由，这里拿不到总页数，返回 (0, 0)。"""
    return 0, 0


# ---------------------------------------------------------------- 详情页

def parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.select_one("h1")
    if h1 is not None:
        title = strip_html(h1.get_text(" ", strip=True))
        # 标题带「· 第1季」后缀，去掉后当剧名
        title = re.sub(r"\s*·\s*第\s*\d+\s*季\s*$", "", title).strip()
    if not title:
        og = soup.select_one('meta[property="og:title"]')
        title = str(og.get("content") or "").strip() if og else ""

    desc = ""
    for sel in ('[itemprop="description"]', ".drama-description", ".season-description",
                "meta[property='og:description']"):
        el = soup.select_one(sel)
        if el is not None:
            desc = str(el.get("content") or "").strip() if el.name == "meta" else strip_html(
                el.get_text(" ", strip=True))
            if desc:
                break

    cover = ""
    img = soup.select_one("img.drama-poster-image") or soup.select_one("img")
    if img is not None:
        cover = str(img.get("data-image-src") or img.get("src") or "").strip()

    episodes = extract_episodes(html)
    total = 0
    m = re.search(r"共\s*(\d+)\s*集", html)
    if m:
        total = int(m.group(1))
    count = max(episodes) if episodes else total

    tags: List[str] = []
    for a in soup.select("a[href*='/tags/']"):
        t = strip_html(a.get_text(" ", strip=True))
        if t and t not in tags:
            tags.append(t)

    return {
        "title": title,
        "desc": desc,
        "cover": cover,
        "episodes": episodes,
        "episode_count": count,
        "total_count": max(total, count),
        "tags": tags[:12],
    }


def extract_episodes(html: str) -> List[int]:
    """从 season-N-episode-M 链接取集号。"""
    soup = BeautifulSoup(html, "html.parser")
    eps = set()
    for a in soup.select("a[href*='-episode-']"):
        m = _EP_RE.search(str(a.get("href") or ""))
        if m:
            eps.add(int(m.group(1)))
    if not eps:
        for m in re.finditer(r"season-\d+-episode-(\d+)/", html):
            eps.add(int(m.group(1)))
    return sorted(eps)


# ---------------------------------------------------------------- 播放页

def parse_content_url(html: str) -> str:
    """从 JSON-LD 里取 contentUrl（m3u8 原始地址，可能需要凭证）。"""
    soup = BeautifulSoup(html, "html.parser")
    for sc in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(sc.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                url = str(item.get("contentUrl") or "")
                if ".m3u8" in url:
                    return url
    m = re.search(r'(https?://[^"\'\\\s]+?\.m3u8[^"\'\\\s]*)', html)
    return m.group(1) if m else ""


def parse_play_srcs(html: str) -> Dict[int, str]:
    """播放页只有当前一集的地址。"""
    url = parse_content_url(html)
    return {1: url} if url else {}


def detail_url(base: str, slug) -> str:
    base = (base or "").rstrip("/")
    return f"{base}/dramas/{slug}/season-1/"


def episode_url(base: str, slug, ep: int) -> str:
    """播放页 URL：/dramas/{slug}/season-1-episode-{ep}/。"""
    base = (base or "").rstrip("/")
    return f"{base}/dramas/{slug}/season-1-episode-{max(1, int(ep))}/"
