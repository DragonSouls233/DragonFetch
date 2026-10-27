# -*- coding: utf-8 -*-
"""XVideos 独立模块（域名 xvideos.com）。

内容：综合片库 · 模特 / 频道
备注：yt-dlp 原生支持；直链一次解析即可拿到。

此文件只描述"本站是什么"，不含任何抓取实现——解析统一交给共享引擎
``multisite``，本站的数据落在 ``DATA/sites/xvideos``。
"""
from __future__ import annotations

from ..base import SiteModule

SITE = SiteModule(
    key="xvideos",
    label="XVideos",
    domains=('xvideos.com', 'xvideos.es'),
    homepage="https://www.xvideos.com",
    content="综合片库 · 模特 / 频道",
    list_hints=('/profiles/', '/channels/', '/model/', '/pornstar/'),
    needs_site_api=False,
    note="yt-dlp 原生支持；直链一次解析即可拿到。",
)

__all__ = ["SITE"]
