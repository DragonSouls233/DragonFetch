# -*- coding: utf-8 -*-
"""单站点页面：**一个域名 = 一个模块 = 一个页面**。

与旧版「多站点」的区别
----------------------
旧版是一页 + 一个「选择网站」下拉，所有站点挤在一起；现在每个域名各自成页
（``SitePage("xvideos")``、``SitePage("xnxx")`` …），页面里没有站点选择器：

* 页头直接写清 **站点名 + 域名 + 内容类型**，右上角「打开网站」直达主页；
* 解析/抓取/搜索全部作用于本站，不会误操作到别的站点；
* 结果与下载记录写进 **本站独立的库** ``DATA/sites/<key>/sites.db``，
  下次打开本页自动回填（别的站点看不到，也不会被覆盖）；
* 下载默认落在 ``DATA/sites/<key>/downloads/<站点名>/``。

版式（与设计稿一致）：
  [链接输入.................................] 页数 [N] [解析直链] [抓取列表]
  [关键词...............................]              [搜索]
  ┌ 结果表：选择 | 标题 | 时长 | 作者 | 观看 | 日期 | 分类 | URL（8 列，均可编辑）
  [全选] [全不选] [复制URL] [导出CSV] [打开页面] [清空结果]   共 N 条 · 已选 M 条
  ┌ 日志
  保存目录 [......] 分组名 [......] [下载选中] [停止]
  下载中 (0/0)  [============= 进度 =============]
"""
from __future__ import annotations

import csv
import os
import time

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QProgressBar, QPushButton, QSpinBox, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...core import backend, paths
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from ..widgets import Card, LogPanel, make_table, open_path
from .base import Page

COLUMNS = ["选择", "标题", "时长", "作者", "观看", "日期", "分类", "URL"]
WIDTHS = [58, 320, 92, 140, 92, 108, 160, 340]
FIELDS = ["", "title", "duration", "author", "views", "date", "categories", "url"]


# ---------------------------------------------------------------------- #
# 下载线程（顺序下载，逐行回传）
# ---------------------------------------------------------------------- #
class _DlWorker(QThread):
    row_state = pyqtSignal(int, str, int)
    finished_all = pyqtSignal(int, int)

    def __init__(self, items, out_dir: str, proxy: str | None):
        super().__init__()
        self._items = list(items)
        self._out = out_dir
        self._proxy = proxy
        self._stop = False

    def run(self) -> None:  # noqa: D401
        ok = fail = 0
        total = max(1, len(self._items))
        try:
            engine = backend.ytdlp().service
        except Exception as exc:  # noqa: BLE001
            self.row_state.emit(-1, f"下载引擎不可用：{exc}", 0)
            self.finished_all.emit(0, len(self._items))
            return
        for i, it in enumerate(self._items):
            if self._stop:
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
                path = engine.download(url, self._out, proxy=self._proxy,
                                       progress=_prog)
                size = 0
                try:
                    size = os.path.getsize(path) if path else 0
                except OSError:
                    size = 0
                self.row_state.emit(
                    it["row"], f"完成 {size / 1024 / 1024:.1f} MB" if size else "完成", 0)
                ok += 1
                self._log(it, True, path or "", size, "")
            except Exception as exc:  # noqa: BLE001
                self.row_state.emit(it["row"], f"失败：{exc}"[:60], 0)
                fail += 1
                self._log(it, False, "", 0, str(exc))
        self.finished_all.emit(ok, fail)

    def _log(self, it, ok: bool, path: str, size: int, err: str) -> None:
        try:
            db = it.get("db")
            dl_id = it.get("dl_id") or 0
            if db is not None and dl_id:
                db.finish_download(dl_id, ok, path, size, err)
        except Exception:
            pass

    def stop(self) -> None:
        self._stop = True


# ---------------------------------------------------------------------- #
class SitePage(Page):
    """单个站点的完整工作流。``key`` 必须是 ``modules/sites`` 里注册过的域名模块。"""

    def __init__(self, key: str):
        self.site_key = key
        self.site = backend.site_module(key)      # 先拿到模块，build() 要用
        self.key = f"site:{key}"
        self.title = self.site.label
        self.subtitle = (f"{self.site.content} · {', '.join(self.site.domains)}"
                         + (f" · {self.site.note}" if self.site.note else ""))
        super().__init__()

    # ---------------------------------------------------------------- 构建
    def build(self) -> None:
        self._items: list[dict] = []
        self._loading = False
        self._worker: _DlWorker | None = None
        self._base_dir = str(self.site.download_dir)

        btn_site = QPushButton("打开网站")
        btn_site.setObjectName("Primary")
        btn_site.clicked.connect(self.open_site)
        self.header.add_action(btn_site)
        btn_dir = QPushButton("打开下载目录")
        btn_dir.setObjectName("Ghost")
        btn_dir.clicked.connect(lambda: open_path(self._base_dir))
        self.header.add_action(btn_dir)

        self.body.addWidget(self._card_toolbar(), 0)
        self.body.addWidget(self._build_table(), 1)
        self.body.addWidget(self._build_actions(), 0)
        self.body.addWidget(self._build_log(), 0)
        self.body.addWidget(self._card_download(), 0)

    def _card_toolbar(self) -> QWidget:
        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText(
            f"{self.site.domain} 的视频页 / 列表页 / 模特页链接")
        self.ed_url.returnPressed.connect(self.fetch_list)
        self.sp_pages = QSpinBox()
        self.sp_pages.setRange(1, 50)
        self.sp_pages.setValue(int(settings().get("sites.pages", 2) or 2))
        self.sp_pages.setPrefix("页数 ")
        self.btn_resolve = QPushButton("解析直链")
        self.btn_resolve.clicked.connect(self.resolve_direct)
        self.btn_list = QPushButton("抓取列表")
        self.btn_list.setObjectName("Primary")
        self.btn_list.clicked.connect(self.fetch_list)
        row1.addWidget(self.ed_url, 1)
        row1.addWidget(self.sp_pages)
        row1.addWidget(self.btn_resolve)
        row1.addWidget(self.btn_list)
        cl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.ed_kw = QLineEdit()
        self.ed_kw.setPlaceholderText(f"在 {self.site.label} 内按关键词搜索")
        self.ed_kw.returnPressed.connect(self.do_search)
        self.btn_search = QPushButton("搜索")
        self.btn_search.setObjectName("Primary")
        self.btn_search.clicked.connect(self.do_search)
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setObjectName("Muted")
        row2.addWidget(self.ed_kw, 1)
        row2.addWidget(self.btn_search)
        row2.addWidget(self.lbl_status, 1)
        cl.addLayout(row2)
        return card

    def _build_table(self) -> QWidget:
        self.table = make_table(COLUMNS, WIDTHS, stretch_last=True)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.itemChanged.connect(self._cell_changed)
        self.table.itemDoubleClicked.connect(self._row_double_clicked)
        return self.table

    def _build_actions(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(8)
        specs = [
            ("全选", lambda: self.set_all_checked(True)),
            ("全不选", lambda: self.set_all_checked(False)),
            ("复制URL", self.copy_urls),
            ("导出CSV", self.export_csv),
            ("打开页面", self.open_page),
            ("清空结果", self.clear_rows),
        ]
        for text, slot in specs:
            b = QPushButton(text)
            b.setObjectName("Ghost")
            b.clicked.connect(slot)
            row.addWidget(b)
        self.lbl_count = QLabel("共 0 条 · 已选 0 条")
        self.lbl_count.setObjectName("Muted")
        row.addSpacing(8)
        row.addWidget(self.lbl_count, 1)
        return w

    def _build_log(self) -> QWidget:
        self.log = LogPanel()
        self.log.setMaximumHeight(150)
        return self.log

    def _card_download(self) -> QWidget:
        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.ed_out = QLineEdit(self._base_dir)
        btn_pick = QPushButton("目录")
        btn_pick.setObjectName("Ghost")
        btn_pick.clicked.connect(self._pick_out)
        self.ed_group = QLineEdit()
        self.ed_group.setPlaceholderText(f"可选：默认 {self.site.key}")
        self.ed_group.setMaximumWidth(220)
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
        """打开本页时回填**本站**的历史记录（各站互不干扰）。"""
        if self._items:
            return
        try:
            rows = self.site.db().records(limit=300)
        except Exception as exc:  # noqa: BLE001
            self.log.append("warn", f"读取本站库失败：{exc}")
            return
        if not rows:
            self.log.append("info", f"{self.site.label}：本站库为空，先抓一次列表或搜索")
            return
        for r in rows:
            self._append_item({
                "url": r.get("url", ""), "title": r.get("title", ""),
                "duration": r.get("duration", ""), "author": r.get("author", ""),
                "views": r.get("views", ""), "publish_date": r.get("publish_date", ""),
                "categories": (r.get("categories") or "").split(", ")
                if r.get("categories") else [],
                "direct": r.get("direct", ""), "checked": False,
            })
        self.log.append("ok", f"已载入本站记录 {len(rows)} 条")

    def _site_db(self):
        return self.site.db()

    def _proxy(self) -> str | None:
        s = settings()
        return s.proxy_url if s.get("sites.proxy_enabled", True) else None

    # ---------------------------------------------------------------- 站点
    def open_site(self) -> None:
        QDesktopServices.openUrl(QUrl(self.site.homepage))
        self.log.append("ok", f"已打开 {self.site.homepage}")

    # ---------------------------------------------------------------- 解析
    def resolve_direct(self) -> None:
        urls = self._selected_urls()
        if not urls:
            u = self.ed_url.text().strip()
            if u:
                urls = [u]
        if not urls:
            self.notify("请先勾选结果或在输入框填链接", "warn")
            return
        proxy = self._proxy()
        self.log.append("info", f"{self.site.label}：解析 {len(urls)} 个页面…")
        self._set_busy(True, "解析中")

        def _run(progress=None):
            from concurrent.futures import ThreadPoolExecutor, as_completed

            out, total = [], max(1, len(urls))
            with ThreadPoolExecutor(max_workers=max(1, min(4, len(urls)))) as ex:
                futs = {ex.submit(backend.resolve_meta, u, proxy): u for u in urls}
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
        saved = []
        for url, data, err in result or []:
            row = self._find_row(url)
            if row < 0:
                row = self._append_item({"url": url})
            if err:
                self.log.append("err", f"解析失败 {url[:60]}：{err}")
                continue
            if not data:
                self.log.append("warn", f"{url[:60]} — 无内容（站点未收录？）")
                continue
            ok += 1
            self._fill_row(row, data)
            saved.append(dict(self._items[row]))
        if saved:
            try:
                self._site_db().save_records(saved, source="resolve")
            except Exception:
                pass
        self.notify(f"解析完成：{ok} 条", "ok" if ok else "warn")

    # ---------------------------------------------------------------- 抓列表
    def fetch_list(self) -> None:
        url = self.ed_url.text().strip()
        if not url:
            self.notify("请先填写列表页 / 模特页链接", "warn")
            return
        pages = int(self.sp_pages.value())
        proxy = self._proxy()
        self.log.append("info", f"{self.site.label} 抓取列表：{url[:90]}（{pages} 页）")
        self._set_busy(True, "抓取列表…")
        site = self.site

        def _run(progress=None):
            if progress:
                progress(10, "解析列表页…")
            return site.list_videos(url, pages=pages, proxy=proxy)

        task = Task(_run, want_progress=True)
        task.progress.connect(self._on_progress)
        task.done.connect(self._list_done)
        task.failed.connect(self._task_failed)
        CENTER.start(task)

    def _list_done(self, items) -> None:
        self._set_busy(False, "抓取完成")
        self._ingest(items, "list")

    # ---------------------------------------------------------------- 搜索
    def do_search(self) -> None:
        kw = self.ed_kw.text().strip()
        if not kw:
            self.notify("请输入关键词", "warn")
            return
        pages = int(self.sp_pages.value())
        proxy = self._proxy()
        self.log.append("info", f"{self.site.label} 搜索：「{kw}」（{pages} 页）")
        self._set_busy(True, "搜索中…")
        site = self.site

        def _run(progress=None):
            if progress:
                progress(10, f"搜索 {kw}…")
            return site.search(kw, pages=pages, proxy=proxy)

        task = Task(_run, want_progress=True)
        task.progress.connect(self._on_progress)
        task.done.connect(self._search_done)
        task.failed.connect(self._task_failed)
        CENTER.start(task)

    def _search_done(self, items) -> None:
        self._set_busy(False, "搜索完成")
        self._ingest(items, "search")

    def _ingest(self, items, source: str) -> None:
        items = items or []
        if not items:
            self.log.append("warn", "没有结果：本站可能不支持该操作，或需要代理")
            self.notify("没有抓到条目", "warn")
            return
        added = 0
        for it in items:
            if self._find_row(it.get("url", "")) >= 0:
                continue
            self._append_item(it)
            added += 1
        try:
            self._site_db().save_records(items, source=source)
        except Exception as exc:  # noqa: BLE001
            self.log.append("warn", f"写入本站库失败：{exc}")
        self.log.append("ok", f"抓到 {len(items)} 条，新增 {added} 条，已存入本站库")
        self.notify(f"抓到 {len(items)} 条", "ok")

    # ---------------------------------------------------------------- 任务反馈
    def _on_progress(self, pct: int, text: str) -> None:
        if pct >= 0:
            self.bar.setValue(pct)
        if text:
            self.lbl_status.setText(text[:80])

    def _task_failed(self, tb: str) -> None:
        self._set_busy(False, "失败")
        last = (tb or "").strip().splitlines()[-1] if tb else "未知错误"
        self.log.append("err", last[:200])
        self.notify(last[:160], "err")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        for b in (self.btn_resolve, self.btn_list, self.btn_search):
            b.setEnabled(not busy)
        if text:
            self.lbl_status.setText(text)
        if not busy:
            self.bar.setValue(0)

    # ---------------------------------------------------------------- 表格
    def _append_item(self, data: dict) -> int:
        urls = data.get("direct_urls") or []
        item = {
            "url": data.get("url", "") or "",
            "direct": data.get("direct") or (urls[-1] if urls else ""),
            "title": data.get("title", "") or "",
            "duration": data.get("duration", "") or "",
            "author": data.get("author", "") or "",
            "views": data.get("views", "") or "",
            "date": data.get("publish_date") or data.get("date") or "",
            "categories": data.get("categories") or "",
        }
        if isinstance(item["categories"], (list, tuple)):
            item["categories"] = ", ".join(str(c) for c in item["categories"] if c)
        item["site"] = self.site.key
        self._items.append(item)
        row = len(self._items) - 1
        self._loading = True
        self.table.insertRow(row)
        chk = QTableWidgetItem("")
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                     | Qt.ItemFlag.ItemIsSelectable)
        chk.setCheckState(Qt.CheckState.Checked if data.get("checked", True)
                          else Qt.CheckState.Unchecked)
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
        urls = data.get("direct_urls") or []
        direct = data.get("direct") or (urls[-1] if urls else "")
        it.update({
            "title": data.get("title") or it.get("title", ""),
            "duration": data.get("duration") or it.get("duration", ""),
            "author": data.get("author") or it.get("author", ""),
            "views": data.get("views") or it.get("views", ""),
            "date": (data.get("publish_date") or data.get("date")
                     or it.get("date", "")),
            "categories": (", ".join(data.get("categories") or [])
                           if isinstance(data.get("categories"), (list, tuple))
                           else (data.get("categories") or it.get("categories", ""))),
            "direct": direct or it.get("direct", ""),
        })
        self._refresh_row(row)

    def _refresh_row(self, row: int) -> None:
        if row < 0 or row >= len(self._items):
            return
        it = self._items[row]
        self._loading = True
        for c, field in enumerate(FIELDS[1:], start=1):
            cell = QTableWidgetItem(str(it.get(field, "")))
            if field != "url":
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                              | Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, c, cell)
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
        if item.column() == 7 and item.text().strip():
            QDesktopServices.openUrl(QUrl(item.text().strip()))

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
        return [self._items[r]["url"] for r in self._selected_rows()
                if self._items[r].get("url")]

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
        self.log.append("info", "已清空结果（本站库里的记录仍保留，可用库管理查看）")

    # ---------------------------------------------------------------- 表格操作
    def copy_urls(self) -> None:
        urls = self._selected_urls()
        if not urls:
            self.notify("请先勾选至少一条结果", "warn")
            return
        QApplication.clipboard().setText("\n".join(urls))
        self.log.append("ok", f"已复制 {len(urls)} 条 URL")
        self.notify(f"已复制 {len(urls)} 条", "ok")

    def export_csv(self) -> None:
        if not self._items:
            self.notify("结果表为空", "warn")
            return
        default = self.site.data_dir / f"{self.site.key}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        out, _ = QFileDialog.getSaveFileName(self, "导出 CSV", str(default),
                                             "CSV (*.csv)")
        if not out:
            return
        try:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["标题", "时长", "作者", "观看", "日期", "分类",
                            "URL", "直链", "站点"])
                for it in self._items:
                    w.writerow([it.get("title", ""), it.get("duration", ""),
                                it.get("author", ""), it.get("views", ""),
                                it.get("date", ""), it.get("categories", ""),
                                it.get("url", ""), it.get("direct", ""),
                                self.site.key])
            self.log.append("ok", f"已导出 {len(self._items)} 条 → {paths.rel(out)}")
            self.notify("导出完成", "ok")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"导出失败：{exc}", "err")

    def open_page(self) -> None:
        urls = self._selected_urls()
        if not urls:
            self.notify("请先勾选结果", "warn")
            return
        for u in urls[:10]:
            QDesktopServices.openUrl(QUrl(u))
        self.log.append("ok", f"已打开 {min(len(urls), 10)} 个页面")

    # ---------------------------------------------------------------- 下载
    def _pick_out(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.ed_out.text())
        if d:
            self.ed_out.setText(d)

    def start_download(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.notify("已有下载任务在跑", "warn")
            return
        rows = self._selected_rows()
        if not rows:
            self.notify("请先勾选要下载的条目", "warn")
            return
        base = self.ed_out.text().strip() or str(self.site.download_dir)
        group = self.ed_group.text().strip() or self.site.key
        base = os.path.join(base, group)
        try:
            os.makedirs(base, exist_ok=True)
        except OSError as exc:
            self.notify(f"无法创建目录：{exc}", "err")
            return

        try:
            db = self._site_db()
        except Exception:
            db = None

        items = []
        for r in rows:
            it = self._items[r]
            dl_id = 0
            if db is not None:
                try:
                    dl_id = db.start_download(it.get("url", ""),
                                              it.get("title", ""))
                except Exception:
                    dl_id = 0
            items.append({"row": r, "url": it.get("url", ""),
                          "direct": it.get("direct", ""), "db": db, "dl_id": dl_id})

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
            cell = self.table.item(row, 1)
            if cell is not None:
                cell.setToolTip(text)
            color = None
            if text.startswith("完成"):
                color = QColor(34, 160, 100, 60)
            elif text.startswith("失败") or text.startswith("无有效"):
                color = QColor(200, 60, 60, 70)
            if color:
                self._loading = True
                for c in range(self.table.columnCount()):
                    it = self.table.item(row, c)
                    if it:
                        it.setBackground(QBrush(color))
                self._loading = False
        if text and not text.startswith(("完成", "失败")):
            self.lbl_status.setText(text[:80])

    def _on_dl_done(self, ok: int, fail: int) -> None:
        self.btn_dl.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_dl.setText(f"下载中 ({ok + fail}/{ok + fail})")
        self.log.append("ok" if fail == 0 else "warn",
                        f"下载结束：成功 {ok} · 失败 {fail}")
        self.notify(f"下载结束：成功 {ok}，失败 {fail}", "ok" if fail == 0 else "warn")
        self.lbl_status.setText("空闲")
        self._worker = None

    def stop_download(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self.log.append("warn", "已发出停止指令（当前文件下载完后停止）")
            self.btn_stop.setEnabled(False)
