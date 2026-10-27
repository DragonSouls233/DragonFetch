# -*- coding: utf-8 -*-
"""xHamster 独立模块（域名 xhamster.com）。

内容：综合片库 · 创作者 / 频道
备注：yt-dlp 原生支持；实测裸域名同样可通。

此文件只描述"本站是什么"，不含任何抓取实现——解析统一交给共享引擎
``multisite``，本站的数据落在 ``DATA/sites/xhamster``。
"""
from __future__ import annotations

from ..base import SiteModule

SITE = SiteModule(
    key="xhamster",
    label="xHamster",
    domains=('xhamster.com', 'xhamster.desi'),
    homepage="https://www.xhamster.com",
    content="综合片库 · 创作者 / 频道",
    list_hints=('/pornstars/', '/channels/', '/creators/', '/users/'),
    needs_site_api=False,
    note="yt-dlp 原生支持；实测裸域名同样可通。",
)

__all__ = ["SITE"]
