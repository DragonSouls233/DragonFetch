# -*- coding: utf-8 -*-
"""麻豆磁力：独立模块（``modules/madouqu``），只做一件事——按关键词把 BT 磁力搜出来。

为什么单独一页
--------------
麻豆走的是完全不同的链路（HTML 抓取 + 磁力链接，不需要 yt-dlp），
把它塞进「多站点」只会让那页的语义变模糊。所以它有自己的页面、自己的
``DATA/madouqu`` 目录、自己的入口。

界面：
  域名 [........] [检测域名]   页数 [N]   [关键词......] [搜索] [停止]
  ┌ 结果表：选择 | 标题 | 大小 | 日期 | 磁力数 | 详情页
  [全选] [复制磁力] [导出CSV] [打开详情] [入库]
  ┌ 日志
  库统计：模特 N · 影片 N · 磁力 N
"""
from __future__ import annotations

import csv
import os
import time

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QSpinBox, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...core import backend, paths
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from ..widgets import Card, LogPanel, make_table
from .base import Page

COLUMNS = ["选择", "标题", "大小", "日期", "磁力数", "详情页"]
WIDTHS = [58, 480, 110, 110, 80, 300]


class MadouquPage(Page):
    key = "madouqu"
    title = "麻豆磁力"
    subtitle = "独立模块：按关键词抓取 madouqu 的 BT 磁力链接，结果可入库、可导出"

    # ---------------------------------------------------------------- 构建
    def build(self) -> None:
        self._videos: list = []
        self._loading = False

        btn_refresh = QPushButton("刷新库统计")
        btn_refresh.setObjectName("Ghost")
        btn_refresh.clicked.connect(self.refresh_stats)
        self.header.add_action(btn_refresh)

        self.body.addWidget(self._card_toolbar(), 0)
        self.body.addWidget(self._build_table(), 1)
        self.body.addWidget(self._build_actions(), 0)
        self.body.addWidget(self._build_log(), 0)
        self.body.addWidget(self._build_stats(), 0)

    def _card_toolbar(self) -> QWidget:
        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.ed_base = QLineEdit(str(settings().get("madouqu.base_url", "https://madouqu.sbs")))
        self.ed_base.setPlaceholderText("站点域名，例如 https://madouqu.sbs")
        self.btn_test = QPushButton("检测域名")
        self.btn_test.setObjectName("Ghost")
        self.btn_test.clicked.connect(self.test_domain)
        self.btn_find = QPushButton("自动找可用域名")
        self.btn_find.setObjectName("Ghost")
        self.btn_find.clicked.connect(self.find_domain)
        self.sp_pages = QSpinBox()
        self.sp_pages.setRange(1, 30)
        self.sp_pages.setValue(int(settings().get("madouqu.max_pages", 5) or 5))
        self.sp_pages.setPrefix("页数 ")
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setObjectName("Muted")
        row1.addWidget(QLabel("域名"))
        row1.addWidget(self.ed_base, 1)
        row1.addWidget(self.btn_test)
        row1.addWidget(self.btn_find)
        row1.addWidget(self.sp_pages)
        row1.addWidget(self.lbl_status, 1)
        cl.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.ed_kw = QLineEdit()
        self.ed_kw.setPlaceholderText("关键词 / 模特名，回车开始搜索")
        self.ed_kw.returnPressed.connect(self.do_search)
        self.btn_search = QPushButton("搜索")
        self.btn_search.setObjectName("Primary")
        self.btn_search.clicked.connect(self.do_search)
        row2.addWidget(self.ed_kw, 1)
        row2.addWidget(self.btn_search)
        cl.addLayout(row2)
        return card

    def _build_table(self) -> QWidget:
        self.table = make_table(COLUMNS, WIDTHS, stretch_last=True)
        self.table.itemDoubleClicked.connect(self._row_double_clicked)
        return self.table

    def _build_actions(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(8)
        self.btn_all = QPushButton("全选")
        self.btn_all.setObjectName("Ghost")
        self.btn_all.clicked.connect(lambda: self.set_all_checked(True))
        self.btn_copy = QPushButton("复制磁力")
        self.btn_copy.setObjectName("Ghost")
        self.btn_copy.clicked.connect(self.copy_magnet)
        self.btn_csv = QPushButton("导出CSV")
        self.btn_csv.setObjectName("Ghost")
        self.btn_csv.clicked.connect(self.export_csv)
        self.btn_page = QPushButton("打开详情")
        self.btn_page.setObjectName("Ghost")
        self.btn_page.clicked.connect(self.open_detail)
        self.btn_save = QPushButton("入库")
        self.btn_save.setObjectName("Primary")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.save_to_db)
        self.lbl_count = QLabel("共 0 条 · 已选 0 条")
        self.lbl_count.setObjectName("Muted")
        for b in (self.btn_all, self.btn_copy, self.btn_csv, self.btn_page,
                  self.btn_save):
            row.addWidget(b)
        row.addSpacing(8)
        row.addWidget(self.lbl_count, 1)
        return w

    def _build_log(self) -> QWidget:
        self.log = LogPanel()
        self.log.setMaximumHeight(150)
        return self.log

    def _build_stats(self) -> QWidget:
        card = Card()
        lay = QHBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(18)
        self.lbl_stats = QLabel("库统计：读取中…")
        self.lbl_stats.setObjectName("Sub")
        lay.addWidget(self.lbl_stats, 1)
        return card

    # ---------------------------------------------------------------- 生命周期
    def on_show(self) -> None:
        self.refresh_stats()

    def refresh_stats(self) -> None:
        try:
            st = backend.madouqu_db().get_stats()
            self.lbl_stats.setText(
                f"库统计：模特 {st.get('model_count', 0)} · "
                f"影片 {st.get('video_count', 0)} · "
                f"磁力 {st.get('magnet_count', 0)}　"
                f"（{paths.rel(paths.data_dir('madouqu'))}）")
        except Exception as exc:  # noqa: BLE001
            self.lbl_stats.setText(f"库统计不可用：{exc}")

    # ---------------------------------------------------------------- 域名
    def test_domain(self) -> None:
        url = self.ed_base.text().strip()
        if not url:
            self.notify("请填写域名", "warn")
            return
        self._set_busy(True, "检测域名…")

        def _run(progress=None):
            c = backend.madouqu_crawler()
            try:
                return c.test_url(url)
            finally:
                try:
                    c.close()
                except Exception:
                    pass

        task = Task(_run)
        task.done.connect(lambda ok: self._domain_done(ok, url))
        task.failed.connect(self._failed)
        CENTER.start(task)

    def find_domain(self) -> None:
        self._set_busy(True, "寻找可用域名…")

        def _run(progress=None):
            c = backend.madouqu_crawler()
            try:
                return c.find_working_url()
            finally:
                try:
                    c.close()
                except Exception:
                    pass

        task = Task(_run)
        task.done.connect(self._find_done)
        task.failed.connect(self._failed)
        CENTER.start(task)

    def _domain_done(self, ok, url: str) -> None:
        self._set_busy(False, "可用" if ok else "不可用")
        self.log.append("ok" if ok else "err",
                        f"{url} → {'可访问' if ok else '无法访问（可能需要代理）'}")
        self.notify("域名可用" if ok else "域名不可用", "ok" if ok else "err")

    def _find_done(self, url: str | None) -> None:
        self._set_busy(False, "找到域名" if url else "全部不可用")
        if url:
            self.ed_base.setText(url)
            settings().set("madouqu.base_url", url)
            settings().save()
            self.log.append("ok", f"可用域名：{url}")
            self.notify(f"可用域名：{url}", "ok")
        else:
            self.log.append("err", "所有备用域名都不可用（检查代理 / 网络）")
            self.notify("没有可用域名", "err")

    # ---------------------------------------------------------------- 搜索
    def do_search(self) -> None:
        kw = self.ed_kw.text().strip()
        if not kw:
            self.notify("请输入关键词", "warn")
            return
        base = self.ed_base.text().strip()
        pages = int(self.sp_pages.value())
        if base:
            settings().set("madouqu.base_url", base)
            settings().set("madouqu.max_pages", pages)
            settings().save()
        self._set_busy(True, "搜索中…")
        self.log.append("info", f"搜索「{kw}」（{pages} 页，含详情页磁力）")

        def _run(progress=None):
            c = backend.madouqu_crawler()
            if base:
                c.set_base_url(base)
            try:
                return c.search(kw, max_pages=pages, fetch_details=True)
            finally:
                try:
                    c.close()
                except Exception:
                    pass

        task = Task(_run)
        task.done.connect(self._search_done)
        task.failed.connect(self._failed)
        CENTER.start(task)

    def _search_done(self, result) -> None:
        self._set_busy(False, "搜索完成")
        videos = list(getattr(result, "videos", []) or [])
        self._videos = videos
        self._loading = True
        self.table.setRowCount(0)
        for v in videos:
            r = self.table.rowCount()
            self.table.insertRow(r)
            chk = QTableWidgetItem("")
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
                         | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(r, 0, chk)
            self.table.setItem(r, 1, QTableWidgetItem(getattr(v, "title", "") or ""))
            self.table.setItem(r, 2, QTableWidgetItem(getattr(v, "size", "") or ""))
            self.table.setItem(r, 3, QTableWidgetItem(getattr(v, "date", "") or ""))
            magnets = getattr(v, "magnet_links", []) or []
            self.table.setItem(r, 4, QTableWidgetItem(str(len(magnets))))
            self.table.setItem(r, 5, QTableWidgetItem(getattr(v, "detail_url", "") or ""))
        self._loading = False
        self._recount()

        ok = bool(getattr(result, "success", True))
        msg = (f"{getattr(result, 'model_name', '')} · {len(videos)} 条 · "
               f"磁力 {getattr(result, 'total_magnets', 0)} · "
               f"{getattr(result, 'duration_seconds', 0):.1f}s")
        if not ok:
            msg += f" · {getattr(result, 'error_message', '')}"
        self.log.append("ok" if ok else "warn", msg)
        self.btn_save.setEnabled(bool(videos))
        self.notify(f"搜索完成：{len(videos)} 条", "ok" if videos else "warn")

    # ---------------------------------------------------------------- 表格
    def _row_checked(self, row: int) -> bool:
        it = self.table.item(row, 0)
        return bool(it and it.checkState() == Qt.CheckState.Checked)

    def _selected_rows(self) -> list[int]:
        if not self._videos:
            return []
        rows = [r for r in range(len(self._videos)) if self._row_checked(r)]
        if rows:
            return rows
        cur = self.table.currentRow()
        return [cur] if 0 <= cur < len(self._videos) else []

    def set_all_checked(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self._loading = True
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it:
                it.setCheckState(state)
        self._loading = False
        self._recount()

    def _recount(self) -> None:
        total = len(self._videos)
        picked = sum(1 for r in range(total) if self._row_checked(r))
        self.lbl_count.setText(f"共 {total} 条 · 已选 {picked} 条")

    def _row_double_clicked(self, item: QTableWidgetItem) -> None:
        if item.column() == 5 and item.text().strip():
            QDesktopServices.openUrl(QUrl(item.text().strip()))

    def _magnets_of(self, row: int) -> list:
        if not (0 <= row < len(self._videos)):
            return []
        return list(getattr(self._videos[row], "magnet_links", []) or [])

    def copy_magnet(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self.notify("请先选中一条结果", "warn")
            return
        if len(rows) == 1:
            magnets = self._magnets_of(rows[0])
            if not magnets:
                self.notify("这条没有磁力链接（详情页可能没抓到）", "warn")
                return
            QApplication.clipboard().setText(getattr(magnets[0], "link", "") or "")
            self.notify(f"已复制磁力（共 {len(magnets)} 条中的第 1 条）", "ok")
            return
        lines = []
        for r in rows:
            for m in self._magnets_of(r):
                link = getattr(m, "link", "") or ""
                if link:
                    lines.append(link)
        if not lines:
            self.notify("所选条目都没有磁力链接", "warn")
            return
        QApplication.clipboard().setText("\n".join(lines))
        self.notify(f"已复制 {len(lines)} 条磁力", "ok")

    def export_csv(self) -> None:
        if not self._videos:
            self.notify("结果为空", "warn")
            return
        default = paths.DATA / "madouqu" / f"madouqu_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        out, _ = QFileDialog.getSaveFileName(self, "导出 CSV", str(default),
                                             "CSV (*.csv)")
        if not out:
            return
        try:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["标题", "大小", "日期", "磁力数", "详情页", "磁力链接"])
                for v in self._videos:
                    magnets = getattr(v, "magnet_links", []) or []
                    w.writerow([getattr(v, "title", ""), getattr(v, "size", "") or "",
                                getattr(v, "date", "") or "", len(magnets),
                                getattr(v, "detail_url", "") or "",
                                " | ".join(getattr(m, "link", "") or "" for m in magnets)])
            self.log.append("ok", f"已导出 {len(self._videos)} 条 → {paths.rel(out)}")
            self.notify("导出完成", "ok")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"导出失败：{exc}", "err")

    def open_detail(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self.notify("请先选中结果", "warn")
            return
        n = 0
        for r in rows[:10]:
            url = getattr(self._videos[r], "detail_url", "") or ""
            if url:
                QDesktopServices.openUrl(QUrl(url))
                n += 1
        self.log.append("info" if n else "warn",
                        f"已打开 {n} 个详情页" if n else "所选条目没有详情页地址")

    # ---------------------------------------------------------------- 入库
    def save_to_db(self) -> None:
        if not self._videos:
            return
        rows = self._selected_rows() or list(range(len(self._videos)))
        picked = [self._videos[r] for r in rows]
        name = self.ed_kw.text().strip() or "未命名"

        def _run(progress=None):
            db = backend.madouqu_db()
            return db.add_new_videos(name, picked)

        task = Task(_run)
        task.done.connect(self._save_done)
        task.failed.connect(self._failed)
        CENTER.start(task)

    def _save_done(self, n: int) -> None:
        self.log.append("ok" if n else "warn", f"入库 {n} 条（去重后）")
        self.notify(f"已入库 {n} 条（去重后）", "ok" if n else "warn")
        self.refresh_stats()

    # ---------------------------------------------------------------- 其他
    def _failed(self, tb: str) -> None:
        self._set_busy(False, "失败")
        last = (tb or "").strip().splitlines()[-1] if tb else "未知错误"
        self.log.append("err", last[:200])
        self.notify(last[:160], "err")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        for b in (self.btn_search, self.btn_test, self.btn_find):
            b.setEnabled(not busy)
        if text:
            self.lbl_status.setText(text)
