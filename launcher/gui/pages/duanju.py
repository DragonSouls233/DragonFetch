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
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ...core import backend, paths
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from ..cover_cache import CoverCache
from ..cover_grid import CoverGrid
from ..widgets import Card, HLine, SearchInput, make_table
from .huangguo import HuangguoPage, _ui_tokens


def _looks_like_image(data: bytes) -> bool:
    return (data[:3] == b"\xff\xd8\xff"          # JPEG
            or data[:4] == b"\x89PNG"            # PNG
            or data[:4] == b"RIFF"               # WEBP
            or data[:4] == b"GIF8")              # GIF


def _decode_any(data: bytes):
    """聚合库混着**多个站点**的封面，各站加密方式不同（aes / xor / 明文）。

    ``decrypt_cover()`` 不传 mode 时按当前站点解 ⇒ 其它站点的图全解不出来
    （实测：huangguome 用 xor，按 aes 解返回 None ⇒ 整屏 40 张空卡）。
    这里三种模式都试一遍，认出图像头就返回。
    """
    try:
        dec = backend.huangguo().media_crypto.decrypt_cover
    except Exception:                            # noqa: BLE001
        return None
    for mode in ("aes", "xor", ""):
        try:
            out = dec(data, mode)
        except Exception:                        # noqa: BLE001
            continue
        if out and _looks_like_image(out):
            return out
    # 本来就是明文（没加密的站），原样返回让 QImage 自己认
    return data if _looks_like_image(data) else None


class _PlatformWall(CoverGrid):
    """平台总览的封面墙：聚合库跨多站，did 会重名 ⇒ key 带上 site_id。"""

    def __init__(self, tokens: dict, parent=None):
        super().__init__(tokens, (160, 224), None, parent=parent)

    def set_rows(self, rows: list[dict]) -> None:
        items = []
        for r in rows or []:
            sid = str(r.get("site_id") or "")
            did = str(r.get("did") or "")
            if not did:
                continue
            # 聚合库里有脏数据：cover_url 存的是字符串 "0"（采集时没拿到图）。
            # 直接当 URL 去请求 ⇒ 每张都 miss，首屏一片"无封面"。这里只认 http。
            url = str(r.get("cover_url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                url = ""
            items.append({
                "key": f"{sid}:{did}",
                "cover": url,
                "title": str(r.get("title") or ""),
                "total": int(r.get("ep_count") or 0),
                "sub": f"{r.get('score') or ''}分" if r.get("score") else "",
                "data": r,
            })
        # 有封面的排前面（稳定排序，不打乱同组内的原有顺序）：
        # 1000+ 条里混着两百多条没图的，不排的话首屏可能整屏都是空卡。
        items.sort(key=lambda d: 0 if d["cover"] else 1)
        self.set_items(items)


class DuanjuPage(HuangguoPage):
    key = "duanju"
    title = "短剧平台"
    subtitle = "多平台统一入库，跨站去重；黄果为主，其它平台只补它没有的"
    # 站点记忆用独立键：不跟黄果页互相覆盖
    _site_pref_key = "duanju.site_id"

    # ---------------------------------------------------------------- 构建
    def build(self) -> None:
        super().build()
        # 平台总览放在最前：进页面先看全局，再切到具体平台浏览。
        # 注意 insertTab 会把原来的 currentIndex 往后顶（剧库变 1），
        # 所以插完要显式切回 0，否则进来看到的是剧库页。
        self.tabs.insertTab(0, self._tab_platform(), "平台总览")
        self.tabs.setCurrentIndex(0)
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

        # ---- 各站与黄果主库的重合度（用户要的「对照」结论）
        ov = Card()
        ol = QVBoxLayout(ov)
        ol.setContentsMargins(14, 8, 14, 8)
        ol.setSpacing(4)
        t2 = QLabel("与黄果主库的对照")
        t2.setObjectName("H2")
        ol.addWidget(t2)
        self.lbl_overlap = QLabel("尚未扫描：点「扫描全平台」后给出各平台重合率")
        self.lbl_overlap.setObjectName("Muted")
        self.lbl_overlap.setWordWrap(True)
        ol.addWidget(self.lbl_overlap)
        lay.addWidget(ov)

        lay.addWidget(HLine())

        # ---- 聚合库：列表 + 封面墙
        # 总览页是进「短剧平台」第一眼看到的页，这里只有表格的话用户看到的就是
        # "短剧平台没有封面"。聚合库本身带 cover_url（实测 1000+ 条），
        # 所以两个视图并列：表格用来筛选/看状态，封面墙用来扫片。
        self.views_platform = QTabWidget()

        tab_list = QWidget()
        tl = QVBoxLayout(tab_list)
        tl.setContentsMargins(0, 0, 0, 0)
        self.table_platform = make_table(
            ["剧名", "来源平台", "集数", "状态", "已下", "编号"],
            [360, 130, 60, 110, 60, 80])
        self.table_platform.itemSelectionChanged.connect(self._on_platform_row)
        self.table_platform.cellDoubleClicked.connect(self._on_platform_open)
        tl.addWidget(self.table_platform, 1)
        self.views_platform.addTab(tab_list, "列表")

        tab_wall = QWidget()
        tw = QVBoxLayout(tab_wall)
        tw.setContentsMargins(0, 0, 0, 0)
        self.wall_platform = _PlatformWall(_ui_tokens())
        self.wall_platform.activated.connect(self._on_wall_open)
        tw.addWidget(self.wall_platform, 1)
        self.views_platform.addTab(tab_wall, "封面墙")

        lay.addWidget(self.views_platform, 1)

        self.lbl_platform_hint = QLabel(
            "双击一行（或封面墙卡片）：切到该平台并继续浏览、选集、下载。")
        self.lbl_platform_hint.setObjectName("Muted")
        lay.addWidget(self.lbl_platform_hint)
        return w

    def _on_wall_open(self, item: dict) -> None:
        """封面墙双击：切到该条所属平台。"""
        data = item.get("data") or {}
        sid = str(data.get("site_id") or "")
        if not sid:
            return
        idx = self.cmb_platform.findData(sid)
        if idx >= 0:
            self.cmb_platform.setCurrentIndex(idx)
        self._switch_to_platform()

    # ---------------------------------------------------------------- 站点
    def _fill_platforms(self) -> None:
        try:
            self.cmb_platform.blockSignals(True)
            self.cmb_platform.clear()
            for s in self._ns.sites.all_sites():
                if not s.get("enabled", True):
                    continue
                role = "主" if self._ns.platform.is_primary(s.get("id")) else "补"
                # 取不到直链的站标「仅浏览」，免得用户以为能下
                mark = "" if s.get("downloadable", True) else "（仅浏览）"
                self.cmb_platform.addItem(
                    f"{s.get('name')}（{role}）{mark}", s.get("id"))
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
            if not site.get("downloadable", True):
                self.notify(f"{site.get('name')}：{site.get('note', '')}", "warn")
            else:
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
        self._update_stats_text()
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
        self._sync_wall(rows)
        if not rows:
            self.lbl_platform_hint.setText(
                "没有匹配的剧目：先点「扫描全平台」把各平台内容收进统一库。")
        else:
            self.lbl_platform_hint.setText(
                f"共 {len(rows)} 条 · 双击一行切到该平台浏览下载")

    def _sync_wall(self, rows: list[dict]) -> None:
        """把聚合库的封面同步到封面墙（缓存后置，后端就绪才有）。"""
        wall = getattr(self, "wall_platform", None)
        if wall is None:
            return
        if getattr(self, "_wall_cover", None) is None:
            ns = self._ns
            if ns is None:
                return
            try:
                self._wall_cover = CoverCache(
                    paths.data_dir("huangguo", "covers", "_platform"),
                    decode=_decode_any,
                    fetch=lambda u: ns.http_client.fetch_bytes(u, timeout=15),
                    size=(int(ns.config.COVER_WIDTH), int(ns.config.COVER_HEIGHT)),
                )
            except Exception:                    # noqa: BLE001
                self._wall_cover = None
        if self._wall_cover is not None and wall._cache is not self._wall_cover:
            wall.set_cache(self._wall_cover)
        wall.set_rows(rows)

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
        self._load_platform_rows()
        self._reload_platform_table()

    def _load_platform_rows(self) -> None:
        """从聚合库里读回已收录的剧目（不用先扫描就能看到历史结果）。"""
        try:
            self._platform_rows = list(self._ns.platform.list_catalog())
        except Exception:  # noqa: BLE001
            self._platform_rows = []
        self._update_stats_text()

    def _update_stats_text(self) -> None:
        """顶部统计：各平台收录条数 + 独有 / 重复，并给出与黄果的重合率。"""
        try:
            stats = self._ns.platform.stats()
        except Exception:  # noqa: BLE001
            return
        if not stats:
            self.lbl_platform_stats.setText("尚未扫描")
            return
        total = sum(v["total"] for v in stats.values())
        uniq = sum(v["unique"] for v in stats.values())
        bits = " · ".join(
            f"{self._ns.platform.site_label(sid)} {v['total']}"
            for sid, v in stats.items())
        self.lbl_platform_stats.setText(
            f"共 {total} 条 · 独有 {uniq} · 重复 {total - uniq}　|　{bits}")
        self._update_overlap_text()

    def _update_overlap_text(self) -> None:
        """各补充站与黄果主库的重合率（重合过多的站会标出建议）。"""
        try:
            report = self._ns.platform.overlap_report()
        except Exception:  # noqa: BLE001
            return
        if not report:
            self.lbl_overlap.setText("尚未扫描：点「扫描全平台」后给出各平台重合率")
            return
        lines = []
        for r in report:
            flag = "建议纳入" if r.get("recommend") else "重合过多·建议不纳入"
            lines.append(
                f"{r['name']}：本站 {r['total']} 部，与黄果重合 {r['dup']} 部"
                f"（{r['overlap_rate'] * 100:.0f}%），黄果没有的 {r['unique']} 部 → {flag}")
        self.lbl_overlap.setText("\n".join(lines))

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
