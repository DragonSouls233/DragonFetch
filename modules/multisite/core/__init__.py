# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
核心共享模块
提供统一的解析器、本地扫描、对比引擎、数据库等功能
"""
from .parser import ParserFactory, BaseParser
from .parser.factory import SiteNotSupportedError
from .local_scanner import LocalScanner, TitleNormalizer
from .comparator import MissingFinder
from .cache import UnifiedCache
from .detector import UnifiedDetector, quick_detect, quick_detect_sync
from .database import ModuleDatabase, ModuleType, ModelInfo, VideoInfo
from .models import DownloadTask, TaskStatus, DetectTask, DetectStatus

__all__ = [
    # 解析器
    'ParserFactory',
    'BaseParser',
    'SiteNotSupportedError',
    # 本地扫描
    'LocalScanner', 
    'TitleNormalizer',
    # 对比
    'MissingFinder',
    # 缓存
    'UnifiedCache',
    # 检测器
    'UnifiedDetector',
    'quick_detect',
    'quick_detect_sync',
    # 数据库
    'ModuleDatabase',
    'ModuleType',
    'ModelInfo',
    'VideoInfo',
    # 数据模型
    'DownloadTask',
    'TaskStatus',
    'DetectTask',
    'DetectStatus',
]
