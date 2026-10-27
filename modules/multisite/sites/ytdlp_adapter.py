# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""通用 yt-dlp 站点适配器

职责只有一件事：**用 yt-dlp 的"只取列表"模式把一个列表页展开成视频条目**。

下载本身不在这里做——本项目已有的 ``DownloadManager`` 已经有一套调好参数、
带代理轮换与重试的 yt-dlp 链路，这里再实现一套只会造成"两套参数不一致"
（这正是本项目此前踩过的坑）。所以本模块只做列表展开。

关于 yt-dlp 调用方式
--------------------
这里用 **Python 库 API** 而非子进程：
列表展开只需要一次 JSON 请求，没有"卡死需要强杀"的问题（那是下载才有的需求），
用库 API 可以免去子进程管理与输出解析。

参数只设必要的几个，且与 ``download_manager`` 的取值保持一致
（``impersonate='chrome'`` / ``socket_timeout=30`` / ``extractor_retries``），
避免出现"列表能取、下载取不到"的诡异组合。
"""

import logging
from typing import Any, Dict, List, Optional

from ..core.net.ytdlp_opts import apply_impersonate
from .base import SiteListItem
from .registry import SiteSpec

logger = logging.getLogger(__name__)

__all__ = ["is_available", "list_videos", "probe_extract_info"]

# yt-dlp 未安装时也不应让整个包导入失败
try:  # pragma: no cover - 环境相关
    import yt_dlp  # noqa: F401
    _YTDLP_AVAILABLE = True
except Exception:  # pragma: no cover
    _YTDLP_AVAILABLE = False


def is_available() -> bool:
    """yt-dlp 库是否可用。"""
    return _YTDLP_AVAILABLE


def _build_opts(proxy: Optional[str], flat: bool = True) -> Dict[str, Any]:
    """构造只取列表用的 yt-dlp 参数。

    刻意**不**带 ``ignoreerrors=True``：那会让真正的失败也返回成功，
    列表抓取时"抓到 0 条"到底是"没有视频"还是"被墙了"就分不清了。
    这里的调用方需要靠异常来区分这两种情况。
    """
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,      # 列表页本身就是 playlist，必须允许
        "extractor_retries": 3,
        "socket_timeout": 30,
        "retries": 3,
        "nocheckcertificate": True,
    }
    # 与本项目下载链路保持一致，避免"列表能取、下载取不到"。
    # ⚠️ 必须走 apply_impersonate：库模式下 YoutubeDL.__init__ 要求
    # impersonate 是 ImpersonateTarget 对象，传 'chrome' 字符串会命中
    # `assert isinstance(target, ImpersonateTarget)` 抛空消息 AssertionError。
    # 详见 modules/core/net/ytdlp_opts.py。
    apply_impersonate(opts, "chrome")
    if flat:
        # in_playlist：只在 playlist 场景展开成扁条目，不逐个解析视频详情，
        # 避免为一个 200 条的列表页发 200 次请求。
        opts["extract_flat"] = "in_playlist"
    if proxy:
        opts["proxy"] = proxy
    return opts


def probe_extract_info(url: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    """调用 ``extract_info`` 并返回原始信息字典（不做条目转换）。

    单独抽出来是为了让验证脚本能在"不臆造字段"的前提下检查真实返回结构。
    """
    if not _YTDLP_AVAILABLE:
        raise RuntimeError("yt-dlp 未安装，无法展开列表")
    with yt_dlp.YoutubeDL(_build_opts(proxy)) as ydl:
        return ydl.extract_info(url, download=False) or {}


def list_videos(
    spec: SiteSpec,
    url: str,
    proxy: Optional[str] = None,
    limit: int = 0,
) -> List[SiteListItem]:
    """展开列表页，返回视频条目。

    Args:
        spec: 站点描述（用于填 ``site`` 字段）。
        url: 列表页 URL。
        proxy: 代理地址，例如 ``socks5://127.0.0.1:10808``。
        limit: 最多返回多少条；``<=0`` 表示不限。

    Returns:
        条目列表。页面确实没有视频时返回空列表；网络/解析失败则抛异常
        （调用方需要能区分"空"与"失败"）。

    Raises:
        RuntimeError: yt-dlp 不可用。
        Exception: yt-dlp 抛出的任何提取失败。
    """
    info = probe_extract_info(url, proxy=proxy)
    entries = info.get("entries")
    if entries is None:
        # 单视频页面：extract_flat 下也可能没有 entries，退化成一条
        single = _entry_to_item(spec, info, fallback_url=url)
        return [single] if single and single.url else []

    items: List[SiteListItem] = []
    for entry in entries:
        if not entry:
            continue
        item = _entry_to_item(spec, entry)
        if item and item.url:
            items.append(item)
            if limit and len(items) >= limit:
                break
    logger.info("[sites:%s] yt-dlp 展开列表得到 %d 条: %s", spec.key, len(items), url[:80])
    return items


def _entry_to_item(
    spec: SiteSpec,
    entry: Dict[str, Any],
    fallback_url: str = "",
) -> Optional[SiteListItem]:
    """把 yt-dlp 的一个 entry 转成 :class:`SiteListItem`。

    扁平模式下 yt-dlp 的字段命名在不同 extractor 间有差异，这里对每个字段都做
    多候选名兜底，缺失即留空，**绝不臆造**。
    """
    if not isinstance(entry, dict):
        return None

    url = (entry.get("url") or entry.get("webpage_url")
           or entry.get("original_url") or fallback_url or "")
    if not url:
        return None

    title = entry.get("title") or entry.get("fulltitle") or ""
    duration = entry.get("duration_string") or ""
    if not duration and entry.get("duration") is not None:
        try:
            total = int(float(entry["duration"]))
            duration = f"{total // 60}:{total % 60:02d}"
        except Exception:
            duration = ""

    thumbnail = entry.get("thumbnail") or ""
    if not thumbnail:
        thumbs = entry.get("thumbnails") or []
        if isinstance(thumbs, list) and thumbs:
            last = thumbs[-1]
            if isinstance(last, dict):
                thumbnail = last.get("url") or ""

    author = (entry.get("uploader") or entry.get("channel")
              or entry.get("creator") or entry.get("uploader_id") or "")

    return SiteListItem(
        url=url,
        title=str(title).strip(),
        site=spec.key,
        duration=str(duration).strip(),
        thumbnail=str(thumbnail).strip(),
        author=str(author).strip(),
        source="ytdlp",
    )
