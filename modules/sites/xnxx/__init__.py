# -*- coding: utf-8 -*-
"""XNXX 独立模块（域名 xnxx.com）。

内容：综合片库 · 标签 / 模特
备注：yt-dlp 原生支持。

此文件只描述"本站是什么"，不含任何抓取实现——解析统一交给共享引擎
``multisite``，本站的数据落在 ``DATA/sites/xnxx``。
"""
from __future__ import annotations

from ..base import SiteModule

SITE = SiteModule(
    key="xnxx",
    label="XNXX",
    domains=('xnxx.com',),
    homepage="https://www.xnxx.com",
    content="综合片库 · 标签 / 模特",
    list_hints=('/profiles/', '/pornstar/', '/model/', '/channels/'),
    needs_site_api=False,
    note="yt-dlp 原生支持。",
)

__all__ = ["SITE"]
