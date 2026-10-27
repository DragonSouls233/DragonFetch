# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
下载器工厂
"""
import re
from typing import Any, Dict, Optional, Callable
from .base import BaseDownloader
import logging

logger = logging.getLogger(__name__)


class DownloaderFactory:
    """下载器工厂类"""

    # URL模式匹配
    M3U8_PATTERNS = [
        r'\.m3u8($|\?)',  # .m3u8结尾
        r'm3u8',  # URL中包含m3u8
    ]

    HTTP_VIDEO_PATTERNS = [
        r'\.(mp4|mkv|avi|mov|flv|wmv|webm)($|\?)',  # 常见视频格式
        r'\.(mp3|m4a|flac|wav|aac)($|\?)',  # 音频格式
    ]

    @staticmethod
    def create_downloader(engine_type: str = 'yt-dlp', config: Dict[str, Any] = None, progress_callback: Optional[Callable] = None) -> BaseDownloader:
        """
        创建下载器实例

        Args:
            engine_type: 引擎类型 ('yt-dlp', 'm3u8', 'http', 'auto')
            config: 配置字典
            progress_callback: 进度回调函数

        Returns:
            下载器实例
        """
        if config is None:
            config = {}

        if engine_type == 'yt-dlp':
            from .engines.yt_dlp_engine import YtDlpEngine
            logger.info("Creating yt-dlp downloader")
            return YtDlpEngine(config, progress_callback=progress_callback)

        elif engine_type == 'm3u8':
            from .engines.m3u8_engine import M3U8Engine
            logger.info("Creating M3U8 downloader")
            return M3U8Engine(config)

        elif engine_type == 'http':
            from .engines.http_engine import HttpEngine
            logger.info("Creating HTTP multi-thread downloader")
            return HttpEngine(config)

        elif engine_type == 'auto':
            # 自动检测引擎类型
            url = config.get('url', '')
            detected_type = DownloaderFactory.detect_engine_type(url)
            logger.info(f"Auto-detected engine type: {detected_type} for URL: {url[:50]}...")
            return DownloaderFactory.create_downloader(detected_type, config)

        else:
            # 默认使用yt-dlp
            from .engines.yt_dlp_engine import YtDlpEngine
            logger.warning(f"Unknown downloader engine type: {engine_type}, using yt-dlp")
            return YtDlpEngine(config)

    @staticmethod
    def detect_engine_type(url: str) -> str:
        """
        根据URL自动检测引擎类型

        Args:
            url: 视频URL

        Returns:
            引擎类型
        """
        if not url:
            return 'yt-dlp'

        url_lower = url.lower()

        # 检查M3U8
        for pattern in DownloaderFactory.M3U8_PATTERNS:
            if re.search(pattern, url_lower):
                return 'm3u8'

        # 检查HTTP直连视频
        for pattern in DownloaderFactory.HTTP_VIDEO_PATTERNS:
            if re.search(pattern, url_lower):
                return 'http'

        # 默认使用yt-dlp
        return 'yt-dlp'

    @staticmethod
    def get_supported_engines() -> list:
        """
        获取支持的引擎列表

        Returns:
            引擎列表
        """
        return ['yt-dlp', 'm3u8', 'http', 'auto']

    @staticmethod
    def get_engine_info(engine_type: str) -> Dict[str, Any]:
        """
        获取引擎信息

        Args:
            engine_type: 引擎类型

        Returns:
            引擎信息字典
        """
        engines_info = {
            'yt-dlp': {
                'name': 'yt-dlp',
                'description': '通用视频下载引擎，支持1000+网站',
                'features': ['多站点支持', '自动格式选择', '字幕下载', '元数据嵌入'],
                'best_for': 'YouTube、Bilibili等主流视频网站',
            },
            'm3u8': {
                'name': 'M3U8 Engine',
                'description': 'HLS流媒体专用引擎，支持多线程下载',
                'features': ['多线程分片', '断点续传', '加密流支持', '自动选择最佳画质'],
                'best_for': 'M3U8/HLS流媒体链接',
            },
            'http': {
                'name': 'HTTP Engine',
                'description': 'HTTP多线程分片下载引擎',
                'features': ['多线程分片', '断点续传', '速度限制', '进度追踪'],
                'best_for': '直链视频/音频文件',
            },
        }

        return engines_info.get(engine_type, {})
