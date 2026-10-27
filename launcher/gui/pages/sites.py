# -*- coding: utf-8 -*-
"""多站点：一个页面干三件事——解析直链 / 抓取列表 / 搜索，然后把勾选的条目批量下载。

界面结构（与设计稿逐项对应）：

  站点 [下拉] [打开网站]   页数 [N]
  [链接输入................................] [解析直链] [抓取列表]
  [关键词输入..............................]           [搜索]
  ┌ 结果表：选择 | 标题 | 时长 | 作者 | 观看 | 日期 | 分类 | URL（8 列，均可编辑）
  [全选] [复制URL] [导出CSV] [打开页面]        ……        [下载选中] [停止]
  ┌ 日志
  保存目录 [........] [目录]   分组名 [........]
  下载中 (0/0)  [============== 进度 ==============]

后端只依赖 ``modules/multisite``（JAVDB 已剥离，麻豆是独立模块）。
"""
from __future__ import annotations

import asyncio
import csv
import os
import re
import threading
import time

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QProgressBar, QPushButton, QSpinBox, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ...core import backend, paths
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from ..widgets import Card, LogPanel, make_table, open_path
from .base import Page

COLUMNS = ["选择", "标题", "时长", "作者", "观看", "日期", "分类", "URL"]
WIDTHS = [58, 320, 92, 140, 92, 108, 160, 340]
FIELDS = ["", "title", "duration", "author", "views", "date", "categories", "url"]

_BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_name(text: str, fallback: str = "未命名") -> str:
    name = _BAD_NAME.sub("_", (text or "").strip()).strip(" .")
    return (name or fallback)[:80]


# ---------------------------------------------------------------------- #
# 下载线程：顺序下载，逐行回传状态
# ---------------------------------------------------------------------- #
class _DlWorker(QThread):
    row_state = pyqtSignal(int, str, int)      # 行号 / 文案 / 百分比
    finished_all = pyqtSignal(int, int)        # 成功数 / 失败数

    def __init__(self, items, out_dir: str, proxy: str | None):
        super().__init__()
        self._items = list(items)
        self._out = out_dir
        self._proxy = proxy
        self._stop = threading.Event()

    def run(self) -> None:  # noqa: D401
        ok = fail = 0
        total = max(1, len(self._items))
        try:
            svc = backend.ytdlp().service
        except Exception as exc:  # noqa: BLE001
            self.row_state.emit(-1, f"下载引擎不可用：{exc}", 0)
            self.finished_all.emit(0, len(self._items))
            return

        for i, it in enumerate(self._items):
            if self._stop.is_set():
                self.row_state.emit(it["row"], "已停止", 0)
                break
            url = it.get("direct") or it.get("url") or ""
            if not url:
                self.row_state.emit(it["row"], "无有效地址", 0)
                fail += 1
                continue
            self.row_state.emit(it["row"], "连接中", 0)

            def _prog(pct=0.0, line="", _row=it["row"], _i=i):
                base = _i / total * 100
                cur = max(0.0, min(100.0, float(pct or 0))) / total
                self.row_state.emit(_row, str(line)[:60], int(base + cur))

            try:
                path = svc.download(url, self._out, proxy=self._proxy,
                                    progress=_prog)
                size = 0
                try:
                    size = os.path.getsize(path) if path else 0
                except OSError:
                    size = 0
                self.row_state.emit(
                    it["row"], f"完成 {size / 1024 / 1024:.1f} MB" if size else "完成", 0)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                self.row_state.emit(it["row"], f"失败：{exc}"[:60], 0)
                fail += 1
        self.finished_all.emit(ok, fail)

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------- #
class SitesPage(Page):
    key = "sites"
    title = "多站点"
    subtitle = "已收录站点：解析直链 / 抓取列表 / 关键词搜索，勾选后批量下载"

    # ---------------------------------------------------------------- 构建
    def build(self) -> None:
        self._items: list[dict] = []
        self._loading = False
        self._worker: _DlWorker | None = None
        self._sites: list[dict] = []
        self._busy = 0

        btn_dir = QPushButton("打开下载目录")
        btn_dir.setObjectName("Ghost")
        btn_dir.clicked.connect(self._open_dl_dir)
        self.header.add_action(btn_dir)

        self.body.addWidget(self._card_toolbar(), 0)
        self.body.addWidget(self._build_table(), 1)
        self.body.addWidget(self._card_actions(), 0)
        self.body.addWidget(self._build_log(), 0)
        self.body.addWidget(self._card_download(), 0)

    # ---------------------------------------------------------------- 工具条
    def _card_toolbar(self) -> QWidget:
        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.cmb_site = QComboBox()
        self.cmb_site.setMinimumWidth(190)
        self.cmb_site.currentIndexChanged.connect(self._site_changed)
        self.btn_open_site = QPushButton("打开网站")
        self.btn_open_site.setObjectName("Ghost")
        self.btn_open_site.clicked.connect(self.open_site)
        self.sp_pages = QSpinBox()
        self.sp_pages.setRange(1, 50)
        self.sp_pages.setValue(int(settings().get("sites.pages", 2) or 2))
        self.sp_pages.setPrefix("页数 ")
        self.badge_info = QLabel("就绪")
        self.badge_info.setObjectName("Muted")
        row1.addWidget(QLabel("站点"))
        row1.addWidget(self.cmb_site)
        row1.addWidget(self.btn_open_site)
        row1.addWidget(self.sp_pages)
        row1.addSpacing(10)
        row1.addWidget(self.badge_info, 1)
        cl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText(
            "视频页 / 列表页 / 模特页链接，例如 https://www.xvideos.com/videoXXXX")
        self.ed_url.returnPressed.connect(self.fetch_list)
        self.btn_resolve = QPushButton("解析直链")
        self.btn_resolve.clicked.connect(self.resolve_direct)
        self.btn_list = QPushButton("抓取列表")
        self.btn_list.setObjectName("Primary")
        self.btn_list.clicked.connect(self.fetch_list)
        row2.addWidget(self.ed_url, 1)
        row2.addWidget(self.btn_resolve)
        row2.addWidget(self.btn_list)
        cl.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(8)
        self.ed_kw = QLineEdit()
        self.ed_kw.setPlaceholderText("在所选站点内按关键词搜索")
        self.ed_kw.returnPressed.connect(self.do_search)
        self.btn_search = QPushButton("搜索")
        self.btn_search.setObjectName("Primary")
        self.btn_search.clicked.connect(self.do_search)
        row3.addWidget(self.ed_kw, 1)
        row3.addWidget(self.btn_search)
        cl.addLayout(row3)
        return card

    # ---------------------------------------------------------------- 结果表
    def _build_table(self) -> QWidget:
        self.table = make_table(COLUMNS, WIDTHS, stretch_last=True)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.itemChanged.connect(self._cell_changed)
        self.table.itemDoubleClicked.connect(self._row_double_clicked)
        return self.table

    # ---------------------------------------------------------------- 操作行
    def _card_actions(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(8)
        self.btn_all = QPushButton("全选")
        self.btn_all.setObjectName("Ghost")
        self.btn_all.clicked.connect(lambda: self.set_all_checked(True))
        self.btn_none = QPushButton("全不选")
        self.btn_none.setObjectName("Ghost")
        self.btn_none.clicked.connect(lambda: self.set_all_checked(False))
        self.btn_copy = QPushButton("复制URL")
        self.btn_copy.setObjectName("Ghost")
        self.btn_copy.clicked.connect(self.copy_urls)
        self.btn_csv = QPushButton("导出CSV")
        self.btn_csv.setObjectName("Ghost")
        self.btn_csv.clicked.connect(self.export_csv)
        self.btn_page = QPushButton("打开页面")
        self.btn_page.setObjectName("Ghost")
        self.btn_page.clicked.connect(self.open_page)
        self.btn_clear = QPushButton("清空结果")
        self.btn_clear.setObjectName("Ghost")
        self.btn_clear.clicked.connect(self.clear_rows)
        self.lbl_count = QLabel("共 0 条 · 已选 0 条")
        self.lbl_count.setObjectName("Muted")
        for b in (self.btn_all, self.btn_none, self.btn_copy, self.btn_csv,
                  self.btn_page, self.btn_clear):
            row.addWidget(b)
        row.addSpacing(8)
        row.addWidget(self.lbl_count, 1)
        return w

    # ---------------------------------------------------------------- 日志
    def _build_log(self) -> QWidget:
        self.log = LogPanel()
        self.log.setMaximumHeight(150)
        return self.log

    # ---------------------------------------------------------------- 下载区
    def _card_download(self) -> QWidget:
        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.ed_out = QLineEdit(str(settings().get("sites.download_dir", "")))
        btn_pick = QPushButton("目录")
        btn_pick.setObjectName("Ghost")
        btn_pick.clicked.connect(self._pick_out)
        self.ed_group = QLineEdit()
        self.ed_group.setPlaceholderText("可选：按分组建子目录")
        self.ed_group.setMaximumWidth(240)
        self.btn_dl = QPushButton("下载选中")
        self.btn_dl.setObjectName("Primary")
        self.btn_dl.clicked.connect(self.start_download)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_download)
        row.addWidget(QLabel("保存目录"))
        row.addWidget(self.ed_out, 1)
        row.addWidget(btn_pick)
        row.addWidget(QLabel("分组名"))
        row.addWidget(self.ed_group)
        row.addWidget(self.btn_dl)
        row.addWidget(self.btn_stop)
        cl.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.lbl_dl = QLabel("下载中 (0/0)")
        self.lbl_dl.setObjectName("Muted")
        self.lbl_dl.setMinimumWidth(150)
        self.bar = QProgressBar()
        self.bar.setObjectName("Big")
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        row2.addWidget(self.lbl_dl)
        row2.addWidget(self.bar, 1)
        cl.addLayout(row2)
        return card

    # ---------------------------------------------------------------- 生命周期
    def on_show(self) -> None:
        if not self._sites:
            self._load_sites()

    def _load_sites(self) -> None:
        try:
            self._sites = backend.site_list()
        except Exception as exc:  # noqa: BLE001
            self.log.append("err", f"站点表加载失败：{exc}")
            self._sites = []
        cur = self.cmb_site.currentData()
        self.cmb_site.blockSignals(True)
        self.cmb_site.clear()
        for s in self._sites:
            self.cmb_site.addItem(f"{s['label']}  ({s['key']})", s["key"])
        self.cmb_site.blockSignals(False)
        if cur:
            for i in range(self.cmb_site.count()):
                if self.cmb_site.itemData(i) == cur:
                    self.cmb_site.setCurrentIndex(i)
                    break
        if self._sites:
            self.badge_info.setText(f"已收录 {len(self._sites)} 个站点")
        else:
            self.badge_info.setText("未加载到站点表")

    def _site_key(self) -> str:
        return (self.cmb_site.currentData() or "").strip()

    def _site_home(self) -> str:
        idx = self.cmb_site.currentIndex()
        if 0 <= idx < len(self._sites):
            return self._sites[idx].get("homepage", "")
        return ""

    def _site_changed(self, _i: int) -> None:
        home = self._site_home()
        if home:
            self.badge_info.setText(f"{self._site_key()} · {home}")
        if not self.ed_group.text().strip():
            self.ed_group.setPlaceholderText(f"可选：默认 {self._site_key()}")

    def _proxy(self) -> str | None:
        s = settings()
        return s.proxy_url if s.get("sites.proxy_enabled", False) else None

    # ---------------------------------------------------------------- 站点
    def open_site(self) -> None:
        home = self._site_home()
        if not home:
            self.notify("该站点没有主页地址", "warn")
            return
        QDesktopServices.openUrl(QUrl(home))
        self.log.append("ok", f"已打开 {home}")

    # ---------------------------------------------------------------- 解析直链
    def resolve_direct(self) -> None:
        """对勾选行（没有勾选则用输入框）逐个解析直链，回填 8 列。"""
        urls = self._selected_urls()
        if not urls:
            u = self.ed_url.text().strip()
            if u:
                urls = [u]
        if not urls:
            self.notify("请先勾选结果或在输入框填链接", "warn")
            return
        proxy = self._proxy()
        self.log.append("info", f"开始解析 {len(urls)} 个页面…")
        self._set_busy(True, "解析中")

        def _run(progress=None):
            from concurrent.futures import ThreadPoolExecutor, as_completed

            out = []
            total = max(1, len(urls))
            workers = max(1, min(4, len(urls)))

            def _one(u):
                return backend.resolve_meta(u, proxy=proxy)

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_one, u): u for u in urls}
                for i, fut in enumerate(as_completed(futs), 1):
                    if progress:
                        progress(int(i * 100 / total), f"解析 {i}/{total}")
                    u = futs[fut]
                    try:
                        out.append((u, fut.result(), ""))
                    except Exception as exc:  # noqa: BLE001
                        out.append((u, None, str(exc)))
            return out

        task = Task(_run, want_progress=True)
        task.progress.connect(self._on_progress)
        task.done.connect(self._resolve_done)
        task.failed.connect(self._task_failed)
        CENTER.start(task)

    def _resolve_done(self, result) -> None:
        self._set_busy(False, "解析完成")
        ok = 0
        for url, data, err in result or []:
            row = self._find_row(url)
            if row < 0:
                row = self._append_item({"url": url})
            if err:
                self.log.append("err", f"解析失败 {url[:60]}：{err}")
                continue
            if not data:
                self.log.append("warn", f"{url[:60]} — 站点未收录或无需解析（可交 yt-dlp）")
                continue
            ok += 1
            self._fill_row(row, data)
            direct = (data.get("direct_urls") or [""])[-1]
            self.log.append("ok", f"直链就绪 · {data.get('title', '')[:40]}"
                                  + (" · 已拿到直链" if direct else " · 无直链，下载走页面解析"))
        self.notify(f"解析完成：{ok} 条", "ok" if ok else "warn")

    # ---------------------------------------------------------------- 抓列表
    def fetch_list(self) -> None:
        url = self.ed_url.text().strip()
        if not url:
            self.notify("请先填写列表页 / 模特页链接", "warn")
            return
        pages = int(self.sp_pages.value())
        proxy = self._proxy()
        self.log.append("info", f"抓取列表：{url[:90]}（{pages} 页）")
        self._set_busy(True, "抓取列表…")

        def _run(progress=None):
            svc = backend.site_service()
            if progress:
                progress(10, "解析列表页…")
            items = asyncio.run(svc.list_videos(url, pages=pages, proxy=proxy))
            return [x.to_dict() for x in items]

        task = Task(_run, want_progress=True)
        task.progress.connect(self._on_progress)
        task.done.connect(self._list_done)
        task.failed.connect(self._task_failed)
        CENTER.start(task)

    def _list_done(self, items) -> None:
        self._set_busy(False, "抓取完成")
        items = items or []
        if not items:
            self.log.append("warn", "列表为空：该站可能不支持展开，或需要代理")
            self.notify("没有抓到条目", "warn")
            return
        added = 0
        for it in items:
            if self._find_row(it.get("url", "")) >= 0:
                continue
            self._append_item(it)
            added += 1
        self._recount()
        self.log.append("ok", f"抓到 {len(items)} 条，新增 {added} 条")
        self.notify(f"抓到 {len(items)} 条", "ok")

    # ---------------------------------------------------------------- 搜索
    def do_search(self) -> None:
        kw = self.ed_kw.text().strip()
        key = self._site_key()
        if not kw:
            self.notify("请输入关键词", "warn")
            return
        if not key:
            self.notify("请先选择站点", "warn")
            return
        pages = int(self.sp_pages.value())
        proxy = self._proxy()
        self.log.append("info", f"搜索 {key}：「{kw}」（{pages} 页）")
        self._set_busy(True, "搜索中…")

        def _run(progress=None):
            svc = backend.site_service()
            if progress:
                progress(10, f"搜索 {kw}…")
            items = asyncio.run(svc.search(key, kw, pages=pages, proxy=proxy))
            return [x.to_dict() for x in items]

        task = Task(_run, want_progress=True)
        task.progress.connect(self._on_progress)
        task.done.connect(self._search_done)
        task.failed.connect(self._task_failed)
        CENTER.start(task)

    def _search_done(self, items) -> None:
        self._set_busy(False, "搜索完成")
        items = items or []
        if not items:
            self.log.append("warn", "搜索无结果（该站可能不支持搜索，或需要代理）")
            self.notify("没有搜索结果", "warn")
            return
        added = 0
        for it in items:
            if self._find_row(it.get("url", "")) >= 0:
                continue
            self._append_item(it)
            added += 1
        self._recount()
        self.log.append("ok", f"搜索到 {len(items)} 条，新增 {added} 条")
        self.notify(f"搜索到 {len(items)} 条", "ok")

    # ---------------------------------------------------------------- 任务反馈
    def _on_progress(self, pct: int, text: str) -> None:
        if pct >= 0:
            self.bar.setValue(pct)
        if text:
            self.badge_info.setText(text[:80])

    def _task_failed(self, tb: str) -> None:
        self._set_busy(False, "失败")
        last = (tb or "").strip().splitlines()[-1] if tb else "未知错误"
        self.log.append("err", last[:200])
        self.notify(last[:160], "err")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self._busy = 1 if busy else 0
        for b in (self.btn_resolve, self.btn_list, self.btn_search,
                  self.btn_open_site):
            b.setEnabled(not busy)
        if text:
            self.badge_info.setText(text)
        if not busy:
            self.bar.setValue(0)

    # ---------------------------------------------------------------- 表格数据
    def _append_item(self, data: dict) -> int:
        item = {
            "url": data.get("url", "") or "",
            "direct": (data.get("direct_urls") or [""])[-1] if data.get("direct_urls") else "",
            "title": data.get("title", "") or "",
            "duration": data.get("duration", "") or "",
            "author": data.get("author", "") or "",
            "views": data.get("views", "") or "",
            "date": data.get("publish_date", "") or "",
            "categories": ", ".join(data.get("categories") or []),
            "site": data.get("site", "") or self._site_key(),
        }
        self._items.append(item)
        row = len(self._items) - 1
        self._loading = True
        self.table.insertRow(row)
        chk = QTableWidgetItem("")
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                     | Qt.ItemFlag.ItemIsSelectable)
        chk.setCheckState(Qt.CheckState.Checked)
        self.table.setItem(row, 0, chk)
        for c, field in enumerate(FIELDS[1:], start=1):
            cell = QTableWidgetItem(str(item.get(field, "")))
            if field != "url":
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                              | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, c, cell)
        self._loading = False
        self._recount()
        return row

    def _fill_row(self, row: int, data: dict) -> None:
        if row < 0 or row >= len(self._items):
            return
        it = self._items[row]
        it.update({
            "title": data.get("title") or it.get("title", ""),
            "duration": data.get("duration") or it.get("duration", ""),
            "author": data.get("author") or it.get("author", ""),
            "views": data.get("views") or it.get("views", ""),
            "date": (data.get("publish_date") or data.get("date")
                     or it.get("date", "")),
            "categories": (", ".join(data.get("categories") or [])
                           or it.get("categories", "")),
        })
        urls = data.get("direct_urls") or []
        if data.get("direct_url") and not urls:
            urls = [data["direct_url"]]
        if urls:
            it["direct"] = urls[-1]
        self._refresh_row(row)

    def _refresh_row(self, row: int) -> None:
        if row < 0 or row >= len(self._items):
            return
        it = self._items[row]
        self._loading = True
        for c, field in enumerate(FIELDS[1:], start=1):
            self.table.setItem(row, c, QTableWidgetItem(str(it.get(field, ""))))
        chk = self.table.item(row, 0)
        if chk:
            chk.setToolTip(f"直链：{it.get('direct') or '（无，下载时用页面地址解析）'}")
        self._loading = False

    def _find_row(self, url: str) -> int:
        if not url:
            return -1
        for i, it in enumerate(self._items):
            if it.get("url") == url:
                return i
        return -1

    def _cell_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        row, col = item.row(), item.column()
        if row >= len(self._items) or col == 0:
            return
        field = FIELDS[col] if col < len(FIELDS) else ""
        if field:
            self._items[row][field] = item.text()

    def _row_double_clicked(self, item: QTableWidgetItem) -> None:
        if item.column() == 7:
            url = item.text().strip()
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _row_checked(self, row: int) -> bool:
        it = self.table.item(row, 0)
        return bool(it and it.checkState() == Qt.CheckState.Checked)

    def _selected_rows(self) -> list[int]:
        if not self._items:
            return []
        rows = [r for r in range(len(self._items)) if self._row_checked(r)]
        if rows:
            return rows
        cur = self.table.currentRow()
        return [cur] if 0 <= cur < len(self._items) else []

    def _selected_urls(self) -> list[str]:
        out = []
        for r in self._selected_rows():
            u = (self._items[r].get("url") or "").strip()
            if u:
                out.append(u)
        return out

    def _recount(self) -> None:
        total = len(self._items)
        picked = sum(1 for r in range(total) if self._row_checked(r))
        self.lbl_count.setText(f"共 {total} 条 · 已选 {picked} 条")

    def set_all_checked(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self._loading = True
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it:
                it.setCheckState(state)
        self._loading = False
        self._recount()

    def clear_rows(self) -> None:
        self._items = []
        self.table.setRowCount(0)
        self._recount()
        self.log.append("info", "已清空结果表")

    # ---------------------------------------------------------------- 表格操作
    def copy_urls(self) -> None:
        urls = self._selected_urls()
        if not urls:
            self.notify("请先勾选至少一条结果", "warn")
            return
        QApplication.clipboard().setText("\n".join(urls))
        self.log.append("ok", f"已复制 {len(urls)} 条 URL 到剪贴板")
        self.notify(f"已复制 {len(urls)} 条", "ok")

    def export_csv(self) -> None:
        if not self._items:
            self.notify("结果表为空", "warn")
            return
        default = paths.DATA / "multisite" / f"sites_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        out, _ = QFileDialog.getSaveFileName(self, "导出 CSV", str(default),
                                             "CSV (*.csv)")
        if not out:
            return
        try:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["标题", "时长", "作者", "观看", "日期", "分类", "URL", "直链", "站点"])
                for it in self._items:
                    w.writerow([it.get("title", ""), it.get("duration", ""),
                                it.get("author", ""), it.get("views", ""),
                                it.get("date", ""), it.get("categories", ""),
                                it.get("url", ""), it.get("direct", ""),
                                it.get("site", "")])
            self.log.append("ok", f"已导出 {len(self._items)} 条 → {paths.rel(out)}")
            self.notify("导出完成", "ok")
        except Exception as exc:  # noqa: BLE001
            self.log.append("err", f"导出失败：{exc}")
            self.notify(f"导出失败：{exc}", "err")

    def open_page(self) -> None:
        urls = self._selected_urls()
        if not urls:
            self.notify("请先勾选结果", "warn")
            return
        for u in urls[:10]:
            QDesktopServices.openUrl(QUrl(u))
        self.log.append("ok", f"已在浏览器打开 {min(len(urls), 10)} 个页面")

    # ---------------------------------------------------------------- 下载
    def _pick_out(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.ed_out.text())
        if d:
            self.ed_out.setText(d)
            settings().set("sites.download_dir", d)
            settings().save()

    def _open_dl_dir(self) -> None:
        d = self.ed_out.text().strip() or str(settings().get("sites.download_dir", ""))
        if d and os.path.isdir(d):
            open_path(d)
        else:
            self.notify("下载目录还不存在", "warn")

    def start_download(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.notify("已有下载任务在跑", "warn")
            return
        rows = self._selected_rows()
        if not rows:
            self.notify("请先勾选要下载的条目", "warn")
            return
        base = self.ed_out.text().strip()
        if not base:
            self.notify("请先选择保存目录", "warn")
            return
        # 分组名留空时按「站点名」建子目录，避免不同站的文件混在一个目录里
        group = self.ed_group.text().strip() or self._site_key()
        if group:
            base = os.path.join(base, _safe_name(group, "downloads"))
        try:
            os.makedirs(base, exist_ok=True)
        except OSError as exc:
            self.notify(f"无法创建目录：{exc}", "err")
            return

        items = [{"row": r, "url": self._items[r].get("url", ""),
                  "direct": self._items[r].get("direct", "")} for r in rows]
        settings().set("sites.download_dir", self.ed_out.text().strip())
        settings().save()

        self._worker = _DlWorker(items, base, self._proxy())
        self._worker.row_state.connect(self._on_row_state)
        self._worker.finished_all.connect(self._on_dl_done)
        self.btn_dl.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.bar.setValue(0)
        self.lbl_dl.setText(f"下载中 (0/{len(items)})")
        self.log.append("info", f"开始下载 {len(items)} 个 → {paths.rel(base)}")
        self._worker.start()

    def _on_row_state(self, row: int, text: str, pct: int) -> None:
        if pct > 0:
            self.bar.setValue(min(100, pct))
        if row >= 0:
            it = self.table.item(row, 1)
            if it is not None:
                it.setToolTip(text)
            color = None
            if text.startswith("完成"):
                color = QColor(34, 160, 100, 60)
            elif text.startswith("失败") or text.startswith("无有效"):
                color = QColor(200, 60, 60, 70)
            if color:
                self._loading = True
                for c in range(self.table.columnCount()):
                    cell = self.table.item(row, c)
                    if cell:
                        cell.setBackground(QBrush(color))
                self._loading = False
        if text and not text.startswith(("完成", "失败")):
            self.badge_info.setText(text[:80])

    def _on_dl_done(self, ok: int, fail: int) -> None:
        self.btn_dl.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.bar.setValue(100 if fail == 0 and ok else self.bar.value())
        self.lbl_dl.setText(f"下载中 ({ok + fail}/{ok + fail})")
        self.log.append("ok" if fail == 0 else "warn",
                        f"下载结束：成功 {ok} · 失败 {fail}")
        self.notify(f"下载结束：成功 {ok}，失败 {fail}", "ok" if fail == 0 else "warn")
        self.badge_info.setText("空闲")
        self._worker = None

    def stop_download(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self.log.append("warn", "已发出停止指令（当前文件下载完后停止）")
            self.btn_stop.setEnabled(False)
