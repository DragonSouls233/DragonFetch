# -*- coding: utf-8 -*-
"""黄果短剧：发布页（代理）→ 镜像域名 → 剧库 → 选集/批量 → 并发队列。

2026-10 重建：从单页改为六标签工作区，把原 GUI 里被砍掉的入口全部补回：
  剧库 / 下载队列 / 更新检测 / 外盘同步 / 下载历史 / 站点与镜像
"""
from __future__ import annotations

import os
import time

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QSplitter, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ...core import backend, paths
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from .. import theme
from ..cover_cache import CoverCache
from ..cover_grid import CoverGrid
from ..widgets import Badge, Card, HLine, SearchInput, make_table, open_path
from .base import Page


def _ui_tokens() -> dict:
    """当前主题的配色（页面里的卡片/角标要按主题走，不能写死深色）。"""
    return theme.tokens(settings().get("theme", "dark") != "light")


class _QueueBridge(QObject):
    """把下载线程的回调安全地转成本线程信号。"""

    task_update = pyqtSignal(object)
    log = pyqtSignal(str)


# 封面网格改用通用组件（媒体库各模块共用一份，避免"有的模块有封面、有的没有"）。
# 黄果的字段名与通用件不同（did / cover_url / episode_count），在这里做映射：
def _drama_sub(d: dict) -> str:
    parts = []
    if d.get("score"):
        parts.append(f"{d['score']}分")
    if d.get("episode_count"):
        parts.append(f"{d['episode_count']}集")
    return " \u00b7 ".join(parts)


class _DramaGrid(CoverGrid):
    """黄果剧库网格：字段映射成通用件的 key/cover/total。"""

    def __init__(self, tokens: dict, cover: tuple[int, int],
                 cache: CoverCache | None = None, parent=None):
        super().__init__(tokens, cover, cache, key_field="did",
                         cover_field="cover_url", total_field="episode_count",
                         sub_fn=_drama_sub, parent=parent)


class HuangguoPage(Page):
    key = "huangguo"
    title = "黄果短剧"
    subtitle = "先用代理读发布页拿最新镜像域名，之后浏览与下载都直连"
    # 「上次停留在哪个站点」记在设置里的键名。短剧平台页继承本页，
    # 但用自己的键，避免两个页面互相覆盖站点记忆。
    _site_pref_key = "huangguo.site_id"

    # ---------------------------------------------------------------- 构建
    def build(self) -> None:
        self._ns = None
        self._queue = None
        self._bridge = None
        self._dramas = []
        self._detail = None
        self._detail_drama = None
        self._cover = None
        self._cover_site = ""
        self._eps_boxes: list[QCheckBox] = []
        self._row_keys: dict[tuple, int] = {}
        self._updates: list = []
        self._sync_targets: list[dict] = []
        self._scanning = False

        btn_open = QPushButton("打开下载目录")
        btn_open.setObjectName("Ghost")
        btn_open.clicked.connect(
            lambda: open_path(settings().get("huangguo.download_dir", "")))
        self.header.add_action(btn_open)
        self.btn_mirror = QPushButton("代理刷新域名")
        self.btn_mirror.setObjectName("Primary")
        self.btn_mirror.clicked.connect(self.refresh_mirror)
        self.header.add_action(self.btn_mirror)

        # 站点状态
        site_card = Card()
        sl = QHBoxLayout(site_card)
        sl.setContentsMargins(16, 10, 16, 10)
        self.badge_site = Badge("站点 -")
        self.badge_domain = Badge("域名 -")
        self.badge_proxy = Badge("直连")
        self.lbl_stats = QLabel("剧库读取中…")
        self.lbl_stats.setObjectName("Muted")
        sl.addWidget(self.badge_site)
        sl.addWidget(self.badge_domain)
        sl.addWidget(self.badge_proxy)
        sl.addWidget(self.lbl_stats, 1)
        self.body.addWidget(site_card)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_library(), "剧库")
        self.tabs.addTab(self._tab_queue(), "下载队列")
        self.tabs.addTab(self._tab_updates(), "更新检测")
        self.tabs.addTab(self._tab_sync(), "外盘同步")
        self.tabs.addTab(self._tab_history(), "下载历史")
        self.tabs.addTab(self._tab_sites(), "站点与镜像")
        self.tabs.currentChanged.connect(self._on_tab)
        # 2026-10-29：媒体库模块不再内置预览面板（播放器/浏览器都不塞）。
        # 全部宽度给封面网格，看片走侧栏独立的「浏览器」TAB。
        self.preview = None
        self.set_content(self.tabs)

        self._timer = QTimer(self)
        self._timer.setInterval(900)
        self._timer.timeout.connect(self._poll_queue)
        self._timer.start()

    # ---------------------------------------------------------------- 剧库页
    def _tab_library(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        split = QSplitter(Qt.Orientation.Horizontal)

        left = Card()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(14, 12, 14, 12)
        ll.setSpacing(8)
        lh = QHBoxLayout()
        t = QLabel("剧库")
        t.setObjectName("H2")
        lh.addWidget(t)
        # 分类切换（AI短剧 / 漫剧 / 换脸 / 魔改 / 排名 / 专题）：
        # 站点首页那几个分类入口以前在界面上根本选不了，只能抓默认第一个
        # 分类。这里做成一个下拉，选完既过滤库内、也决定「在线抓取」抓哪个。
        self.cmb_category = QComboBox()
        self.cmb_category.setMinimumWidth(150)
        self.cmb_category.setToolTip("切换站点分类：选完点「在线抓取」抓该分类，库内也会按分类过滤")
        self.cmb_category.currentIndexChanged.connect(self._on_category_changed)
        lh.addWidget(self.cmb_category)
        lh.addStretch(1)
        self.ed_filter = SearchInput("筛选剧名…", width=170)
        self.ed_filter.textChanged.connect(self._filter)
        lh.addWidget(self.ed_filter)
        self.btn_batch = QPushButton("批量选择")
        self.btn_batch.setObjectName("Ghost")
        self.btn_batch.setToolTip("进入多选模式：在封面墙上勾选多部剧，再点底部「批量下载」")
        self.btn_batch.clicked.connect(lambda: self.grid.enter_select_mode())
        lh.addWidget(self.btn_batch)
        self.sp_pages = QSpinBox()
        self.sp_pages.setRange(1, 10)
        self.sp_pages.setValue(2)
        self.btn_online = QPushButton("在线抓取")
        self.btn_online.setObjectName("Ghost")
        self.btn_online.clicked.connect(self.fetch_online)
        self.btn_reload = QPushButton("刷新")
        self.btn_reload.setObjectName("Ghost")
        self.btn_reload.clicked.connect(self.reload)
        lh.addWidget(self.sp_pages)
        lh.addWidget(self.btn_online)
        lh.addWidget(self.btn_reload)
        ll.addLayout(lh)

        # 扫描下载目录：把「磁盘上已经有的集」写回库，卡片角标才会显示已下 N/M
        oh = QHBoxLayout()
        self.btn_scan = QPushButton("扫描下载目录")
        self.btn_scan.setObjectName("Ghost")
        self.btn_scan.setToolTip("用 ffprobe 逐个校验下载目录，把已经下好的集标记为「已下载」")
        self.btn_scan.clicked.connect(self.scan_downloads)
        self.lbl_scan = QLabel("")
        self.lbl_scan.setObjectName("Muted")
        oh.addWidget(self.btn_scan)
        oh.addWidget(self.lbl_scan, 1)
        self.btn_warm = QPushButton("预下载封面")
        self.btn_warm.setObjectName("Ghost")
        self.btn_warm.setToolTip(
            "把当前列表里所有封面提前抓下来，存到数据库同目录的 covers 缓存里。\n"
            "下次打开（或断网时）直接读磁盘，不用等网络。")
        self.btn_warm.clicked.connect(self.warm_covers)
        oh.addWidget(self.btn_warm)
        ll.addLayout(oh)

        # 尺寸与 hgcore.config.COVER_WIDTH/COVER_HEIGHT 保持一致；
        # 这里写常量是为了不在建界面时就去初始化黄果后端（那会顺带建库）。
        self.grid = _DramaGrid(_ui_tokens(), (160, 224), None)
        self.grid.selected.connect(self._on_card_selected)
        # 「加载更多」：先把本地还没显示完的翻出来，本地翻完了再去抓下一页
        self.grid.set_more_loader(self._load_more_online)
        # 封面墙多选 / 筛选清除：接住网格发出的信号
        self.grid.batch_download_requested.connect(self._grid_batch_download)
        self.grid.clear_filter_requested.connect(self._clear_grid_filter)
        # 排序：按剧名 / 按集数（默认不排序，用户点下拉才排）
        self.grid.set_sort_options([
            {"label": "按剧名", "key": "title"},
            {"label": "按集数多→少", "key": "episode_count", "reverse": True,
             "fn": lambda d: int(d.get("episode_count") or 0)},
        ])
        ll.addWidget(self.grid, 1)
        split.addWidget(left)

        right = Card()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(14, 12, 14, 12)
        rl.setSpacing(8)
        self.lbl_detail = QLabel("选择左侧剧集查看详情")
        self.lbl_detail.setObjectName("H2")
        self.lbl_detail.setWordWrap(True)
        rl.addWidget(self.lbl_detail)
        self.lbl_detail_sub = QLabel("")
        self.lbl_detail_sub.setObjectName("Muted")
        self.lbl_detail_sub.setWordWrap(True)
        rl.addWidget(self.lbl_detail_sub)
        rl.addWidget(HLine())

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumHeight(160)
        self.eps_host = QWidget()
        self.eps_grid = QGridLayout(self.eps_host)
        self.eps_grid.setContentsMargins(2, 2, 2, 2)
        self.eps_grid.setSpacing(6)
        self.scroll.setWidget(self.eps_host)
        rl.addWidget(self.scroll, 1)

        dh = QHBoxLayout()
        self.lbl_pick = QLabel("已选 0 集")
        self.lbl_pick.setObjectName("Muted")
        btn_all = QPushButton("全选")
        btn_all.setObjectName("Ghost")
        btn_all.clicked.connect(lambda: self._check_all(True))
        btn_none = QPushButton("清空")
        btn_none.setObjectName("Ghost")
        btn_none.clicked.connect(lambda: self._check_all(False))
        self.btn_dl = QPushButton("下载选中")
        self.btn_dl.setObjectName("Primary")
        self.btn_dl.clicked.connect(self.enqueue)
        dh.addWidget(self.lbl_pick, 1)
        dh.addWidget(btn_all)
        dh.addWidget(btn_none)
        dh.addWidget(self.btn_dl)
        rl.addLayout(dh)
        split.addWidget(right)
        # 剧库是封面网格，需要横向空间才能多排几列；详情/选集区窄一点够用
        split.setSizes([760, 380])
        lay.addWidget(split, 1)
        return w

    # ---------------------------------------------------------------- 队列页
    def _tab_queue(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        card = Card()
        ql = QVBoxLayout(card)
        ql.setContentsMargins(14, 12, 14, 12)
        ql.setSpacing(8)
        qh = QHBoxLayout()
        t = QLabel("下载队列")
        t.setObjectName("H2")
        self.lbl_q = QLabel("空闲")
        self.lbl_q.setObjectName("Muted")
        self.btn_start = QPushButton("开始")
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self.start_queue)
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setObjectName("Ghost")
        self.btn_pause.clicked.connect(self.pause_queue)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.clicked.connect(self.stop_queue)
        qh.addWidget(t)
        qh.addWidget(self.lbl_q, 1)
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            qh.addWidget(b)
        ql.addLayout(qh)
        self.table_q = make_table(["集", "状态", "进度", "大小", "速度"],
                                  [340, 130, 90, 110, 110])
        ql.addWidget(self.table_q, 1)
        lay.addWidget(card, 1)
        return w

    # ---------------------------------------------------------------- 更新页
    def _tab_updates(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)
        row = QHBoxLayout()
        t = QLabel("已下载剧的新增集检测")
        t.setObjectName("H2")
        row.addWidget(t)
        row.addStretch(1)
        self.sp_upd_workers = QSpinBox()
        self.sp_upd_workers.setRange(1, 12)
        self.sp_upd_workers.setValue(4)
        self.btn_check = QPushButton("检测更新")
        self.btn_check.setObjectName("Primary")
        self.btn_check.clicked.connect(self.check_updates)
        self.btn_pending = QPushButton("读取待补清单")
        self.btn_pending.setObjectName("Ghost")
        self.btn_pending.clicked.connect(self.load_pending)
        self.btn_enq_all = QPushButton("全部加入队列")
        self.btn_enq_all.setObjectName("Ghost")
        self.btn_enq_all.clicked.connect(self.enqueue_all_updates)
        row.addWidget(QLabel("并发"))
        row.addWidget(self.sp_upd_workers)
        row.addWidget(self.btn_check)
        row.addWidget(self.btn_pending)
        row.addWidget(self.btn_enq_all)
        cl.addLayout(row)
        self.lbl_upd = QLabel("尚未检测。点「检测更新」会逐部对比平台集数（首轮建议只测少量）")
        self.lbl_upd.setObjectName("Muted")
        cl.addWidget(self.lbl_upd)
        lay.addWidget(card)

        self.table_upd = make_table(
            ["剧名", "已下", "平台", "待补", "待补集号", "站点"], [340, 70, 70, 70, 320, 120])
        lay.addWidget(self.table_upd, 1)
        return w

    # ---------------------------------------------------------------- 同步页
    def _tab_sync(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)
        t = QLabel("外盘同步（把下载目录增量拷到移动硬盘 / 服务器盘）")
        t.setObjectName("H2")
        cl.addWidget(t)
        cl.addWidget(HLine())

        self.ed_sync_dl = QLineEdit(settings().get("huangguo.sync_download", ""))
        self.ed_sync_dl.setPlaceholderText("移动硬盘目标根目录，例如 H:\\AI短剧")
        self.ed_sync_sv = QLineEdit(settings().get("huangguo.sync_server", ""))
        self.ed_sync_sv.setPlaceholderText("服务器/备份盘目标根目录（可留空）")
        for label, line in (("下载盘 →", self.ed_sync_dl), ("服务器 →", self.ed_sync_sv)):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(line, 1)
            btn = QPushButton("浏览…")
            btn.setObjectName("Ghost")
            btn.clicked.connect(lambda _c=False, l=line: self._pick_dir(l))
            row.addWidget(btn)
            cl.addLayout(row)

        row2 = QHBoxLayout()
        self.chk_full = QCheckBox("全量（重建基准，慢）")
        self.btn_preview = QPushButton("预演（只统计不拷贝）")
        self.btn_preview.setObjectName("Ghost")
        self.btn_preview.clicked.connect(self.preview_sync)
        self.btn_sync = QPushButton("开始同步")
        self.btn_sync.setObjectName("Primary")
        self.btn_sync.clicked.connect(self.run_sync)
        self.btn_auto = QPushButton("自动识别移动硬盘")
        self.btn_auto.setObjectName("Ghost")
        self.btn_auto.clicked.connect(self.detect_target)
        row2.addWidget(self.chk_full)
        row2.addStretch(1)
        row2.addWidget(self.btn_auto)
        row2.addWidget(self.btn_preview)
        row2.addWidget(self.btn_sync)
        cl.addLayout(row2)
        self.lbl_sync = QLabel("增量模式：只拷上一轮同步之后新下载/补回的文件")
        self.lbl_sync.setObjectName("Muted")
        cl.addWidget(self.lbl_sync)
        lay.addWidget(card)

        self.log_sync = QPlainTextEdit()
        self.log_sync.setReadOnly(True)
        self.log_sync.setMaximumBlockCount(1200)
        lay.addWidget(self.log_sync, 1)
        return w

    # ---------------------------------------------------------------- 历史页
    def _tab_history(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        card = Card()
        cl = QHBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        t = QLabel("下载历史（最近 200 条）")
        t.setObjectName("H2")
        self.lbl_hist = QLabel("")
        self.lbl_hist.setObjectName("Muted")
        btn = QPushButton("刷新")
        btn.setObjectName("Ghost")
        btn.clicked.connect(self.reload_history)
        cl.addWidget(t)
        cl.addWidget(self.lbl_hist, 1)
        cl.addWidget(btn)
        lay.addWidget(card)

        self.table_hist = make_table(
            ["时间", "剧名", "集", "状态", "大小", "错误"],
            [130, 380, 60, 90, 100, 240])
        lay.addWidget(self.table_hist, 1)
        return w

    # ---------------------------------------------------------------- 站点页
    def _tab_sites(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(8)
        row = QHBoxLayout()
        t = QLabel("当前站点")
        t.setObjectName("H2")
        self.cmb_site = QComboBox()
        self.cmb_site.setMinimumWidth(260)
        btn_apply = QPushButton("切换站点")
        btn_apply.setObjectName("Primary")
        btn_apply.clicked.connect(self.apply_site)
        btn_disc = QPushButton("探测镜像（网络）")
        btn_disc.setObjectName("Ghost")
        btn_disc.clicked.connect(self.discover_mirrors)
        row.addWidget(t)
        row.addWidget(self.cmb_site)
        row.addWidget(btn_apply)
        row.addStretch(1)
        row.addWidget(btn_disc)
        cl.addLayout(row)
        self.lbl_site_hint = QLabel("")
        self.lbl_site_hint.setObjectName("Muted")
        self.lbl_site_hint.setWordWrap(True)
        cl.addWidget(self.lbl_site_hint)
        lay.addWidget(card)

        self.table_mirror = make_table(["镜像地址", "备注", "来源"], [420, 260, 140])
        lay.addWidget(self.table_mirror, 1)
        return w

    # -------------------------------------------------------------- 后端
    def on_show(self) -> None:
        if self._ensure():
            self.reload()
            self._refresh_site_bar()

    def _ensure(self) -> bool:
        if self._ns is not None:
            return True
        try:
            self._ns = backend.huangguo()
        except Exception as exc:  # noqa: BLE001
            self.badge_site.setText("后端不可用")
            self.badge_site.set_tone("err")
            self.notify(f"黄果后端加载失败：{exc}", "err")
            return False
        self._restore_site()
        self._init_cover_cache()
        self._refresh_site_bar()
        self._init_queue()
        self._fill_sites()
        self._fill_categories()
        return True

    def _restore_site(self) -> None:
        """恢复**本页自己**的站点（黄果页=huangguoai，短剧页=上次选的平台）。

        ``config.ACTIVE_SITE`` 是进程级全局：短剧页切到黄桃后，再进黄果页
        看到的还是黄桃的剧库（实测踩到，两边互相串）。这里各页按自己的
        记忆键恢复，封面缓存与分类下拉也要跟着重建。
        """
        try:
            want = str(settings().get(self._site_pref_key, "") or "").strip()
            site = self._ns.sites.get_site(want) if want else None
            if site is not None and self._ns.config.ACTIVE_SITE_ID != site["id"]:
                self._ns.config.set_active_site(site)
        except Exception:                       # noqa: BLE001
            pass

    def _init_cover_cache(self) -> None:
        """建封面缓存（按站点分目录：不同站点的 did 会重名）。

        黄果的封面是**加密图**，必须走 hgcore.media_crypto 解密，直接当普通
        图片加载只会得到解不开的字节 —— 这就是之前「封面一个都没有」的原因。
        """
        ns = self._ns
        if ns is None:
            return
        site = str(getattr(ns.config, "ACTIVE_SITE_ID", "") or "default")
        if self._cover is not None and self._cover_site == site:
            return
        try:
            self._cover = CoverCache(
                paths.data_dir("huangguo", "covers", site),
                decode=ns.media_crypto.decrypt_cover,
                fetch=lambda u: ns.http_client.fetch_bytes(u, timeout=15),
                size=(int(ns.config.COVER_WIDTH), int(ns.config.COVER_HEIGHT)),
            )
            self._cover_site = site
            self.grid.set_cache(self._cover)
        except Exception as exc:  # noqa: BLE001
            self._cover = None
            self.notify(f"封面缓存初始化失败：{exc}", "warn")

    def _on_tab(self, idx: int) -> None:
        if not self._ensure():
            return
        if idx == 4:
            self.reload_history()
        elif idx == 5:
            self._fill_mirrors()

    def _refresh_site_bar(self) -> None:
        try:
            meta = backend.huangguo_site_meta()
            cfg = self._ns.config
            self.badge_site.setText(f"{meta.get('site_name') or meta.get('site_id')}")
            self.badge_domain.setText(str(meta.get("base_url") or "-"))
            if settings().get("proxy.enabled", True):
                self.badge_proxy.setText("代理已配置（下载仍直连）")
                self.badge_proxy.set_tone("warn")
            else:
                self.badge_proxy.setText("直连")
                self.badge_proxy.set_tone("ok")
            st = self._ns.library_db.stats(cfg.ACTIVE_SITE_ID)
            self._apply_stats_text(st)
            self.lbl_site_hint.setText(
                f"站点 {meta.get('site_id')} · 可用镜像 {len(meta.get('mirrors') or [])} 个 · "
                f"当前 {meta.get('base_url')}")
        except Exception as exc:  # noqa: BLE001
            self.lbl_stats.setText(f"状态读取失败：{exc}")

    def _apply_stats_text(self, st: dict) -> None:
        eps = st.get("episodes") or {}
        if isinstance(eps, dict):
            done = int(eps.get("done", 0) or 0)
            total_eps = sum(int(v or 0) for v in eps.values())
        else:
            done = total_eps = 0
        mb = int(st.get("total_bytes") or 0) // (1024 * 1024)
        last = st.get("last_episode_at")
        when = time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "-"
        self.lbl_stats.setText(
            f"剧库 {st.get('dramas', 0)} 部 · 集记录 {total_eps} 条（完成 {done}）· "
            f"累计 {mb} MB · 最近 {when}")

    def _fill_sites(self) -> None:
        try:
            self.cmb_site.clear()
            for s in self._ns.sites.all_sites():
                # enabled=False 的站（解析器还没补齐）先不显示，
                # 免得切过去直接报错；站点定义里注明了原因。
                if not s.get("enabled", True):
                    continue
                self.cmb_site.addItem(f"{s.get('name')} ({s.get('id')})", s.get("id"))
            cur = self._ns.config.ACTIVE_SITE_ID
            for i in range(self.cmb_site.count()):
                if self.cmb_site.itemData(i) == cur:
                    self.cmb_site.setCurrentIndex(i)
                    break
        except Exception:
            pass

    def apply_site(self) -> None:
        if not self._ensure():
            return
        sid = self.cmb_site.currentData()
        site = self._ns.sites.get_site(sid)
        if not site:
            self.notify("未找到该站点配置", "warn")
            return
        try:
            self._ns.config.set_active_site(site)
            settings().set(self._site_pref_key, sid)
            settings().save()
            backend.apply_huangguo_settings(self._ns)
            self._refresh_site_bar()
            self.reload()
            self.notify(f"已切换到 {site.get('name')}", "ok")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"切换失败：{exc}", "err")

    def _fill_mirrors(self) -> None:
        if not self._ensure():
            return
        try:
            sid = self._ns.config.ACTIVE_SITE_ID
            rows = self._ns.site_db.get_candidates(sid)
            self.table_mirror.setRowCount(0)
            for url, note, site in rows:
                r = self.table_mirror.rowCount()
                self.table_mirror.insertRow(r)
                self.table_mirror.setItem(r, 0, QTableWidgetItem(url))
                self.table_mirror.setItem(r, 1, QTableWidgetItem(note or ""))
                self.table_mirror.setItem(r, 2, QTableWidgetItem(site or ""))
        except Exception as exc:  # noqa: BLE001
            self.notify(f"镜像读取失败：{exc}", "err")

    def discover_mirrors(self) -> None:
        if not self._ensure():
            return
        self.notify("正在探测镜像（约 10~30 秒）…", "info")

        def _run(progress=None):
            return self._ns.site_discovery.apply_discovery(timeout=8)

        task = Task(_run, want_progress=True)
        task.progress.connect(lambda p, t: self.lbl_site_hint.setText(t[:120]))
        task.done.connect(self._discover_done)
        task.failed.connect(lambda tb: self.notify(tb.splitlines()[-1][:160], "err"))
        CENTER.start(task)

    def _discover_done(self, res) -> None:
        try:
            got = len(res or []) if not isinstance(res, dict) else len(
                res.get("usable") or res.get("applied") or [])
        except Exception:
            got = 0
        backend.apply_huangguo_settings(self._ns)
        self._refresh_site_bar()
        self._fill_mirrors()
        self.notify(f"镜像探测完成：可用 {got} 个", "ok" if got else "warn")

    # -------------------------------------------------------------- 队列
    def _init_queue(self) -> None:
        if self._queue is not None:
            return
        try:
            conc = int(settings().get("huangguo.concurrency", 3))
            self._queue = self._ns.task_queue.TaskQueue(concurrency=conc)
            self._bridge = _QueueBridge()
            self._bridge.task_update.connect(self._on_task_update)
            self._bridge.log.connect(self._on_queue_log)
            self._queue.on_task_update = self._bridge.task_update.emit
            self._queue.on_log = self._bridge.log.emit
        except Exception as exc:  # noqa: BLE001
            self.notify(f"队列初始化失败：{exc}", "err")

    def _on_queue_log(self, msg: str) -> None:
        self.lbl_q.setText(str(msg)[:120])

    # -------------------------------------------------------------- 剧库
    # ------------------------------------------------------------ 分类
    def _fill_categories(self) -> None:
        """分类下拉：站点配置里的 CATEGORIES（短剧/漫剧/换脸/魔改/排名/专题）。"""
        try:
            cats = list(getattr(self._ns.config, "CATEGORIES", []) or [])
        except Exception:                       # noqa: BLE001
            cats = []
        self.cmb_category.blockSignals(True)
        self.cmb_category.clear()
        self.cmb_category.addItem("全部分类", "")
        for name, url in cats:
            self.cmb_category.addItem(str(name), str(url))
        cur = str(getattr(self, "_category", "") or "")
        for i in range(self.cmb_category.count()):
            if self.cmb_category.itemData(i) == cur:
                self.cmb_category.setCurrentIndex(i)
                break
        self.cmb_category.blockSignals(False)

    def _on_category_changed(self, *_a) -> None:
        self._category = str(self.cmb_category.currentData() or "")
        self.reload()

    def _current_category_name(self) -> str:
        return str(self.cmb_category.currentText() or "").strip()

    def _dramas_done(self, payload) -> None:
        rows, st, status = payload
        self.btn_reload.setEnabled(True)
        self._dramas = list(rows or [])
        self.grid.set_items(self._dramas, status)
        self._apply_stats_text(st)
        self.grid.select_first()
        # 库是空的 ⇒ 打开就是一片灰（用户看到的"封面都没有"其实多半是这个：
        # 这个站点的剧从来没抓进过库）。首次进来自动抓一次把库填上；
        # 只试一次，抓不到就保持空，不反复联网骚扰。
        if not self._dramas and not getattr(self, "_auto_fetched", False):
            self._auto_fetched = True
            QTimer.singleShot(300, self._auto_fetch_first)

    def _auto_fetch_first(self) -> None:
        if self.tabs.tabText(self.tabs.currentIndex()) != "剧库":
            return
        self.lbl_q.setText("库是空的，正在自动抓取…")
        self.fetch_online(pages=2)

    def _filter(self, text: str) -> None:
        self.grid.set_filter(text)

    def warm_covers(self) -> None:
        """把列表里所有封面提前抓到磁盘（数据库同目录的 covers 缓存）。

        为什么要有这个按钮：列表几百张封面全靠滚到眼前才抓，翻页就是一片
        灰块；提前抓完落盘后，之后每次打开都是磁盘命中（秒开、断网也能看）。
        """
        if not self._ensure() or self._cover is None:
            self.notify("封面缓存还没准备好", "warn")
            return
        rows = list(getattr(self, "_dramas", []) or [])
        pairs = [(str(d.get("did")), (d.get("cover_url") or d.get("cover") or "").strip())
                 for d in rows]
        pairs = [(k, u) for k, u in pairs if u]
        if not pairs:
            self.notify("当前列表没有可下载的封面", "warn")
            return
        self.btn_warm.setEnabled(False)
        self.lbl_scan.setText(f"预下载封面 0/{len(pairs)}")

        def _run(progress=None):
            return self._cover.warm(pairs, on_progress=progress)

        task = Task(_run, want_progress=True)
        task.progress.connect(
            lambda p, t: self.lbl_scan.setText(f"预下载封面 {p}/{len(pairs)}"))
        task.done.connect(self._warm_done)
        task.failed.connect(lambda tb: (self.btn_warm.setEnabled(True),
                                        self.lbl_scan.setText("预下载失败")))
        CENTER.start(task)

    def _warm_done(self, stat) -> None:
        self.btn_warm.setEnabled(True)
        stat = stat or {}
        self.lbl_scan.setText(
            f"封面：新下 {stat.get('ok', 0)} · 已有 {stat.get('skip', 0)}"
            f" · 失败 {stat.get('fail', 0)}")
        self.notify(
            f"封面预下载完成：新下 {stat.get('ok', 0)} 张"
            f"（已有 {stat.get('skip', 0)} 张）", "ok")

    def _load_more_online(self) -> None:
        """本地列表已经翻完 ⇒ 再去站点抓一页（「加载更多」的联网分支）。"""
        if getattr(self, "_loading_more", False):
            return
        self._loading_more = True
        # 从「库里已经有了几页」往后加一页：原来固定 +1 会出现"库里早有 2 页、
        # 又去抓 2 页 ⇒ 新增 0 ⇒ 看着像没反应"的问题（已实测复现）。
        base = max(self._guess_pages(),
                   int(getattr(self, "_pages_done", 0) or 0),
                   int(getattr(self, "_last_pages", 0) or 0))
        self._pages_done = base + 1
        # 按钮立刻给反馈，否则用户点了没动静就以为"没效果"
        try:
            self.grid.btn_more.setText("加载中…")
            self.grid.btn_more.setEnabled(False)
        except Exception:                       # noqa: BLE001
            pass
        self.fetch_online(pages=self._pages_done, append=True)

    def _load_more_done(self, payload) -> None:
        self._loading_more = False
        self._restore_more_btn()

    def _guess_pages(self) -> int:
        """估算库里已经抓到第几页（「加载更多」从下一页开始才不会重复抓）。

        每页条数用上次实测值（黄果系一般 20~24 条/页），抓过一次后就准了。
        """
        n = len(getattr(self, "_dramas", []) or [])
        if not n:
            return 1
        per = int(getattr(self, "_per_page", 0) or 0) or 22
        return max(1, -(-n // per))

    @staticmethod
    def _item(text, user):
        """表格单元格（更新检测/下载历史还在用表格，这里保留的小工具）。"""
        item = QTableWidgetItem(str(text))
        item.setData(Qt.ItemDataRole.UserRole, user)
        return item

    # -------------------------------------------------------- 扫描下载目录
    def scan_downloads(self) -> None:
        """用 ffprobe 校验下载目录，把磁盘上已完整的集回填成「已下载」。

        为什么需要：库里的 episodes 状态只在**本程序下载完成**时写入。以前用
        原版黄果下的、或者从别处拷进来的文件，库里一律不认识 —— 卡片全显示
        「未下载」，点批量下载还会重复下。这里按剧逐个校验后回填。
        """
        if not self._ensure() or self._scanning:
            return
        self._scanning = True
        self.btn_scan.setEnabled(False)
        self.lbl_scan.setText("扫描下载目录…")
        site_id = self._ns.config.ACTIVE_SITE_ID

        def _run(progress=None):
            ns = self._ns
            rows = ns.library_db.list_dramas(site_id, limit=10000)
            total = len(rows)
            found = 0
            for i, d in enumerate(rows):
                title = d.get("title") or ""
                cnt = int(d.get("episode_count") or 0)
                did = d.get("did")
                if title and cnt > 0:
                    known = ns.library_db.known_done_eps(site_id, did)
                    eps = [e for e in range(1, cnt + 1) if e not in known]
                    if eps:
                        hit = ns.library_scan.scan_drama(title, eps)
                        for ep, path in hit.items():
                            try:
                                size = path.stat().st_size
                            except OSError:
                                size = 0
                            ns.library_db.set_episode_status(
                                site_id, did, ep, ns.library_db.EP_DONE,
                                file_path=str(path), size_bytes=size)
                        found += len(hit)
                if progress:
                    progress(i + 1, total, f"{i + 1}/{total} 部 · 新识别 {found} 集")
            return found, total

        task = Task(_run, want_progress=True)
        task.progress.connect(lambda p, t: self.lbl_scan.setText(t))
        task.done.connect(self._scan_done)
        task.failed.connect(self._scan_failed)
        CENTER.start(task)

    def _scan_done(self, payload) -> None:
        found, total = payload
        self._scanning = False
        self.btn_scan.setEnabled(True)
        self.lbl_scan.setText(f"扫描完成：{total} 部剧，新识别 {found} 集")
        self.notify(f"扫描完成：新识别 {found} 集已下载", "ok" if found else "warn")
        site_id = self._ns.config.ACTIVE_SITE_ID
        try:
            self.grid.refresh_status(self._ns.library_db.downloaded_status_map(site_id))
            self._apply_stats_text(self._ns.library_db.stats(site_id))
        except Exception:                       # noqa: BLE001
            pass
        if self._detail_drama:
            self.load_detail(self._detail_drama)     # 右侧集数的 ✓ 也刷新一遍

    def _scan_failed(self, tb: str) -> None:
        self._scanning = False
        self.btn_scan.setEnabled(True)
        self.lbl_scan.setText("扫描失败")
        self.notify((tb or "").splitlines()[-1][:160] if (tb or "").strip() else "扫描失败", "err")

    def _on_card_selected(self, drama: dict) -> None:
        """卡片被点中：右侧拉详情/集数（预览面板已按用户要求屏蔽）。"""
        self.load_detail(drama)

    def _preview_drama(self, drama: dict) -> None:
        """占位：预览面板已屏蔽（保留方法是为了不破坏子类/旧调用）。"""

    def _resolve_play_url(self, drama: dict) -> str:
        """取该剧第 1 集的播放直链（预览面板「在线播放」用）。"""
        ns = self._ns
        if ns is None:
            return ""
        ep = 1
        eps = (self._detail or {}).get("episodes") or []
        if eps:
            try:
                ep = int(eps[0])
            except (TypeError, ValueError):
                ep = 1
        return ns.site_api.get_play_url_for(
            ns.config.ACTIVE_SITE_ID, drama.get("did"), ep) or ""

    def load_detail(self, drama: dict) -> None:
        if not self._ensure():
            return
        self._detail_drama = drama
        did = drama.get("did")
        self.lbl_detail.setText(drama.get("title", "") or "-")
        self.lbl_detail_sub.setText("加载集数…")
        self._clear_eps()
        site_id = self._ns.config.ACTIVE_SITE_ID

        def _run():
            return self._ns.site_api.load_detail_for(site_id, did)

        task = Task(_run)
        task.done.connect(lambda detail: self._detail_done(drama, detail))
        task.failed.connect(lambda tb: self.lbl_detail_sub.setText(
            "详情抓取失败：" + tb.splitlines()[-1][:100]))
        CENTER.start(task)

    def _detail_done(self, drama: dict, detail: dict) -> None:
        self._detail = detail or {}
        eps = list(self._detail.get("episodes") or [])
        total = self._detail.get("total_count") or self._detail.get("episode_count") or 0
        desc = (self._detail.get("desc") or "").strip().replace("\n", " ")
        self.lbl_detail_sub.setText(
            f"{drama.get('title', '')} · 可下载 {len(eps)} 集 / 共 {total} 集"
            + (f" · {desc[:80]}" if desc else ""))
        self._clear_eps()
        done = set()
        try:
            ready = self._ns.library_db.known_done_eps(
                self._ns.config.ACTIVE_SITE_ID, drama.get("did"))
            done = {int(k) for k in (ready or {}).keys()}
        except Exception:
            done = set()
        for idx, ep in enumerate(eps):
            label = f"第 {ep} 集" + (" ✓" if int(ep) in done else "")
            box = QCheckBox(label)
            box.setProperty("ep", ep)
            box.stateChanged.connect(self._update_pick)
            self.eps_grid.addWidget(box, idx // 8, idx % 8)
            self._eps_boxes.append(box)
        self._update_pick()
        if not eps:
            self.lbl_detail_sub.setText(
                self.lbl_detail_sub.text() + " · 未解析到集数（可能需要先刷新域名）")

    def _clear_eps(self) -> None:
        for box in self._eps_boxes:
            box.setParent(None)
        self._eps_boxes = []

    def _check_all(self, on: bool) -> None:
        for box in self._eps_boxes:
            box.setChecked(on)
        self._update_pick()

    def _update_pick(self, *_a) -> None:
        n = sum(1 for b in self._eps_boxes if b.isChecked())
        self.lbl_pick.setText(f"已选 {n} 集")

    # -------------------------------------------------------------- 队列操作
    def enqueue(self) -> None:
        if not self._ensure():
            return
        drama = self.grid.current() or self._detail_drama
        if not drama:
            self.notify("请先在剧库中点选一部剧", "warn")
            return
        eps = [b.property("ep") for b in self._eps_boxes if b.isChecked()]
        if not eps:
            self.notify("请勾选要下载的集", "warn")
            return
        tasks = self._queue.add_many(drama.get("did"), drama.get("title", ""), eps)
        self.lbl_q.setText(f"已入队 {len(tasks)} 集")
        self.tabs.setCurrentIndex(1)
        self.start_queue()

    # ------------------------------------------------------- 封面墙批量下载
    def _grid_batch_download(self, keys: list) -> None:
        """封面墙多选后点「批量下载」：后台逐部取集数，再全部入队。"""
        if not self._ensure():
            return
        items = self.grid.items_for_keys(keys)
        dramas = [it.get("data") or it for it in items]
        dramas = [d for d in dramas if d.get("did")]
        if not dramas:
            self.notify("没有可批量下载的剧", "warn")
            return
        self.notify(f"正在为选中的 {len(dramas)} 部剧加载集数…", "ok")
        site_id = self._ns.config.ACTIVE_SITE_ID

        def _run():
            out = []
            for d in dramas:
                try:
                    detail = self._ns.site_api.load_detail_for(site_id, d.get("did"))
                except Exception:                       # noqa: BLE001
                    detail = None
                eps = list((detail or {}).get("episodes") or [])
                out.append((d, eps))
            return out

        def _done(results):
            n = 0
            for d, eps in results:
                did = d.get("did")
                title = d.get("title", "") or str(did)
                if not eps:
                    continue
                try:
                    self._queue.add_many(did, title, eps)
                    n += len(eps)
                except Exception:                       # noqa: BLE001
                    pass
            if not n:
                self.notify("所选剧目暂无可下载的集（可能未联网或站点结构变化）", "warn")
                return
            self.tabs.setCurrentIndex(1)
            self.start_queue()
            self.notify(f"已加入队列 {n} 集", "ok")

        task = Task(_run)
        task.done.connect(_done)
        task.failed.connect(
            lambda tb: self.notify("批量加载失败：" + tb.splitlines()[-1][:120], "err"))
        CENTER.start(task)

    def _clear_grid_filter(self) -> None:
        try:
            self.ed_filter.setText("")
        except Exception:                               # noqa: BLE001
            pass

    def start_queue(self) -> None:
        if not self._ensure():
            return
        try:
            self._queue.start()
            self.lbl_q.setText("队列运行中")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"启动队列失败：{exc}", "err")

    def pause_queue(self) -> None:
        if self._queue:
            self._queue.pause()
            self.lbl_q.setText("已暂停")

    def stop_queue(self) -> None:
        if self._queue:
            self._queue.stop()
            self.lbl_q.setText("已停止")

    def _on_task_update(self, task) -> None:
        key = (task.did, task.ep)
        row = self._row_keys.get(key)
        if row is None:
            row = self.table_q.rowCount()
            self.table_q.insertRow(row)
            self.table_q.setItem(row, 0, QTableWidgetItem(
                f"{task.drama_title} 第{task.ep}集"))
            for c in (1, 2, 3, 4):
                self.table_q.setItem(row, c, QTableWidgetItem("-"))
            self._row_keys[key] = row
        self.table_q.item(row, 1).setText(task.status_text)
        self.table_q.item(row, 2).setText(f"{task.percent:.0f}%")
        self.table_q.item(row, 3).setText(task.size_text or "-")
        self.table_q.item(row, 4).setText(task.speed_text or "-")

    def _poll_queue(self) -> None:
        if not self._queue or self.tabs.currentIndex() != 1:
            return
        try:
            tasks = self._queue.tasks
        except Exception:
            return
        for task in tasks:
            self._on_task_update(task)
        if tasks:
            done = sum(1 for t in tasks
                       if t.status.name in ("DONE", "FAILED", "CANCELLED"))
            self.lbl_q.setText(
                f"{done}/{len(tasks)} 结束 · 运行中 {self._queue.is_running}")

    # -------------------------------------------------------------- 更新检测
    def check_updates(self) -> None:
        if not self._ensure():
            return
        self.btn_check.setEnabled(False)
        self.lbl_upd.setText("检测中…")
        workers = int(self.sp_upd_workers.value())
        site_id = self._ns.config.ACTIVE_SITE_ID

        def _run(progress=None):
            res = self._ns.update_check.check_updates(
                site_id=site_id, max_workers=workers, confirm=False,
                on_progress=lambda d, t, title: progress(d, t, str(title)[:40]))
            try:
                self._ns.update_check.save_pending(res)
            except Exception:
                pass
            return res

        task = Task(_run, want_progress=True)
        task.progress.connect(
            lambda p, t: self.lbl_upd.setText(f"检测中… {p}% {t}" if t else "检测中…"))
        task.done.connect(self._updates_done)
        task.failed.connect(lambda tb: (self.btn_check.setEnabled(True),
                                        self.lbl_upd.setText("检测失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _updates_done(self, res) -> None:
        self.btn_check.setEnabled(True)
        ups = list(getattr(res, "updates", []) or [])
        self._updates = ups
        self.table_upd.setRowCount(0)
        for u in ups:
            r = self.table_upd.rowCount()
            self.table_upd.insertRow(r)
            self.table_upd.setItem(r, 0, self._item(getattr(u, "title", ""), u))
            self.table_upd.setItem(r, 1, QTableWidgetItem(str(getattr(u, "base_count", 0))))
            self.table_upd.setItem(r, 2, QTableWidgetItem(str(getattr(u, "new_count", 0))))
            eps = list(getattr(u, "new_eps", []) or [])
            self.table_upd.setItem(r, 3, QTableWidgetItem(str(len(eps))))
            self.table_upd.setItem(r, 4, QTableWidgetItem(
                "、".join(str(e) for e in eps[:12]) + (" …" if len(eps) > 12 else "")))
            self.table_upd.setItem(r, 5, QTableWidgetItem(str(getattr(u, "site_id", ""))))
        total = sum(len(getattr(u, "new_eps", []) or []) for u in ups)
        self.lbl_upd.setText(
            f"检测 {getattr(res, 'checked', 0)} 部 · 有更新 {len(ups)} 部 · "
            f"待补 {total} 集 · 失败 {getattr(res, 'failed', 0)} 部")
        self.notify(f"更新检测完成：{len(ups)} 部有新集", "ok" if ups else "info")

    def load_pending(self) -> None:
        if not self._ensure():
            return
        try:
            items = self._ns.update_check.pending_updates()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"读取失败：{exc}", "err")
            return
        self._updates = []
        self.table_upd.setRowCount(0)
        total = 0
        for it in items:
            r = self.table_upd.rowCount()
            self.table_upd.insertRow(r)
            eps = list(it.get("new_eps") or [])
            total += len(eps)
            self.table_upd.setItem(r, 0, QTableWidgetItem(str(it.get("title", ""))))
            self.table_upd.setItem(r, 1, QTableWidgetItem(str(it.get("base_count", ""))))
            self.table_upd.setItem(r, 2, QTableWidgetItem(str(it.get("new_count", ""))))
            self.table_upd.setItem(r, 3, QTableWidgetItem(str(len(eps))))
            self.table_upd.setItem(r, 4, QTableWidgetItem(
                "、".join(str(e) for e in eps[:12]) + (" …" if len(eps) > 12 else "")))
            self.table_upd.setItem(r, 5, QTableWidgetItem(str(it.get("site_id", ""))))
            self.table_upd.item(r, 0).setData(Qt.ItemDataRole.UserRole, it)
        self.lbl_upd.setText(f"待补清单：{len(items)} 部 · 共 {total} 集")

    def enqueue_all_updates(self) -> None:
        if not self._ensure():
            return
        n = 0
        for it in self._pending_items():
            eps = [int(e) for e in (it.get("new_eps") or [])]
            if not eps:
                continue
            try:
                self._queue.add_many(it.get("did"), it.get("title") or str(it.get("did")), eps)
                n += len(eps)
            except Exception:
                pass
        if not n:
            self.notify("没有可加入的待补集（先检测或读取待补清单）", "warn")
            return
        self.tabs.setCurrentIndex(1)
        self.start_queue()
        self.notify(f"已加入队列 {n} 集", "ok")

    def _pending_items(self) -> list:
        out = []
        for r in range(self.table_upd.rowCount()):
            item = self.table_upd.item(r, 0)
            obj = item.data(Qt.ItemDataRole.UserRole) if item else None
            if isinstance(obj, dict):
                out.append(obj)
                continue
            # 来自 CheckResult 的行
            if r < len(self._updates):
                u = self._updates[r]
                out.append({"did": getattr(u, "did", None),
                            "title": getattr(u, "title", ""),
                            "new_eps": list(getattr(u, "new_eps", []) or [])})
        return out

    # -------------------------------------------------------------- 同步
    def _pick_dir(self, line: QLineEdit) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择同步目标目录", line.text())
        if d:
            line.setText(d)

    def _collect_targets(self, save: bool = False) -> list:
        s = settings()
        dl = self.ed_sync_dl.text().strip()
        sv = self.ed_sync_sv.text().strip()
        if save:
            s.set("huangguo.sync_download", dl)
            s.set("huangguo.sync_server", sv)
            s.save()
        out = []
        if dl:
            out.append({"role": "download", "path": dl})
        if sv:
            out.append({"role": "server", "path": sv})
        return out

    def detect_target(self) -> None:
        if not self._ensure():
            return
        try:
            p = self._ns.sync_mgr.detect_removable_target()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"识别失败：{exc}", "err")
            return
        if p:
            self.ed_sync_dl.setText(p)
            self.notify(f"已识别：{p}", "ok")
        else:
            self.notify("未识别到移动硬盘（可手动选择目录）", "warn")

    def preview_sync(self) -> None:
        if not self._ensure():
            return
        targets = self._collect_targets(save=True)
        if not targets:
            self.notify("请先填写至少一个同步目标目录", "warn")
            return
        full = self.chk_full.isChecked()
        self.btn_preview.setEnabled(False)
        self.lbl_sync.setText("统计中…")
        try:
            src_root = self._ns.config.DOWNLOAD_DIR
        except Exception:
            src_root = settings().get("huangguo.download_dir", "")

        def _run(progress=None):
            return self._ns.sync_mgr.preview_sync(
                targets, full=full, src_root=str(src_root),
                on_scan=lambda n: progress(0, 0, f"已扫描 {n} 个文件"))

        task = Task(_run, want_progress=True)
        task.progress.connect(lambda p, t: self.lbl_sync.setText(t or "统计中…"))
        task.done.connect(self._preview_done)
        task.failed.connect(lambda tb: (self.btn_preview.setEnabled(True),
                                        self.lbl_sync.setText("预演失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _preview_done(self, res: dict) -> None:
        self.btn_preview.setEnabled(True)
        if not res:
            self.lbl_sync.setText("没有需要同步的文件")
            return
        parts = []
        for role, info in (res or {}).items():
            label = "下载盘" if role == "download" else "服务器盘"
            parts.append(f"{label} {info.get('count', 0)} 个 / "
                         f"{info.get('bytes', 0) / 1024 / 1024:.0f} MB")
        self.lbl_sync.setText("待同步：" + " ｜ ".join(parts))

    def run_sync(self) -> None:
        if not self._ensure():
            return
        targets = self._collect_targets(save=True)
        if not targets:
            self.notify("请先填写至少一个同步目标目录", "warn")
            return
        full = self.chk_full.isChecked()
        self.btn_sync.setEnabled(False)
        self.log_sync.appendPlainText(f"=== 开始同步（{'全量' if full else '增量'}）===")
        try:
            src_root = self._ns.config.DOWNLOAD_DIR
        except Exception:
            src_root = settings().get("huangguo.download_dir", "")

        def _run(log=None):
            return self._ns.sync_mgr.sync_all(
                targets, full=full, src_root=str(src_root),
                on_log=(lambda m: log(m)) if log else None)

        task = Task(_run, want_progress=True)
        task.progress.connect(lambda p, t: self.log_sync.appendPlainText(t))
        task.done.connect(self._sync_done)
        task.failed.connect(lambda tb: (self.btn_sync.setEnabled(True),
                                        self.log_sync.appendPlainText("失败：" + tb[-300:]),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _sync_done(self, results) -> None:
        self.btn_sync.setEnabled(True)
        total = 0
        for r in results or []:
            try:
                self.log_sync.appendPlainText(f"· {r.summary_line()}")
                total += getattr(r, "copied", 0)
            except Exception:
                self.log_sync.appendPlainText(str(r))
        self.lbl_sync.setText(f"同步完成：共拷贝 {total} 个文件")
        self.notify(f"同步完成：{total} 个文件", "ok")

    # -------------------------------------------------------------- 历史
    def reload_history(self) -> None:
        if not self._ensure():
            return
        try:
            rows = self._ns.library_db.recent_downloads(limit=200)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"读取失败：{exc}", "err")
            return
        self.table_hist.setRowCount(0)
        for rec in rows or []:
            r = self.table_hist.rowCount()
            self.table_hist.insertRow(r)
            ts = rec.get("started_at") or 0
            self.table_hist.setItem(r, 0, QTableWidgetItem(
                time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"))
            self.table_hist.setItem(r, 1, QTableWidgetItem(str(rec.get("title", "") or "")))
            self.table_hist.setItem(r, 2, QTableWidgetItem(str(rec.get("ep", ""))))
            self.table_hist.setItem(r, 3, QTableWidgetItem(str(rec.get("status", ""))))
            b = int(rec.get("bytes") or 0)
            self.table_hist.setItem(r, 4, QTableWidgetItem(
                f"{b / 1024 / 1024:.1f} MB" if b else "-"))
            self.table_hist.setItem(r, 5, QTableWidgetItem(str(rec.get("error", "") or "")[:80]))
        self.lbl_hist.setText(f"共 {len(rows or [])} 条")

    # -------------------------------------------------------------- 域名
    def refresh_mirror(self) -> None:
        if not self._ensure():
            return
        self.btn_mirror.setEnabled(False)
        self.lbl_q.setText("读取发布页（代理）…")
        site_id = self._ns.config.ACTIVE_SITE_ID

        def _run(log=None):
            return self._ns.mirror_refresh.refresh(
                site_id=site_id, allow_proxy=True, log=(lambda m: None))

        task = Task(_run, want_progress=True)
        task.done.connect(self._mirror_done)
        task.failed.connect(lambda tb: (self.btn_mirror.setEnabled(True),
                                        self.lbl_q.setText("发布页读取失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _mirror_done(self, res: dict) -> None:
        self.btn_mirror.setEnabled(True)
        added = res.get("added") or []
        switched = res.get("switched_to") or ""
        via_proxy = res.get("fetched_via_proxy")
        msg = f"新增 {len(added)} 个镜像" + (f" · 已切换 {switched}" if switched else "")
        self.lbl_q.setText(msg)
        self.notify(msg + ("（发布页经代理读取）" if via_proxy else ""),
                    "ok" if (added or switched) else "warn")
        backend.apply_huangguo_settings(self._ns)
        self._refresh_site_bar()
        self._fill_mirrors()
        self.reload()

    # -------------------------------------------------------------- 在线抓取
    def fetch_online(self, pages: int = 0, append: bool = False) -> None:
        """按当前选中的分类抓取。

        append=True（「加载更多」）：只把新抓到的**追加**到网格，不清空已有卡片
        ——否则用户翻了三页、点一下"更多"又回到第一页，等于白翻。
        """
        if not self._ensure():
            return
        pages = int(pages or self.sp_pages.value())
        self._last_pages = pages          # 记住"已经抓到第几页"，加载更多从下一页开始
        self.btn_online.setEnabled(False)
        self.lbl_q.setText("在线抓取中…")

        def _run(progress=None):
            ns = self._ns
            cfg = ns.config
            sid = cfg.ACTIVE_SITE_ID
            rows: list = []
            from_cache = False
            # 用下拉里选中的分类（不再写死第一个）：换脸/魔改/漫剧要能切
            url = str(getattr(self, "_category", "") or "").strip()
            cat_name = self._current_category_name()
            cats = list(getattr(cfg, "CATEGORIES", []) or [])
            if not url and cats:
                url = str(cats[0][1] or "")
            switched = ""
            if url:
                # 网络优先（用户点「在线抓取」就是要最新的），
                # 抓不到才回落到本地列表缓存 —— 黄果域名常换，断网/换域名时
                # 有缓存至少还能看到上次的剧库，不至于整页空白。
                try:
                    rows = ns.site_api.list_category_all(url, max_pages=pages)
                except Exception:               # noqa: BLE001
                    rows = []
                if not rows:
                    # 写死在配置里的域名失效了 ⇒ 「在线抓取」和「加载更多」
                    # 永远返回 0 条（用户看到的就是"点了没反应"）。这里自动
                    # 轮换该站点的候选域名，挑一个能通的切过去再抓一次。
                    alt, rows = self._retry_on_other_host(url, pages, sid)
                    switched = alt
                if rows:
                    try:
                        ns.cache.put_list(url, "category", rows, site_id=sid)
                    except Exception:           # noqa: BLE001
                        pass
                else:
                    try:
                        rows = ns.cache.get_list(url, "category", site_id=sid,
                                                 allow_stale=True) or []
                        from_cache = bool(rows)
                    except Exception:           # noqa: BLE001
                        rows = []
            if not rows:
                rows = ns.site_api.list_dramas()
            # 抓来的行补上分类标记，库内按分类过滤才有依据
            if cat_name and cat_name != "全部分类":
                for d in rows:
                    try:
                        if isinstance(d, dict):
                            d.setdefault("category", cat_name)
                        elif not getattr(d, "category", ""):
                            setattr(d, "category", cat_name)
                    except Exception:           # noqa: BLE001
                        pass
            n = ns.library_db.record_dramas(rows, sid) if rows else 0
            return n, len(rows), from_cache, append, switched

        task = Task(_run, want_progress=True)
        task.done.connect(self._online_done)
        task.failed.connect(self._online_failed)
        CENTER.start(task)

    def _retry_on_other_host(self, url: str, pages: int, site_id: str):
        """当前域名抓不到 ⇒ 轮换候选域名重试，通了就切过去。

        返回 ``(新URL, rows)``；全都不通返回 ``("", [])``。
        黄果/黄桃这类站域名隔几个月就换一次，配置里那个 base_url 一旦失效，
        不自动换就只剩"点了没反应"——这是「加载更多没效果」的根因之一。
        """
        ns = self._ns
        if ns is None:
            return "", []
        import time
        from urllib.parse import urlsplit, urlunsplit

        # 整轮换一遍最多给 90 秒：域名失效时逐个超时能把"加载更多"拖成假死
        deadline = time.time() + 90
        parts = urlsplit(url)
        cands: list[str] = []
        # 1) 同步中心刚校验通过的地址**优先**：它是"此刻真的能打开"的那个，
        #    站点表里躺着的老候选往往早就失效了（实测 .com 全挂、.io 能抓）。
        try:
            from ...core import navsync
            for row in navsync.summary() or []:
                if not row.get("ok") or not row.get("url"):
                    continue
                key = str(row.get("key") or "")
                if key and (site_id.startswith(key) or key.startswith(site_id)):
                    cands.append(str(row["url"]).rstrip("/"))
        except Exception:                       # noqa: BLE001
            pass
        try:
            for row in ns.site_db.get_candidates(site_id) or []:
                if row and row[0]:
                    cands.append(str(row[0]).rstrip("/"))
        except Exception:                       # noqa: BLE001
            pass
        site = ns.sites.get_site(site_id) or {}
        for key in ("mirrors", "publish_pages", "candidates"):
            for u in (site.get(key) or []):
                if u:
                    cands.append(str(u).rstrip("/"))
        seen = {parts.netloc}
        for cand in cands:
            if time.time() > deadline:
                break
            host = urlsplit(cand).netloc or cand
            if not host or host in seen or "." not in host:
                continue
            seen.add(host)
            alt = urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
            try:
                rows = ns.site_api.list_category_all(alt, max_pages=pages)
            except Exception:                   # noqa: BLE001
                rows = []
            if rows:
                try:
                    base = f"{parts.scheme}://{host}"
                    ns.config.BASE_URL = base
                    ns.config.ACTIVE_SITE["base_url"] = base
                    ns.config.ACTIVE_SITE["home_url"] = base + "/"
                    ns.config._rebuild_urls()
                    # 持久化：不写的话下次启动又回到失效域名，再"点了没反应"一轮。
                    # 按站点存（SITE_MIRRORS），不能写全局 base_url —— 那会让
                    # 其它站点也跟着跳到黄果的域名（config.py 注释里记着这个坑）。
                    from ...core.settings import settings as _st
                    ns.config.SITE_MIRRORS = {**ns.config.SITE_MIRRORS,
                                              site_id: [base]}
                    _st().save({"site_mirrors": ns.config.SITE_MIRRORS})
                except Exception:               # noqa: BLE001
                    pass
                return alt, rows
        return "", []

    def _online_failed(self, tb: str) -> None:
        self.btn_online.setEnabled(True)
        self._loading_more = False
        self._restore_more_btn()
        self.lbl_q.setText("在线抓取失败")
        self.notify((tb or "").splitlines()[-1][:160], "err")

    def _restore_more_btn(self) -> None:
        """把「加载更多」按钮从"加载中…"恢复（网格会按剩余量自己定文案）。"""
        grid = getattr(self, "grid", None)
        if grid is None:
            return
        try:
            grid.btn_more.setEnabled(True)
            grid._update_more_btn()
        except Exception:                       # noqa: BLE001
            pass

    def _online_done(self, payload) -> None:
        self.btn_online.setEnabled(True)
        vals = list(payload) + [False, False, ""]
        n, total, from_cache, append, switched = vals[:5]
        tag = "（离线缓存）" if from_cache else ""
        self.lbl_q.setText(f"在线抓取{tag}：入库 {n} / 抓到 {total}")
        self._loading_more = False
        self._restore_more_btn()
        if switched:
            # 域名换了 ⇒ 分类 URL 也跟着变，下拉要重建，否则下次又抓旧的
            self._fill_categories()
            self._refresh_site_bar()
            self.notify(f"原域名不通，已自动切换到 {switched.split('//')[-1].split('/')[0]}",
                        "ok")
        if total == 0:
            # 明确告诉用户"是没抓到"而不是默默什么都不做（＝"加载更多没效果"）
            self.notify("在线抓取：站点没返回数据（网络不通或域名失效）", "warn")
        else:
            pages = int(getattr(self, "_last_pages", 0) or 0)
            if pages > 0:
                self._per_page = max(1, total // pages)
            self.notify(f"在线抓取完成{tag}：入库 {n} 部", "ok" if n else "warn")
        if append:
            # 「加载更多」：不清空现有卡片，直接把库里新出现的补进网格
            self.reload(append=True)
        else:
            self.reload()

    def reload(self, append: bool = False) -> None:
        """刷新剧库。append=True 时只补新增卡片，不重置分页与选中。"""
        if not self._ensure():
            return
        self.btn_reload.setEnabled(False)
        site_id = self._ns.config.ACTIVE_SITE_ID
        cat = str(getattr(self, "_category", "") or "")

        def _run():
            rows = self._ns.library_db.list_dramas(site_id, limit=2000)
            if cat:
                name = self._current_category_name()
                picked = [d for d in rows
                          if str(d.get("category") or "").strip()
                          and (str(d.get("category")).strip() == name
                               or name in str(d.get("category")).strip())]
                if picked:
                    rows = picked
            st = self._ns.library_db.stats(site_id)
            status = self._ns.library_db.downloaded_status_map(site_id)
            return rows, st, status

        task = Task(_run)
        if append:
            task.done.connect(self._dramas_appended)
        else:
            task.done.connect(self._dramas_done)
        task.failed.connect(lambda tb: (self.btn_reload.setEnabled(True),
                                        self._loading_more and None,
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _dramas_appended(self, payload) -> None:
        rows, st, status = payload
        self.btn_reload.setEnabled(True)
        self._dramas = list(rows or [])
        before = self.grid.count()
        # 追加：网格内部按 key 去重，已有的卡片不会被重建（不闪、不回顶部）
        self.grid.append_items(self._dramas, status)
        self._apply_stats_text(st)
        added = self.grid.count() - before
        if added:
            self.notify(f"加载更多：新增 {added} 部", "ok")
        else:
            # 不能默默什么都不说：那正是用户报的"点了没效果"
            self.notify("没有更多内容了（站点已翻到最后一页，或当前网络取不到新数据）",
                        "warn")
