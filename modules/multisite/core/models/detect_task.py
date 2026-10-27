# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
检测任务数据模型
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class DetectStatus(Enum):
    """检测状态枚举"""
    IDLE = "idle"            # 空闲
    DETECTING = "detecting"  # 检测中
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"        # 失败


@dataclass
class DetectTask:
    """检测任务数据模型"""
    model_id: int            # 订阅ID
    model_name: str          # 订阅名
    total_count: int = 0     # 总视频数
    current_count: int = 0   # 已检测数
    new_count: int = 0       # 新增数量
    missing_count: int = 0   # 缺失数量
    status: DetectStatus = DetectStatus.IDLE
    
    @property
    def progress(self) -> int:
        """计算进度百分比"""
        if self.total_count == 0:
            return 0
        return int(self.current_count / self.total_count * 100)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'model_id': self.model_id,
            'model_name': self.model_name,
            'total_count': self.total_count,
            'current_count': self.current_count,
            'new_count': self.new_count,
            'missing_count': self.missing_count,
            'status': self.status.value,
            'progress': self.progress
        }
