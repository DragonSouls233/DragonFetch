# -*- coding: utf-8 -*-
"""91成人短剧（91crdjai.com）页面解析。

第三套前端模板（2026-10-28 实测），与黄果（hg-drama-card）、黄桃
（article.drama-card + ninePlayData）都不同：

1) 列表页 /duanju/（分页 /duanju/page/{n}/，共 22 页）
    <a class="card" data-track-item-id="6016"
       data-track-item-name="维修工与巨乳人妻们的欢愉"
       href="/duanju/6016-weixiugongyujururenqimendehuanyu1/">
      <div class="poster"><img class="p-img" data-src="https://pic.drleao.cn/....jpeg?auth_key=...">
        <span class="badge new">新剧</span>
        <span class="eps-flag">更新中 · 3集</span></div>
      <div class="card-info"><h3>标题</h3>
        <div class="meta"><span>成人短剧 · 7193热度</span><span class="score">★8.8</span></div>
    </a>
   ⚠ 详情链接带 slug，**但服务端接受纯 id 并 302 到 slug 版**
     （/duanju/6016/ → /duanju/6016-weixiugong.../，实测），
     所以库里只存 did 就够，不用把 slug 写进数据模型。
   ⚠ 分页是 /duanju/page/{n}/，不是 /duanju/{n}/——后者是「剧 id=n」的详情
     （实测 /duanju/2/ 直接跳到 AC娘露营 详情页），分页规则必须写 segment="page"。

2) 详情页
    <h1>标题</h1>  <div class="d-poster">…<img class="p-img" data-src=…>
    <div class="d-info">简介 …</div>  <div class="d-rating">8.8 /10 综合</div>
    <div class="d-tags">调教 美乳 美腿</div>
    <div class="ep-panel"><div class="ep-grid">
      <a href="/duanju/{id}-{slug}/1/">1</a> … 共 N 集 · 全部免费

3) 播放页 /duanju/{id}-{slug}/{ep}/
    <script id="playInitialData" type="application/json">
      {"id":6016,"title":"…","poster":"…","eps":[{"n":1,"title":"第一集",…}],
       "current":{"n":1,"title":"第一集","src":"https://…m3u8?auth_key=…"},
       "playbackEndpoint":"/videos/6016/episodes/__EP__/playback",
       "playBase":"/duanju/6016-weixiugong…/"}
    </script>
   eps[] 里**没有 src**（只有 videoId），所以逐集取址要打
   /videos/{did}/episodes/{ep}/playback（实测 200 JSON，data.src 就是 m3u8）。
   与黄桃站是同一套后端 API（路径完全一致），只是前端模板不同。
"""

import json
import re
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup

from .models import Drama
from .utils import parse_episode_count, parse_score, strip_html

_DID_RE = re.compile(r"/duanju/(\d+)(?:-|/)")
_EP_RE = re.compile(r"/duanju/\d+[^/]*/(\d+)/")


def _did_of(href: str) -> int:
    m = _DID_RE.search(href or "")
    try:
        return int(m.group(1)) if m else 0
    except (TypeError, ValueError):
        return 0


def _cover_of(img) -> str:
    if img is None:
        return ""
    for attr in ("data-src", "data-original", "src"):
        url = str(img.get(attr) or "").strip()
        if not url or url.startswith("data:") or "loading" in url:
            continue
        return url
    return ""


def _parse_card(card) -> Drama | None:
    href = str(card.get("href") or "")
    did = _did_of(href)
    if not did:
        try:
            did = int(str(card.get("data-track-item-id") or "") or 0)
        except ValueError:
            did = 0
    if not did:
        return None

    title = str(card.get("data-track-item-name") or "").strip()
    if not title:
        h = card.select_one("h3")
        title = strip_html(h.get_text(" ", strip=True)) if h else ""

    img = card.select_one("img.p-img") or card.select_one("img")
    score_el = card.select_one(".score")
    eps_el = card.select_one(".eps-flag")
    meta = card.select_one(".meta")

    category = ""
    tags: List[str] = []
    if meta is not None:
        # 「成人短剧 · 7193热度」或「成人短剧 · 现代 · 都市」
        raw = strip_html(meta.get_text(" ", strip=True))
        raw = re.sub(r"★?\s*\d+(?:\.\d+)?\s*$", "", raw).strip()
        raw = re.sub(r"·?\s*[\d.]+\s*[万亿]?\s*热度.*$", "", raw).strip()
        for part in re.split(r"[·/|]", raw):
            part = part.strip()
            if not part or "热度" in part:
                continue
            if not category:
                category = part
            else:
                tags.append(part)

    ep_text = strip_html(eps_el.get_text(" ", strip=True)) if eps_el else ""
    return Drama(did=did, title=title, cover=_cover_of(img),
                 score=parse_score(score_el.get_text(" ", strip=True)) if score_el else "",
                 episode_text=ep_text, category=category, tags=tags)


def parse_list(html: str) -> List[Drama]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("a.card") or soup.select(".list a[href*='/duanju/']")
    seen: Dict[int, Drama] = {}
    order: List[Drama] = []
    for card in cards:
        d = _parse_card(card)
        if d is None:
            continue
        if d.did in seen:
            old = seen[d.did]
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
    """(总条数, 总页数)。本站只有「跳至 N 页」输入框的 max 属性。"""
    soup = BeautifulSoup(html, "html.parser")
    pages = 0
    el = soup.select_one("input.pg-input[max]")
    if el is not None:
        try:
            pages = int(str(el.get("max") or 0))
        except ValueError:
            pages = 0
    if not pages:
        nums = [int(m.group(1)) for m in re.finditer(r"/duanju/page/(\d+)/", html)]
        pages = max(nums) if nums else 0
    return 0, pages


# ---------------------------------------------------------------- 详情页

def parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    h1 = soup.select_one("h1")
    if h1 is not None:
        title = strip_html(h1.get_text(" ", strip=True))
    if not title:
        og = soup.select_one('meta[property="og:title"]')
        title = str(og.get("content") or "").strip() if og else ""

    cover = ""
    poster = soup.select_one(".d-poster")
    if poster is not None:
        cover = _cover_of(poster.select_one("img.p-img") or poster.select_one("img"))
    if not cover:
        og = soup.select_one('meta[property="og:image"]')
        cover = str(og.get("content") or "").strip() if og else ""

    info = soup.select_one(".d-info")
    desc = strip_html(info.get_text(" ", strip=True)) if info else ""
    if not desc:
        og = soup.select_one('meta[property="og:description"]') or soup.select_one(
            'meta[name="description"]')
        desc = str(og.get("content") or "").strip() if og else ""
    # 「作品简介」标题与「本剧共 N 集」尾巴对下载/查重没用，掐掉开头标签
    desc = re.sub(r"^作品简介\s*", "", desc).strip()

    rating = soup.select_one(".d-rating")
    score = ""
    if rating is not None:
        score = parse_score(rating.get_text(" ", strip=True))

    tags: List[str] = []
    box = soup.select_one(".d-tags")
    if box is not None:
        items = box.select("a")
        if items:
            tags = [strip_html(a.get_text(" ", strip=True)) for a in items]
        else:
            tags = [t for t in strip_html(box.get_text(" ", strip=True)).split() if t]

    episodes = extract_episodes(html)
    total_count = 0
    m = re.search(r"共\s*(\d+)\s*集", html)
    if m:
        total_count = int(m.group(1))
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
    """从 .ep-panel .ep-grid 取集号；退路是正文里的 /duanju/{id}-{slug}/{ep}/。"""
    soup = BeautifulSoup(html, "html.parser")
    eps = set()
    grid = soup.select_one(".ep-panel .ep-grid") or soup.select_one(".ep-grid")
    if grid is not None:
        for a in grid.select("a[href]"):
            m = _EP_RE.search(str(a.get("href") or ""))
            if m:
                eps.add(int(m.group(1)))
                continue
            txt = strip_html(a.get_text(" ", strip=True))
            if txt.isdigit():
                eps.add(int(txt))
    if not eps:
        for m in re.finditer(r"/duanju/\d+-[^/]+/(\d+)/", html):
            eps.add(int(m.group(1)))
    return sorted(eps)


# ---------------------------------------------------------------- 播放页

def parse_play_data(html: str) -> dict:
    m = re.search(
        r'<script[^>]*id=["\']playInitialData["\'][^>]*>(.*?)</script>', html, re.S)
    if not m:
        return {}
    try:
        data = json.loads((m.group(1) or "").strip())
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_play_srcs(html: str) -> Dict[int, str]:
    """播放页只能拿到**当前集**的 m3u8（eps[] 里没有 src）。"""
    data = parse_play_data(html)
    srcs: Dict[int, str] = {}
    cur = data.get("current")
    if isinstance(cur, dict) and str(cur.get("src") or "").startswith("http"):
        try:
            n = int(cur.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        srcs[n] = str(cur["src"]).strip()
    if not srcs:
        m = re.search(r'https?://[^"\'\\\s]+?\.m3u8[^"\'\\\s]*', html)
        if m:
            srcs[1] = m.group(0).replace("\\u0026", "&")
    return srcs


def parse_play_meta(html: str) -> dict:
    data = parse_play_data(html)
    if not data:
        return {}
    eps = data.get("eps")
    total = len(eps) if isinstance(eps, list) and eps else 0
    return {
        "did": str(data.get("id") or ""),
        "title": str(data.get("title") or ""),
        "poster": str(data.get("poster") or ""),
        "desc": str(data.get("description") or ""),
        "ep_total": total,
        "play_base": str(data.get("playBase") or ""),
        "playback_endpoint": str(data.get("playbackEndpoint") or ""),
    }


def episode_url(base: str, did, ep: int) -> str:
    """按站点根构造播放页 URL。纯 id 会被服务端 302 到 slug 版（实测可用）。"""
    base = (base or "").rstrip("/")
    return f"{base}/duanju/{did}/{max(1, int(ep))}/"


def playback_url(base: str, did, ep: int) -> str:
    """逐集取 m3u8 的接口地址（同黄桃站的 /videos/{did}/episodes/{ep}/playback）。"""
    base = (base or "").rstrip("/")
    return f"{base}/videos/{did}/episodes/{max(1, int(ep))}/playback"
