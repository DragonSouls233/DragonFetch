# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
下载任务数据模型
"""
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from typing import Optional


class TaskStatus(Enum):
    """任务状态枚举"""
    WAITING = "waiting"        # 等待中
    DOWNLOADING = "downloading"  # 下载中
    PAUSED = "paused"          # 已暂停
    COMPLETED = "completed"    # 已完成
    FAILED = "failed"          # 失败


@dataclass
class DownloadTask:
    """下载任务数据模型"""
    task_id: str              # 任务ID (UUID)
    name: str                 # 视频标题
    model_name: str           # 订阅名
    url: str                  # 下载URL
    save_path: str            # 保存路径
    file_size: int = 0        # 文件大小 (字节)
    downloaded_size: int = 0  # 已下载大小 (字节)
    status: TaskStatus = TaskStatus.WAITING
    speed: str = ""           # 下载速度
    remaining_time: str = ""  # 剩余时间
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    def __post_init__(self):
        """初始化后处理"""
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()
    
    @property
    def progress(self) -> int:
        """计算进度百分比"""
        if self.file_size == 0:
            return 0
        return int(self.downloaded_size / self.file_size * 100)
    
    def update_timestamp(self):
        """更新时间戳"""
        self.updated_at = datetime.now()
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'task_id': self.task_id,
            'name': self.name,
            'model_name': self.model_name,
            'url': self.url,
            'save_path': self.save_path,
            'file_size': self.file_size,
            'downloaded_size': self.downloaded_size,
            'status': self.status.value,
            'speed': self.speed,
            'remaining_time': self.remaining_time,
            'progress': self.progress,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
