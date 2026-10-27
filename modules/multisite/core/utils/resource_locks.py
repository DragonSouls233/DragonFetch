# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
重任务互斥锁（归档 ↔ 下载）
==========================

背景
----
2026-10-08 15:08:38 服务端闪退：归档线程（``_ArchiveWorker``，跨盘 ``copytree``）
与下载线程（yt-dlp 分片合并 900MB+ 文件）并发，8 GB 机器可用内存被压到
460 MB（``dwMemoryLoad=94%``），Qt 内部走 ``qFatal`` → ``abort()``，
进程瞬间消失（事件 1000：``Qt5Core.dll`` / ``c0000409``）。

对策：把「跨盘归档」与「批量下载」这两个吃内存的大户**互斥化**，
任一在跑时另一个必须让路。这样即使别的任务并发，峰值内存也不会叠加到崩溃边缘。

用法
----
::

    from .resource_locks import resource_guard, ResourceBusyError

    # 归档 Tab 启动前
    with resource_guard(Resource.ARCHIVE):
        arch.run(apply=True)

    # 下载入口处
    if not resource_available(Resource.DOWNLOAD):
        raise ResourceBusyError('归档进行中，请等归档完成再下载')
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from enum import Enum
from typing import Optional

#: 模块级日志（避免与业务 logger 循环依赖）
import logging

logger = logging.getLogger(__name__)


class Resource(Enum):
    """可互斥的重资源任务类型"""
    ARCHIVE = 'archive'    # 已注销归档（跨盘 copytree，极吃内存与磁盘）
    DOWNLOAD = 'download'  # 批量下载（yt-dlp 合并，极吃内存）
    DETECT = 'detect'      # 检测（解析在线视频 + 扫描本地目录，吃内存大户）


class ResourceBusyError(RuntimeError):
    """目标资源正被其它重任务占用"""


#: 当前占用的资源 -> 占用者描述。None = 空闲
_state_lock = threading.Lock()
_holder: Optional[tuple] = None  # (Resource, who)


def _try_acquire(res: Resource, who: str) -> None:
    """尝试占用；被占用则抛 ResourceBusyError"""
    global _holder
    with _state_lock:
        if _holder is not None and _holder[0] is not res:
            raise ResourceBusyError(
                f"{_holder[1]} 正在运行（资源={_holder[0].value}），"
                f"{who} 需等待其完成后再开始")
        if _holder is None:
            _holder = (res, who)
            logger.info(f"重任务互斥锁：{who} 占用 {res.value}")


def _release(res: Resource, who: str) -> None:
    global _holder
    with _state_lock:
        if _holder is not None and _holder[0] is res:
            logger.info(f"重任务互斥锁：{who} 释放 {res.value}")
            _holder = None


def resource_available(res: Resource) -> bool:
    """目标资源当前是否可被占用（UI 用来决定按钮是否置灰）"""
    with _state_lock:
        return _holder is None or _holder[0] is res


def current_holder() -> Optional[tuple]:
    """返回 (Resource, who)；空闲时返回 None"""
    with _state_lock:
        return _holder


@contextmanager
def resource_guard(res: Resource, who: str = ''):
    """上下文管理器：占用资源，退出自动释放

    Raises:
        ResourceBusyError: 若同一时刻有别的重任务在跑
    """
    label = who or res.value
    _try_acquire(res, label)
    try:
        yield
    finally:
        _release(res, label)


def describe_holder() -> str:
    """给用户看的一句话说明（当前谁在占用）"""
    h = current_holder()
    if not h:
        return '空闲'
    return f'{h[1]}（{h[0].value}）'
