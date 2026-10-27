# -*- coding: utf-8 -*-
"""后台任务执行器：所有阻塞型后端调用都必须走这里，禁止在 UI 线程里联网。

用法：
    task = Task(fn, arg1, arg2, want_progress=True)
    task.done.connect(on_ok); task.failed.connect(on_err); task.progress.connect(on_prog)
    CENTER.start(task)
"""
from __future__ import annotations

import inspect
import traceback
from typing import Any, Callable, Set

from PyQt6.QtCore import QObject, QThread, pyqtSignal


class Task(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, str)

    def __init__(self, fn: Callable, *args, want_progress: bool = False,
                 parent: QObject | None = None, **kwargs):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._want_progress = want_progress
        self.result: Any = None
        self.error: str = ""

    def _emit_progress(self, *raw, **kw) -> None:
        """兼容各后端的进度回调签名：progress(cur, tot, phase, msg) / progress(pct, text)。"""
        pct, text = 0, ""
        nums = [x for x in raw if isinstance(x, (int, float))]
        strs = [x for x in raw if isinstance(x, str)]
        if len(nums) >= 2 and nums[1]:
            pct = int(max(0, min(100, nums[0] / nums[1] * 100)))
        elif nums:
            pct = int(max(0, min(100, nums[0] if nums[0] <= 100 else nums[0])))
        if strs:
            text = " ".join(s for s in strs if s)
        try:
            self.progress.emit(pct, text[:200])
        except RuntimeError:
            pass

    def run(self) -> None:  # noqa: D401
        kwargs = dict(self._kwargs)
        try:
            if self._want_progress:
                params = inspect.signature(self._fn).parameters
                for key in ("progress", "on_progress", "cb", "log"):
                    if key in params:
                        kwargs[key] = self._emit_progress if key != "log" else self._log
                        break
            self.result = self._fn(*self._args, **kwargs)
            self.done.emit(self.result)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            self.failed.emit(traceback.format_exc(limit=6))

    def _log(self, msg: str) -> None:
        try:
            self.progress.emit(-1, str(msg)[:200])
        except RuntimeError:
            pass


class TaskCenter(QObject):
    """持有运行中的 Task 引用（防止被 GC），并提供计数供状态栏显示。"""

    changed = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()
        self._running: Set[Task] = set()

    def start(self, task: Task) -> Task:
        self._running.add(task)

        def _finish(*_a) -> None:
            self._running.discard(task)
            self.changed.emit(len(self._running))

        task.finished.connect(_finish)
        task.finished.connect(task.deleteLater)
        self.changed.emit(len(self._running))
        task.start()
        return task

    @property
    def running(self) -> int:
        return len(self._running)

    def stop_all(self) -> None:
        for t in list(self._running):
            try:
                t.requestInterruption()
            except Exception:
                pass


CENTER = TaskCenter()
