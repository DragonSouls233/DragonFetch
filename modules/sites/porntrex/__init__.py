# -*- coding: utf-8 -*-
"""PornTrex 独立模块（域名 porntrex.com）。

内容：需站点包解析（yt-dlp 无 extractor）
备注：yt-dlp 无 extractor；直链路径形如 /get_file/…，解析后交给下载引擎。

此文件只描述"本站是什么"，不含任何抓取实现——解析统一交给共享引擎
``multisite``，本站的数据落在 ``DATA/sites/porntrex``。
"""
from __future__ import annotations

from ..base import SiteModule

SITE = SiteModule(
    key="porntrex",
    label="PornTrex",
    domains=('porntrex.com',),
    homepage="https://www.porntrex.com",
    content="需站点包解析（yt-dlp 无 extractor）",
    list_hints=('/model/', '/channels/', '/channel/', '/pornstar/'),
    needs_site_api=True,
    note="yt-dlp 无 extractor；直链路径形如 /get_file/…，解析后交给下载引擎。",
)

__all__ = ["SITE"]
