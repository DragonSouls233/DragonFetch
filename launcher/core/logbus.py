# -*- coding: utf-8 -*-
"""前端日志总线：一条信号给 UI，一份落盘文件（logs/launcher-YYYYMMDD.log）。"""
from __future__ import annotations

import datetime as _dt
import logging

from PyQt6.QtCore import QObject, pyqtSignal

from . import paths


class LogBus(QObject):
    message = pyqtSignal(str, str)   # (level, text)

    def __init__(self) -> None:
        super().__init__()
        paths.LOGS.mkdir(parents=True, exist_ok=True)
        self._file_logger = logging.getLogger("dragonfetch")
        if not self._file_logger.handlers:
            day = _dt.date.today().strftime("%Y%m%d")
            handler = logging.FileHandler(paths.LOGS / f"launcher-{day}.log",
                                          encoding="utf-8")
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s"))
            self._file_logger.addHandler(handler)
            self._file_logger.setLevel(logging.INFO)

    def emit_log(self, level: str, text: str) -> None:
        text = str(text)
        self._file_logger.log(
            {"err": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO),
            text)
        self.message.emit(level, text)

    def info(self, text: str) -> None:
        self.emit_log("info", text)

    def warn(self, text: str) -> None:
        self.emit_log("warn", text)

    def error(self, text: str) -> None:
        self.emit_log("err", text)


LOG = LogBus()
