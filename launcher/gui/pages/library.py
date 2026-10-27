# -*- coding: utf-8 -*-
"""库管理：把各模块的本地库（SQLite / JSON）收进一个可读可改的界面。

两块：

  库浏览      → 统一的库/表浏览、搜索、**就地改一格**、删行、导出 CSV
  备份与清理  → 一键备份（SQLite 用热备份）、清空单库、备份目录管理

数据全部位于根目录 ``DATA/``，模块目录里不落任何运行期文件。
"""
from __future__ import annotations

import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSpinBox, QTabWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...core import dbstore, paths
from ..widgets import Badge, Card, HLine, SearchInput, make_table, open_path
from .base import Page


class LibraryPage(Page):
    key = "library"
    title = "库管理"
    subtitle = "各模块的本地库（SQLite / JSON）统一浏览、改值、删行、导出、备份"

    def build(self) -> None:
        self._offset = 0
        self._total = 0
        self._cols: list[str] = []
        self._loading = False
        self._store_desc = "选择库后自动载入"

        btn_open = QPushButton("打开 DATA 目录")
        btn_open.setObjectName("Ghost")
        btn_open.clicked.connect(lambda: open_path(str(paths.DATA)))
        self.header.add_action(btn_open)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_browse(), "库浏览")
        self.tabs.addTab(self._tab_maint(), "备份与清理")
        self.body.addWidget(self.tabs, 1)

    # ---------------------------------------------------------------- 浏览页
    def _tab_browse(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)
        row = QHBoxLayout()
        self.cmb_store = QComboBox()
        self.cmb_store.setMinimumWidth(240)
        self.cmb_store.currentIndexChanged.connect(self._store_changed)
        self.cmb_table = QComboBox()
        self.cmb_table.setMinimumWidth(160)
        self.cmb_table.currentIndexChanged.connect(lambda _i: self.reload_rows(reset=True))
        self.badge_size = Badge("-")
        row.addWidget(QLabel("库"))
        row.addWidget(self.cmb_store)
        row.addWidget(QLabel("表"))
        row.addWidget(self.cmb_table)
        row.addWidget(self.badge_size)
        row.addStretch(1)
        self.ed_search = SearchInput("搜索本表…", width=220)
        self.ed_search.textChanged.connect(lambda _t: self.reload_rows(reset=True))
        row.addWidget(self.ed_search)
        self.sp_page = QSpinBox()
        self.sp_page.setRange(20, 2000)
        self.sp_page.setValue(200)
        self.sp_page.setPrefix("每页 ")
        self.sp_page.valueChanged.connect(lambda _v: self.reload_rows(reset=True))
        row.addWidget(self.sp_page)
        cl.addLayout(row)

        row2 = QHBoxLayout()
        self.lbl_store = QLabel("选择库后自动载入")
        self.lbl_store.setObjectName("Muted")
        self.btn_prev = QPushButton("上一页")
        self.btn_prev.setObjectName("Ghost")
        self.btn_prev.clicked.connect(lambda: self._page(-1))
        self.btn_next = QPushButton("下一页")
        self.btn_next.setObjectName("Ghost")
        self.btn_next.clicked.connect(lambda: self._page(1))
        self.btn_del = QPushButton("删除选中行")
        self.btn_del.setObjectName("Danger")
        self.btn_del.clicked.connect(self.delete_row)
        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.setObjectName("Ghost")
        self.btn_export.clicked.connect(self.export_csv)
        self.btn_reload = QPushButton("刷新")
        self.btn_reload.setObjectName("Ghost")
        self.btn_reload.clicked.connect(lambda: self.reload_rows(reset=True))
        row2.addWidget(self.lbl_store, 1)
        row2.addWidget(self.btn_prev)
        row2.addWidget(self.btn_next)
        row2.addWidget(self.btn_del)
        row2.addWidget(self.btn_export)
        row2.addWidget(self.btn_reload)
        cl.addLayout(row2)
        self.lbl_tip = QLabel("提示：双击单元格可直接改值，回车提交（写回原库）")
        self.lbl_tip.setObjectName("Muted")
        cl.addWidget(self.lbl_tip)
        lay.addWidget(card)

        self.table = make_table(["-"], None, stretch_last=True)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.itemChanged.connect(self._cell_changed)
        lay.addWidget(self.table, 1)
        return w

    # ---------------------------------------------------------------- 维护页
    def _tab_maint(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)
        t = QLabel("备份与清理")
        t.setObjectName("H2")
        cl.addWidget(t)
        cl.addWidget(HLine())
        row = QHBoxLayout()
        self.cmb_maint = QComboBox()
        self.cmb_maint.setMinimumWidth(260)
        btn_backup = QPushButton("备份此库")
        btn_backup.setObjectName("Primary")
        btn_backup.clicked.connect(self.backup_store)
        btn_clear = QPushButton("清空此库数据")
        btn_clear.setObjectName("Danger")
        btn_clear.clicked.connect(self.clear_store)
        btn_dir = QPushButton("打开备份目录")
        btn_dir.setObjectName("Ghost")
        btn_dir.clicked.connect(
            lambda: open_path(str(paths.DATA / "app" / "backups")))
        row.addWidget(QLabel("目标库"))
        row.addWidget(self.cmb_maint)
        row.addWidget(btn_backup)
        row.addWidget(btn_clear)
        row.addStretch(1)
        row.addWidget(btn_dir)
        cl.addLayout(row)
        self.lbl_maint = QLabel("备份为热备份（SQLite 用 backup API，运行中也安全）")
        self.lbl_maint.setObjectName("Muted")
        cl.addWidget(self.lbl_maint)
        lay.addWidget(card)

        self.table_bak = make_table(["文件", "大小", "时间"], [420, 100, 160])
        lay.addWidget(self.table_bak, 1)
        return w

    # ---------------------------------------------------------------- 生命周期
    def on_show(self) -> None:
        self._fill_stores()
        self._fill_backups()

    def _fill_stores(self) -> None:
        stores = dbstore.stores()
        cur = self.cmb_store.currentData()
        self.cmb_store.blockSignals(True)
        self.cmb_store.clear()
        for s in stores:
            self.cmb_store.addItem(f"{s.label}  ·  {s.size_text}", s.sid)
        if cur:
            for i in range(self.cmb_store.count()):
                if self.cmb_store.itemData(i) == cur:
                    self.cmb_store.setCurrentIndex(i)
                    break
        self.cmb_store.blockSignals(False)

        self.cmb_maint.clear()
        for s in stores:
            self.cmb_maint.addItem(f"{s.label}  ·  {s.path}", s.sid)

        if self.cmb_store.count():
            self._store_changed()
        else:
            self.lbl_store.setText("尚未生成任何本地库（先跑一次对应模块即可）")

    def _store_changed(self) -> None:
        sid = self.cmb_store.currentData()
        st = dbstore.store(sid) if sid else None
        self.cmb_table.blockSignals(True)
        self.cmb_table.clear()
        if st:
            self.badge_size.setText(st.size_text)
            self._store_desc = f"{st.label} ｜ {st.path} ｜ {st.hint}"
            self.lbl_store.setText(self._store_desc)
            tables = list(st.tables)
            if st.kind == "json":
                tables = [t for t in tables if dbstore._json_file(st.sid, t).exists()]
            for t in tables:
                self.cmb_table.addItem(t, t)
        self.cmb_table.blockSignals(False)
        self.reload_rows(reset=True)

    # ---------------------------------------------------------------- 行读取
    def reload_rows(self, reset: bool = False) -> None:
        sid = self.cmb_store.currentData()
        table = self.cmb_table.currentData()
        if not sid or not table:
            self.table.setRowCount(0)
            self.table.setColumnCount(1)
            self.table.setHorizontalHeaderLabels(["-"])
            return
        if reset:
            self._offset = 0
        limit = int(self.sp_page.value())
        kw = self.ed_search.text().strip()
        rows, total = dbstore.rows(sid, table, search=kw, limit=limit,
                                   offset=self._offset)
        cols = dbstore.columns(sid, table)
        self._cols = cols
        self._total = total

        self._loading = True
        self.table.setColumnCount(len(cols) + 1)
        self.table.setHorizontalHeaderLabels(["_rowid"] + cols)
        self.table.setRowCount(0)
        for row in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            key = row[0]
            item0 = QTableWidgetItem(str(key))
            item0.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(r, 0, item0)
            for c, name in enumerate(cols, start=1):
                val = row[c] if c < len(row) else ""
                item = QTableWidgetItem("" if val is None else str(val))
                item.setData(Qt.ItemDataRole.UserRole, name)
                self.table.setItem(r, c, item)
        self._loading = False
        self.table.setColumnWidth(0, 80)
        # 按内容自适应列宽（上限 240，避免某个长 URL 把整表撑飞）
        for c in range(1, self.table.columnCount()):
            self.table.resizeColumnToContents(c)
            if self.table.columnWidth(c) > 240:
                self.table.setColumnWidth(c, 240)
        shown = min(self._offset + len(rows), total)
        first = self._offset + 1 if total else 0
        self.lbl_store.setText(
            f"{self._store_desc}   第 {first}–{shown} 行 / 共 {total} 行")

    def _page(self, delta: int) -> None:
        limit = int(self.sp_page.value())
        new = self._offset + delta * limit
        if new < 0:
            new = 0
        if new >= self._total and delta > 0:
            return
        self._offset = new
        self.reload_rows()

    def _cell_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        col = item.column()
        if col == 0:
            return
        name = item.data(Qt.ItemDataRole.UserRole) or (
            self._cols[col - 1] if col - 1 < len(self._cols) else "")
        key_item = self.table.item(item.row(), 0)
        if not name or key_item is None:
            return
        sid = self.cmb_store.currentData()
        table = self.cmb_table.currentData()
        ok = dbstore.update_cell(sid, table, key_item.text(), name, item.text())
        self.notify(f"已写回 {name} = {item.text()[:40]}", "ok" if ok else "err")

    def delete_row(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            self.notify("请先选中一行", "warn")
            return
        key = self.table.item(row, 0)
        if key is None:
            return
        sid = self.cmb_store.currentData()
        table = self.cmb_table.currentData()
        if QMessageBox.question(
                self, "确认删除",
                f"从 {sid} / {table} 删除 rowid={key.text()} 这一行？\n此操作不可撤销。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        if dbstore.delete_row(sid, table, key.text()):
            self.notify("已删除 1 行", "ok")
            self.reload_rows(reset=True)
        else:
            self.notify("删除失败", "err")

    def export_csv(self) -> None:
        sid = self.cmb_store.currentData()
        table = self.cmb_table.currentData()
        if not sid or not table:
            self.notify("请先选择库与表", "warn")
            return
        out, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", str(paths.DATA / "app" / f"{table}.csv"), "CSV (*.csv)")
        if not out:
            return
        try:
            n = dbstore.export_csv(sid, table, out)
            self.notify(f"已导出 {n} 行：{paths.rel(out)}", "ok")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"导出失败：{exc}", "err")

    # ---------------------------------------------------------------- 维护
    def backup_store(self) -> None:
        sid = self.cmb_maint.currentData()
        if not sid:
            return
        p = dbstore.backup(sid)
        if p:
            self.lbl_maint.setText(f"已备份：{paths.rel(p)}")
            self.notify("备份完成", "ok")
            self._fill_backups()
        else:
            self.notify("备份失败（库文件可能不存在）", "err")

    def clear_store(self) -> None:
        sid = self.cmb_maint.currentData()
        if not sid:
            return
        if QMessageBox.question(
                self, "确认清空",
                f"清空 {sid} 的全部数据？\n建议先点「备份此库」。此操作不可撤销。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        n = dbstore.clear_store(sid)
        self.lbl_maint.setText(f"已清空 {sid}：删除 {n} 行")
        self.notify(f"已清空：{n} 行", "ok" if n else "warn")
        self._fill_stores()

    def _fill_backups(self) -> None:
        bdir = paths.DATA / "app" / "backups"
        self.table_bak.setRowCount(0)
        if not bdir.exists():
            return
        files = sorted(bdir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            r = self.table_bak.rowCount()
            self.table_bak.insertRow(r)
            self.table_bak.setItem(r, 0, QTableWidgetItem(f.name))
            self.table_bak.setItem(r, 1, QTableWidgetItem(
                f"{st.st_size / 1024 / 1024:.1f} MB"
                if st.st_size > 1024 * 1024 else f"{st.st_size / 1024:.0f} KB"))
            self.table_bak.setItem(r, 2, QTableWidgetItem(
                time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))))
