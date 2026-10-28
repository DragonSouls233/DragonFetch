# -*- coding: utf-8 -*-
"""通用 UI 组件：卡片、页头、统计卡、空态、提示条、日志面板、表格工具。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Sequence

from PyQt6.QtCore import QObject, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
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


# ------------------------------------------------------------------ 封面缩略
class CoverLoader(QObject):
    """列表封面异步加载器（线程池 + requests，绝不阻塞 UI）。

    为什么要单独一个类：九色列表 29 条、糖心演员云 600+ 条，同步拉图会把界面
    卡住十几秒；而单靠 QNetworkAccessManager 又受"同一域名 6 条连接"限制
    （实测 4 条 lane 仍要 38 秒才把 29 张拉完）。这里改用 8 线程的 requests
    池：单张实测 1.3~5.6s，一批 29 张 5~8 秒就齐了，且走系统代理（图床多在墙外）。
    """

    loaded = pyqtSignal(str, QPixmap)     # url, 已缩放好的 pixmap

    #: 同时拉图的线程数。再大对图床也不礼貌，收益很低
    WORKERS = 10

    def __init__(self, parent=None, size: QSize | None = None, lanes: int = 4):
        super().__init__(parent)
        self._size = size or QSize(128, 72)
        self._cache: dict[str, QPixmap] = {}
        self._pending: set[str] = set()
        self._pool = None
        self._referer = ""

    # ------------------------------------------------------------ 对外
    @property
    def icon_size(self) -> QSize:
        return self._size

    def set_referer(self, referer: str) -> None:
        """图床多防盗链，带上站点 Referer 才给图。"""
        self._referer = (referer or "").strip()

    def cached(self, url: str) -> QPixmap | None:
        return self._cache.get(url or "")

    def load(self, url: str) -> QPixmap | None:
        """返回已缓存的图；没缓存就丢进线程池，返回 None。"""
        if not url:
            return None
        pm = self._cache.get(url)
        if pm is not None:
            return pm
        if url in self._pending:
            return None
        self._pending.add(url)
        if self._pool is None:
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(max_workers=self.WORKERS,
                                            thread_name_prefix="cover")
        self._pool.submit(self._fetch, url)
        return None

    def clear(self) -> None:
        self._cache.clear()
        self._pending.clear()

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _cache_file(url: str) -> Path:
        """封面磁盘缓存路径（同一张图第二次打开列表就不再压图床）。"""
        import hashlib

        root = os.environ.get("DF_DATA_ROOT") or ""
        if root:
            base = Path(root)
        else:
            try:
                from ..core import paths as _paths

                base = _paths.DATA
            except Exception:  # noqa: BLE001
                base = Path(".")
        key = hashlib.md5(url.encode("utf-8", "ignore")).hexdigest()[:20]
        return base / "app" / "cover_cache" / f"{key}.jpg"

    def _fetch(self, url: str) -> None:
        data = b""
        cf = self._cache_file(url)
        try:
            if cf.is_file() and cf.stat().st_size > 512:
                data = cf.read_bytes()
        except OSError:
            data = b""
        if not data:
            try:
                import requests
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/122.0.0.0 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                }
                if self._referer:
                    headers["Referer"] = self._referer
                s = requests.Session()
                s.trust_env = True          # 图床多在墙外，跟随系统代理
                r = s.get(url, headers=headers, timeout=12, verify=False)
                if r.status_code == 200:
                    data = r.content or b""
            except Exception:  # noqa: BLE001
                data = b""
            if data:
                try:
                    cf.parent.mkdir(parents=True, exist_ok=True)
                    cf.write_bytes(data)
                except OSError:
                    pass
        self._pending.discard(url)
        if not data:
            return
        pm = QPixmap()
        if not pm.loadFromData(data):
            return
        scaled = pm.scaled(self._size,
                           Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
        self._cache[url] = scaled
        self.loaded.emit(url, scaled)    # 跨线程信号 → Qt 自动排队回主线程
