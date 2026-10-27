# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""多站点服务门面

对上层只暴露三件事：

1. :meth:`SiteService.engine_url` —— **给下载引擎用的 URL**。
   这是接入下载链路唯一的入口，且对 PornHub 是恒等映射（传入什么返回什么）。
2. :meth:`SiteService.resolve` —— 解析单个视频页面的元数据 + 直链。
3. :meth:`SiteService.list_videos` —— 展开列表页。

为什么要有"恒等映射"这一条
--------------------------
因为本模块的最高优先级约束是：**PornHub 原有链路必须逐字节不变**。
所以 ``engine_url()`` 对 PornHub（以及一切 yt-dlp 能直接处理的站点）永远原样
返回入参，绝不插入任何解析动作。真正会被改写的只有"yt-dlp 没有 extractor"
的那三个站点。
"""

import logging
import threading
from typing import Dict, List, Optional

from .base import ResolvedVideo, SiteListItem
from .registry import SiteSpec, detect_site, looks_like_direct_media

logger = logging.getLogger(__name__)

__all__ = ["SiteService", "get_site_service", "reset_site_service"]


class SiteService:
    """多站点解析服务。

    Args:
        proxy: 代理地址（``socks5://127.0.0.1:10808``）。下载链路会传入当前正在用的
            代理，保证解析与下载走同一条出口。
        timeout: 解析请求超时（秒）。
        enabled: 总开关。``False`` 时 ``engine_url()`` 恒等返回，
            等价于"本模块不存在"。
        api_fallback: 是否允许用 ``unofficial-api-for-*`` 解析直链。
            关掉后，yt-dlp 无 extractor 的站点将无法解析出直链。
        cache_size: 直链解析结果的内存缓存条数上限（重试时避免重复解析）。
    """

    def __init__(self, proxy: Optional[str] = None, timeout: int = 30,
                 enabled: bool = True, api_fallback: bool = True,
                 cache_size: int = 256) -> None:
        self.proxy = proxy
        self.timeout = int(timeout)
        self.enabled = bool(enabled)
        self.api_fallback = bool(api_fallback)
        self._cache_size = max(0, int(cache_size))
        self._cache: Dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 站点识别
    # ------------------------------------------------------------------ #
    def detect(self, url: str) -> Optional[SiteSpec]:
        """识别 URL 所属站点；未收录返回 ``None``。"""
        return detect_site(url)

    def needs_resolution(self, url: str) -> bool:
        """该 URL 是否需要"先解析出直链再交给下载引擎"。

        PornHub（``native``）与 yt-dlp 可直接处理的站点一律返回 ``False``，
        即**不做任何介入**。

        另外：URL **本身已经是直链**时也返回 ``False``。
        否则会把 144MB 的 mp4 当页面送去站点包解析 —— 上游拿它当 HTML load，
        超时 30s × 4 次重试全失败（白等约 2 分钟）才回退。
        （2026-09-20 端到端测试实测到这个浪费，见 ``looks_like_direct_media``。）
        """
        if not self.enabled or not self.api_fallback:
            return False
        if looks_like_direct_media(url):
            logger.debug("[sites] 已是直链，跳过解析: %s", url[:80])
            return False
        spec = detect_site(url)
        if spec is None:
            return False
        return spec.needs_eaf_resolution

    # ------------------------------------------------------------------ #
    # 下载链路入口（唯一的介入点）
    # ------------------------------------------------------------------ #
    async def engine_url(self, url: str, proxy: Optional[str] = None) -> str:
        """返回交给下载引擎的 URL。

        对绝大多数 URL 是**恒等映射**；只有需要兜底解析的站点才会被换成直链。
        解析失败时**原样返回入参**，让原有链路自己去试——绝不因为本模块的
        失败而中断下载。
        """
        if not self.needs_resolution(url):
            return url

        spec = detect_site(url)
        if spec is None:  # pragma: no cover - needs_resolution 已保证非 None
            return url

        cached = self._cache_get(url)
        if cached:
            logger.info("[sites:%s] 命中直链缓存: %s", spec.key, url[:80])
            return cached

        try:
            resolved = await self.resolve(url, proxy=proxy)
        except Exception as exc:
            logger.warning("[sites:%s] 直链解析失败，回退原 URL: %s", spec.key, exc)
            return url

        target = resolved.best_direct_url if resolved else None
        if not target:
            logger.warning("[sites:%s] 未解析出直链，回退原 URL: %s", spec.key, url[:80])
            return url

        self._cache_put(url, target)
        logger.info("[sites:%s] 直链解析成功，改用直链下载: %s", spec.key, url[:80])
        return target

    # ------------------------------------------------------------------ #
    # 解析
    # ------------------------------------------------------------------ #
    async def resolve(self, url: str, proxy: Optional[str] = None
                      ) -> Optional[ResolvedVideo]:
        """解析单个视频页面。

        Returns:
            :class:`ResolvedVideo`；站点未收录或未启用时返回 ``None``。
        """
        if not self.enabled:
            return None
        if looks_like_direct_media(url):
            # 已经是直链，没有什么"页面"可解析。
            logger.debug("[sites] 已是直链，无需解析: %s", url[:80])
            return None
        spec = detect_site(url)
        if spec is None:
            return None
        if spec.native or spec.ytdlp:
            # 能由 yt-dlp 直接处理的站点不做解析：多一次请求没有收益，
            # 反而多一个出错点。
            return None

        from . import eaf_adapter

        use_proxy = proxy if proxy is not None else self.proxy
        return await eaf_adapter.fetch_video(spec, url, proxy=use_proxy,
                                             timeout=self.timeout)

    async def list_videos(self, url: str, pages: int = 2, limit: int = 0,
                          proxy: Optional[str] = None) -> List[SiteListItem]:
        """展开列表页。

        策略：
        - 站点有 yt-dlp extractor → 用 yt-dlp 的扁平模式（一次请求）；
        - 否则 → 用 ``unofficial-api-for-*`` 的分页展开；
        - yt-dlp 为空且站点有站点包 → 自动再试站点包（tube8 这类
          extractor 已损坏的情况靠这条兜住）。

        Args:
            url: 列表页 URL（模特/频道/搜索页）。
            pages: 展开页数。
            limit: 最多返回条数，``<=0`` 不限。
            proxy: 代理地址，缺省用服务实例上的。

        Returns:
            条目列表；站点未收录或无能力时返回空列表。
        """
        if not self.enabled:
            return []
        spec = detect_site(url)
        if spec is None:
            return []
        if spec.native:
            # PornHub 的列表解析归原有的刮削链路，本模块不接手。
            logger.info("[sites:pornhub] 列表解析仍由原刮削链路负责，跳过")
            return []

        use_proxy = proxy if proxy is not None else self.proxy
        errors: List[str] = []

        if spec.ytdlp:
            try:
                from . import ytdlp_adapter
                items = ytdlp_adapter.list_videos(spec, url, proxy=use_proxy, limit=limit)
                if items:
                    return items
                logger.info("[sites:%s] yt-dlp 返回 0 条，尝试站点包兜底", spec.key)
            except Exception as exc:
                errors.append(f"yt-dlp: {exc}")
                logger.warning("[sites:%s] yt-dlp 展开失败: %s", spec.key, exc)

        if not self.api_fallback:
            if errors:
                logger.warning("[sites:%s] 列表抓取失败: %s", spec.key, "; ".join(errors))
            return []

        try:
            from . import eaf_adapter
            if not eaf_adapter.is_available(spec):
                if errors:
                    logger.warning("[sites:%s] 列表抓取失败且无站点包兜底: %s",
                                   spec.key, "; ".join(errors))
                return []
            return await eaf_adapter.list_videos(spec, url, pages=pages,
                                                 proxy=use_proxy,
                                                 timeout=self.timeout, limit=limit)
        except Exception as exc:
            errors.append(f"{spec.eaf_module}: {exc}")
            logger.warning("[sites:%s] 列表抓取失败: %s", spec.key, "; ".join(errors))
            return []

    async def search(self, url_or_site: str, query: str, pages: int = 1,
                     limit: int = 0, proxy: Optional[str] = None
                     ) -> List[SiteListItem]:
        """按关键词搜索。

        Args:
            url_or_site: 站点 key（如 ``"xvideos"``）或该站的任意 URL
                （用域名推断站点）。
            query: 关键词。
            pages: 搜索页数。
            limit: 最多返回条数。
            proxy: 代理地址，缺省用服务实例上的。

        Returns:
            条目列表。站点未收录 / 不支持搜索 / 出错时返回空列表（不抛异常）。
        """
        if not self.enabled or not self.api_fallback:
            return []

        from .registry import get_site as _get_site

        spec = _get_site(url_or_site) or detect_site(url_or_site)
        if spec is None:
            logger.warning("[sites] 搜索失败：无法识别站点 %r", url_or_site)
            return []
        if spec.native:
            logger.info("[sites:pornhub] 搜索仍由原刮削链路负责，跳过")
            return []

        use_proxy = proxy if proxy is not None else self.proxy
        try:
            from . import eaf_adapter
            if not eaf_adapter.is_available(spec):
                logger.info("[sites:%s] 站点包未安装，无法搜索", spec.key)
                return []
            return await eaf_adapter.search(spec, query, pages=pages,
                                            proxy=use_proxy,
                                            timeout=self.timeout, limit=limit)
        except Exception as exc:
            logger.warning("[sites:%s] 搜索失败: %s", spec.key, exc)
            return []

    # ------------------------------------------------------------------ #
    # 直链缓存
    # ------------------------------------------------------------------ #
    def _cache_get(self, url: str) -> Optional[str]:
        with self._lock:
            return self._cache.get(url)

    def _cache_put(self, url: str, target: str) -> None:
        if self._cache_size <= 0:
            return
        with self._lock:
            if len(self._cache) >= self._cache_size:
                # 简单 FIFO：直链带签名会过期，缓存只是为了一次下载内的重试，
                # 不需要 LRU 精细度。
                for key in list(self._cache.keys())[: max(1, self._cache_size // 4)]:
                    self._cache.pop(key, None)
            self._cache[url] = target

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


# ---------------------------------------------------------------------- #
# 进程级单例
# ---------------------------------------------------------------------- #
_SERVICE: Optional[SiteService] = None
_SERVICE_LOCK = threading.Lock()


def load_sites_config() -> Dict[str, object]:
    """从项目根 ``config.yaml`` 的 ``sites:`` 段读取配置。

    刻意写成"读不到就用默认值、绝不抛异常"：多站点是新功能，
    不能因为配置文件缺失/写错就把整个下载链路拖挂。

    支持的键：
        enabled(bool)      总开关，默认 True
        api_fallback(bool) 是否用 unofficial-api 兜底，默认 True
        timeout(int)       解析超时秒数，默认 30
        list_pages(int)    列表抓取默认页数，默认 2
    """
    defaults: Dict[str, object] = {
        "enabled": True,
        "api_fallback": True,
        "timeout": 30,
        "list_pages": 2,
    }
    try:
        import os
        import yaml

        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(here, "..", "..", "config.yaml"),  # 项目根（源码运行）
            os.path.join(os.getcwd(), "config.yaml"),       # 打包后 exe 同目录
        ]
        for path in candidates:
            path = os.path.normpath(path)
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            section = data.get("sites")
            if isinstance(section, dict):
                merged = dict(defaults)
                for key in defaults:
                    if key in section and section[key] is not None:
                        merged[key] = section[key]
                return merged
    except Exception as exc:  # pragma: no cover - 配置问题不致命
        logger.debug("[sites] 读取 sites 配置失败，使用默认值: %s", exc)
    return defaults


def get_site_service(proxy: Optional[str] = None, **kwargs) -> SiteService:
    """取进程级单例。首次调用时用传入参数创建；之后参数被忽略（除代理）。

    代理每次都会跟随传入值更新——下载过程中节点是会切换的，解析必须跟着走。
    未显式传参时，开关等取自 ``config.yaml`` 的 ``sites:`` 段。
    """
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            cfg = load_sites_config()
            params: Dict[str, object] = {
                "enabled": bool(cfg.get("enabled", True)),
                "api_fallback": bool(cfg.get("api_fallback", True)),
                "timeout": int(cfg.get("timeout", 30) or 30),
            }
            params.update(kwargs)
            _SERVICE = SiteService(proxy=proxy, **params)
        elif proxy:
            _SERVICE.proxy = proxy
        return _SERVICE


def reset_site_service() -> None:
    """重建单例（测试用）。"""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None
