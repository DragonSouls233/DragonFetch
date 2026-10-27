# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
数据库变更观察者
监听数据库变化，实时更新GUI统计信息
"""
import logging
import threading
from typing import Dict, Callable, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class ChangeType(Enum):
    """变更类型"""
    MODEL_ADDED = "model_added"
    MODEL_UPDATED = "model_updated"
    MODEL_DELETED = "model_deleted"
    VIDEO_ADDED = "video_added"
    VIDEO_STATUS_CHANGED = "video_status_changed"
    STATS_UPDATED = "stats_updated"
    DOWNLOAD_COMPLETED = "download_completed"


@dataclass
class DatabaseChange:
    """数据库变更事件"""
    change_type: ChangeType
    module: str  # PORN, JAVDB, MADOUQU
    model_id: Optional[int] = None
    model_name: Optional[str] = None
    video_url: Optional[str] = None
    old_value: Any = None
    new_value: Any = None
    timestamp: datetime = field(default_factory=datetime.now)


class DatabaseChangeObserver:
    """
    数据库变更观察者

    功能：
    1. 监听数据库变更事件
    2. 通知所有注册的观察者
    3. 支持多个观察者同时监听
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._observers: List[Callable[[DatabaseChange], None]] = []
        self._observer_lock = threading.Lock()
        self._change_history: List[DatabaseChange] = []
        self._max_history = 100  # 保留最近100条变更记录
        self._initialized = True

        logger.info("数据库变更观察者初始化成功")

    def register(self, callback: Callable[[DatabaseChange], None]):
        """
        注册观察者

        Args:
            callback: 回调函数，接收 DatabaseChange 事件
        """
        with self._observer_lock:
            if callback not in self._observers:
                self._observers.append(callback)
                logger.info(f"观察者注册成功: {callback.__name__ if hasattr(callback, '__name__') else str(callback)}")

    def unregister(self, callback: Callable[[DatabaseChange], None]):
        """
        取消注册观察者

        Args:
            callback: 回调函数
        """
        with self._observer_lock:
            if callback in self._observers:
                self._observers.remove(callback)
                logger.info(f"观察者取消注册: {callback.__name__ if hasattr(callback, '__name__') else str(callback)}")

    def notify(self, change: DatabaseChange):
        """
        通知所有观察者

        Args:
            change: 数据库变更事件
        """
        # 记录到历史
        self._add_to_history(change)

        # 通知所有观察者
        with self._observer_lock:
            for observer in self._observers:
                try:
                    observer(change)
                except Exception as e:
                    logger.error(f"通知观察者失败: {e}")

    def _add_to_history(self, change: DatabaseChange):
        """
        添加到变更历史

        Args:
            change: 数据库变更事件
        """
        self._change_history.append(change)
        # 限制历史记录数量
        if len(self._change_history) > self._max_history:
            self._change_history = self._change_history[-self._max_history:]

    def get_recent_changes(self, count: int = 10, module: str = None) -> List[DatabaseChange]:
        """
        获取最近的变更记录

        Args:
            count: 返回数量
            module: 可选，按模块筛选

        Returns:
            变更记录列表
        """
        changes = self._change_history[-count:] if count > 0 else self._change_history
        if module:
            changes = [c for c in changes if c.module == module]
        return changes

    def clear_history(self):
        """清空变更历史"""
        self._change_history.clear()


# 全局观察者实例
_db_observer = None


def get_db_observer() -> DatabaseChangeObserver:
    """获取数据库变更观察者实例"""
    global _db_observer
    if _db_observer is None:
        _db_observer = DatabaseChangeObserver()
    return _db_observer


def emit_model_updated(
    module: str,
    model_id: int,
    model_name: str,
    old_stats: Dict[str, int] = None,
    new_stats: Dict[str, int] = None
):
    """
    发射模型更新事件

    Args:
        module: 模块名
        model_id: 模特ID
        model_name: 模特名
        old_stats: 旧统计 {'local': 198, 'missing': 2, 'online': 200}
        new_stats: 新统计 {'local': 199, 'missing': 1, 'online': 200}
    """
    change = DatabaseChange(
        change_type=ChangeType.MODEL_UPDATED,
        module=module,
        model_id=model_id,
        model_name=model_name,
        old_value=old_stats,
        new_value=new_stats
    )
    get_db_observer().notify(change)


def emit_download_completed(
    module: str,
    model_id: int,
    model_name: str,
    video_url: str,
    video_title: str = None,
    file_size: int = 0
):
    """
    发射下载完成事件

    Args:
        module: 模块名
        model_id: 模特ID
        model_name: 模特名
        video_url: 视频URL
        video_title: 视频标题
        file_size: 文件大小
    """
    change = DatabaseChange(
        change_type=ChangeType.DOWNLOAD_COMPLETED,
        module=module,
        model_id=model_id,
        model_name=model_name,
        video_url=video_url,
        old_value={'file_size': file_size},
        new_value={'file_size': file_size, 'title': video_title}
    )
    get_db_observer().notify(change)


def emit_stats_updated(module: str, stats: Dict[str, Any]):
    """
    发射统计更新事件

    Args:
        module: 模块名
        stats: 统计信息
    """
    change = DatabaseChange(
        change_type=ChangeType.STATS_UPDATED,
        module=module,
        new_value=stats
    )
    get_db_observer().notify(change)