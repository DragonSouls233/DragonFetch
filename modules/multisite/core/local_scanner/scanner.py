# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
本地视频扫描器
扫描本地目录中的视频文件，提取标题用于对比
"""
import os
import re
import logging
from typing import List, Dict, Set, Tuple, Optional
from pathlib import Path
from .title_normalizer import TitleNormalizer

logger = logging.getLogger(__name__)


class LocalScanner:
    """
    本地视频扫描器
    扫描指定目录下的视频文件，提取标题信息
    """
    
    # 支持的视频扩展名
    VIDEO_EXTENSIONS = {
        '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm',
        '.m4v', '.mpg', '.mpeg', '.3gp', '.ts', '.mts', '.m2ts',
    }
    
    # 模特目录前缀模式
    MODEL_PREFIX_PATTERNS = [
        r'^\[Channel\]\s*',      # [Channel] 模特名
        r'^\[PornHub\]\s*',      # [PornHub] 模特名
        r'^\[PH\]\s*',           # [PH] 模特名
        r'^\[.*?\]\s*',          # 通用 [xxx] 前缀
    ]
    
    def __init__(self, config: dict = None):
        """
        初始化扫描器
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        self.normalizer = TitleNormalizer(config)
        self.video_extensions = set(self.config.get('video_extensions', self.VIDEO_EXTENSIONS))
    
    def scan_directory(self, directory: str, recursive: bool = True) -> List[Dict[str, str]]:
        """
        扫描目录中的视频文件
        
        Args:
            directory: 目录路径
            recursive: 是否递归扫描
        
        Returns:
            视频信息列表 [{path, title, normalized_title}, ...]
        """
        directory = os.path.normpath(directory)
        
        if not os.path.exists(directory):
            logger.warning(f"目录不存在: {directory}")
            return []
        
        videos = []
        logger.info(f"开始扫描目录: {directory}")
        
        if recursive:
            for root, dirs, files in os.walk(directory):
                for filename in files:
                    video_info = self._process_video_file(root, filename)
                    if video_info:
                        videos.append(video_info)
        else:
            for filename in os.listdir(directory):
                video_info = self._process_video_file(directory, filename)
                if video_info:
                    videos.append(video_info)
        
        logger.info(f"扫描完成，共找到 {len(videos)} 个视频文件")
        return videos
    
    def scan_model_directory(self, directory: str) -> Dict[str, List[Dict[str, str]]]:
        """
        扫描模特目录结构
        假设目录结构为: root/[Channel] 模特名/视频文件
        
        Args:
            directory: 根目录路径
        
        Returns:
            模特视频映射 {模特名: [{path, title, normalized_title}, ...]}
        """
        directory = os.path.normpath(directory)
        
        if not os.path.exists(directory):
            logger.warning(f"目录不存在: {directory}")
            return {}
        
        model_videos = {}
        logger.info(f"开始扫描模特目录: {directory}")
        
        for item in os.listdir(directory):
            item_path = os.path.join(directory, item)
            
            if not os.path.isdir(item_path):
                continue
            
            # 提取模特名
            model_name = self._extract_model_name(item)
            if not model_name:
                logger.debug(f"跳过非模特目录: {item}")
                continue
            
            # 扫描该模特的视频
            videos = self.scan_directory(item_path, recursive=True)
            
            if videos:
                model_videos[model_name] = videos
                logger.info(f"模特 [{model_name}] 找到 {len(videos)} 个视频")
        
        total_videos = sum(len(v) for v in model_videos.values())
        logger.info(f"扫描完成，共 {len(model_videos)} 个模特，{total_videos} 个视频")
        
        return model_videos
    
    def get_local_titles(self, directory: str) -> Set[str]:
        """
        获取目录中所有视频的标题集合（归一化后）
        
        Args:
            directory: 目录路径
        
        Returns:
            归一化后的标题集合
        """
        videos = self.scan_directory(directory)
        return {v['normalized_title'] for v in videos if v.get('normalized_title')}
    
    def get_local_videos_map(self, directory: str) -> Dict[str, str]:
        """
        获取目录中所有视频的标题到路径映射（归一化后）
        
        Args:
            directory: 目录路径
        
        Returns:
            归一化标题 -> 文件路径 的映射
        """
        videos = self.scan_directory(directory)
        return {v['normalized_title']: v['path'] for v in videos if v.get('normalized_title')}
    
    def _process_video_file(self, root: str, filename: str) -> Optional[Dict[str, str]]:
        """
        处理单个视频文件
        
        Args:
            root: 文件所在目录
            filename: 文件名
        
        Returns:
            视频信息字典，如果不是视频文件则返回None
        """
        # 检查扩展名
        ext = os.path.splitext(filename)[1].lower()
        if ext not in self.video_extensions:
            return None
        
        # 提取标题（去掉扩展名）
        title = os.path.splitext(filename)[0]
        
        # 清理标题
        cleaned_title = self._clean_video_title(title)
        
        # 归一化
        normalized_title = self.normalizer.normalize(cleaned_title)
        
        # 完整路径
        full_path = os.path.join(root, filename)
        
        return {
            'path': full_path,
            'title': cleaned_title,
            'normalized_title': normalized_title,
            'original_filename': filename,
        }
    
    def _clean_video_title(self, title: str) -> str:
        """
        清理视频标题
        
        Args:
            title: 原始标题
        
        Returns:
            清理后的标题
        """
        result = title
        
        # 移除常见的文件名前缀/后缀
        patterns = [
            r'^\d{4}-\d{2}-\d{2}[_\s-]*',  # 日期前缀
            r'^\d+[_\s-]*',                 # 数字前缀
            r'\s*-\s*$',                    # 尾部破折号
            r'\s*_\s*$',                    # 尾部下划线
            # 移除 [xxx] 前缀（如 [Ruth lee]）
            r'^\[[^\]]*\]\s*',
            # 移除尾部 (hex_id) 后缀（如 (69e51ebcb70e3)）
            r'\s*\([a-fA-F0-9]{8,}\)\s*$',
            # 移除尾部 (数字id) 后缀
            r'\s*\(\d+\)\s*$',
        ]
        
        for pattern in patterns:
            result = re.sub(pattern, '', result)
        
        # 移除多余的空格
        result = re.sub(r'\s+', ' ', result).strip()
        
        return result
    
    def _extract_model_name(self, dirname: str) -> Optional[str]:
        """
        从目录名中提取模特名
        
        Args:
            dirname: 目录名
        
        Returns:
            模特名，如果无法提取则返回None
        """
        for pattern in self.MODEL_PREFIX_PATTERNS:
            match = re.match(pattern, dirname)
            if match:
                # 移除前缀，得到模特名
                model_name = dirname[match.end():].strip()
                if model_name:
                    return model_name
        
        return None
    
    def count_videos(self, directory: str) -> int:
        """
        统计目录中的视频数量
        
        Args:
            directory: 目录路径
        
        Returns:
            视频数量
        """
        videos = self.scan_directory(directory)
        return len(videos)
    
    def get_video_stats(self, directory: str) -> Dict[str, any]:
        """
        获取目录视频统计信息
        
        Args:
            directory: 目录路径
        
        Returns:
            统计信息字典
        """
        videos = self.scan_directory(directory)
        
        # 计算总大小
        total_size = 0
        for video in videos:
            try:
                total_size += os.path.getsize(video['path'])
            except Exception:
                logger.debug("get_video_stats 忽略异常", exc_info=True)
        
        return {
            'total_videos': len(videos),
            'total_size': total_size,
            'total_size_mb': round(total_size / (1024 * 1024), 2),
            'total_size_gb': round(total_size / (1024 * 1024 * 1024), 2),
        }
