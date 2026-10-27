# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
Madouqu核心模块
"""
from .crawler import MadouquCrawler
from .database import MadouquDatabase
from .models import ModelInfo, VideoInfo, MagnetLink, CrawlResult

__all__ = [
    'MadouquCrawler',
    'MadouquDatabase',
    'ModelInfo',
    'VideoInfo',
    'MagnetLink',
    'CrawlResult',
]
