# -*- coding: utf-8 -*-
"""详情页解析：标题、简介、封面、集数。

已验证的选择器:
    标题      h1
    集数网格  div.hg-web-detail__ep-grid a[data-ep-id]
    集数文案  「共 N 集 · 已更新 M 集」
    简介      meta[name=description] / meta[property=og:description]
    封面      meta[property=og:image]；是站点占位图时回退 .hg-web-detail__poster img
               （漫剧站 huangguoac 的 og:image 恒为 /static/web/images/home_og.jpg）
    标签      .hg-web-detail__tags a（避免收录「相关推荐」卡片的标签）

集数口径（重要）:
    站点把**全部分集**都画进网格，但「未更新」的集是一个没有 href、
    带 class="is-locked" / title="未更新" 的死按钮（点不开、取不到播放地址）。
    因此「可下载集数」必须只算**已更新（有 href）**的集。
    旧版把所有 data-ep-id 都算进去，把「共 8 集 · 已更新 1 集」的剧当成 8 集，
    导致更新检测误报「1 → 8」、补下时整片 404（详见 2026-09-12 日志复盘）。
"""

import re
from typing import List, Tuple

from bs4 import BeautifulSoup

from .utils import parse_episode_count, strip_html


def parse_detail(html: str) -> dict:
    """解析详情页，返回结构化字段字典。

    返回:
        {
          "title": str,
          "desc": str,
          "cover": str,
          "episodes": [可下载集号 int, ...],   # 升序，仅「已更新」的集
          "episode_count": int,               # 可下载集数（= 已更新集数）
          "total_count": int,                 # 平台预告总集数「共 N 集」
          "tags": [str, ...],
        }
    """
    soup = BeautifulSoup(html, "html.parser")

    title = _extract_title(soup)
    desc = _extract_desc(soup)
    cover = _extract_cover(soup)
    episodes = extract_episodes(html)
    tags = _extract_tags(soup)

    total_count = extract_total_count(html)
    updated = extract_updated_count(html)

    # 权威值：网格里「有 href」的集；网格解析不出来时退化到「已更新 M 集」
    count = max(episodes) if episodes else updated
    if total_count and total_count < count:
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


def _is_locked_ep(a) -> bool:
    """该集是否「未更新 / 锁定」：无 href，或带 is-locked / aria-disabled。"""
    if str(a.get("aria-disabled") or "").lower() == "true":
        return True
    cls = " ".join(a.get("class") or [])
    if "is-locked" in cls:
        return True
    return not a.get("href")


def extract_episodes(html: str) -> List[int]:
    """从集数网格提取**已更新（可下载）**的集号（升序）。

    只保留有 href 且未被标记 is-locked 的集；「未更新」的死按钮一律排除。
    """
    soup = BeautifulSoup(html, "html.parser")
    eps = set()

    for a in soup.select("div.hg-web-detail__ep-grid a[data-ep-id]"):
        if _is_locked_ep(a):
            continue
        try:
            eps.add(int(a.get("data-ep-id")))
        except (TypeError, ValueError):
            continue

    # 网格缺失时，退回到 ep-N 链接解析（这些链接本身就代表剧集存在）
    if not eps:
        for m in re.finditer(r"/video/\d+/ep-(\d+)/", html):
            eps.add(int(m.group(1)))
        if re.search(r"/video/\d+/(?:\"|')", html):
            eps.add(1)      # 第 1 集无后缀

    return sorted(eps)


def extract_total_count(html: str) -> int:
    """「共 N 集」——平台预告的总集数（含尚未更新的集）。"""
    m = re.search(r"共\s*(\d+)\s*集", html)
    return int(m.group(1)) if m else 0


def extract_updated_count(html: str) -> int:
    """「已更新 M 集」——当前真正可播放的集数。"""
    m = re.search(r"已更新\s*(\d+)\s*集", html)
    return int(m.group(1)) if m else 0


def _is_placeholder(url: str) -> bool:
    """该图地址是否是站点通用占位图（不是真实剧集封面）。

    漫剧站(huangguoac)的 og:image 指向 /static/web/images/home_og.jpg —— 全站
    同一张，拿它当封面会把所有剧的 poster 都写成同一张图。
    """
    if not url:
        return True
    u = url.lower()
    return ("/static/web/images/" in u
            or "cover-placeholder" in u
            or "placehold" in u)


def _extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)

    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        # "高三爱情故事 - 黄果短剧" -> 去掉站点后缀
        return re.sub(r"\s*-\s*黄果短剧\s*$", "", og["content"]).strip()

    if soup.title and soup.title.string:
        return re.sub(r"\s*-\s*.*$", "", soup.title.string).strip()

    return ""


def _extract_desc(soup: BeautifulSoup) -> str:
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return strip_html(tag["content"])
    return ""


def _extract_cover(soup: BeautifulSoup) -> str:
    """详情页封面。

    优先 og:image（短剧站为真实封面）；若它是站点占位图（漫剧站如此），
    回退到详情页海报图 .hg-web-detail__poster img。
    """
    og = soup.find("meta", property="og:image")
    cover = (og.get("content") if og else "") or ""
    if not _is_placeholder(cover):
        return cover

    poster = soup.select_one(".hg-web-detail__poster img")
    if poster is not None:
        src = (poster.get("data-src") or poster.get("src") or "").strip()
        if not _is_placeholder(src):
            return src

    # 兜底：页面里第一张非占位的剧集图
    for img in soup.select("img"):
        src = (img.get("data-src") or img.get("src") or "").strip()
        if src and not _is_placeholder(src) and "/static/" not in src:
            return src
    return cover


def _extract_tags(soup: BeautifulSoup) -> List[str]:
    """详情页标签。

    优先取详情区的标签容器——直接扫全局 a.hg-tag 会把「相关推荐」卡片的
    标签一并收进来（实测 40+ 个），污染 NFO。
    """
    tags: List[str] = []
    for a in soup.select(".hg-web-detail__tags a"):
        t = a.get_text(strip=True)
        if t and t not in tags:
            tags.append(t)
    if tags:
        return tags

    for a in soup.select("a.hg-tag"):
        t = a.get_text(strip=True)
        if t and t not in tags:
            tags.append(t)
    return tags


def parse_episode_titles(html: str) -> dict:
    """提取每集的标题（若站点提供），返回 {集号: 标题}。

    只收集**已更新**的集；「未更新」的按钮其 title 就是「未更新」，
    不是真正的分集标题，收录进去会误导用户。
    """
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    for a in soup.select("div.hg-web-detail__ep-grid a[data-ep-id]"):
        if _is_locked_ep(a):
            continue
        try:
            ep = int(a.get("data-ep-id"))
        except (TypeError, ValueError):
            continue
        title = (a.get("title") or a.get("data-ep-title") or "").strip()
        if title and title != "未更新":
            result[ep] = title
    return result
