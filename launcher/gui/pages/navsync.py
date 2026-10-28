# -*- coding: utf-8 -*-
"""地址同步页：糖心 / 黄果 / 麻豆 / 九色 的地址，一处同步、一处校验。

页面只做三件事：
  1. 展示四个模块「地址从哪来、现在用哪个、是不是直连能用」（读 navsync 状态）；
  2. 一键同步（后台线程，4 个目标并发），并把结果写回各模块配置；
  3. 允许改地址：手动指定 → 先直连校验，通过才落地（避免手填一个下不了的域名）。

颜色语义：绿 = 直连可用（不用代理）；黄 = 只能通过代理；红 = 同步失败。
"""
from __future__ import annotations

import time
import webbrowser
from typing import Dict, List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QGridLayout, QHBoxLayout, QInputDialog,
    QLabel, QPushButton, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...core import navsync
from ...core.logbus import LOG
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from .. import theme
from ..widgets import Card, HLine, LogPanel, StatCard, make_table
from .base import Page

HEADERS = ["模块", "地址来源", "当前地址", "直连", "延迟", "下载策略", "上次同步", "说明"]


class NavSyncPage(Page):
    key = "navsync"
    title = "地址同步"
    subtitle = "糖心 / 黄果 / 麻豆 / 九色：启动自动同步一次，直连能过就关掉代理"

    synced = pyqtSignal(dict)

    def build(self) -> None:
        self._rows: List[dict] = []
        self._task: Task | None = None

        btn_all = QPushButton("立即同步全部")
        btn_all.setObjectName("Primary")
        btn_all.clicked.connect(lambda: self.sync(None))
        self.btn_all = btn_all
        self.header.add_action(btn_all)

        self.chk_auto = QCheckBox("启动时自动同步")
        self.chk_auto.setChecked(bool(settings().get("navsync.auto_on_start", True)))
        self.chk_auto.toggled.connect(self._toggle_auto)
        self.header.add_action(self.chk_auto)

        # ---------------- 概览
        grid = QGridLayout()
        grid.setSpacing(12)
        self._stats: Dict[str, StatCard] = {}
        for idx, (k, label) in enumerate((("direct", "直连可用"),
                                          ("proxy", "只能代理"),
                                          ("fail", "同步失败"),
                                          ("last", "上次同步"))):
            card = StatCard(label, "-", "尚未同步")
            self._stats[k] = card
            grid.addWidget(card, 0, idx)
        self.body.addLayout(grid)

        # ---------------- 表格
        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(10)
        head = QHBoxLayout()
        t = QLabel("四个模块的地址状态")
        t.setObjectName("H2")
        head.addWidget(t)
        head.addStretch(1)
        self.lbl_state = QLabel("等待同步")
        self.lbl_state.setObjectName("Muted")
        head.addWidget(self.lbl_state)
        cl.addLayout(head)
        cl.addWidget(HLine())

        self.table = make_table(HEADERS, [140, 150, 250, 70, 70, 90, 120])
        self.table.setMinimumHeight(198)
        self.table.setMaximumHeight(232)
        self.table.doubleClicked.connect(lambda *_: self.open_site())
        cl.addWidget(self.table)

        row = QHBoxLayout()
        row.setSpacing(8)
        specs = [
            ("同步选中", self.sync_one, "Primary"),
            ("直连测试", self.test_one, "Ghost"),
            ("手动指定地址", self.manual, "Ghost"),
            ("打开网站", self.open_site, "Ghost"),
            ("复制地址", self.copy_url, "Ghost"),
            ("进入模块", self.goto_page, "Ghost"),
        ]
        for text, slot, obj in specs:
            b = QPushButton(text)
            b.setObjectName(obj)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        cl.addLayout(row)

        note = QLabel("这里只管糖心 / 黄果 / 麻豆 / 九色（国内、有发布页或地址页）。"
                      "国外的站点（XVideos 等）不在此列，仍按「设置 → 代理」正常走代理。")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        cl.addWidget(note)
        self.body.addWidget(card)

        # ---------------- 日志
        log_card = Card()
        ll = QVBoxLayout(log_card)
        ll.setContentsMargins(16, 12, 16, 12)
        self.log = LogPanel()
        ll.addWidget(self.log)
        self.body.addWidget(log_card, 1)

        LOG.message.connect(self.log.append)
        self._fill()

    # ---------------------------------------------------------------- 数据
    def on_show(self) -> None:
        self._fill()

    def _fill(self) -> None:
        self._rows = navsync.summary()
        tokens = theme.tokens(settings().get("theme", "dark") == "dark")
        self.table.setRowCount(0)
        direct = proxy = fail = unsynced = 0
        for r in self._rows:
            if not r["synced"]:
                unsynced += 1
            elif not r["ok"]:
                fail += 1
            elif r["direct"]:
                direct += 1
            else:
                proxy += 1
            self._fill_row(self.table.rowCount(), r, tokens)

        self._stats["direct"].set_value(direct)
        self._stats["direct"].set_hint("不用代理，直接下载")
        self._stats["proxy"].set_value(proxy)
        self._stats["proxy"].set_hint("站点只能经代理打开")
        self._stats["fail"].set_value(fail + unsynced)
        self._stats["fail"].set_hint("未同步" if unsynced else "同步失败")
        st = navsync.state()
        if st.get("at"):
            self._stats["last"].set_value(
                time.strftime("%m-%d %H:%M", time.localtime(st["at"])))
            self._stats["last"].set_hint(f"{st.get('ok', 0)}/{len(navsync.ORDER)} 可用")
        self.lbl_state.setText(navsync.last_run_text())

    def _fill_row(self, idx: int, r: dict, tokens: dict) -> None:
        self.table.insertRow(idx)
        ok = r["ok"]
        if not r["synced"]:
            status, tone = "未同步", tokens["muted"]
        elif not ok:
            status, tone = "失败", tokens["err"]
        elif r["direct"]:
            status, tone = "可用", tokens["ok"]
        else:
            status, tone = "需代理", tokens["warn"]
        policy = "直连" if (ok and r["direct"]) else ("代理" if ok else "—")
        hint = r["hint"] or r["error"] or r["note"]
        when = (time.strftime("%m-%d %H:%M", time.localtime(r["at"]))
                if r["at"] else "—")
        ms = r["latency_ms"]
        latency = "—" if not ms else (f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s")
        values = [
            r["label"], r["source"], r["url"] or "—", status,
            latency, policy, when, hint,
        ]
        for col, text in enumerate(values):
            cell = QTableWidgetItem(str(text))
            if col == 0:
                cell.setData(Qt.ItemDataRole.UserRole, r["key"])
            if col == 3:
                cell.setForeground(QBrush(QColor(tone)))
            elif col in (2, 7):
                cell.setForeground(QBrush(QColor(tokens["sub"])))
            elif col in (4, 5, 6):
                cell.setForeground(QBrush(QColor(tokens["muted"])))
            self.table.setItem(idx, col, cell)

    # ---------------------------------------------------------------- 交互
    def _toggle_auto(self, on: bool) -> None:
        s = settings()
        s.set("navsync.auto_on_start", bool(on))
        s.save()
        self.notify("启动时将自动同步地址" if on else "已关闭启动自动同步")

    def _selected(self) -> dict:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        key = item.data(Qt.ItemDataRole.UserRole) if item else ""
        if not key:
            return {}
        return next((r for r in self._rows if r["key"] == key), {})

    def _busy(self, on: bool, text: str = "") -> None:
        self.btn_all.setEnabled(not on)
        if text:
            self.lbl_state.setText(text)

    def sync(self, _keys=None) -> None:
        if self._task is not None and self._task.isRunning():
            self.notify("同步正在进行", "warn")
            return
        self._busy(True, "同步中…")
        task = Task(navsync.sync_all, None, want_progress=True)
        task.progress.connect(lambda p, t: self.lbl_state.setText(t or "同步中…"))
        task.done.connect(self._on_synced)
        task.failed.connect(self._on_failed)
        self._task = task
        CENTER.start(task)

    def sync_one(self) -> None:
        r = self._selected()
        if not r:
            self.notify("先选中一行", "warn")
            return
        if self._task is not None and self._task.isRunning():
            self.notify("同步正在进行", "warn")
            return
        self._busy(True, f"同步 {r['label']}…")
        task = Task(navsync.sync_one, r["key"], want_progress=True)
        task.done.connect(lambda item, _k=r["key"]: self._on_synced(
            {"ok": 1 if item.get("ok") else 0,
             "direct": 1 if item.get("ok") and item.get("direct") else 0,
             "total": 1, "targets": {_k: item}}))
        task.failed.connect(self._on_failed)
        self._task = task
        CENTER.start(task)

    def test_one(self) -> None:
        r = self._selected()
        if not r or not r["url"]:
            self.notify("这一行还没有地址", "warn")
            return
        self.lbl_state.setText(f"直连测试 {r['url']} …")
        task = Task(self._probe, r["url"])
        task.done.connect(lambda p, _r=r: self._on_probe(p, _r))
        task.failed.connect(self._on_failed)
        CENTER.start(task)

    @staticmethod
    def _probe(url: str) -> dict:
        return navsync.probe_direct(url, timeout=float(
            settings().get("navsync.timeout", 8) or 8))

    def _on_probe(self, p: dict, row: dict) -> None:
        if p.get("ok"):
            self.notify(f"{row['label']} 直连可达 · {p['ms']} ms", "ok")
            self.log.append("ok", f"{row['label']} 直连可达 {p['url']} · {p['ms']} ms")
        else:
            self.notify(f"{row['label']} 直连不通：{p.get('error', '')[:60]}", "warn")
            self.log.append("warn", f"{row['label']} 直连不通 {p.get('url', '')} · "
                                    f"{p.get('error', '')}")
        self.lbl_state.setText(navsync.last_run_text())

    def manual(self) -> None:
        r = self._selected()
        if not r:
            self.notify("先选中一行", "warn")
            return
        url, ok = QInputDialog.getText(
            self, f"手动指定 · {r['label']}",
            "地址（会先做直连校验，通过才生效）：", text=r["url"])
        if not ok or not url.strip():
            return
        self._busy(True, f"校验 {url.strip()} …")
        task = Task(navsync.manual_set, r["key"], url.strip())
        task.done.connect(lambda item: self._on_manual(item))
        task.failed.connect(self._on_failed)
        self._task = task
        CENTER.start(task)

    def _on_manual(self, item: dict) -> None:
        self._busy(False)
        if item.get("ok"):
            self.notify(f"已切换为 {item['url']}", "ok")
        else:
            self.notify(f"地址未生效：{item.get('error', '')[:70]}", "err")
        self._fill()
        self.synced.emit({item.get("key", ""): item})

    def open_site(self) -> None:
        r = self._selected()
        if not r or not r["url"]:
            self.notify("这一行还没有地址", "warn")
            return
        webbrowser.open(r["url"])

    def copy_url(self) -> None:
        from PyQt6.QtWidgets import QApplication

        r = self._selected()
        if not r or not r["url"]:
            self.notify("这一行还没有地址", "warn")
            return
        QApplication.clipboard().setText(r["url"])
        self.notify(f"已复制 {r['url']}", "ok")

    def goto_page(self) -> None:
        r = self._selected()
        if not r:
            self.notify("先选中一行", "warn")
            return
        self.switch_page(r.get("page") or "home")

    # ---------------------------------------------------------------- 回调
    def _on_synced(self, res: dict) -> None:
        self._busy(False)
        self._fill()
        ok, total = res.get("ok", 0), res.get("total", 0)
        direct = res.get("direct", 0)
        msg = f"地址同步完成：{ok}/{total} 可用"
        if direct:
            msg += f"，直连 {direct} 个"
        self.notify(msg, "ok" if ok else "warn")
        self.log.append("ok" if ok else "warn", msg)
        for key, item in (res.get("targets") or {}).items():
            if item.get("ok"):
                self.log.append("ok", f"{item.get('label', key)} → {item.get('url')}"
                                      f"（{'直连' if item.get('direct') else '需代理'}）")
            else:
                self.log.append("warn", f"{item.get('label', key)} 失败："
                                        f"{item.get('error') or '未知原因'}")
        win = self.window()
        if win is not None and hasattr(win, "sidebar"):
            try:
                win.sidebar.set_status(navsync.last_run_text())
                win.refresh_side_status()
            except Exception:  # noqa: BLE001
                pass
        self.synced.emit(res.get("targets") or {})

    def _on_failed(self, tb: str) -> None:
        self._busy(False)
        self.lbl_state.setText("同步失败")
        self.notify(tb.splitlines()[-1][:120] if tb else "同步失败", "err")
        self.log.append("err", tb.splitlines()[-1][:160] if tb else "同步失败")
