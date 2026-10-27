# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
数据模型模块
"""
from .download_task import DownloadTask, TaskStatus
from .detect_task import DetectTask, DetectStatus

__all__ = [
    'DownloadTask',
    'TaskStatus',
    'DetectTask',
    'DetectStatus'
]
