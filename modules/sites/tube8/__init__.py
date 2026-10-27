# -*- coding: utf-8 -*-
"""Tube8 独立模块（域名 tube8.com）。

内容：综合片库（extractor 已损坏，走站点包）
备注：yt-dlp 的 Tube8 extractor 标记为 CURRENTLY BROKEN，实测必须走站点包。

此文件只描述"本站是什么"，不含任何抓取实现——解析统一交给共享引擎
``multisite``，本站的数据落在 ``DATA/sites/tube8``。
"""
from __future__ import annotations

from ..base import SiteModule

SITE = SiteModule(
    key="tube8",
    label="Tube8",
    domains=('tube8.com',),
    homepage="https://www.tube8.com",
    content="综合片库（extractor 已损坏，走站点包）",
    list_hints=('/pornstar/', '/channel/', '/model/'),
    needs_site_api=True,
    note="yt-dlp 的 Tube8 extractor 标记为 CURRENTLY BROKEN，实测必须走站点包。",
)

__all__ = ["SITE"]
