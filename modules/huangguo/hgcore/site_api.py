# -*- coding: utf-8 -*-
"""站点 API 门面（分发层）：按当前生效站点把调用转给对应后端。

上层（UI / 任务队列）只调用这里的函数，不直接接触具体解析器。
当前站点由 config.ACTIVE_SITE 决定（启动时由 site_discovery 自动识别/切换）。

关键约定:
  - 播放地址带时效，get_play_url 每次实时请求，不做跨调用缓存（现抓现用）。
  - 两个「黄果」站点结构不同，但对外暴露同一组函数，UI 无需感知差异，
    仅通过 config.ACTIVE_SITE 的 name/color 做「区分」展示。
  - **并发下载必须用 get_play_url_for(site_id, ...) / load_detail_for(site_id, ...)**，
    它们不读全局站点状态；而 get_play_url / load_detail 是"当前站点"版本，
    只适合 UI 线程的即时操作。混用会导致切站瞬间把 A 站 did 打到 B 站域名上。
"""

import importlib
from typing import Dict, List, Tuple

from . import config, sites
from .http_client import fetch_text

# 后端模块缓存：site_id -> module
_BACKEND_CACHE = {}


def _backend():
    """取得当前生效站点对应的后端模块。"""
    name = config.ACTIVE_SITE.get("backend")
    return _backend_by_name(name)


def _backend_by_name(name: str):
    if not name:
        name = "backend_huangguoai"
    if name in _BACKEND_CACHE:
        return _BACKEND_CACHE[name]
    mod = importlib.import_module(f".{name}", __name__.rsplit(".", 1)[0])
    _BACKEND_CACHE[name] = mod
    return mod


def _backend_for(site_id: str):
    """按站点 ID 取后端模块（并发场景用，不依赖全局 ACTIVE_SITE）。"""
    site = sites.get_site(site_id) or config.ACTIVE_SITE
    return _backend_by_name(site.get("backend"))


# ---------------------------------------------------------------- 列表

def list_dramas() -> List:
    return _backend().list_dramas()


def list_category(url: str) -> List:
    return _backend().list_category(url)


def list_category_all(url: str, max_pages: int = 0, on_page=None) -> List:
    """翻页拉取分类页全部剧集（去重）。后端不支持时退回单页。"""
    mod = _backend()
    fn = getattr(mod, "list_category_all", None)
    if fn is None:
        return mod.list_category(url)
    return fn(url, max_pages=max_pages, on_page=on_page)


def list_category_meta(url: str) -> Tuple[int, int]:
    """分类页 (总数, 总页数)；后端不支持时返回 (0, 0)。"""
    mod = _backend()
    fn = getattr(mod, "list_category_meta", None)
    if fn is None:
        return 0, 0
    try:
        return fn(url)
    except Exception:                    # noqa: BLE001
        return 0, 0


def list_ranks() -> List:
    return _backend().list_ranks()


def list_topics() -> list:
    return _backend().list_topics()


def list_topic_dramas(slug: str) -> List:
    return _backend().list_topic_dramas(slug)


def search_dramas(keyword: str) -> List:
    return _backend().search_dramas(keyword)


# ---------------------------------------------------------------- 详情 / 播放

def load_detail(did) -> dict:
    return _backend().load_detail(did)


def get_play_url(did, ep: int) -> str:
    return _backend().get_play_url(did, ep)


def site_base_url(site_id: str) -> str:
    """某站点的根地址（并发场景按站点拼 URL 用）。"""
    site = sites.get_site(site_id) or config.ACTIVE_SITE
    return (site.get("base_url") or config.BASE_URL).rstrip("/")


def get_play_url_for(site_id: str, did, ep: int) -> str:
    """按**指定站点**实时获取某一集的 m3u8 地址（并发安全）。

    与 get_play_url 的关键区别：**不读全局 ACTIVE_SITE**。因此即使用户在
    队列运行中切换了站点，在途任务仍然请求自己所属站点的域名——旧版这里读
    全局站点，切站后剩余任务会把 A 站 did 拼到 B 站域名上，表现为整片 404。
    """
    site = sites.get_site(site_id) or config.ACTIVE_SITE
    mod = _backend_by_name(site.get("backend"))
    base = (site.get("base_url") or config.BASE_URL).rstrip("/")
    fn = getattr(mod, "get_play_url_at", None)
    if fn is None:                  # 后端未实现按站点取址：退回全局（尽力而为）
        return mod.get_play_url(did, ep)
    return fn(base, did, ep)


# ---------------------------------------------------------------- 指定站点（并发安全）

def detail_url_for(site_id: str, did) -> str:
    """按站点构造详情页 URL（不依赖全局 BASE_URL）。"""
    site = sites.get_site(site_id) or config.ACTIVE_SITE
    return site["base_url"].rstrip("/") + f"/detail/{did}/"


def load_detail_for(site_id: str, did, timeout: int = 20) -> dict:
    """按站点 ID 抓取并解析详情（并发安全：不改动全局 config）。

    两个站点的详情页路径同为 /detail/{did}/，差异只在解析器，故按站点选后端。
    """
    html = fetch_text(detail_url_for(site_id, did))
    return _backend_for(site_id).parse_detail_html(html)


# ---------------------------------------------------------------- TAB 映射

def tab_info(idx: int) -> Tuple[str, str, str]:
    """返回某 TAB 的 (kind, url, 名称)。

    kind:
      - "ranks"   排行榜（UI 用 LoadRanksWorker）
      - "topics"  专题（UI 用 LoadTopicsWorker）
      - "category"/"list"  普通列表（UI 用 LoadCategoryWorker(url)）

    判定改为**按分类 URL 的语义**（含 /ranks/、/topics/），不再按站点 id +
    下标硬编码：站点分类顺序调整、或新增站点（如黄果漫剧）时这里无需再改。
    """
    site = config.ACTIVE_SITE
    cats = config.CATEGORIES

    if idx < 0 or idx >= len(cats):
        return ("list", config.URL_HOME, "首页")

    name, url = cats[idx]

    if "/ranks/" in url:
        return ("ranks", url, name)
    if "/topics/" in url or url.rstrip("/").endswith("/topics"):
        return ("topics", url, name)

    # huangguome：所有 TAB 均为列表页（无独立排行/专题页）
    if site["id"] == "huangguome":
        return ("list", url, name)
    return ("category", url, name)
