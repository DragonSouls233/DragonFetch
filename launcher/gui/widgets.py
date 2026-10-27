# -*- coding: utf-8 -*-
"""通用 UI 组件：卡片、页头、统计卡、空态、提示条、日志面板、表格工具。"""
from __future__ import annotations

import os
from typing import Iterable, List, Sequence

from PyQt6.QtCore import QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from . import theme


# ------------------------------------------------------------------ 基础容器
class Card(QFrame):
    def __init__(self, parent: QWidget | None = None, flat: bool = False):
        super().__init__(parent)
        self.setObjectName("CardFlat" if flat else "Card")


class HLine(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("HLine")
        self.setFixedHeight(1)


class Badge(QLabel):
    """小圆角标签，用于状态（直连/代理/失败…）。"""

    TONES = {"": "Badge", "ok": "BadgeOk", "warn": "BadgeWarn", "err": "BadgeErr"}

    def __init__(self, text: str = "", tone: str = ""):
        super().__init__(text)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        self.setObjectName(self.TONES.get(tone, "Badge"))
        self.style().unpolish(self)
        self.style().polish(self)


class Chip(QPushButton):
    def __init__(self, text: str, checkable: bool = True):
        super().__init__(text)
        self.setObjectName("Chip")
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


# ------------------------------------------------------------------ 页头
class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        texts = QVBoxLayout()
        texts.setSpacing(3)
        self.title = QLabel(title)
        self.title.setObjectName("H1")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("Sub")
        self.subtitle.setWordWrap(True)
        texts.addWidget(self.title)
        if subtitle:
            texts.addWidget(self.subtitle)
        row.addLayout(texts, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        row.addLayout(self.actions)
        root.addLayout(row)

    def add_action(self, widget: QWidget) -> QWidget:
        self.actions.addWidget(widget)
        return widget

    def add_stretch(self) -> None:
        self.actions.addStretch(1)


class SectionTitle(QWidget):
    def __init__(self, text: str, right: QWidget | None = None):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(text)
        lbl.setObjectName("H2")
        lay.addWidget(lbl)
        lay.addStretch(1)
        if right is not None:
            lay.addWidget(right)


# ------------------------------------------------------------------ 统计卡
class StatCard(Card):
    def __init__(self, label: str, value: str = "-", hint: str = ""):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(4)
        self.lbl_label = QLabel(label)
        self.lbl_label.setObjectName("StatLabel")
        self.lbl_value = QLabel(value)
        self.lbl_value.setObjectName("StatValue")
        self.lbl_hint = QLabel(hint)
        self.lbl_hint.setObjectName("StatHint")
        lay.addWidget(self.lbl_label)
        lay.addWidget(self.lbl_value)
        lay.addWidget(self.lbl_hint)
        self.setMinimumHeight(104)

    def set_value(self, value) -> None:
        self.lbl_value.setText(str(value))

    def set_hint(self, hint: str) -> None:
        self.lbl_hint.setText(hint)


# ------------------------------------------------------------------ 输入 / 空态
class SearchInput(QLineEdit):
    def __init__(self, placeholder: str = "搜索…", width: int = 240):
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.setFixedWidth(width)


class EmptyState(QWidget):
    def __init__(self, title: str, desc: str = "", icon: str = "◍"):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 32, 24, 32)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon = QLabel(icon)
        f = QFont()
        f.setPointSize(30)
        self.icon.setFont(f)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon.setStyleSheet("color: #4a5468;")
        self.title = QLabel(title)
        self.title.setObjectName("H2")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.desc = QLabel(desc)
        self.desc.setObjectName("Muted")
        self.desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.desc.setWordWrap(True)
        lay.addWidget(self.icon)
        lay.addWidget(self.title)
        if desc:
            lay.addWidget(self.desc)

    def set_text(self, title: str, desc: str = "") -> None:
        self.title.setText(title)
        self.desc.setText(desc)


# ------------------------------------------------------------------ 提示条
class Toast(QFrame):
    """右下角浮动提示，不阻塞操作。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        lay.addWidget(self.label)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, kind: str = "info", msec: int = 4200) -> None:
        name = {"info": "Toast", "ok": "ToastOk", "warn": "ToastWarn",
                "err": "ToastErr"}.get(kind, "Toast")
        self.setObjectName(name)
        self.label.setText(text)
        self.style().unpolish(self)
        self.style().polish(self)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._timer.start(msec)

    def reposition(self) -> None:
        if not self.parentWidget():
            return
        p = self.parentWidget().rect()
        self.adjustSize()
        w = min(self.width(), max(320, p.width() - 80))
        self.resize(w, self.sizeHint().height())
        self.move(p.width() - self.width() - 24, p.height() - self.height() - 24)


# ------------------------------------------------------------------ 日志
class LogPanel(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        row = QHBoxLayout()
        title = QLabel("运行日志")
        title.setObjectName("H2")
        row.addWidget(title)
        row.addStretch(1)
        btn_clear = QPushButton("清空")
        btn_clear.setObjectName("Ghost")
        btn_clear.clicked.connect(lambda: self.view.clear())
        row.addWidget(btn_clear)
        lay.addLayout(row)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(2000)
        self.view.setMinimumHeight(130)
        lay.addWidget(self.view)

    def append(self, level: str, text: str) -> None:
        prefix = {"err": "✕", "warn": "!", "ok": "✓"}.get(level, "·")
        self.view.appendPlainText(f"{prefix} {text}")


# ------------------------------------------------------------------ 表格
def make_table(headers: Sequence[str], widths: Sequence[int] | None = None,
               stretch_last: bool = True) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(list(headers))
    t.verticalHeader().setVisible(False)
    t.setShowGrid(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setAlternatingRowColors(False)
    hh = t.horizontalHeader()
    hh.setHighlightSections(False)
    for i in range(len(headers)):
        hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        if widths and i < len(widths):
            t.setColumnWidth(i, widths[i])
    if stretch_last:
        hh.setStretchLastSection(True)
    t.verticalHeader().setDefaultSectionSize(34)
    return t


def cell(text: str, user=None) -> QTableWidgetItem:
    item = QTableWidgetItem(str(text))
    if user is not None:
        item.setData(Qt.ItemDataRole.UserRole, user)
    return item


def open_path(path: str) -> bool:
    if not path:
        return False
    p = os.path.abspath(path)
    if os.path.exists(p):
        return QDesktopServices.openUrl(QUrl.fromLocalFile(p))
    return False
