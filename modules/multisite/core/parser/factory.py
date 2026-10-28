# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
解析器工厂
自动识别站点并创建对应的解析器
"""
import re
import logging
from typing import Dict, Any
from urllib.parse import urlparse
from .base_parser import BaseParser

logger = logging.getLogger(__name__)


class SiteNotSupportedError(Exception):
    """站点不支持异常"""
    pass


class ParserFactory:
    """解析器工厂类"""

    # 支持的站点映射
    SITE_PARSERS = {
        'porn': 'PornAPIParser',
        'porn91': 'Porn91Parser',
        'xvideos': 'XVideosParser',
        'xhamster': 'XHamsterParser',
        'javdb': 'JAVDBParser',
    }

    # 站点域名映射
    DOMAIN_PATTERNS = {
        'porn91': [
            r'91porn\.com',
            r'porn91',
        ],
        'porn': [
            r'porn\.com',
            r'pornhub\.com',
            r'pornhub',
        ],
        'xvideos': [
            r'xvideos\.com',
            r'xvideos',
        ],
        'xhamster': [
            r'xhamster\.com',
            r'xhamster',
        ],
        'javdb': [
            r'javdb',
            r'javdb\.com',
        ],
    }

    @staticmethod
    def create_parser(site: str, config: Dict[str, Any] = None) -> BaseParser:
        """
        创建解析器实例
        
        Args:
            site: 站点名称
            config: 配置字典
        
        Returns:
            解析器实例
        """
        if config is None:
            config = {}

        site_lower = site.lower()

        if site_lower == 'porn':
            # PORNHUB 后端已剥离到独立项目，本模块不再内置 PH 解析器
            raise SiteNotSupportedError(
                "PORNHUB 解析器已从多站点模块移除，请使用 PORNHUB 独立模块"
            )
        elif site_lower in ('porn91', 'xvideos', 'xhamster', 'javdb'):
            # 这些站点的解析器模块在仓库中并不存在（历史上未实现或已移除）。
            # 原先直接 import 会抛出难以定位的 ModuleNotFoundError，
            # 这里改为显式的不支持错误；javdb 请走各自的独立模块。
            raise SiteNotSupportedError(
                f"站点 '{site}' 的解析器尚未实现，无法创建"
            )
        elif site_lower == 'madouqu':
            # 麻豆是 BT 磁力站，唯一实现在独立模块 modules/madouqu
            # （backend.madouqu_crawler 已接线 madouqu.core.crawler）。
            # 这里不再内置同名解析器，避免两套实现各自漂移。
            raise SiteNotSupportedError(
                "madouqu 请使用独立模块 modules/madouqu（BT 磁力），多站点工厂不再创建该解析器"
            )
        else:
            logger.error(f"不支持的站点: {site}")
            raise SiteNotSupportedError(f"不支持的站点: {site}")

    @staticmethod
    def get_supported_sites() -> list:
        """获取支持的站点列表"""
        return list(ParserFactory.SITE_PARSERS.keys())

    @staticmethod
    def detect_site(url: str) -> str:
        """
        从URL自动检测站点类型
        
        Args:
            url: URL
        
        Returns:
            站点名称
        """
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            full_url = url.lower()

            logger.info(f"检测站点: {domain}")

            for site, patterns in ParserFactory.DOMAIN_PATTERNS.items():
                for pattern in patterns:
                    if re.search(pattern, domain) or re.search(pattern, full_url):
                        logger.info(f"识别站点: {site}")
                        return site

            logger.warning(f"无法识别站点: {domain}，默认使用porn解析器")
            return 'porn'

        except Exception as e:
            logger.error(f"检测站点失败: {e}")
            raise SiteNotSupportedError(f"无法识别站点: {url}")

    @staticmethod
    def is_supported(url: str) -> bool:
        """检查URL是否支持"""
        try:
            site = ParserFactory.detect_site(url)
            return site in ParserFactory.SITE_PARSERS
        except Exception:
            return False
    
    @staticmethod
    def detect_video_type(url: str) -> str:
        """
        检测视频类型（针对Pornhub）
        
        Args:
            url: URL字符串
        
        Returns:
            视频类型: 'normal', 'shorty', 'unknown'
        """
        url_lower = url.lower()
        
        # 检测小视频 (shorties)
        if '/shorties/' in url_lower:
            return 'shorty'
        
        # 检测普通视频
        if 'view_video.php' in url_lower or 'viewkey=' in url_lower:
            return 'normal'
        
        return 'unknown'
    
    @staticmethod
    def is_shorty(url: str) -> bool:
        """
        判断是否是Pornhub小视频(shorties)
        
        Args:
            url: URL字符串
        
        Returns:
            是否为小视频
        """
        return ParserFactory.detect_video_type(url) == 'shorty'
    
    @staticmethod
    def is_downloadable(url: str) -> bool:
        """
        判断URL是否可以下载
        
        Args:
            url: URL字符串
        
        Returns:
            是否可以下载
        """
        video_type = ParserFactory.detect_video_type(url)
        # 只有普通视频可以下载，小视频不能下载
        return video_type == 'normal'
