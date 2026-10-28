# -*- coding: utf-8 -*-
"""黄桃短剧（aihuangdou.com）页面解析。

与黄果树**不是同一套前端**（2026-10-28 实测）：黄果树是 div.hg-drama-card，
本站是 article.drama-card + .poster-orbit，所以独立一份解析器，不硬套
parser_home / parser_detail。

三处已验证的真实结构：

1) 列表卡片
    <article class="drama-card">
      <a class="poster-link" data-track-item-id="2726"
         data-track-item-name="高三爱情故事" href="https://aihuangdou.com/drama/2726/">
        <img class="poster-img" data-src="https://pic.drleao.cn/....jpg?auth_key=...">
        <span class="card-badges"><em class="card-score">9.6<i>分</i></em></span>
        <span class="eps-flag">更新中 · 42集</span>
      </a>
      <div class="card-info"><h3>高三爱情故事</h3>
        <p>AI短剧 · 校园 <span>· 25.0万 热度</span></p>
        <p class="card-desc">...</p></div>
    </article>
   封面 data-src 带 auth_key 签名，且**是 AES 加密图**（与 huangguoai 同一套
   key/iv，media_crypto.decrypt_cover(mode="aes") 直接能解，实测 224304 字节
   → JPEG 224289 字节）。

2) 详情页
    <section class="detail-top">
      <div class="detail-cover"><span class="detail-cover-status">更新至 3 集</span>
        <img class="poster-img" data-src="...">
      <div class="detail-main"><h1>八荒美色录</h1>
        <p class="detail-stats">8.4 分 ... · 已更新至 3 集 · ...</p>
        <div class="detail-intro">简介</div>
        <div class="detail-tags">修仙 剑修 ...</div>
    <section class="detail-section" id="nineEpisodes">
      <div class="episode-grid" data-total="3" id="nineEpisodeGrid">
        <a href="https://aihuangdou.com/video/13622/" title="第 1 集"><b>1</b></a>
        <a href="https://aihuangdou.com/video/13622/2/" title="第 2 集"><b>2</b></a>

3) 播放页
    <script id="ninePlayData" type="application/json">
      {"id":"13622","title":"八荒美色录","poster":"...","epTotal":3,
       "epCount":"已更新至 3 集","eps":[{"n":1,"src":"https://...m3u8?auth_key=..."}],
       "current":{"n":1,"src":"..."},"playBase":"/video/13622/"}
    </script>
   注意：JSON 是合法 JSON（& 被转义成 \\u0026），json.loads 后需还原成 &。
   m3u8 是 AES-128 加密的 HLS，yt-dlp + ffmpeg 能自动解密合并（与黄果一致）。

4) 排行榜（/rankings/）用的是 li.rank-row，不是 drama-card，单独 parse_ranks。
"""

import json
import re
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup

from .models import Drama
from .utils import parse_episode_count, parse_score, strip_html

_DID_RE = re.compile(r"/(?:drama|video)/(\d+)/?")
_EP_RE = re.compile(r"/video/(\d+)/(?:(\d+)/)?$")


def _did_of(href: str) -> int:
    """从 /drama/123/ 或 /video/123/2/ 里取剧 id。"""
    m = _DID_RE.search(href or "")
    try:
        return int(m.group(1)) if m else 0
    except (TypeError, ValueError):
        return 0


def _cover_of(img) -> str:
    """封面优先取 data-src（懒加载真实地址），否则 src。

    占位图（static/web/images/drama-loading.png）不算封面，返回空串。
    """
    if img is None:
        return ""
    for attr in ("data-src", "data-original", "src"):
        url = str(img.get(attr) or "").strip()
        if not url or "drama-loading" in url or url.startswith("data:"):
            continue
        return url
    return ""


def _parse_card(card) -> Drama | None:
    """解析一张列表卡片；信息不足时返回 None。"""
    link = card.select_one("a.poster-link[href]") or card.select_one(
        "a[href*='/drama/'], a[href*='/video/']")
    if link is None:
        return None
    href = str(link.get("href") or "")
    did = _did_of(href)
    if not did:
        try:
            did = int(str(link.get("data-track-item-id") or "").strip() or 0)
        except ValueError:
            did = 0
    if not did:
        return None

    title = str(link.get("data-track-item-name") or "").strip()
    if not title:
        h = card.select_one("h3") or card.select_one(".card-info a")
        title = strip_html(h.get_text(" ", strip=True)) if h else ""

    img = card.select_one("img.poster-img") or card.select_one("img")
    cover = _cover_of(img)

    score_el = card.select_one(".card-score")
    score = parse_score(score_el.get_text(" ", strip=True)) if score_el else ""

    eps_el = card.select_one(".eps-flag")
    episode_text = strip_html(eps_el.get_text(" ", strip=True)) if eps_el else ""

    desc_el = card.select_one(".card-desc") or card.select_one(".hover-panel")
    desc = strip_html(desc_el.get_text(" ", strip=True)) if desc_el else ""

    # 「AI短剧 · 校园 · 25.0万 热度」：第一段是分类，其余是标签
    tags: List[str] = []
    category = ""
    meta = card.select_one(".card-info p")
    if meta is not None:
        raw = strip_html(meta.get_text(" ", strip=True))
        raw = re.sub(r"·\s*[\d.]+\s*[万亿]?\s*热度.*$", "", raw).strip()
        for part in re.split(r"[·/|]", raw):
            part = part.strip()
            if not part or "热度" in part:
                continue
            if not category:
                category = part
            else:
                tags.append(part)

    return Drama(did=did, title=title, cover=cover, score=score,
                 episode_text=episode_text, desc=desc, tags=tags,
                 category=category)


def _list_scope(soup):
    """首页有多块 drama-grid（今日热播/猜你喜欢…），优先只取网格内的卡片。

    分类页整页就是一个网格，取与不取等价；首页若取全文会把 hero 区、
    推荐区的重复卡片都算进来。
    """
    grids = soup.select("div.drama-grid")
    return grids if grids else None


def parse_list(html: str) -> List[Drama]:
    """解析首页 / 分类页，返回去重后的剧集列表（保留页面顺序）。"""
    soup = BeautifulSoup(html, "html.parser")
    grids = _list_scope(soup)

    cards = []
    if grids:
        for g in grids:
            cards.extend(g.select("article.drama-card"))
    if not cards:
        cards = soup.select("article.drama-card")
    if not cards:                       # 兜底：任何带 /drama/ 链接的卡片
        cards = soup.select("div.drama-card, li.drama-card")

    seen: Dict[int, Drama] = {}
    order: List[Drama] = []
    for card in cards:
        d = _parse_card(card)
        if d is None:
            continue
        old = seen.get(d.did)
        if old is not None:
            # 同一部剧可能出现在多个板块，保留信息更全的那份
            if not old.desc and d.desc:
                old.desc = d.desc
            if not old.score and d.score:
                old.score = d.score
            if not old.cover and d.cover:
                old.cover = d.cover
            if not old.episode_text and d.episode_text:
                old.episode_text = d.episode_text
                old.episode_count = parse_episode_count(d.episode_text)
            continue
        seen[d.did] = d
        order.append(d)
    return order


def parse_list_meta(html: str) -> Tuple[int, int]:
    """列表页 (总条数, 总页数)。本站只有 data-pages，没有总条数 → 总数返回 0。"""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one("[data-pages]")
    pages = 0
    if el is not None:
        try:
            pages = int(str(el.get("data-pages") or 0))
        except ValueError:
            pages = 0
    total = 0
    el2 = soup.select_one("[data-total]")
    if el2 is not None:
        try:
            total = int(str(el2.get("data-total") or 0))
        except ValueError:
            total = 0
    return total, pages


def parse_ranks(html: str) -> List[Drama]:
    """排行榜（/rankings/）：li.rank-row 结构，不是 drama-card。"""
    soup = BeautifulSoup(html, "html.parser")
    out: List[Drama] = []
    seen = set()
    for row in soup.select("li.rank-row"):
        link = row.select_one("a[href*='/drama/']") or row.select_one("a[href]")
        if link is None:
            continue
        did = _did_of(str(link.get("href") or ""))
        if not did:
            try:
                did = int(str(link.get("data-track-item-id") or "") or 0)
            except ValueError:
                did = 0
        if not did or did in seen:
            continue
        title = str(link.get("data-track-item-name") or "").strip()
        h = row.select_one("h3")
        if not title and h is not None:
            title = strip_html(h.get_text(" ", strip=True))
        img = row.select_one("img.poster-img") or row.select_one("img")
        metric = row.select_one(".rank-row-metric b")
        copy = row.select_one(".rank-row-copy")
        text = strip_html(copy.get_text(" ", strip=True)) if copy else ""
        ep_text = ""
        m = re.search(r"(已更新至\s*\d+\s*集|更新中\s*·\s*\d+\s*集|全\s*\d+\s*集)", text)
        if m:
            ep_text = m.group(1)
        seen.add(did)
        out.append(Drama(
            did=did, title=title, cover=_cover_of(img),
            score=strip_html(metric.get_text(" ", strip=True)) if metric else "",
            episode_text=ep_text, desc=text, category="排行榜",
        ))
    return out


# ---------------------------------------------------------------- 详情页

def parse_detail(html: str) -> dict:
    """解析详情页：标题/简介/封面/标签/可下载集号。"""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.select_one("section.detail-top h1") or soup.select_one("h1")
    if h1 is not None:
        title = strip_html(h1.get_text(" ", strip=True))
    if not title:
        og = soup.select_one('meta[property="og:title"]')
        title = str(og.get("content") or "").strip() if og else ""

    cover = ""
    top = soup.select_one("section.detail-top")
    if top is not None:
        cover = _cover_of(top.select_one("img.poster-img") or top.select_one("img"))
    if not cover:
        og = soup.select_one('meta[property="og:image"]')
        cover = str(og.get("content") or "").strip() if og else ""

    intro = soup.select_one(".detail-intro")
    desc = strip_html(intro.get_text(" ", strip=True)) if intro else ""
    if not desc:
        og = soup.select_one('meta[property="og:description"]') or soup.select_one(
            'meta[name="description"]')
        desc = str(og.get("content") or "").strip() if og else ""

    tags: List[str] = []
    box = soup.select_one(".detail-tags")
    if box is not None:
        items = box.select("a")
        if items:
            tags = [strip_html(a.get_text(" ", strip=True)) for a in items]
        else:
            tags = [t for t in strip_html(box.get_text(" ", strip=True)).split() if t]

    episodes = extract_episodes(html)

    stats = soup.select_one(".detail-stats")
    stats_text = strip_html(stats.get_text(" ", strip=True)) if stats else ""
    total_count = 0
    m = re.search(r"已更新至\s*(\d+)\s*集", stats_text)
    if m:
        total_count = int(m.group(1))
    if not total_count:
        m = re.search(r"共\s*(\d+)\s*集", html)
        total_count = int(m.group(1)) if m else 0

    count = max(episodes) if episodes else total_count
    if total_count < count:
        total_count = count

    return {
        "title": title,
        "desc": desc,
        "cover": cover,
        "episodes": episodes,
        "episode_count": count,
        "total_count": total_count,
        "tags": tags,
    }


def extract_episodes(html: str) -> List[int]:
    """从 #nineEpisodeGrid 取可下载集号（升序）。

    第 1 集链接是 /video/{did}/（无集号后缀），其余是 /video/{did}/{ep}/。
    只认带 href 的 <a>：站点把未更新的集画成不可点的 <span>。
    """
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.select_one("#nineEpisodeGrid") or soup.select_one(".episode-grid")
    eps = set()
    if grid is not None:
        for a in grid.select("a[href]"):
            m = _EP_RE.search(str(a.get("href") or "").rstrip("/") + "/")
            if m:
                eps.add(int(m.group(2) or 1))
                continue
            digits = strip_html(a.get_text(" ", strip=True))
            if digits.isdigit():
                eps.add(int(digits))
    if not eps:
        # 兜底：正文里所有 /video/{did}/{ep}/ 链接
        for m in re.finditer(r"/video/\d+/(\d+)/", html):
            eps.add(int(m.group(1)))
        if re.search(r"/video/\d+/[\"']", html):
            eps.add(1)
    return sorted(eps)


# ---------------------------------------------------------------- 播放页

def parse_play_data(html: str) -> dict:
    """解析 <script id="ninePlayData"> 的 JSON；缺失返回空字典。"""
    m = re.search(
        r'<script[^>]*id=["\']ninePlayData["\'][^>]*>(.*?)</script>', html, re.S)
    if not m:
        return {}
    raw = (m.group(1) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_play_srcs(html: str) -> Dict[int, str]:
    """从播放页提取 {集号: m3u8 地址}。

    主路径是 ninePlayData.eps（一次性带全所有已更新集的 src）；
    退路是 ninePlayData.current（只有当前集），再退到全文扫 m3u8。
    """
    srcs: Dict[int, str] = {}
    data = parse_play_data(html)

    eps = data.get("eps")
    if isinstance(eps, list):
        for item in eps:
            if not isinstance(item, dict):
                continue
            src = str(item.get("src") or "").strip()
            if not src.startswith("http"):
                continue
            try:
                n = int(item.get("n") or 0)
            except (TypeError, ValueError):
                n = 0
            if n:
                srcs[n] = src

    cur = data.get("current")
    if isinstance(cur, dict) and str(cur.get("src") or "").startswith("http"):
        try:
            n = int(cur.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        srcs.setdefault(n, str(cur["src"]).strip())

    if not srcs:
        m = re.search(r'https?://[^"\'\\\s]+?\.m3u8[^"\'\\\s]*', html)
        if m:
            srcs[1] = m.group(0).replace("\\u0026", "&")
    return srcs


def parse_play_meta(html: str) -> dict:
    """播放页附带的信息（标题/集数/播放根路径），供取址与元数据兜底。"""
    data = parse_play_data(html)
    if not data:
        return {}
    try:
        ep_total = int(data.get("epTotal") or 0)
    except (TypeError, ValueError):
        ep_total = 0
    return {
        "did": str(data.get("id") or ""),
        "title": str(data.get("title") or ""),
        "poster": str(data.get("poster") or ""),
        "desc": str(data.get("description") or ""),
        "ep_total": ep_total,
        "ep_text": str(data.get("epCount") or ""),
        "play_base": str(data.get("playBase") or ""),
    }


def pick_episode(srcs: Dict[int, str], ep: int) -> str:
    """取指定集地址；精确匹配优先，只有一集时退回它。"""
    if not srcs:
        return ""
    if ep in srcs:
        return srcs[ep]
    if len(srcs) == 1:
        return next(iter(srcs.values()))
    return ""


def episode_url(base: str, did, ep: int) -> str:
    """按站点根构造某集播放页 URL（第 1 集无后缀）。"""
    base = (base or "").rstrip("/")
    if int(ep) <= 1:
        return f"{base}/video/{did}/"
    return f"{base}/video/{did}/{int(ep)}/"
