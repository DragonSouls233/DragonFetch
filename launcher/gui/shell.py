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
from ..core.settings import WINDOW_DEFAULT_H, WINDOW_DEFAULT_W, settings
from ..core.tasks import CENTER, Task
from . import theme
from .widgets import Toast

# 基础导航（站点条目在 _build_nav 里按模块动态插入）
BASE_NAV = [
    ("home", "◎", "总览", "工作台"),
    ("tangxin", "◈", "糖心 Vlog", "媒体库"),
    ("huangguo", "◇", "黄果短剧", "媒体库"),
    ("duanju", "◐", "短剧平台", "媒体库"),
    ("jiuse", "◉", "九色视频", "媒体库"),
    ("madouqu", "◍", "麻豆磁力", "下载器"),
    ("yttiktok", "▶", "映取 · YT/TikTok", "下载器"),
    ("nav", "✦", "导航页 · 黄视频", "下载器"),
    # 浏览器独立成入口：媒体库各模块不再内嵌 Chromium（2026-10-29）
    ("browser", "⊞", "浏览器", "工作台"),
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
        from .pages.duanju import DuanjuPage
        from .pages.jiuse import JiusePage
        from .pages.madouqu import MadouquPage
        from .pages.yttiktok import YtTiktokPage
        from .pages.navpage import NavPage
        from .pages.navsync import NavSyncPage
        from .pages.library import LibraryPage
        from .pages.settings_page import SettingsPage
        from .pages.browser_page import BrowserPage
        _FACTORIES.update({
            "home": HomePage, "tangxin": TangxinPage, "huangguo": HuangguoPage,
            "duanju": DuanjuPage,
            "jiuse": JiusePage, "madouqu": MadouquPage,
            "yttiktok": YtTiktokPage, "nav": NavPage, "navsync": NavSyncPage,
            "library": LibraryPage, "settings": SettingsPage,
            "browser": BrowserPage,
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


def _startup_geometry(s) -> tuple:
    """算出启动窗口的 (宽, 高, x, y)。

    基准是 1080P（1920×1080），但不能硬塞：笔记本 1366×768、或者 2K 屏开了
    150% 缩放（可用逻辑像素只有 1707×960）都装不下 1920×1080，硬 resize 会让
    窗口顶出屏幕，右侧内置浏览器和底部状态栏直接点不到。所以统一按屏幕的
    ``availableGeometry``（已排除任务栏）收边，再居中摆。

    返回的 x/y 为 None 表示没有屏幕信息，交给 Qt 自己放。
    """
    try:
        w = int(s.get("window.width", WINDOW_DEFAULT_W) or WINDOW_DEFAULT_W)
        h = int(s.get("window.height", WINDOW_DEFAULT_H) or WINDOW_DEFAULT_H)
    except Exception:  # noqa: BLE001 - 配置被手改成字符串也不能挡住开界面
        w, h = WINDOW_DEFAULT_W, WINDOW_DEFAULT_H
    # 保底：存盘尺寸不足基准的 80%（<1536×864）时按 1080P 重来。
    # 两个来源都会把尺寸带偏：一是老配置里存着 1440×900 时代的尺寸，只改
    # DEFAULTS 对已存盘的键无效；二是测试/截图脚本构造完主窗口会顺手把
    # resize 过的尺寸写回真实配置（已实测踩到，1440/1500 反复冒出来）。
    # 用户在 80% 以上范围内自己调，仍然照旧记住。
    if w < WINDOW_DEFAULT_W * 0.8 or h < WINDOW_DEFAULT_H * 0.8:
        w, h = WINDOW_DEFAULT_W, WINDOW_DEFAULT_H
    scr = QApplication.primaryScreen()
    if scr is None:
        return w, h, None, None
    avail = scr.availableGeometry()
    # 高度再留 40px 给标题栏和边缘，宽度留 16px 防贴边
    max_w = max(640, avail.width() - 16)
    max_h = max(480, avail.height() - 40)
    w, h = min(w, max_w), min(h, max_h)
    x = avail.x() + (avail.width() - w) // 2
    y = avail.y() + (avail.height() - h) // 2
    return w, h, x, y


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setObjectName("Root")
        self.setWindowTitle(f"DragonFetch · 统一下载工作台  v{APP_VERSION}")
        s = settings()
        w, h, x, y = _startup_geometry(s)
        # 最小尺寸跟着屏幕走：屏幕比 1180×760 还小的时候不能再拿它当地板，
        # 否则窗口的下半截永远在屏幕外。
        self.setMinimumSize(min(1180, w), min(760, h))
        self.resize(w, h)
        if x is not None:
            self.move(x, y)

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
        self._ytdlp_version_task: Task | None = None
        self.toast = Toast(central)

        self._build_statusbar()
        LOG.message.connect(self._on_log)
        CENTER.changed.connect(self._on_tasks_changed)

        self.switch_page(s.get("window.last_page", "home") or "home")
        self._refresh_side_status()
        # 先让主窗口完成构造并交还事件循环，再在后台启动 onefile 版 yt-dlp。
        # 它的 ``--version`` 首次执行约 3.7 秒，绝不能挡住首帧。
        QTimer.singleShot(0, self._start_ytdlp_version_check)

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
        # 内置浏览器懒加载：只有真正切到这一页才创建 WebEngineView。
        # 20 多个页面各开一个 Chromium 渲染进程会吃掉几 GB 内存，所以宁可
        # 每次进来晚 0.3 秒，也不在启动时全建出来。
        bp = getattr(page, "browser_panel", None)
        if bp is not None:
            try:
                bp.ensure_loaded()
            except Exception as exc:  # noqa: BLE001
                LOG.error(f"页面 {key} 内置浏览器启动失败：{exc}")
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
        self.lbl_ytdlp = QLabel("yt-dlp：检测中…")
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
        self.lbl_ytdlp.setText(
            f"yt-dlp：{backend.cached_ytdlp_version('检测中…')}")

    def _start_ytdlp_version_check(self) -> None:
        """在后台查询 yt-dlp 版本并回填状态栏。"""
        cached = backend.cached_ytdlp_version("")
        if cached:
            self.lbl_ytdlp.setText(f"yt-dlp：{cached}")
            return
        if self._ytdlp_version_task is not None and self._ytdlp_version_task.isRunning():
            return
        self.lbl_ytdlp.setText("yt-dlp：检测中…")
        task = Task(backend.ytdlp_version)
        task.done.connect(self._on_ytdlp_version_ready)
        task.failed.connect(lambda _tb: self.lbl_ytdlp.setText("yt-dlp：不可用"))
        self._ytdlp_version_task = task
        CENTER.start(task)

    def _on_ytdlp_version_ready(self, version: object) -> None:
        self.lbl_ytdlp.setText(f"yt-dlp：{str(version or '未知')}")

    def refresh_side_status(self) -> None:
        """对外（各页面同步完地址后）刷新状态栏与侧栏摘要。"""
        self._refresh_side_status()

    # ------------------------------------------------------------ 地址同步
    def auto_navsync(self) -> None:
        """启动后台地址同步：不阻塞界面，完成后只在提示与状态栏报结果。"""
        if self._navsync_task is not None and self._navsync_task.isRunning():
            return
        self.sidebar.set_status("地址同步中…")

        def _run() -> dict:
            """记录同步前域名，供主线程明确展示自动改写痕迹。"""
            try:
                before = str(backend.huangguo_site_meta().get("base_url") or "")
            except Exception:  # noqa: BLE001 - 地址同步本身仍应继续
                before = ""
            result = dict(navsync.sync_all() or {})
            result["huangguo_url_before"] = before
            return result

        task = Task(_run)
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

        # 地址同步是有意保留的启动行为；若它替用户切换了黄果域名，则同时写入
        # DragonFetch 日志并给非阻塞 Toast，不能只在 hgcore 私有日志里静默发生。
        before = str(res.get("huangguo_url_before") or "").rstrip("/")
        hg = (res.get("targets") or {}).get("huangguo") or {}
        after = str(hg.get("url") or "").rstrip("/")
        if before and after and before != after and hg.get("ok"):
            text = f"黄果域名已自动切换：{before} → {after}"
            LOG.info(text)
            self.notify(text, "warn")

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
