# -*- coding: utf-8 -*-
"""YouPorn 独立模块（域名 youporn.com）。

内容：综合片库 · 频道 / 模特
备注：yt-dlp 原生支持。

此文件只描述"本站是什么"，不含任何抓取实现——解析统一交给共享引擎
``multisite``，本站的数据落在 ``DATA/sites/youporn``。
"""
from __future__ import annotations

from ..base import SiteModule

SITE = SiteModule(
    key="youporn",
    label="YouPorn",
    domains=('youporn.com',),
    homepage="https://www.youporn.com",
    content="综合片库 · 频道 / 模特",
    list_hints=('/pornstar/', '/channel/', '/model/'),
    needs_site_api=False,
    note="yt-dlp 原生支持。",
)

__all__ = ["SITE"]
