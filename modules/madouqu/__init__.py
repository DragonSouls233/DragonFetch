# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
Madouqu模块 - BT磁力链接爬虫
独立模块，用于爬取 madouqu.sbs 网站的模特BT资源
"""
from .core.crawler import MadouquCrawler
from .core.database import MadouquDatabase
from .core.models import ModelInfo, VideoInfo, MagnetLink

__all__ = [
    'MadouquCrawler',
    'MadouquDatabase',
    'ModelInfo',
    'VideoInfo',
    'MagnetLink',
]

__version__ = '1.0.0'
