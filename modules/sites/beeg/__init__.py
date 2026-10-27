# -*- coding: utf-8 -*-
"""Beeg 独立模块（域名 beeg.com）。

内容：综合片库 · 频道
备注：yt-dlp 原生支持；裸域名更直接。

此文件只描述"本站是什么"，不含任何抓取实现——解析统一交给共享引擎
``multisite``，本站的数据落在 ``DATA/sites/beeg``。
"""
from __future__ import annotations

from ..base import SiteModule

SITE = SiteModule(
    key="beeg",
    label="Beeg",
    domains=('beeg.com',),
    homepage="https://beeg.com",
    content="综合片库 · 频道",
    list_hints=('/profile/', '/channel/', '/pornstar/'),
    needs_site_api=False,
    note="yt-dlp 原生支持；裸域名更直接。",
)

__all__ = ["SITE"]
