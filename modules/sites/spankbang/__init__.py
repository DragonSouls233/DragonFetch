# -*- coding: utf-8 -*-
"""SpankBang 独立模块（域名 spankbang.com）。

内容：综合片库 · 模特 / 频道
备注：yt-dlp 原生支持。

此文件只描述"本站是什么"，不含任何抓取实现——解析统一交给共享引擎
``multisite``，本站的数据落在 ``DATA/sites/spankbang``。
"""
from __future__ import annotations

from ..base import SiteModule

SITE = SiteModule(
    key="spankbang",
    label="SpankBang",
    domains=('spankbang.com',),
    homepage="https://www.spankbang.com",
    content="综合片库 · 模特 / 频道",
    list_hints=('/profile/', '/pornstar/', '/channel/'),
    needs_site_api=False,
    note="yt-dlp 原生支持。",
)

__all__ = ["SITE"]
