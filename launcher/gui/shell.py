# -*- coding: utf-8 -*-
"""主窗口：左侧导航 + 页面栈 + 状态栏 + 全局提示。

一个进程、一个窗口承载全部模块；每个模块是一页（独立的 QWidget 子类），
后端通过 ``launcher.core.backend`` 在进程内直接调用。
"""
from __future__ import annotations

from typing import Callable, Dict

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QStatusBar,
    QVBoxLayout, QWidget,
)

from ..core import backend, navsync, paths
from ..core.version import APP_VERSION
from ..core.logbus import LOG
from ..core.settings import settings
from ..core.tasks import CENTER, Task
from . import theme
from .widgets import Toast

# 基础导航（站点条目在 _build_nav 里按模块动态插入）
BASE_NAV = [
    ("home", "◎", "总览", "工作台"),
    ("tangxin", "◈", "糖心 Vlog", "媒体库"),
    ("huangguo", "◇", "黄果短剧", "媒体库"),
    ("jiuse", "◉", "九色视频", "媒体库"),
    ("madouqu", "◍", "麻豆磁力", "下载器"),
    ("yttiktok", "▶", "映取 · YT/TikTok", "下载器"),
    ("nav", "✦", "导航页 · 黄视频", "下载器"),
    ("navsync", "⇄", "地址同步", "系统"),
    ("library", "▤", "库管理", "系统"),
    ("settings", "⚙", "设置", "系统"),
]

SITE_GROUP = "站点"


def _site_nav() -> list:
    """一个域名一个模块，所以侧栏里也是一个域名一个入口（不共用一个页面）。"""
    try:
        return [(f"site:{s['key']}", "◦", s["label"], SITE_GROUP)
                for s in backend.site_list()]
    except Exception:  # noqa: BLE001 - 后端缺失时界面仍要能开
        return []


def _build_nav() -> list:
    """在「媒体库」之后插入全部站点入口，其余顺序不变。"""
    out: list = []
    for item in BASE_NAV:
        if item[0] == "madouqu":
            out.extend(_site_nav())
        out.append(item)
    return out


NAV_ITEMS = _build_nav()

_FACTORIES: Dict[str, Callable[[], QWidget]] = {}


def _factory(key: str) -> Callable[[], QWidget]:
    if key.startswith("site:"):
        from .pages.site_page import SitePage

        site_key = key.split(":", 1)[1]
        return lambda: SitePage(site_key)
    if not _FACTORIES:
        from .pages.home import HomePage
        from .pages.tangxin import TangxinPage
        from .pages.huangguo import HuangguoPage
        from .pages.jiuse import JiusePage
        from .pages.madouqu import MadouquPage
        from .pages.yttiktok import YtTiktokPage
        from .pages.navpage import NavPage
        from .pages.navsync import NavSyncPage
        from .pages.library import LibraryPage
        from .pages.settings_page import SettingsPage
        _FACTORIES.update({
            "home": HomePage, "tangxin": TangxinPage, "huangguo": HuangguoPage,
            "jiuse": JiusePage, "madouqu": MadouquPage,
            "yttiktok": YtTiktokPage, "nav": NavPage, "navsync": NavSyncPage,
            "library": LibraryPage, "settings": SettingsPage,
        })
    return _FACTORIES[key]


class Sidebar(QWidget):
    selected = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("Sidebar")
        self.setFixedWidth(228)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 18, 14, 14)
        outer.setSpacing(2)

        brand = QLabel("DragonFetch")
        brand.setObjectName("BrandTitle")
        sub = QLabel("统一下载工作台")
        sub.setObjectName("BrandSub")
        outer.addWidget(brand)
        outer.addWidget(sub)
        outer.addSpacing(10)

        # 站点是"一域名一入口"，条目会比以前多，必须可滚动，否则小窗口会被截断
        self._scroll = QScrollArea()
        self._scroll.setObjectName("NavScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("NavInner")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        self._buttons: Dict[str, QPushButton] = {}
        last_group = None
        for key, icon, label, group in NAV_ITEMS:
            if group != last_group:
                sec = QLabel(group.upper() if group != SITE_GROUP else group)
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
        self._scroll.setWidget(inner)
        outer.addWidget(self._scroll, 1)

        self.lbl_status = QLabel("代理：待检测")
        self.lbl_status.setObjectName("Version")
        self.lbl_status.setWordWrap(True)
        outer.addWidget(self.lbl_status)
        ver = QLabel(f"v{APP_VERSION} · DragonFetch")
        ver.setObjectName("Version")
        outer.addWidget(ver)

    def _pick(self, key: str) -> None:
        self.set_active(key)
        self.selected.emit(key)

    def set_active(self, key: str) -> None:
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
        btn = self._buttons.get(key)
        if btn is not None:
            self._scroll.ensureWidgetVisible(btn, 0, 40)

    def set_status(self, text: str) -> None:
        self.lbl_status.setText(text)


class LogDialog(QWidget):
    """运行日志窗（**非模态**）。

    非模态是硬要求：用户开着日志要能继续点主界面的下载/预览。
    所以这里不 exec()、不 setModal(True)，只是个普通顶层窗口。
    """

    def __init__(self, parent=None):
        # 用 Window 而不是 Dialog：Dialog 在部分平台会跟随父窗口置顶/模态化
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("运行日志")
        s = settings()
        self.resize(int(s.get("logwin.width", 900) or 900),
                    int(s.get("logwin.height", 480) or 480))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        tip = QLabel("日志实时追加；本窗口不阻塞主界面，开着也能继续下载/预览。")
        tip.setObjectName("Muted")
        bar.addWidget(tip, 1)
        btn_save = QPushButton("另存为…")
        btn_save.setObjectName("Ghost")
        btn_save.clicked.connect(self._save_as)
        chk_top = QCheckBox("置顶")
        chk_top.setChecked(bool(s.get("logwin.on_top", False)))
        chk_top.toggled.connect(self._set_on_top)
        bar.addWidget(chk_top)
        bar.addWidget(btn_save)
        lay.addLayout(bar)

        from .widgets import LogPanel
        self.panel = LogPanel()
        lay.addWidget(self.panel, 1)
        LOG.message.connect(self.panel.append)
        self._set_on_top(chk_top.isChecked())

    def _set_on_top(self, on: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(on))
        self.show()          # 改 flag 后必须 show 才生效
        settings().set("logwin.on_top", bool(on))

    def _save_as(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        from ..core import paths

        out, _ = QFileDialog.getSaveFileName(
            self, "保存日志", str(paths.LOGS / "ui-log.txt"), "文本 (*.txt)")
        if not out:
            return
        try:
            # LogPanel 内的文本框是 .view（QPlainTextEdit）
            text = self.panel.view.toPlainText() if hasattr(self.panel, "view") else ""
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass

    def closeEvent(self, event):  # noqa: N802
        s = settings()
        s.set("logwin.width", self.width())
        s.set("logwin.height", self.height())
        s.save()
        super().closeEvent(event)


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
        self._navsync_task: Task | None = None
        self.toast = Toast(central)

        self._build_statusbar()
        LOG.message.connect(self._on_log)
        CENTER.changed.connect(self._on_tasks_changed)

        self.switch_page(s.get("window.last_page", "home") or "home")
        self._refresh_side_status()

        # 每次打开都同步一次地址并做直连校验：糖心/黄果/麻豆/九色四个模块
        # 的域名靠这一步保鲜，直连能过就把它们的代理开关关掉。
        if navsync.auto_on_start():
            QTimer.singleShot(2500, self.auto_navsync)

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
            self.sidebar.set_status(navsync.last_run_text())
        else:
            self.lbl_proxy.setText("代理：关闭（直连）")
            self.sidebar.set_status("代理：关闭 · " + navsync.last_run_text())
        try:
            self.lbl_ytdlp.setText(f"yt-dlp：{backend.ytdlp_version()}")
        except Exception:
            self.lbl_ytdlp.setText("yt-dlp：-")

    def refresh_side_status(self) -> None:
        """对外（各页面同步完地址后）刷新状态栏与侧栏摘要。"""
        self._refresh_side_status()

    # ------------------------------------------------------------ 地址同步
    def auto_navsync(self) -> None:
        """启动后台地址同步：不阻塞界面，完成后只在提示与状态栏报结果。"""
        if self._navsync_task is not None and self._navsync_task.isRunning():
            return
        self.sidebar.set_status("地址同步中…")
        task = Task(navsync.sync_all)
        task.done.connect(self._on_navsync_done)
        task.failed.connect(self._on_navsync_failed)
        self._navsync_task = task
        CENTER.start(task)

    def _on_navsync_done(self, res: dict) -> None:
        ok = int(res.get("ok") or 0)
        total = int(res.get("total") or 0)
        direct = int(res.get("direct") or 0)
        self._refresh_side_status()
        if ok:
            self.notify(f"地址同步完成：{ok}/{total} 可用（直连 {direct} 个）",
                        "ok" if direct else "warn")
        else:
            self.notify("地址同步失败：四个模块都没拿到可用地址，已沿用上次配置",
                        "warn")
        page = self._pages.get("navsync")
        if page is not None and hasattr(page, "_fill"):
            try:
                page._fill()  # noqa: SLF001 - 页面刷新态就是它的职责
            except Exception:  # noqa: BLE001
                pass

    def _on_navsync_failed(self, tb: str) -> None:
        self.sidebar.set_status(navsync.last_run_text())
        LOG.warn(f"地址同步异常：{(tb or '').splitlines()[-1][:120]}")

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
