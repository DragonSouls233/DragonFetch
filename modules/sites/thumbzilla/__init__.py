# -*- coding: utf-8 -*-
"""Thumbzilla 独立模块（域名 thumbzilla.com）。

内容：需站点包解析（yt-dlp 无 extractor）
备注：yt-dlp 无 extractor；必须先用站点包解析出直链。

此文件只描述"本站是什么"，不含任何抓取实现——解析统一交给共享引擎
``multisite``，本站的数据落在 ``DATA/sites/thumbzilla``。
"""
from __future__ import annotations

from ..base import SiteModule

SITE = SiteModule(
    key="thumbzilla",
    label="Thumbzilla",
    domains=('thumbzilla.com',),
    homepage="https://www.thumbzilla.com",
    content="需站点包解析（yt-dlp 无 extractor）",
    list_hints=('/pornstar/', '/channels/', '/model/', '/channel/'),
    needs_site_api=True,
    note="yt-dlp 无 extractor；必须先用站点包解析出直链。",
)

__all__ = ["SITE"]
