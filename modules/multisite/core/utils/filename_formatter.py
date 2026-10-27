# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
文件命名工具
根据 F:\\porn 的命名规范生成文件名和文件夹名
"""
import re
import hashlib
from datetime import datetime
from typing import Optional


class FileNameFormatter:
    """文件名格式化器"""

    @staticmethod
    def format_folder_name(artist: str, prefix: str = "Channel") -> str:
        """
        格式化文件夹名称
        
        格式: [prefix] artist
        例如: [Channel] Abella Olsen
        
        Args:
            artist: 艺术家/模特名称
            prefix: 前缀（Channel, Model等）
        
        Returns:
            格式化后的文件夹名
        """
        # 清理艺术家名称
        artist = FileNameFormatter._clean_name(artist)
        
        # 格式: [prefix] artist
        return f"[{prefix}] {artist}"

    @staticmethod
    def format_video_filename(
        artist: str,
        title: str,
        video_id: str,
        date: Optional[datetime] = None
    ) -> str:
        """
        格式化视频文件名
        
        格式1: [artist] title (id).ext
        例如: [Abella Olsen] 我性感的拉丁裔邻居来找牛奶 (6929dbb9475a1).mp4
        
        格式2: [artist] date:%Y-%m-%d %H:%M; title.ext
        例如: [Abella Olsen] 2024-03-09 15:30; 我性感的拉丁裔邻居.mp4
        
        Args:
            artist: 艺术家/模特名称
            title: 视频标题
            video_id: 视频ID
            date: 可选的日期时间
        
        Returns:
            格式化后的文件名（不含扩展名）
        """
        # 清理名称
        artist = FileNameFormatter._clean_name(artist)
        title = FileNameFormatter._clean_name(title)
        
        # 根据是否有日期选择格式
        if date:
            # 格式2: [artist] date:%Y-%m-%d %H:%M; title
            date_str = date.strftime("%Y-%m-%d %H:%M")
            filename = f"[{artist}] {date_str}; {title}"
        else:
            # 格式1: [artist] title (id)
            filename = f"[{artist}] {title} ({video_id})"
        
        return filename

    @staticmethod
    def format_video_filename_with_ext(
        artist: str,
        title: str,
        video_id: str,
        ext: str = "mp4",
        date: Optional[datetime] = None
    ) -> str:
        """
        格式化视频文件名（含扩展名）
        
        Args:
            artist: 艺术家/模特名称
            title: 视频标题
            video_id: 视频ID
            ext: 文件扩展名
            date: 可选的日期时间
        
        Returns:
            格式化后的完整文件名
        """
        filename = FileNameFormatter.format_video_filename(artist, title, video_id, date)
        
        # 确保扩展名以点开头
        if not ext.startswith('.'):
            ext = f'.{ext}'
        
        return f"{filename}{ext}"

    @staticmethod
    def _clean_name(name: str) -> str:
        """
        清理名称，移除非法字符
        
        Args:
            name: 原始名称
        
        Returns:
            清理后的名称
        """
        if not name:
            return "Unknown"
        
        # Windows非法字符: < > : " / \ | ? *
        illegal_chars = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
        for char in illegal_chars:
            name = name.replace(char, '_')
        
        # 移除首尾空格和点
        name = name.strip('. ')
        
        # 移除连续的空格
        name = re.sub(r'\s+', ' ', name)
        
        # 限制长度
        if len(name) > 200:
            name = name[:200]
        
        return name or "Unknown"

    @staticmethod
    def extract_video_id_from_url(url: str) -> str:
        """
        从URL中提取视频ID
        
        Args:
            url: 视频URL
        
        Returns:
            视频ID
        """
        # PornHub Shorties URL格式: /shorties/xxxxx
        match = re.search(r'/shorties/([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)
        
        # PornHub URL格式: view_video.php?viewkey=xxxxx
        match = re.search(r'viewkey=([a-zA-Z0-9]+)', url)
        if match:
            return match.group(1)
        
        # 其他格式
        match = re.search(r'/([a-zA-Z0-9]+)(?:\?|$)', url)
        if match:
            return match.group(1)
        
        # 使用URL的hash作为ID
        return hashlib.md5(url.encode()).hexdigest()[:13]

    @staticmethod
    def parse_filename(filename: str) -> dict:
        """
        解析文件名，提取信息
        
        格式: [artist] title (id).ext
        
        Args:
            filename: 文件名
        
        Returns:
            包含artist, title, id的字典
        """
        result = {
            'artist': None,
            'title': None,
            'id': None
        }
        
        # 提取artist: [artist]
        match = re.match(r'\[([^\]]+)\]', filename)
        if match:
            result['artist'] = match.group(1)
        
        # 提取id: (id)
        match = re.search(r'\(([a-zA-Z0-9]+)\)', filename)
        if match:
            result['id'] = match.group(1)
        
        # 提取title: [artist] title (id)
        match = re.search(r'\[([^\]]+)\]\s+(.+?)\s*\(', filename)
        if match:
            result['title'] = match.group(2)
        
        return result

    @staticmethod
    def format(
        title: str,
        model_name: str = None,
        index: int = None,
        date: datetime = None,
        extension: str = "mp4",
        url: str = None
    ) -> str:
        """
        通用格式化方法
        
        Args:
            title: 视频标题
            model_name: 模特名
            index: 序号（可选）
            date: 日期（可选）
            extension: 扩展名
            url: 视频URL（用于提取ID）
        
        Returns:
            格式化后的文件名
        """
        # 获取视频ID
        if url:
            video_id = FileNameFormatter.extract_video_id_from_url(url)
        elif index:
            video_id = f"{index:04d}"
        else:
            video_id = hashlib.md5(title.encode()).hexdigest()[:13]
        
        # 使用模特名或默认值
        artist = model_name or "Unknown"
        
        return FileNameFormatter.format_video_filename_with_ext(
            artist=artist,
            title=title,
            video_id=video_id,
            ext=extension,
            date=date
        )
