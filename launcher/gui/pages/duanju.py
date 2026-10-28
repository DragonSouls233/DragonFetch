# -*- coding: utf-8 -*-
"""短剧平台：多平台统一入库、跨站去重、只补黄果没有的。

页面结构沿用黄果（剧库 / 下载队列 / 更新检测 / 外盘同步 / 下载历史 /
站点与镜像），额外在最前面加一个「平台总览」：

  * 上半部：平台下拉 + 「扫描全平台」+「仅看独有」+ 各站统计
  * 下半部：聚合库表格（剧名 / 来源平台 / 集数 / 状态 / 已下）
    双击某条 → 切到对应平台并跳到剧库页继续浏览、选集、下载

去重口径在 hgcore/platform.py：跨站以「归一化标题」为键，主站（黄果系）
优先占位，其它平台同名的只标记为重复、不进独有列表。
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...core import backend
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from ..widgets import Card, HLine, SearchInput, make_table
from .huangguo import HuangguoPage


class DuanjuPage(HuangguoPage):
    key = "duanju"
    title = "短剧平台"
    subtitle = "多平台统一入库，跨站去重；黄果为主，其它平台只补它没有的"
    # 站点记忆用独立键：不跟黄果页互相覆盖
    _site_pref_key = "duanju.site_id"

    # ---------------------------------------------------------------- 构建
    def build(self) -> None:
        super().build()
        # 平台总览放在最前：进页面先看全局，再切到具体平台浏览
        self.tabs.insertTab(0, self._tab_platform(), "平台总览")
        self._platform_rows: list[dict] = []
        self._scan_task = None

    def _tab_platform(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        # ---- 工具条
        bar = Card()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(14, 10, 14, 10)
        bl.setSpacing(8)

        t = QLabel("短剧平台")
        t.setObjectName("H2")
        bl.addWidget(t)

        self.cmb_platform = QComboBox()
        self.cmb_platform.setMinimumWidth(200)
        self.cmb_platform.currentIndexChanged.connect(self._on_platform_changed)
        bl.addWidget(self.cmb_platform)

        btn_switch = QPushButton("切到该平台浏览")
        btn_switch.setObjectName("Ghost")
        btn_switch.clicked.connect(self._switch_to_platform)
        bl.addWidget(btn_switch)

        self.sp_scan_pages = QSpinBox()
        self.sp_scan_pages.setRange(1, 20)
        self.sp_scan_pages.setValue(3)
        self.sp_scan_pages.setToolTip("每个平台每个分类抓几页")
        bl.addWidget(self.sp_scan_pages)

        self.btn_scan = QPushButton("扫描全平台")
        self.btn_scan.setObjectName("Primary")
        self.btn_scan.clicked.connect(self.scan_platforms)
        bl.addWidget(self.btn_scan)

        self.chk_unique = QCheckBox("仅看独有")
        self.chk_unique.stateChanged.connect(self._reload_platform_table)
        bl.addWidget(self.chk_unique)

        self.ed_platform_filter = SearchInput("筛选剧名…", width=170)
        self.ed_platform_filter.textChanged.connect(self._reload_platform_table)
        bl.addWidget(self.ed_platform_filter)

        self.lbl_platform_stats = QLabel("尚未扫描")
        self.lbl_platform_stats.setObjectName("Muted")
        bl.addWidget(self.lbl_platform_stats, 1)
        lay.addWidget(bar)

        lay.addWidget(HLine())

        # ---- 聚合库表格
        self.table_platform = make_table(
            ["剧名", "来源平台", "集数", "状态", "已下", "编号"],
            [360, 130, 60, 110, 60, 80])
        self.table_platform.itemSelectionChanged.connect(self._on_platform_row)
        self.table_platform.cellDoubleClicked.connect(self._on_platform_open)
        lay.addWidget(self.table_platform, 1)

        self.lbl_platform_hint = QLabel(
            "双击一行：切到该平台并继续浏览、选集、下载。")
        self.lbl_platform_hint.setObjectName("Muted")
        lay.addWidget(self.lbl_platform_hint)
        return w

    # ---------------------------------------------------------------- 站点
    def _fill_platforms(self) -> None:
        try:
            self.cmb_platform.blockSignals(True)
            self.cmb_platform.clear()
            for s in self._ns.sites.all_sites():
                if not s.get("enabled", True):
                    continue
                role = "主" if self._ns.platform.is_primary(s.get("id")) else "补"
                self.cmb_platform.addItem(
                    f"{s.get('name')}（{role}）", s.get("id"))
            self.cmb_platform.blockSignals(False)
        except Exception:  # noqa: BLE001
            pass

    def _on_platform_changed(self, *_a) -> None:
        # 下拉只决定「切到该平台浏览」的目标，不自动切换（避免误操作）
        pass

    def _switch_to_platform(self) -> None:
        sid = self.cmb_platform.currentData()
        if not sid:
            return
        site = self._ns.sites.get_site(sid)
        if not site:
            self.notify("未找到该平台", "warn")
            return
        try:
            self._ns.config.set_active_site(site)
            settings().set(self._site_pref_key, sid)
            settings().save()
            backend.apply_huangguo_settings(self._ns)
            self._init_cover_cache()
            self._refresh_site_bar()
            self.reload()
            self.tabs.setCurrentIndex(self._index_of_library())
            self.notify(f"已切到 {site.get('name')}", "ok")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"切换失败：{exc}", "err")

    def _index_of_library(self) -> int:
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == "剧库":
                return i
        return 0

    # ---------------------------------------------------------------- 扫描
    def scan_platforms(self) -> None:
        if not self._ensure():
            return
        if self._scan_task is not None and self._scan_task.isRunning():
            self.notify("扫描进行中，请稍候", "warn")
            return
        self.btn_scan.setEnabled(False)
        pages = int(self.sp_scan_pages.value())

        def _run(progress=None):
            plat = self._ns.platform
            # 先把主站（黄果系）剧库里的剧索引进聚合表，作为判重基准
            plat.ensure_primary_index()
            res = plat.scan_all(max_pages=pages,
                                on_progress=lambda i, t, m: progress(
                                    f"{m}" if not t else f"({i}/{t}) {m}"))
            rows = plat.list_catalog()
            return res, rows

        task = Task(_run, want_progress=True)
        self._scan_task = task
        task.progress.connect(lambda msg: self.lbl_platform_stats.setText(str(msg)[:120]))
        task.done.connect(self._scan_done)
        task.failed.connect(self._scan_failed)
        CENTER.start(task)

    def _scan_done(self, payload) -> None:
        res, rows = payload
        self.btn_scan.setEnabled(True)
        self._platform_rows = list(rows or [])
        self._reload_platform_table()
        ok = sum(1 for v in (res or {}).values() if isinstance(v, int) and v > 0)
        self.lbl_platform_stats.setText(
            f"扫描完成：{ok} 个平台入库 · 聚合 {len(self._platform_rows)} 条")
        self.notify(f"平台扫描完成，共 {len(self._platform_rows)} 条", "ok")

    def _scan_failed(self, tb: str) -> None:
        self.btn_scan.setEnabled(True)
        self.lbl_platform_stats.setText("扫描失败")
        self.notify((tb or "").splitlines()[-1][:160], "err")

    # ---------------------------------------------------------------- 表格
    def _reload_platform_table(self) -> None:
        if not hasattr(self, "table_platform"):
            return
        only_unique = bool(self.chk_unique.isChecked()) if hasattr(self, "chk_unique") else False
        kw = self.ed_platform_filter.text().strip() if hasattr(self, "ed_platform_filter") else ""
        rows = self._platform_rows
        if only_unique:
            rows = [r for r in rows if int(r.get("is_unique") or 0) == 1]
        if kw:
            rows = [r for r in rows if kw in str(r.get("title") or "")]
        self.table_platform.setRowCount(0)
        for r in rows:
            i = self.table_platform.rowCount()
            self.table_platform.insertRow(i)
            uniq = int(r.get("is_unique") or 0) == 1
            dup_of = r.get("dup_of") or ""
            state = "独有" if uniq else (f"与「{self._ns.platform.site_label(dup_of)}」重复"
                                        if dup_of else "重复")
            vals = [
                str(r.get("title") or ""),
                self._ns.platform.site_label(str(r.get("site_id") or "")),
                str(r.get("ep_count") or 0),
                state,
                str(r.get("done_eps") or 0),
                str(r.get("did") or ""),
            ]
            for c, v in enumerate(vals):
                self.table_platform.setItem(i, c, QTableWidgetItem(v))
        if not rows:
            self.lbl_platform_hint.setText(
                "没有匹配的剧目：先点「扫描全平台」把各平台内容收进统一库。")
        else:
            self.lbl_platform_hint.setText(
                f"共 {len(rows)} 条 · 双击一行切到该平台浏览下载")

    def _on_platform_row(self) -> None:
        rows = self.table_platform.selectionModel().selectedRows()
        if not rows:
            return
        i = rows[0].row()
        if i >= len(self.table_platform_items()):
            return
        item = self.table_platform_items()[i]
        self.lbl_platform_hint.setText(
            f"《{item.get('title')}》 · {self._ns.platform.site_label(item.get('site_id'))}"
            f" · {item.get('ep_count') or 0} 集 · "
            + ("独有（其它平台没有）" if int(item.get("is_unique") or 0) == 1
               else f"与「{self._ns.platform.site_label(item.get('dup_of') or '')}」重复"))

    def table_platform_items(self) -> list[dict]:
        """当前表格里的数据（按插入顺序）。"""
        if not hasattr(self, "_platform_rows"):
            return []
        only_unique = bool(self.chk_unique.isChecked()) if hasattr(self, "chk_unique") else False
        kw = self.ed_platform_filter.text().strip() if hasattr(self, "ed_platform_filter") else ""
        rows = self._platform_rows
        if only_unique:
            rows = [r for r in rows if int(r.get("is_unique") or 0) == 1]
        if kw:
            rows = [r for r in rows if kw in str(r.get("title") or "")]
        return rows

    def _on_platform_open(self, row: int, col: int) -> None:
        items = self.table_platform_items()
        if row < 0 or row >= len(items):
            return
        item = items[row]
        sid = str(item.get("site_id") or "")
        idx = self.cmb_platform.findData(sid)
        if idx >= 0:
            self.cmb_platform.setCurrentIndex(idx)
        self._switch_to_platform()

    # ---------------------------------------------------------------- 生命周期
    def on_show(self) -> None:
        # 恢复本页面自己的站点（黄果页与短剧页共用全局 ACTIVE_SITE，
        # 谁进谁设，切换页面时互不干扰）
        self._ensure_platform_site()
        super().on_show()
        self._fill_platforms()
        self._reload_platform_table()

    def _ensure_platform_site(self) -> None:
        try:
            if self._ns is None:
                self._ns = backend.huangguo()
        except Exception:  # noqa: BLE001
            return
        want = str(settings().get(self._site_pref_key, "") or "").strip()
        site = self._ns.sites.get_site(want) if want else None
        if site is None:
            # 默认落在黄桃（已接入、可用），主站黄果在黄果页里
            site = self._ns.sites.get_site("aihuangdou")
        if site is None:
            return
        if self._ns.config.ACTIVE_SITE_ID != site["id"]:
            try:
                self._ns.config.set_active_site(site)
            except Exception:  # noqa: BLE001
                return
        self._init_cover_cache()

    # ---------------------------------------------------------------- 详情对照
    def _detail_done(self, drama: dict, detail: dict) -> None:
        """详情里补一行跨站对照：这部剧在别的平台有没有、那边下过几集。"""
        super()._detail_done(drama, detail)
        try:
            title = drama.get("title") or ""
            rows = self._ns.platform.lookup_by_title(title)
        except Exception:  # noqa: BLE001
            return
        others = [r for r in rows
                  if str(r.get("site_id")) != str(self._ns.config.ACTIVE_SITE_ID)]
        if not others:
            return
        bits = []
        for r in others:
            name = self._ns.platform.site_label(str(r.get("site_id")))
            bits.append(f"{name} {r.get('ep_count') or 0} 集"
                        f"（已下 {r.get('done_eps') or 0}）")
        self.lbl_detail_sub.setText(
            self.lbl_detail_sub.text() + " · 其他平台也有：" + "、".join(bits))
