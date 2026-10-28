# -*- coding: utf-8 -*-
"""AI污短剧（aiwu.ai-wu.cc）页面解析（maccms / 苹果CMS 模板）。

实测结构（2026-10-28）：

1) 列表 /browse/ 与分类页 /aidj/crdj/ …
    <a class="nf-card" href="/detail/3083/">
      <div class="nf-poster">
        <img src="https://media.hgpicimg.cc//upload/huangguo/10362.jpg">
        <span class="nf-badge">更新至5集</span>
      </div>
      <div class="nf-card-title">裸贷</div>
      <div class="nf-card-meta">2026-10-03</div>
    </a>

2) 详情 /detail/{id}/
    <h1>宝贝妈妈蓝菲菲</h1>
    <section><h2>剧情介绍</h2>…</section>
    <section><h2>播放列表</h2>
      <a href="/play/3083-1-1/">第1集</a> <a href="/play/3083-1-2/">第2集</a>
    </section>

3) 播放 /play/{id}-{sid}-{ep}/
    var player_data={"flag":"play","encrypt":0,"url":"10362|1","from":"huangguo",…}
   encrypt=0 表示 url 未加密；「10362|1」= maccms 的 vod id | 集号，
   **vod id 与封面路径一致**（/upload/huangguo/10362.jpg），说明本站片源
   就是黄果那批内容（from="huangguo"）——这一点决定了它与黄果高度重合。

集数：从 /play/{id}-{sid}-{ep}/ 里的第三段取。
"""

import json
import re
from typing import Dict, List, Tuple

from bs4 import BeautifulSoup

from .models import Drama
from .utils import parse_episode_count, strip_html

_DID_RE = re.compile(r"/detail/(\d+)")
_PLAY_RE = re.compile(r"/play/(\d+)-(\d+)-(\d+)/")


def _did_of(href: str) -> int:
    m = _DID_RE.search(href or "")
    try:
        return int(m.group(1)) if m else 0
    except (TypeError, ValueError):
        return 0


def _parse_card(card) -> Drama | None:
    href = str(card.get("href") or "")
    did = _did_of(href)
    if not did:
        return None
    title_el = card.select_one(".nf-card-title")
    title = strip_html(title_el.get_text(" ", strip=True)) if title_el else ""
    if not title:
        img = card.select_one("img")
        title = str(img.get("alt") or "").strip() if img else ""
    badge = card.select_one(".nf-badge")
    ep_text = strip_html(badge.get_text(" ", strip=True)) if badge else ""
    img = card.select_one("img")
    cover = str(img.get("src") or "").strip() if img else ""
    meta = card.select_one(".nf-card-meta")
    return Drama(did=did, title=title, cover=cover, episode_text=ep_text,
                 desc=strip_html(meta.get_text(" ", strip=True)) if meta else "",
                 category="AI污")


def parse_list(html: str) -> List[Drama]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("a.nf-card") or soup.select("a[href*='/detail/']")
    seen, order = set(), []
    for card in cards:
        d = _parse_card(card)
        if d is None or d.did in seen:
            continue
        seen.add(d.did)
        order.append(d)
    return order


def parse_list_meta(html: str) -> Tuple[int, int]:
    """站点没给总页数，返回 (0, 0)。"""
    return 0, 0


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

    desc = ""
    for sec in soup.select("section"):
        h2 = sec.find("h2")
        if h2 is not None and "剧情" in h2.get_text():
            desc = strip_html(sec.get_text(" ", strip=True))
            desc = re.sub(r"^剧情介绍\s*", "", desc)
            break
    if not desc:
        og = soup.select_one('meta[property="og:description"]') or soup.select_one(
            'meta[name="description"]')
        desc = str(og.get("content") or "").strip() if og else ""

    cover = ""
    for img in soup.find_all("img"):
        src = str(img.get("src") or "")
        if "/upload/huangguo/" in src or "media.hgpicimg" in src:
            cover = src
            break

    episodes = extract_episodes(html)
    total = 0
    m = re.search(r"共\s*(\d+)\s*集", html)
    if m:
        total = int(m.group(1))
    count = max(episodes) if episodes else total

    tags: List[str] = []
    return {
        "title": title,
        "desc": desc,
        "cover": cover,
        "episodes": episodes,
        "episode_count": count,
        "total_count": max(total, count),
        "tags": tags,
    }


def extract_episodes(html: str) -> List[int]:
    """从 /play/{id}-{sid}-{ep}/ 取集号（第三段）。"""
    soup = BeautifulSoup(html, "html.parser")
    eps = set()
    for a in soup.select("a[href*='/play/']"):
        m = _PLAY_RE.search(str(a.get("href") or ""))
        if m:
            eps.add(int(m.group(3)))
    if not eps:
        for m in re.finditer(r"/play/\d+-\d+-(\d+)/", html):
            eps.add(int(m.group(1)))
    return sorted(eps)


# ---------------------------------------------------------------- 播放页

def parse_player_data(html: str) -> dict:
    """解析 var player_data={...}（maccms 标准字段）。"""
    m = re.search(r"var\s+player_data\s*=\s*(\{.*?\})\s*;?", html, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_play_srcs(html: str) -> Dict[int, str]:
    """播放页直链。

    maccms 的 player_data.url 是「vod id|集号」，真正的直链由站点解析器
    下发；页面里若直接出现 m3u8（部分解析器会把地址写在页面中）则直接取。
    """
    srcs: Dict[int, str] = {}
    for m in re.finditer(r'(https?://[^"\'\\\s]+?\.m3u8[^"\'\\\s]*)', html):
        url = m.group(1).replace("\\u0026", "&")
        if url not in srcs.values():
            srcs[len(srcs) + 1] = url
            break
    return srcs


def parse_vod_ref(html: str) -> dict:
    """从 player_data 取出 (vod_id, 集号, 来源源)。

    vod_id 与封面路径 /upload/huangguo/{vod_id}.jpg 对应，
    是 maccms 的 vid（不是本站的 /detail/{id}）。
    """
    data = parse_player_data(html)
    if not data:
        return {}
    url = str(data.get("url") or "")
    vid, ep = "", 0
    if "|" in url:
        vid, _, ep_s = url.partition("|")
        try:
            ep = int(ep_s)
        except ValueError:
            ep = 0
    return {
        "vid": vid.strip(),
        "ep": ep,
        "from": str(data.get("from") or ""),
        "encrypt": data.get("encrypt"),
        "raw_url": url,
    }


def episode_url(base: str, did, ep: int) -> str:
    """播放页 URL：/play/{detail_id}-1-{ep}/（季固定为 1）。"""
    base = (base or "").rstrip("/")
    return f"{base}/play/{did}-1-{max(1, int(ep))}/"
