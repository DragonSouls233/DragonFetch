# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""多站点接入层的数据模型

设计原则
--------
本层只负责"**把一个页面 URL 变成下载引擎能吃的 URL**"和"**把一个列表页变成
一批视频条目**"，不负责落库、不负责命名、不负责进度展示——那些仍然由原有链路
（``TempDownloadManager`` / ``FileNameFormatter`` / ``DownloadCacheDB``）负责。

这样切分的好处是：原有链路一行都不用改，新站点只是多了一个"URL 翻译"步骤。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = ["ResolvedVideo", "SiteListItem"]


@dataclass
class ResolvedVideo:
    """单个视频页面解析结果。

    ``direct_urls`` 是**下载引擎真正要吃的地址**（直链或 m3u8），
    而 ``page_url`` 是用户/数据库看到的页面地址。两者必须区分开：
    缓存、去重、数据库记录一律用 ``page_url``。
    """

    page_url: str
    site: str
    title: Optional[str] = None
    duration: Optional[str] = None
    author: Optional[str] = None
    thumbnail: Optional[str] = None
    publish_date: Optional[str] = None
    views: Optional[str] = None
    description: Optional[str] = None
    categories: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    direct_urls: List[str] = field(default_factory=list)
    qualities: List[str] = field(default_factory=list)
    source: str = ""  # "eaf" / "ytdlp" / "native"，便于排查

    @property
    def best_direct_url(self) -> Optional[str]:
        """取最高画质直链（``direct_urls`` 约定按画质升序）。"""
        return self.direct_urls[-1] if self.direct_urls else None

    @property
    def engine_url(self) -> str:
        """交给下载引擎的 URL。

        有直链就用最高画质直链；没有就退回页面地址（让 yt-dlp 自己去解析）。
        """
        return self.best_direct_url or self.page_url

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.page_url,
            "site": self.site,
            "title": self.title,
            "duration": self.duration,
            "author": self.author,
            "thumbnail": self.thumbnail,
            "publish_date": self.publish_date,
            "views": self.views,
            "description": self.description,
            "categories": list(self.categories),
            "tags": list(self.tags),
            "direct_urls": list(self.direct_urls),
            "qualities": list(self.qualities),
            "source": self.source,
        }


@dataclass
class SiteListItem:
    """列表页里的一条视频条目（只含列表页能拿到的字段）。"""

    url: str
    title: str = ""
    site: str = ""
    duration: str = ""
    thumbnail: str = ""
    author: str = ""
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "site": self.site,
            "duration": self.duration,
            "thumbnail": self.thumbnail,
            "author": self.author,
            "source": self.source,
        }
