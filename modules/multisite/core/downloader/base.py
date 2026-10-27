# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
下载器基类
"""
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional
from pathlib import Path


class BaseDownloader(ABC):
    """下载器基类"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化下载器
        
        Args:
            config: 配置字典
        """
        self.config = config
        self.progress_callback: Optional[Callable] = None
        self._is_cancelled = False
    
    @abstractmethod
    async def download(self, url: str, output_path: str) -> bool:
        """
        下载视频
        
        Args:
            url: 视频URL
            output_path: 输出路径
        
        Returns:
            是否成功
        """
        pass
    
    @abstractmethod
    async def get_video_info(self, url: str) -> Dict[str, Any]:
        """
        获取视频信息
        
        Args:
            url: 视频URL
        
        Returns:
            视频信息字典
        """
        pass
    
    def set_progress_callback(self, callback: Callable) -> None:
        """
        设置进度回调函数
        
        Args:
            callback: 回调函数，接收进度信息字典
        """
        self.progress_callback = callback
    
    def cancel(self) -> None:
        """取消下载"""
        self._is_cancelled = True
    
    def _report_progress(self, progress_info: Dict[str, Any]) -> None:
        """
        报告进度
        
        Args:
            progress_info: 进度信息
        """
        if self.progress_callback:
            self.progress_callback(progress_info)
    
    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """
        清理文件名，移除非法字符
        
        Args:
            filename: 原始文件名
        
        Returns:
            清理后的文件名
        """
        # Windows非法字符
        illegal_chars = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
        for char in illegal_chars:
            filename = filename.replace(char, '_')
        
        # 移除首尾空格和点
        filename = filename.strip('. ')
        
        # 限制文件名长度
        if len(filename) > 200:
            filename = filename[:200]
        
        return filename or 'unnamed'
    
    @staticmethod
    def ensure_dir(path: str) -> Path:
        """
        确保目录存在
        
        Args:
            path: 目录路径
        
        Returns:
            Path对象
        """
        dir_path = Path(path)
        dir_path.mkdir(parents=True, exist_ok=True)
        return dir_path
