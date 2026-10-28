# -*- coding: utf-8 -*-
"""九色 · 91PORNY：从地址页（pages.dev）拿最新线路，**全程直连**。

结构和其他导航型模块一致：
  地址页（代理/直连）→ 解析候选线路 → 逐条探测校验 → 缓存可用地址 → 打开/采集/下载

与糖心的区别：本模块按需求**不挂代理**（client 里 trust_env=False），
所以即使系统挂着代理也不会“偷偷”走代理；不通时直接给出原因。
"""
from __future__ import annotations

import os
import threading
import time

from PyQt6.QtCore import QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QSpinBox, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ...core import backend, paths
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from ..preview import PreviewPane
from ..widgets import Badge, Card, CoverLoader, HLine, make_table, open_path
from .base import Page


class _DlWorker(QThread):
    row_state = pyqtSignal(int, str, int)
    finished_all = pyqtSignal(int, int)

    def __init__(self, items, out_dir: str, referer: str, proxy: str = ""):
        super().__init__()
        self._items = list(items)
        self._out = out_dir
        self._referer = referer
        self._proxy = (proxy or "").strip()
        self._stop = threading.Event()

    def run(self) -> None:  # noqa: D401
        ok = fail = 0
        try:
            ns = backend.jiuse()
            svc = backend.ytdlp().service
        except Exception as exc:  # noqa: BLE001
            self.finished_all.emit(0, 0)
            return
        for idx, item in enumerate(self._items):
            if self._stop.is_set():
                self.row_state.emit(idx, "已停止", 0)
                break
            url, title = item["url"], item.get("title", "")
            dl_id = 0
            try:
                dl_id = ns.db.start_download(url, title)
            except Exception:
                dl_id = 0
            self.row_state.emit(idx, "解析中", 3)

            def _prog(pct=0.0, line="", _i=idx):
                self.row_state.emit(_i, str(line)[:40], int(max(0, min(100, pct))))

            try:
                path = svc.download(url, self._out, progress=_prog,
                                    proxy=self._proxy or None)
                size = 0
                try:
                    size = os.path.getsize(path) if path else 0
                except OSError:
                    size = 0
                self.row_state.emit(idx, "完成", 100)
                ok += 1
                try:
                    ns.db.finish_download(dl_id, True, path or "", size)
                except Exception:
                    pass
            except Exception as exc:  # noqa: BLE001
                self.row_state.emit(idx, f"失败：{exc}"[:44], 0)
                fail += 1
                try:
                    ns.db.finish_download(dl_id, False, error=str(exc))
                except Exception:
                    pass
        self.finished_all.emit(ok, fail)

    def stop(self) -> None:
        self._stop.set()


class JiusePage(Page):
    key = "jiuse"
    title = "九色 · 91PORNY"
    subtitle = "地址页 dizhi222.pages.dev 直连取最新线路（不走代理）→ 打开站点 / 抓列表 / 下载"

    def build(self) -> None:
        self._ns = None
        self._items: list[dict] = []
        self._worker = None
        self._last_refresh = 0.0

        btn_open = QPushButton("打开下载目录")
        btn_open.setObjectName("Ghost")
        btn_open.clicked.connect(
            lambda: open_path(settings().get("jiuse.download_dir", "")) or
            self.notify("下载目录尚未创建（下载一次后自动建立）", "warn"))
        self.header.add_action(btn_open)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_main(), "线路与采集")
        self.tabs.addTab(self._tab_records(), "检测与下载记录")
        self.tabs.currentChanged.connect(
            lambda i: self.reload_records() if i == 1 else None)
        self.preview = PreviewPane()
        self.preview.setMinimumHeight(320)
        self.attach_preview(self.tabs, self.preview)

    # ---------------------------------------------------------------- 主标签
    def _tab_main(self) -> QWidget:
        s = settings()
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        # ---- 线路
        route = Card()
        rl = QVBoxLayout(route)
        rl.setContentsMargins(16, 12, 16, 12)
        rl.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("地址页与线路")
        t.setObjectName("H2")
        head.addWidget(t)
        self.badge_mode = Badge("直连")
        self.badge_mode.set_tone("ok")
        head.addWidget(self.badge_mode)
        head.addStretch(1)
        self.badge_state = Badge("未刷新")
        head.addWidget(self.badge_state)
        rl.addLayout(head)

        row1 = QHBoxLayout()
        self.ed_nav = QLineEdit(s.get("jiuse.nav_url", "https://dizhi222.pages.dev/"))
        self.ed_nav.setPlaceholderText("地址页 URL，例如 https://dizhi222.pages.dev/")
        self.sp_limit = QSpinBox()
        self.sp_limit.setRange(1, 20)
        self.sp_limit.setValue(int(s.get("jiuse.check_limit", 8)))
        self.sp_limit.setPrefix("探测 ")
        self.btn_refresh = QPushButton("刷新最新地址")
        self.btn_refresh.setObjectName("Primary")
        self.btn_refresh.clicked.connect(self.refresh)
        row1.addWidget(QLabel("地址页"))
        row1.addWidget(self.ed_nav, 1)
        row1.addWidget(self.sp_limit)
        row1.addWidget(self.btn_refresh)
        rl.addLayout(row1)

        row2 = QHBoxLayout()
        self.cmb_addr = QComboBox()
        self.cmb_addr.setMinimumWidth(380)
        self.cmb_addr.currentIndexChanged.connect(self._addr_changed)
        self.btn_use = QPushButton("使用此地址")
        self.btn_use.setObjectName("Ghost")
        self.btn_use.clicked.connect(self.use_address)
        self.btn_site = QPushButton("打开站点")
        self.btn_site.setObjectName("Ghost")
        self.btn_site.clicked.connect(self.open_site)
        self.btn_copy = QPushButton("复制地址")
        self.btn_copy.setObjectName("Ghost")
        self.btn_copy.clicked.connect(self.copy_address)
        row2.addWidget(QLabel("线路"))
        row2.addWidget(self.cmb_addr, 1)
        row2.addWidget(self.btn_use)
        row2.addWidget(self.btn_site)
        row2.addWidget(self.btn_copy)
        rl.addLayout(row2)
        self.lbl_route = QLabel("尚未读取地址页。点「刷新最新地址」会直连 pages.dev 并逐条探测。")
        self.lbl_route.setObjectName("Muted")
        self.lbl_route.setWordWrap(True)
        rl.addWidget(self.lbl_route)
        lay.addWidget(route)

        # ---- 采集
        grab = Card()
        gl = QVBoxLayout(grab)
        gl.setContentsMargins(16, 12, 16, 12)
        gl.setSpacing(8)
        gh = QHBoxLayout()
        t2 = QLabel("站点采集")
        t2.setObjectName("H2")
        self.badge_grab = Badge("0 条")
        gh.addWidget(t2)
        gh.addWidget(self.badge_grab)
        gh.addStretch(1)
        self.btn_home = QPushButton("抓首页")
        self.btn_home.setObjectName("Ghost")
        self.btn_home.clicked.connect(lambda: self.grab_page(""))
        self.btn_grab = QPushButton("抓取此页")
        self.btn_grab.setObjectName("Primary")
        self.btn_grab.clicked.connect(lambda: self.grab_page(self.ed_page.text().strip()))
        gh.addWidget(self.btn_home)
        gh.addWidget(self.btn_grab)
        gl.addLayout(gh)

        row3 = QHBoxLayout()
        self.ed_page = QLineEdit()
        self.ed_page.setPlaceholderText("粘贴站内列表页/详情页 URL（留空则抓首页）")
        row3.addWidget(self.ed_page, 1)
        gl.addLayout(row3)

        row4 = QHBoxLayout()
        self.chk_all = QCheckBox("全选")
        self.chk_all.stateChanged.connect(self._toggle_all)
        self.lbl_pick = QLabel("已选 0")
        self.lbl_pick.setObjectName("Muted")
        self.btn_dl = QPushButton("下载选中")
        self.btn_dl.setObjectName("Primary")
        self.btn_dl.clicked.connect(self.start_download)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_download)
        row4.addWidget(self.chk_all)
        row4.addWidget(self.lbl_pick, 1)
        row4.addWidget(self.btn_stop)
        row4.addWidget(self.btn_dl)
        gl.addLayout(row4)

        self.table = make_table(["选择", "封面 / 标题", "作者", "地址"],
                                [50, 440, 140, 460])
        # 行高给缩略图留位置：34px 的行塞进 128x72 的封面会只剩一条缝
        self.table.verticalHeader().setDefaultSectionSize(78)
        self.table.setIconSize(QSize(120, 68))
        self._covers = CoverLoader(self.table, QSize(120, 68))
        self._covers.set_referer("https://91porny.com/")
        self._covers.loaded.connect(self._cover_ready)
        self._cover_rows: dict[str, list[int]] = {}
        self.table.itemChanged.connect(self._update_pick)
        self.table.itemSelectionChanged.connect(self._preview_selected)
        self.table.setMinimumHeight(220)
        gl.addWidget(self.table, 1)
        lay.addWidget(grab, 1)

        # ---- 队列
        q = Card()
        ql = QVBoxLayout(q)
        ql.setContentsMargins(16, 12, 16, 12)
        ql.setSpacing(8)
        qh = QHBoxLayout()
        t3 = QLabel("下载队列")
        t3.setObjectName("H2")
        self.lbl_q = QLabel("空闲")
        self.lbl_q.setObjectName("Muted")
        qh.addWidget(t3)
        qh.addWidget(self.lbl_q, 1)
        ql.addLayout(qh)
        self.table_q = make_table(["视频", "状态", "进度"], [460, 200, 200])
        self.table_q.setMinimumHeight(110)
        ql.addWidget(self.table_q)
        lay.addWidget(q)
        return w

    # ---------------------------------------------------------------- 记录页
    def _tab_records(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        card = Card()
        cl = QHBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        t = QLabel("线路检测历史")
        t.setObjectName("H2")
        self.lbl_hist = QLabel("")
        self.lbl_hist.setObjectName("Muted")
        btn = QPushButton("刷新")
        btn.setObjectName("Ghost")
        btn.clicked.connect(self.reload_records)
        cl.addWidget(t)
        cl.addWidget(self.lbl_hist, 1)
        cl.addWidget(btn)
        lay.addWidget(card)

        self.table_hist = make_table(
            ["时间", "地址页", "通道", "可用/总数", "详情"], [130, 220, 110, 90, 380])
        lay.addWidget(self.table_hist, 1)

        card2 = Card()
        c2 = QHBoxLayout(card2)
        c2.setContentsMargins(16, 12, 16, 12)
        t2 = QLabel("下载记录")
        t2.setObjectName("H2")
        self.lbl_dl = QLabel("")
        self.lbl_dl.setObjectName("Muted")
        c2.addWidget(t2)
        c2.addWidget(self.lbl_dl, 1)
        lay.addWidget(card2)
        self.table_dl = make_table(
            ["时间", "标题", "状态", "大小", "错误"], [130, 380, 80, 90, 240])
        lay.addWidget(self.table_dl, 1)
        return w

    # ---------------------------------------------------------------- 后端
    def on_show(self) -> None:
        if self._ns is not None:
            return
        try:
            self._ns = backend.jiuse()
        except Exception as exc:  # noqa: BLE001
            self.badge_state.set_tone("err")
            self.badge_state.setText("后端不可用")
            self.notify(f"九色后端加载失败：{exc}", "err")
            return
        st = self._ns.nav.current()
        self._apply_state(st)
        if not (st.get("addresses") or []):
            self.lbl_route.setText(
                "还没有缓存地址。点「刷新最新地址」直连 pages.dev 抓取并探测。")
        # 首次进入就自动抓一次首页：否则表格恒为 0 条，用户以为站点没内容。
        if not getattr(self, "_auto_loaded", False):
            self._auto_loaded = True
            if self.table.rowCount() == 0:
                QTimer.singleShot(250, lambda: self.grab_page(""))

    def _apply_state(self, st: dict) -> None:
        items = list(st.get("addresses") or [])
        active = st.get("active") or ""
        self.cmb_addr.blockSignals(True)
        self.cmb_addr.clear()
        for it in items:
            ok = bool(it.get("ok"))
            ms = int(it.get("elapsed_ms") or 0)
            label = f"{'✓' if ok else '✗'} {it.get('host') or it.get('url')}"
            if ms:
                label += f"  {ms} ms"
            self.cmb_addr.addItem(label, it.get("url"))
        self.cmb_addr.blockSignals(False)
        if active:
            for i in range(self.cmb_addr.count()):
                if self.cmb_addr.itemData(i) == active:
                    self.cmb_addr.setCurrentIndex(i)
                    break
        ts = int(st.get("updated_at") or 0)
        if ts:
            self.badge_state.set_tone("ok" if st.get("active") else "warn")
            self.badge_state.setText("更新于 " + time.strftime("%m-%d %H:%M",
                                                              time.localtime(ts)))
        good = sum(1 for i in items if i.get("ok"))
        self.lbl_route.setText(
            f"共 {len(items)} 条线路，可用 {good} 条"
            + (f" · 当前 {active}" if active else " · 尚未选定")
            + (f" · 通道 {st.get('via')}" if st.get("via") else "")
            + (f" · 错误 {st.get('error')}" if st.get("error") else ""))

    # ---------------------------------------------------------------- 线路
    def refresh(self) -> None:
        try:
            self._ns = self._ns or backend.jiuse()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"后端不可用：{exc}", "err")
            return
        nav_url = self.ed_nav.text().strip() or "https://dizhi222.pages.dev/"
        s = settings()
        s.set("jiuse.nav_url", nav_url)
        s.set("jiuse.check_limit", int(self.sp_limit.value()))
        s.save()
        # 地址页常在墙外：允许时用代理读（站点本身仍按直连校验结果决定）
        nav_proxy = ""
        if s.get("navsync.use_proxy_for_nav", True) and s.get("proxy.enabled", True):
            nav_proxy = s.proxy_url

        self.btn_refresh.setEnabled(False)
        self.badge_state.set_tone("warn")
        self.badge_state.setText("读取中…")
        self.lbl_route.setText("正在读取地址页…")

        task = Task(self._ns.nav.refresh, nav_url,
                    float(s.get("jiuse.timeout", 8) or 8),
                    int(self.sp_limit.value()), want_progress=True,
                    proxy=nav_proxy, allow_proxy_fallback=bool(nav_proxy))
        task.progress.connect(
            lambda p, t: self.lbl_route.setText(t or "读取中…"))
        task.done.connect(self._refresh_done)
        task.failed.connect(lambda tb: (self.btn_refresh.setEnabled(True),
                                        self.badge_state.set_tone("err"),
                                        self.badge_state.setText("失败"),
                                        self.lbl_route.setText("读取失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _refresh_done(self, res: dict) -> None:
        self.btn_refresh.setEnabled(True)
        self._last_refresh = time.time()
        self._apply_state(res)
        if res.get("ok"):
            self.notify(f"最新地址：可用 {res.get('good', 0)} / 探测 {res.get('checked', 0)}",
                        "ok")
        else:
            self.badge_state.set_tone("warn")
            self.badge_state.setText("缓存")
            self.notify((res.get("error") or "地址页读取失败")[:160], "warn")

    def _current_addr(self) -> str:
        return (self.cmb_addr.currentData() or "").strip()

    def _addr_changed(self, _i: int) -> None:
        url = self._current_addr()
        if url and not self.ed_page.text().strip():
            self.ed_page.setPlaceholderText(f"留空则抓首页：{url}/")

    def use_address(self) -> None:
        url = self._current_addr()
        if not url:
            self.notify("先刷新最新地址", "warn")
            return
        try:
            self._ns = self._ns or backend.jiuse()
            self._ns.nav.set_active(url)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"保存失败：{exc}", "err")
            return
        self.notify(f"已使用：{url}", "ok")
        self._apply_state(self._ns.nav.current())

    def open_site(self) -> None:
        url = self._current_addr()
        if not url:
            self.notify("先刷新最新地址", "warn")
            return
        import webbrowser
        webbrowser.open(url)
        self.notify(f"已在浏览器打开：{url}", "info")

    def copy_address(self) -> None:
        url = self._current_addr()
        if not url:
            return
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(url)
        self.notify("地址已复制", "ok")

    # ---------------------------------------------------------------- 采集
    def grab_page(self, url: str) -> None:
        try:
            self._ns = self._ns or backend.jiuse()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"后端不可用：{exc}", "err")
            return
        base = self._current_addr()
        if not url:
            if not base:
                self.notify("先刷新并选定一条线路", "warn")
                return
            url = base.rstrip("/") + "/"

        self.btn_grab.setEnabled(False)
        self.btn_home.setEnabled(False)
        self.badge_grab.setText("抓取中…")
        timeout = float(settings().get("jiuse.timeout", 8) or 8) + 8

        task = Task(self._ns.site.probe_page, url, timeout=timeout)
        task.done.connect(self._grab_done)
        task.failed.connect(lambda tb: (self.btn_grab.setEnabled(True),
                                        self.btn_home.setEnabled(True),
                                        self.badge_grab.setText("抓取失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _grab_done(self, res: dict) -> None:
        self.btn_grab.setEnabled(True)
        self.btn_home.setEnabled(True)
        items = list(res.get("items") or [])
        self._items = items
        self._cover_rows = {}
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for it in items:
            r = self.table.rowCount()
            self.table.insertRow(r)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, it)
            self.table.setItem(r, 0, chk)
            title_it = QTableWidgetItem(it.get("title", ""))
            # 封面临时占位：先用缓存，没缓存就发起异步请求，图到了再 setIcon。
            # 直接把图塞进合成大图会卡 UI（29 张 × 上百 KB）。
            cover = (it.get("cover") or "").strip()
            if cover:
                pm = self._covers.cached(cover)
                if pm is not None:
                    title_it.setIcon(QIcon(pm))
                else:
                    self._cover_rows.setdefault(cover, []).append(r)
                    self._covers.load(cover)
            title_it.setToolTip(it.get("title", ""))
            self.table.setItem(r, 1, title_it)
            # 作者/演员：卡片里两种写法（/dashen/ 与 /author/）都已解析，
            # 单独一列显示才看得出"这条是谁的"，否则只能去 tooltip 里找。
            author_it = QTableWidgetItem((it.get("author") or "").strip())
            if it.get("duration"):
                author_it.setToolTip(f"时长 {it['duration']}")
            self.table.setItem(r, 2, author_it)
            addr_it = QTableWidgetItem(it.get("url", ""))
            dur = (it.get("duration") or "").strip()
            if dur:
                addr_it.setToolTip(f"时长 {dur}\n{it.get('url', '')}")
            self.table.setItem(r, 3, addr_it)
        self.table.blockSignals(False)
        self.badge_grab.setText(f"{len(items)} 条")
        self.lbl_pick.setText("已选 0")
        with_cover = sum(1 for it in items if (it.get("cover") or "").strip())
        self.notify(f"{res.get('title') or '页面'} · 识别到 {len(items)} 条"
                    + (f"，封面 {with_cover} 条" if with_cover else "")
                    + ("" if items else "（站点结构可能变了，可试详情页 URL）"),
                    "ok" if items else "warn")

    def _cover_ready(self, url: str, pm) -> None:
        """封面到货 → 回填所有引用同一张图的行。"""
        rows = self._cover_rows.pop(url, [])
        icon = QIcon(pm)
        for r in rows:
            if 0 <= r < self.table.rowCount():
                item = self.table.item(r, 1)
                if item is not None:
                    item.setIcon(icon)

    def _preview_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._items):
            return
        it = self._items[row]
        url = (it.get("url") or "").strip()
        self.preview.preview(
            title=it.get("title", "") or url or "",
            thumb=it.get("cover") or None,
            # 点「在线播放」时后台解析直链（91 站多为 m3u8，面板会自动转封装后内嵌播）
            resolver=(lambda u=url: backend.resolve_direct(u)) if url else None,
            # 九色解析只要 2 秒（一次 GET + 正则），放心在选中时就预解析
            prefetch=bool(url),
        )

    def _toggle_all(self, state: int) -> None:
        on = bool(state)
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                item.setCheckState(Qt.CheckState.Checked if on
                                   else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_pick()

    def _picked(self) -> list[dict]:
        out = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                data = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(data, dict):
                    out.append(data)
        return out

    def _update_pick(self, *_a) -> None:
        n = len(self._picked())
        self.lbl_pick.setText(f"已选 {n}")
        self.btn_dl.setText(f"下载选中 ({n})" if n else "下载选中")

    # ---------------------------------------------------------------- 下载
    def start_download(self) -> None:
        picked = self._picked()
        if not picked:
            self.notify("请先勾选条目", "warn")
            return
        if self._worker is not None:
            self.notify("已有下载任务在跑", "warn")
            return
        out = settings().get("jiuse.download_dir", "")
        if not out:
            self.notify("请先在设置里指定九色下载目录", "warn")
            return
        os.makedirs(out, exist_ok=True)

        self.table_q.setRowCount(0)
        for it in picked:
            r = self.table_q.rowCount()
            self.table_q.insertRow(r)
            self.table_q.setItem(r, 0, QTableWidgetItem(it.get("title", "")))
            self.table_q.setItem(r, 1, QTableWidgetItem("等待"))
            bar = QProgressBar()
            bar.setRange(0, 100)
            self.table_q.setCellWidget(r, 2, bar)

        proxy = (settings().proxy_url
                 if settings().get("jiuse.proxy_enabled", False) else "")
        self._worker = _DlWorker(picked, out, self._current_addr(), proxy=proxy)
        self._worker.row_state.connect(self._row_state)
        self._worker.finished_all.connect(self._dl_done)
        self.btn_stop.setEnabled(True)
        self.btn_dl.setEnabled(False)
        self.lbl_q.setText(f"下载中 · {len(picked)} 条（91 系站点可能需要 Cookie，失败请用浏览器）")
        self._worker.start()

    def _row_state(self, idx: int, status: str, pct: int) -> None:
        if idx >= self.table_q.rowCount():
            return
        item = self.table_q.item(idx, 1)
        if item:
            item.setText(status)
        bar = self.table_q.cellWidget(idx, 2)
        if isinstance(bar, QProgressBar):
            bar.setValue(max(0, min(100, pct)))

    def _dl_done(self, ok: int, fail: int) -> None:
        self.btn_stop.setEnabled(False)
        self.btn_dl.setEnabled(True)
        self.lbl_q.setText(f"完成 {ok} · 失败 {fail}")
        self.notify(f"下载结束：成功 {ok}，失败 {fail}", "ok" if not fail else "warn")
        self._worker = None

    def stop_download(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self.btn_stop.setEnabled(False)
            self.lbl_q.setText("正在停止…")

    # ---------------------------------------------------------------- 记录
    def reload_records(self) -> None:
        try:
            self._ns = self._ns or backend.jiuse()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"后端不可用：{exc}", "err")
            return
        checks = self._ns.db.recent_checks(50)
        self.table_hist.setRowCount(0)
        for c in checks:
            r = self.table_hist.rowCount()
            self.table_hist.insertRow(r)
            ts = int(c.get("created_at") or 0)
            self.table_hist.setItem(r, 0, QTableWidgetItem(
                time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"))
            self.table_hist.setItem(r, 1, QTableWidgetItem(str(c.get("nav_url", ""))))
            self.table_hist.setItem(r, 2, QTableWidgetItem(str(c.get("via", ""))))
            self.table_hist.setItem(r, 3, QTableWidgetItem(
                f"{c.get('n_ok', 0)}/{c.get('n_total', 0)}"))
            self.table_hist.setItem(r, 4, QTableWidgetItem(
                str(c.get("detail", ""))[:120]))
        self.lbl_hist.setText(f"共 {len(checks)} 条")

        dls = self._ns.db.recent_downloads(100)
        self.table_dl.setRowCount(0)
        for d in dls:
            r = self.table_dl.rowCount()
            self.table_dl.insertRow(r)
            ts = int(d.get("created_at") or 0)
            self.table_dl.setItem(r, 0, QTableWidgetItem(
                time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"))
            self.table_dl.setItem(r, 1, QTableWidgetItem(str(d.get("title", ""))))
            self.table_dl.setItem(r, 2, QTableWidgetItem(str(d.get("status", ""))))
            size = int(d.get("size") or 0)
            self.table_dl.setItem(r, 3, QTableWidgetItem(
                f"{size / 1024 / 1024:.1f} MB" if size else "-"))
            self.table_dl.setItem(r, 4, QTableWidgetItem(
                str(d.get("error", "") or "")[:80]))
        self.lbl_dl.setText(f"共 {len(dls)} 条")
