# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
解析器基类
"""
import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


class BaseParser(ABC):
    """解析器基类"""
    
    def __init__(self, config: Dict[str, Any] = None):
        """
        初始化解析器
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        self.site_name = "Unknown"
        self.base_url = ""
        
        # 网络请求配置
        self.timeout = self.config.get('timeout', 30)
        self.max_retries = self.config.get('max_retries', 3)
        self.retry_delay = self.config.get('retry_delay', 2)
        self.request_delay = self.config.get('request_delay', 1)
        self._last_request_time = None
    
    @abstractmethod
    async def parse_model_page(self, url: str, html: str = None, max_pages: int = 100) -> List[Dict[str, Any]]:
        """
        解析模特页面，获取所有视频
        
        Args:
            url: 模特页面URL
            html: HTML内容（可选）
            max_pages: 最大解析页数
        
        Returns:
            视频列表 [{url, title, thumbnail, duration}, ...]
        """
        pass
    
    @abstractmethod
    async def parse_video_page(self, url: str, html: str = None) -> Dict[str, Any]:
        """
        解析视频页面
        
        Args:
            url: 视频URL
            html: HTML内容（可选）
        
        Returns:
            视频信息字典
        """
        pass
    
    def get_site_name(self) -> str:
        """获取站点名称"""
        return self.site_name
    
    def get_base_url(self) -> str:
        """获取基础URL"""
        return self.base_url
    
    @staticmethod
    def clean_text(text: str) -> str:
        """清理文本"""
        if not text:
            return ""
        text = ' '.join(text.split())
        text = text.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')
        return text.strip()
    
    @staticmethod
    def is_valid_url(url: str) -> bool:
        """检查URL是否有效"""
        if not url:
            return False
        return url.startswith('http')
