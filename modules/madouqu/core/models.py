# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
Madouqu数据模型
"""
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, List


@dataclass
class MagnetLink:
    """磁力链接"""
    link: str
    title: Optional[str] = None
    size: Optional[str] = None
    date: Optional[str] = None
    
    def to_dict(self):
        return asdict(self)


@dataclass
class VideoInfo:
    """视频信息"""
    title: str
    model_name: str
    magnet_links: List[MagnetLink]
    detail_url: Optional[str] = None
    thumbnail: Optional[str] = None
    size: Optional[str] = None
    date: Optional[str] = None
    source: str = "madouqu"
    created_at: Optional[str] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()
    
    def to_dict(self):
        data = asdict(self)
        data['magnet_links'] = [m.to_dict() if isinstance(m, MagnetLink) else m for m in self.magnet_links]
        return data


@dataclass
class ModelInfo:
    """模特信息"""
    name: str
    url: Optional[str] = None
    avatar: Optional[str] = None
    video_count: int = 0
    magnet_count: int = 0
    last_crawled: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()
        if self.updated_at is None:
            self.updated_at = datetime.now().isoformat()
    
    def to_dict(self):
        return asdict(self)


@dataclass
class CrawlResult:
    """爬取结果"""
    model_name: str
    videos: List[VideoInfo]
    total_magnets: int
    pages_crawled: int
    duration_seconds: float
    success: bool = True
    error_message: Optional[str] = None
    
    def to_dict(self):
        data = asdict(self)
        data['videos'] = [v.to_dict() if hasattr(v, 'to_dict') else v for v in self.videos]
        return data
