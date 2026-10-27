# -*- coding: utf-8 -*-
"""Eporner 独立模块（域名 eporner.com）。

内容：高清片库 · 频道 / 模特
备注：yt-dlp 原生支持。

此文件只描述"本站是什么"，不含任何抓取实现——解析统一交给共享引擎
``multisite``，本站的数据落在 ``DATA/sites/eporner``。
"""
from __future__ import annotations

from ..base import SiteModule

SITE = SiteModule(
    key="eporner",
    label="Eporner",
    domains=('eporner.com',),
    homepage="https://www.eporner.com",
    content="高清片库 · 频道 / 模特",
    list_hints=('/profile/', '/pornstar/', '/channel/'),
    needs_site_api=False,
    note="yt-dlp 原生支持。",
)

__all__ = ["SITE"]
