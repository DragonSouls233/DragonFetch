# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""``unofficial-api-for-*`` 兜底适配器

用途
----
对上 yt-dlp **没有 extractor** 的站点（实测：``porntrex`` / ``xfreehd`` /
``thumbzilla``），以及 yt-dlp extractor 已损坏的站点（``tube8``），
用 EchterAlsFake 的 ``unofficial-api-for-<site>`` 包解析出**直链**，
再把直链交给本项目原有的下载引擎。

为什么用这个包而不是自己爬
--------------------------
13 个站点包是同一位作者用同一条 ``base_api`` 底座写的，**API 形状完全一致**
（``Client().get_video/get_model/get_channel`` + ``Video.direct_download_urls``），
所以这里只需要写**一个**适配器就能覆盖全部站点，站点差异全部由上游包承担。
自己为 3 个站点各写一套 DOM 解析，维护成本远高于引入这层依赖。

依赖与许可
----------
- 包名：``unofficial-api-for-<site>`` → import 名 ``<site>_api``
- 新增依赖：``eaf-base-api`` / ``selectolax`` / ``json5``（``m3u8``、``curl-cffi``
  本项目已有）
- 许可：GPL-3.0-or-later，与本项目兼容；本文件不复制其代码，只调用其公开 API
- **惰性导入**：没装的站点包不会导致 import 失败，只是该站点拿不到兜底能力

代理
----
这些包读的是 ``base_api.modules.config.config`` 这个**模块级单例**，
必须在创建 ``Client`` **之前**写入 ``proxy``，否则请求会直连（在境内必然失败）。
"""

import importlib
import inspect
import logging
from typing import Any, Dict, List, Optional

from .base import ResolvedVideo, SiteListItem
from .registry import SiteSpec

logger = logging.getLogger(__name__)

__all__ = ["is_available", "configure_runtime", "fetch_video",
           "list_videos", "search", "close_clients", "module_name_for"]

# 站点包 import 名 → 已导入模块对象缓存
_MODULE_CACHE: Dict[str, Any] = {}
# 每个站点包各自维护一个 Client（内含 asyncio.Lock 与 HTTP 会话）
_CLIENT_CACHE: Dict[str, Any] = {}
# 上次写入 base_api 全局单例的代理值，用于判断是否需要重设
_LAST_PROXY: Optional[str] = None


def module_name_for(spec: SiteSpec) -> Optional[str]:
    """返回站点对应的 import 模块名。"""
    return spec.eaf_module


def is_available(spec: SiteSpec) -> bool:
    """该站点的 ``unofficial-api-for-*`` 包是否已安装且可导入。"""
    name = module_name_for(spec)
    if not name:
        return False
    if name in _MODULE_CACHE:
        return True
    try:
        _MODULE_CACHE[name] = importlib.import_module(name)
        return True
    except Exception as exc:  # ImportError 及其依赖缺失都算不可用
        logger.debug("[sites:%s] 站点包 %s 不可用: %s", spec.key, name, exc)
        return False


def configure_runtime(proxy: Optional[str] = None,
                      timeout: int = 30,
                      impersonation: str = "chrome",
                      verify_ssl: bool = True) -> bool:
    """把代理等设置写进 ``base_api`` 的全局运行时配置。

    ⚠️ 必须幂等且必须在 ``Client()`` 之前调用：``base_api`` 是在
    ``initialize_session()`` 里读 ``config.proxy`` 的，一旦会话建好再改就晚了。

    Returns:
        是否成功写入（base_api 未安装时返回 ``False``，调用方可据此降级）。
    """
    global _LAST_PROXY
    try:
        cfg_mod = importlib.import_module("base_api.modules.config")
    except Exception as exc:
        logger.debug("[sites] base_api 不可用: %s", exc)
        return False

    cfg = cfg_mod.config
    cfg.proxy = proxy or None
    cfg.impersonation = impersonation or "chrome"
    cfg.verify_ssl = bool(verify_ssl)
    if timeout:
        cfg.timeout = int(timeout)
    _LAST_PROXY = cfg.proxy
    logger.debug("[sites] base_api 运行时已配置: proxy=%s impersonation=%s",
                 cfg.proxy, cfg.impersonation)
    return True


def _get_client(spec: SiteSpec, proxy: Optional[str], timeout: int):
    """获取（或创建）该站点的 Client。"""
    importlib.import_module  # 显式引用，便于静态分析
    if not is_available(spec):
        raise RuntimeError(f"站点包 {spec.eaf_module} 未安装，无法处理 {spec.key}")

    mod = _MODULE_CACHE[spec.eaf_module]
    if proxy != _LAST_PROXY:
        # 代理变了必须重建会话，否则还是走旧代理
        _CLIENT_CACHE.pop(spec.key, None)
        configure_runtime(proxy=proxy, timeout=timeout)

    if spec.key not in _CLIENT_CACHE:
        _CLIENT_CACHE[spec.key] = mod.Client()
        logger.info("[sites:%s] 已创建 unofficial-api Client（proxy=%s）",
                    spec.key, proxy or "无")
    return _CLIENT_CACHE[spec.key]


async def close_clients() -> None:
    """关闭所有缓存的 Client 会话（进程退出或切换代理时调用）。"""
    for key, client in list(_CLIENT_CACHE.items()):
        core = getattr(client, "core", None)
        if core is not None and hasattr(core, "close"):
            try:
                await core.close()
            except Exception as exc:
                logger.debug("[sites:%s] 关闭会话失败: %s", key, exc)
    _CLIENT_CACHE.clear()


# URL 路径片段 → 优先尝试的 Client 方法。
# 实测结论：13 个站点包的 Client **方法集并不一致**（同一个 base_api 底座，
# 但各站只实现自己页面类型对应的方法）。例如：
#   - xvideos 有 get_pornstar/get_channel/get_playlist，**没有** get_model
#   - xnxx 只有 get_user，连 get_channel 都没有
#   - beeg 只有 get_video，**完全不支持列表**
# 所以绝不能假设"有一个统一的 get_model"。
_PATH_GETTER_HINTS = (
    ("/pornstars/", "get_pornstar"),
    ("/pornstar/", "get_pornstar"),
    ("/models/", "get_model"),
    ("/model/", "get_model"),
    ("/channels/", "get_channel"),
    ("/channel/", "get_channel"),
    ("/creators/", "get_creator"),
    ("/creator/", "get_creator"),
    ("/users/", "get_user"),
    ("/user/", "get_user"),
    ("/profiles/", "get_user"),
    ("/profile/", "get_profile"),
    ("/collections/", "get_collection"),
    ("/collection/", "get_collection"),
    ("/playlists/", "get_playlist"),
    ("/playlist/", "get_playlist"),
    ("/albums/", "get_album"),
    ("/album/", "get_album"),
    ("/amateur", "get_amateur"),
)

# 路径识别不出来时的通用回退顺序（按实测支持率从高到低）
_FALLBACK_GETTERS = (
    "get_pornstar", "get_channel", "get_model", "get_creator", "get_user",
    "get_profile", "get_collection", "get_playlist", "get_album", "get_amateur",
)


def _candidate_getters(client: Any, url: str) -> List[str]:
    """给出该 URL 应该依次尝试的 Client 方法名。

    只做"路径提示 + 实有方法过滤"，**不碰网络**；
    真正的可用性判定放在 :func:`list_videos` 里，用
    "返回对象是否有可调用的 ``videos()``" 来定夺——这比静态推断可靠得多。
    """
    path = ""
    try:
        from urllib.parse import urlparse
        path = (urlparse(url).path or "").lower()
    except Exception:
        path = (url or "").lower()

    ordered: List[str] = []
    for fragment, method in _PATH_GETTER_HINTS:
        if fragment in path and method not in ordered:
            ordered.append(method)
    for method in _FALLBACK_GETTERS:
        if method not in ordered:
            ordered.append(method)

    return [m for m in ordered if callable(getattr(client, m, None))]


async def fetch_video(spec: SiteSpec, url: str, proxy: Optional[str] = None,
                      timeout: int = 30) -> ResolvedVideo:
    """解析单个视频页面，返回含直链的 :class:`ResolvedVideo`。

    Raises:
        RuntimeError: 站点包未安装。
        Exception: 页面请求/解析失败（由上游抛）。
    """
    client = _get_client(spec, proxy, timeout)
    video = await client.get_video(url)

    result = ResolvedVideo(
        page_url=url,
        site=spec.key,
        title=_clean(getattr(video, "title", None)),
        duration=_clean(getattr(video, "duration", None)),
        author=_clean(getattr(video, "author", None)),
        thumbnail=_clean(getattr(video, "thumbnail", None)),
        publish_date=_clean(getattr(video, "publish_date", None)),
        views=_clean(getattr(video, "views", None)),
        description=_clean(getattr(video, "description", None)),
        categories=_as_str_list(getattr(video, "categories", None)),
        tags=_as_str_list(getattr(video, "tags", None)),
        direct_urls=_as_str_list(getattr(video, "direct_download_urls", None)),
        qualities=_as_str_list(getattr(video, "video_qualities", None)),
        source="eaf",
    )
    logger.info("[sites:%s] 解析直链成功: %s（%d 个画质）",
                spec.key, result.title or url, len(result.direct_urls))
    return result


async def _maybe_await(value: Any) -> Any:
    """按返回值类型分派：可等待就 await，否则原样返回。

    ⚠️ 这是本适配器最容易踩的坑。站点包的 Client 方法**返回类型并不统一**
    （实测 xvideos，13 站同一底座但各站实现不同）：

    ==========================  ==================  ==========================
    方法                        类型                 正确用法
    ==========================  ==================  ==========================
    ``get_video/get_pornstar/`` 协程                 ``await fn(url)``
    ``get_channel``
    ``get_playlist/search``     普通函数，**直接     ``async for res in fn(url)``
                                返回 AsyncGenerator``   —— **不能 await**
    ==========================  ==================  ==========================

    此前这里无条件 ``await fn(url)``，遇到 ``get_playlist`` 这类（xvideos 的
    ``/profiles/`` 页面回退到它就是这条）会抛
    ``TypeError: 'async_generator' object can't be awaited``，
    表现为"列表抓取失败"，但错误信息完全指不到真正原因。
    """
    if inspect.isawaitable(value):
        return await value
    return value


def _call_getter(fn: Any, url: str, pages: int) -> Any:
    """调用列表 getter，容错 ``pages`` 参数。

    各站 ``pages`` 的默认值和是否接受该参数都不同（``get_playlist`` 有
    ``pages=2``，而 ``get_pornstar`` 只有 ``load_html``）。传不进去就退回
    单参数调用。注意 TypeError 在**调用时**即抛出（不是 await 时），
    所以这里 try 住是安全的。
    """
    try:
        return fn(url, pages=max(1, int(pages)))
    except TypeError:
        return fn(url)


async def list_videos(spec: SiteSpec, url: str, pages: int = 2,
                      proxy: Optional[str] = None, timeout: int = 30,
                      limit: int = 0) -> List[SiteListItem]:
    """展开列表页（模特/频道/演员/用户/合集），返回视频条目。

    由于各站点包的 Client 方法集不一致（见 :data:`_PATH_GETTER_HINTS` 的说明），
    这里按"逐个候选尝试"的方式取列表页对象：

    1. 按 URL 路径提示 + 实有方法，得到候选 getter 顺序；
    2. 依次调用，**能取到页面且该页面有可调用的 ``videos()``** 才算命中；
       getter 若直接返回 ``AsyncGenerator``（如 xvideos 的 ``get_playlist``），
       则把它本身当作结果流收集，不再要求 ``videos()``；
    3. 全都不行则抛最后一个错误（调用方 ``SiteService`` 会兜住并降级）。

    这样 beeg 这种"只有 get_video"的站点会自然地得到"不支持列表"，
    而不是抛一个让人误以为是网络问题的异常。
    """
    client = _get_client(spec, proxy, timeout)
    candidates = _candidate_getters(client, url)
    if not candidates:
        raise RuntimeError(f"{spec.key} 的站点包没有任何可用于列表的 getter")

    last_error: Optional[Exception] = None
    for getter in candidates:
        try:
            raw = _call_getter(getattr(client, getter), url, pages)
            raw = await _maybe_await(raw)
        except Exception as exc:
            last_error = exc
            logger.debug("[sites:%s] %s(%s) 失败: %s", spec.key, getter, url[:60], exc)
            continue

        # 情况 A：getter 直接产出 ScrapeResult 流（普通函数 + AsyncGenerator）
        if hasattr(raw, "__aiter__"):
            items = await _collect_items(spec, raw, "", limit)
            logger.info("[sites:%s] 列表展开命中 %s(%s) 的直出流，得到 %d 条",
                        spec.key, getter, url[:60], len(items))
            return items

        # 情况 B：getter 返回页面对象，需要再调它的 videos()
        page = raw
        videos_fn = getattr(page, "videos", None)
        if not callable(videos_fn):
            logger.debug("[sites:%s] %s 返回的 %s 不支持 videos()",
                         spec.key, getter, type(page).__name__)
            continue

        author = _clean(getattr(page, "name", None)) or ""
        try:
            stream = videos_fn(pages=max(1, int(pages)))
        except TypeError:
            # 少数站点的 videos() 不接受 pages 参数
            stream = videos_fn()
        stream = await _maybe_await(stream)
        if not hasattr(stream, "__aiter__"):
            logger.debug("[sites:%s] %s.videos() 未返回可异步迭代对象: %s",
                         spec.key, getter, type(stream).__name__)
            continue

        items = await _collect_items(spec, stream, author, limit)
        logger.info("[sites:%s] 列表展开命中 %s(%s)，得到 %d 条",
                    spec.key, getter, url[:60], len(items))
        return items

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{spec.key} 站点包无法展开该页: {url}")


async def _collect_items(spec: SiteSpec, stream: Any, author: str,
                         limit: int) -> List[SiteListItem]:
    """把 ``videos()`` / 直出流产出的 ``ScrapeResult`` 收集成条目列表。

    采用 ``try/finally`` 关闭异步生成器：命中 ``limit`` 会提前 ``break``，
    不关闭的话生成器会悬着（上游可能因此保留连接/会话资源）。
    """
    items: List[SiteListItem] = []
    try:
        async for res in stream:
            if not getattr(res, "succeeded", False):
                continue
            try:
                video = res.unwrap()
            except Exception:
                continue
            item_url = _clean(getattr(video, "url", None)) or ""
            if not item_url:
                continue
            items.append(SiteListItem(
                url=item_url,
                title=_clean(getattr(video, "title", None)) or "",
                site=spec.key,
                duration=_clean(getattr(video, "duration", None)) or "",
                thumbnail=_clean(getattr(video, "thumbnail", None)) or "",
                author=author or (_clean(getattr(video, "author", None)) or ""),
                source="eaf",
            ))
            if limit and len(items) >= limit:
                break
    finally:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except Exception as exc:  # pragma: no cover - 关闭失败不影响结果
                logger.debug("[sites:%s] 关闭结果流失败: %s", spec.key, exc)
    return items


async def search(spec: SiteSpec, query: str, pages: int = 1,
                 proxy: Optional[str] = None, timeout: int = 30,
                 limit: int = 0) -> List[SiteListItem]:
    """按关键词搜索，返回条目列表。

    各站点的搜索方法名不统一：``search``（xvideos/spankbang/redtube/tube8/
    porntrex/thumbzilla/xfreehd）与 ``search_videos``（pornhub/xnxx/eporner/
    xhamster/youporn）并存；也有站点（beeg）根本没有搜索。
    """
    client = _get_client(spec, proxy, timeout)
    fn = None
    for name in ("search", "search_videos"):
        if callable(getattr(client, name, None)):
            fn = getattr(client, name)
            break
    if fn is None:
        raise RuntimeError(f"{spec.key} 的站点包不支持搜索")

    try:
        raw = fn(query, pages=max(1, int(pages)))
    except TypeError:
        raw = fn(query)
    raw = await _maybe_await(raw)

    if not hasattr(raw, "__aiter__"):
        raise RuntimeError(
            f"{spec.key} 的 {fn.__name__}() 未返回可异步迭代对象"
            f"（实际为 {type(raw).__name__}），无法收集搜索结果"
        )

    items = await _collect_items(spec, raw, "", limit)
    logger.info("[sites:%s] 搜索 %r 得到 %d 条", spec.key, query, len(items))
    return items


def _clean(value: Any) -> Optional[str]:
    """统一清洗字符串字段：去掉首尾空白，空串归为 ``None``。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return None
    text = str(value).strip()
    return text or None


def _as_str_list(value: Any) -> List[str]:
    """把任意值规整成字符串列表（上游这些字段都是 ``list[str] | None``）。"""
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    try:
        return [str(v).strip() for v in value if str(v).strip()]
    except Exception:
        return []
