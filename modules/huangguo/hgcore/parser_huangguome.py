# -*- coding: utf-8 -*-
"""huangguo.me 站点解析：首页卡片、详情页分集、播放地址（chapter API）。

与 huangguoai.com 的 hg-drama-card 结构不同，huangguo.me 使用：
  - 首页卡片:  <a class="poster-card" href="/detail/{slug}/">
  - 封面:      <img class="poster-cover-img lazy" data-src="...">
  - 标题:      <div class="poster-card-title">
  - 集数角标:  <div class="episode-badge">N集</div>
  - 分集:      <a class="detail-episode" data-ep="N" data-chapter-id="ID"
                  data-coin-dialog-open  aria-label="第N集，金币解锁" href="/play/{slug}/{ID}/">
  - 播放地址:  /api/playlet/chapter/detail?chapterId={id} -> data.videoUrl
                相对路径按 play.js 规则补 /h5/m3u8/ 前缀；
                空串 + needUnlock=true 表示需金币/会员解锁（付费内容不绕过）。

注意：huangguo.me 为金币/会员付费站，通常仅前 1-2 集免费，其余需解锁。
本模块只解析结构，不对付费内容做任何绕过。
"""

import json
import re
from typing import Dict, List, Set, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import config
from .utils import strip_html

# 真实列表容器（避免首页多区块推荐位重复计入）
_GRID_SELECTOR = "div.card-grid"


def parse_list_meta(html: str) -> Tuple[int, int]:
    """读取列表页的 (总页数, 当前页)；缺失返回 (0, 0)。

    huangguome 用 <nav class="pagination" data-page="1" data-total="2">。
    """
    soup = BeautifulSoup(html, "html.parser")
    nav = soup.select_one("nav.pagination[data-total], [data-pagination][data-total]")
    if not nav:
        return 0, 0
    try:
        pages = int(nav.get("data-total") or 0)
    except ValueError:
        pages = 0
    try:
        cur = int(nav.get("data-page") or 1)
    except ValueError:
        cur = 1
    return pages, cur


def parse_page_links(html: str) -> Dict[int, str]:
    """从分页控件里提取 {页码: 相对URL}。

    比猜 URL 模板可靠：不同频道的分页路径不一致，例如
      /category/rebang/page/2/
      /list/ai-short-play/latest/page/2/   （多一个 latest 段）
    页面通常只显式给出首尾页链接（中间以 … 省略），因此只取能拿到的。
    """
    soup = BeautifulSoup(html, "html.parser")
    links: Dict[int, str] = {}
    for a in soup.select("a.pagination-btn[href], .pagination a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        m = re.search(r"/page/(\d+)/?", href)
        if not m:
            continue
        links[int(m.group(1))] = href
    return links


def pager_template(html: str) -> str:
    """从页面真实分页链接推导「第 N 页」URL 模板（含 {page} 占位）。

    例：/list/ai-short-play/latest/page/2/  ->  /list/ai-short-play/latest/page/{page}/
    推不出来时返回空串（调用方回退到 page_url 猜测）。
    """
    links = parse_page_links(html)
    if not links:
        return ""
    # 取任意一个已知页码的真实链接，把页码替换为占位符
    for num, href in links.items():
        tpl = re.sub(r"/page/\d+/?", "/page/{page}/", href, count=1)
        if "{page}" in tpl:
            return tpl
    return ""


def list_page_url(tpl: str, base_url: str, page: int) -> str:
    """用模板拼第 N 页绝对地址；tpl 为空则返回空串。"""
    if not tpl:
        return ""
    path = tpl.replace("{page}", str(page))
    return urljoin(base_url.rstrip("/") + "/", path)


def page_url(url: str, page: int) -> str:
    """按 huangguome 分页规则生成第 N 页 URL（猜模板，作为兜底）。

    规则（来自 pager 链接 /category/rebang/page/2/）:
        第 1 页 -> 原地址（/category/rebang/）
        第 N 页 -> /category/rebang/page/{N}/

    注意：部分频道（如 /list/ai-short-play/）实际路径含额外段
    （/list/ai-short-play/latest/page/2/）。优先用 parse_page_links() 读真实链接，
    本函数仅在前者不可用时兜底。
    """
    if page <= 1:
        return url
    u = url.rstrip("/")
    return f"{u}/page/{page}/"


# ---------------------------------------------------------------- 首页 / 列表

def parse_home(html: str) -> List["object"]:
    """解析首页/列表页的 poster-card，返回 Drama 列表。

    限定到 div.card-grid 内的卡片；首页无该容器时退回全文解析。
    """
    from .models import Drama

    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one(_GRID_SELECTOR)
    scope = container if container is not None else soup

    seen: Dict[str, bool] = {}
    order: List = []

    for card in scope.select("a.poster-card"):
        href = card.get("href", "")
        m = re.search(r"/detail/([A-Za-z0-9_-]+)/?", href)
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        seen[slug] = True

        img = card.select_one("img.poster-cover-img")
        cover = ""
        if img:
            cover = img.get("data-src") or img.get("src") or ""

        title_el = card.select_one(".poster-card-title")
        title = title_el.get_text(strip=True) if title_el else ""

        sub_el = card.select_one(".poster-card-subtitle")
        desc = strip_html(sub_el.get_text()) if sub_el else ""

        # 集数角标：<div class="episode-badge"><div ...>1集</div></div>
        badge = card.select_one(".episode-badge")
        episode_text = strip_html(badge.get_text()) if badge else ""

        order.append(Drama(
            did=slug,
            title=title or f"未命名{slug}",
            cover=cover,
            episode_text=episode_text,
            desc=desc,
        ))

    return order


# 兼容别名
def parse_search(html: str) -> List["object"]:
    return parse_home(html)


# ---------------------------------------------------------------- 详情

def parse_detail(html: str) -> dict:
    """解析详情页，返回结构化字段 + 分集到 chapterId 的映射 + 锁定集信息。

    返回:
        {
          "title", "desc", "cover",
          "episodes": [集号 int, ...],          # 升序（1-based，含锁定集）
          "episode_count": int,
          "tags": [str, ...],
          "chapters": {集号 int: chapterId int},  # 仅含已知 chapterId 的集
          "locked_eps": [集号 int, ...],          # 需金币/会员解锁的集
          "free_eps": [集号 int, ...],            # 可免费播放的集
        }
    """
    soup = BeautifulSoup(html, "html.parser")

    title = _extract_title(soup)
    desc = _extract_desc(soup)
    cover = _extract_cover(soup)
    tags = _extract_tags(soup)

    episodes, chapters, locked = _extract_episodes(html)

    free_eps = [ep for ep in episodes if ep not in locked]
    return {
        "title": title,
        "desc": desc,
        "cover": cover,
        "episodes": episodes,
        "episode_count": max(episodes) if episodes else 0,
        "tags": tags,
        "chapters": chapters,
        "locked_eps": sorted(locked),
        "free_eps": free_eps,
    }


def _extract_episodes(html: str) -> Tuple[List[int], Dict[int, int], Set[int]]:
    """提取 (全部集号, {集号: chapterId}, 锁定集集合)。

    以 .detail-episode 的 data-ep 为权威集号（HTML 顺序可能乱序，不可依赖）。
    chapterId 取自 data-chapter-id，缺失时回退到 href=/play/{slug}/{id}/。
    """
    soup = BeautifulSoup(html, "html.parser")
    chapters: Dict[int, int] = {}
    locked: Set[int] = set()
    eps: Set[int] = set()

    nodes = soup.select(".detail-episode")
    for el in nodes:
        ep = _ep_num_of(el)
        if not ep:
            continue
        eps.add(ep)

        cid = el.get("data-chapter-id") or el.get("data-id")
        if not cid:
            href = el.get("href", "")
            m = re.search(r"/play/[A-Za-z0-9_-]+/(\d+)/?", href)
            if m:
                cid = m.group(1)
        if cid and str(cid).isdigit():
            chapters[ep] = int(cid)

        # 锁定判定：data-coin-dialog-open 存在 或 aria-label 含「解锁/金币」
        if el.has_attr("data-coin-dialog-open"):
            locked.add(ep)
        else:
            label = el.get("aria-label") or ""
            if ("解锁" in label) or ("金币" in label):
                locked.add(ep)

    # 兜底：页面无 .detail-episode 时，按 /play/{slug}/{id}/ 出现顺序编号
    if not eps:
        slug_m = re.search(r"/detail/([A-Za-z0-9_-]+)/?", html)
        slug = slug_m.group(1) if slug_m else r"[A-Za-z0-9_-]+"
        idx = 0
        seen_cids: Set[int] = set()
        for mm in re.finditer(r"/play/%s/(\d+)/" % re.escape(slug), html):
            cid = int(mm.group(1))
            if cid in seen_cids:
                continue
            seen_cids.add(cid)
            idx += 1
            eps.add(idx)
            chapters[idx] = cid

    return sorted(eps), chapters, locked


def _ep_num_of(el) -> int:
    """取分集元素的集号：优先 data-ep，其次从文本「第N集」提取。"""
    ep = el.get("data-ep")
    if ep and str(ep).isdigit():
        return int(ep)
    m = re.search(r"(\d+)", el.get_text() or "")
    return int(m.group(1)) if m else 0


def _extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return re.sub(r"\s*[-–]\s*黄果.*$", "", og["content"]).strip()
    if soup.title and soup.title.string:
        return re.sub(r"\s*[-–]\s*.*$", "", soup.title.string).strip()
    return ""


def _extract_desc(soup: BeautifulSoup) -> str:
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return strip_html(tag["content"])
    return ""


def _extract_cover(soup: BeautifulSoup) -> str:
    og = soup.find("meta", property="og:image")
    return og["content"] if og and og.get("content") else ""


def _extract_tags(soup: BeautifulSoup) -> List[str]:
    tags: List[str] = []
    for a in soup.select("a.tag, .poster-card-tags a, .detail-tags a"):
        t = a.get_text(strip=True)
        if t and t not in tags:
            tags.append(t)
    return tags


# ---------------------------------------------------------------- 播放地址

def absolutize_m3u8_at(base_url: str, video_url: str) -> str:
    """按**指定站点根**把 chapter API 的 videoUrl 还原为绝对地址。

    规则与站点 play.js 的 resolvePlayUrl 一致：
      - http(s):// 开头：原样返回
      - 相对路径：去掉前导斜杠；若已以 h5/m3u8/ 开头则补 '/'，
        否则补 '/h5/m3u8/'（同域代理，携带 ?ticket= 等参数）
    """
    raw = (video_url or "").strip()
    if not raw:
        return ""
    if re.match(r"^https?://", raw, re.I):
        return raw
    base = (base_url or config.BASE_URL).rstrip("/")
    path = raw.lstrip("/")
    if path.startswith("h5/m3u8/"):
        return urljoin(base + "/", path)
    return urljoin(base + "/", "h5/m3u8/" + path)


def absolutize_m3u8(video_url: str) -> str:
    """当前站点版本（保留旧签名）。"""
    return absolutize_m3u8_at(config.BASE_URL, video_url)


def resolve_play_url_at(base_url: str, chapter_id: int) -> str:
    """按指定站点根通过 chapter API 获取某集的 m3u8。

    返回空串表示：地址为空（通常是金币/会员付费内容，无法免费获取）。
    """
    from .http_client import fetch_text

    base = (base_url or config.BASE_URL).rstrip("/")
    api = f"{base}/api/playlet/chapter/detail?chapterId={chapter_id}"
    try:
        raw = fetch_text(api, referer=base + "/", timeout=config.TIMEOUT)
    except Exception:                          # noqa: BLE001
        return ""

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""

    if not isinstance(data, dict) or data.get("code") != 200:
        return ""
    chapter = data.get("data") or {}
    video_url = (chapter.get("videoUrl") or "").strip()
    if not video_url:
        # 空地址 = 需解锁（金币/会员）。不在代码层绕过付费。
        return ""
    return absolutize_m3u8_at(base, video_url)


def resolve_play_url(chapter_id: int) -> str:
    """当前站点版本（保留旧签名）。"""
    return resolve_play_url_at(config.BASE_URL, chapter_id)
