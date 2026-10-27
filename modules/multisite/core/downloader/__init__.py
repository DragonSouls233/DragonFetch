# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
下载器模块
"""
from .factory import DownloaderFactory
from .base import BaseDownloader

__all__ = ['DownloaderFactory', 'BaseDownloader']

# 兼容旧的VideoDownloader
from .video_downloader import VideoDownloader
