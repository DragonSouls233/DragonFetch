# -*- coding: utf-8 -*-
"""主窗口：左侧导航 + 页面栈 + 状态栏 + 全局提示。

一个进程、一个窗口承载全部模块；每个模块是一页（独立的 QWidget 子类），
后端通过 ``launcher.core.backend`` 在进程内直接调用。
"""
from __future__ import annotations

from typing import Callable, Dict

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMainWindow, QPushButton,
    QSizePolicy, QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from ..core import backend, paths
from ..core.version import APP_VERSION
from ..core.logbus import LOG
from ..core.settings import settings
from ..core.tasks import CENTER
from . import theme
from .widgets import Toast

# key, 图标, 名称, 分组
NAV_ITEMS = [
    ("home", "◎", "总览", "工作台"),
    ("tangxin", "◈", "糖心 Vlog", "媒体库"),
    ("huangguo", "◇", "黄果短剧", "媒体库"),
    ("jiuse", "◉", "九色视频", "媒体库"),
    ("sites", "⌘", "多站点", "下载器"),
    ("madouqu", "◍", "麻豆磁力", "下载器"),
    ("yttiktok", "▶", "映取 · YT/TikTok", "下载器"),
    ("nav", "✦", "导航页 · 黄视频", "下载器"),
    ("library", "▤", "库管理", "系统"),
    ("settings", "⚙", "设置", "系统"),
]

_FACTORIES: Dict[str, Callable[[], QWidget]] = {}


def _factory(key: str) -> Callable[[], QWidget]:
    if not _FACTORIES:
        from .pages.home import HomePage
        from .pages.tangxin import TangxinPage
        from .pages.huangguo import HuangguoPage
        from .pages.jiuse import JiusePage
        from .pages.sites import SitesPage
        from .pages.madouqu import MadouquPage
        from .pages.yttiktok import YtTiktokPage
        from .pages.navpage import NavPage
        from .pages.library import LibraryPage
        from .pages.settings_page import SettingsPage
        _FACTORIES.update({
            "home": HomePage, "tangxin": TangxinPage, "huangguo": HuangguoPage,
            "jiuse": JiusePage, "sites": SitesPage, "madouqu": MadouquPage,
            "yttiktok": YtTiktokPage, "nav": NavPage,
            "library": LibraryPage, "settings": SettingsPage,
        })
    return _FACTORIES[key]


class Sidebar(QWidget):
    selected = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("Sidebar")
        self.setFixedWidth(228)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 18, 14, 14)
        lay.setSpacing(2)

        brand = QLabel("DragonFetch")
        brand.setObjectName("BrandTitle")
        sub = QLabel("统一下载工作台")
        sub.setObjectName("BrandSub")
        lay.addWidget(brand)
        lay.addWidget(sub)
        lay.addSpacing(10)

        self._buttons: Dict[str, QPushButton] = {}
        last_group = None
        for key, icon, label, group in NAV_ITEMS:
            if group != last_group:
                sec = QLabel(group.upper())
                sec.setObjectName("NavSection")
                lay.addWidget(sec)
                last_group = group
            btn = QPushButton(f"{icon}   {label}")
            btn.setObjectName("NavBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=key: self._pick(k))
            lay.addWidget(btn)
            self._buttons[key] = btn

        lay.addStretch(1)
        self.lbl_status = QLabel("代理：待检测")
        self.lbl_status.setObjectName("Version")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)
        ver = QLabel(f"v{APP_VERSION} · DragonFetch")
        ver.setObjectName("Version")
        lay.addWidget(ver)

    def _pick(self, key: str) -> None:
        self.set_active(key)
        self.selected.emit(key)

    def set_active(self, key: str) -> None:
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)

    def set_status(self, text: str) -> None:
        self.lbl_status.setText(text)


class LogDialog(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.setWindowTitle("运行日志")
        self.resize(820, 460)
        lay = QVBoxLayout(self)
        from .widgets import LogPanel
        self.panel = LogPanel()
        lay.addWidget(self.panel)
        LOG.message.connect(self.panel.append)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle(f"DragonFetch · 统一下载工作台  v{APP_VERSION}")
        self.setMinimumSize(1180, 760)
        s = settings()
        self.resize(int(s.get("window.width", 1440)), int(s.get("window.height", 900)))

        central = QWidget()
        central.setObjectName("Root")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = Sidebar()
        self.sidebar.selected.connect(self.switch_page)
        root.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._pages: Dict[str, QWidget] = {}
        self._log_dialog: LogDialog | None = None
        self.toast = Toast(central)

        self._build_statusbar()
        LOG.message.connect(self._on_log)
        CENTER.changed.connect(self._on_tasks_changed)

        self.switch_page(s.get("window.last_page", "home") or "home")
        self._refresh_side_status()

    # ------------------------------------------------------------ 页面
    def switch_page(self, key: str) -> None:
        if key not in dict((k, 1) for k, *_ in NAV_ITEMS):
            key = "home"
        page = self._pages.get(key)
        if page is None:
            try:
                page = _factory(key)()
            except Exception as exc:  # noqa: BLE001
                page = self._error_page(key, exc)
            self._pages[key] = page
            self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)
        self.sidebar.set_active(key)
        if hasattr(page, "on_show"):
            try:
                page.on_show()
            except Exception as exc:  # noqa: BLE001
                LOG.error(f"页面 {key} 刷新失败：{exc}")
        settings().set("window.last_page", key)
        settings().save()

    def _error_page(self, key: str, exc: Exception) -> QWidget:
        from .widgets import Card, EmptyState
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(26, 24, 26, 24)
        card = Card()
        inner = QVBoxLayout(card)
        inner.addWidget(EmptyState(f"{key} 页面加载失败", str(exc), "⚠"))
        lay.addWidget(card)
        return w

    # ------------------------------------------------------------ 状态栏
    def _build_statusbar(self) -> None:
        bar = QStatusBar()
        bar.setSizeGripEnabled(False)
        self.setStatusBar(bar)

        self.lbl_proxy = QLabel("代理：-")
        self.lbl_ytdlp = QLabel("yt-dlp：-")
        self.lbl_tasks = QLabel("任务：0")
        self.lbl_root = QLabel(f"数据：{paths.rel(paths.DATA)}")
        self.btn_log = QPushButton("日志")
        self.btn_log.setObjectName("Ghost")
        self.btn_log.setFixedHeight(22)
        self.btn_log.clicked.connect(self._open_log)

        bar.addWidget(self.lbl_proxy)
        bar.addWidget(self.lbl_ytdlp)
        bar.addWidget(self.lbl_tasks)
        bar.addPermanentWidget(self.btn_log)
        bar.addPermanentWidget(self.lbl_root)

    def _refresh_side_status(self) -> None:
        s = settings()
        if s.get("proxy.enabled", True):
            self.lbl_proxy.setText(f"代理：{s.proxy_url or '未填写'}")
            self.sidebar.set_status(f"导航页代理：{s.proxy_url or '未填写'}")
        else:
            self.lbl_proxy.setText("代理：关闭（直连）")
            self.sidebar.set_status("导航页代理：关闭")
        try:
            self.lbl_ytdlp.setText(f"yt-dlp：{backend.ytdlp_version()}")
        except Exception:
            self.lbl_ytdlp.setText("yt-dlp：-")

    def _on_tasks_changed(self, count: int) -> None:
        self.lbl_tasks.setText(f"任务：{count}")

    def _on_log(self, level: str, text: str) -> None:
        if level in ("err", "warn"):
            self.notify(text, "err" if level == "err" else "warn")

    def _open_log(self) -> None:
        if self._log_dialog is None:
            self._log_dialog = LogDialog(self)
        self._log_dialog.show()
        self._log_dialog.raise_()

    # ------------------------------------------------------------ 提示
    def notify(self, text: str, kind: str = "info") -> None:
        self.toast.show_message(text, kind)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "toast"):
            self.toast.reposition()

    def closeEvent(self, event):  # noqa: N802
        s = settings()
        s.set("window.width", self.width())
        s.set("window.height", self.height())
        s.save()
        CENTER.stop_all()
        super().closeEvent(event)
