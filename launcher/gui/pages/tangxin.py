# -*- coding: utf-8 -*-
"""糖心 Vlog：导航页取线路 → 演员云 → 作品库 → **并发**下载队列 → 下载记录。

2026-10 重建要点：
  * 并发下载池（同时下 N 部，每部内部再按分片并发），行级实时进度
  * 暂停 / 继续 / 停止（可中断正在写盘的任务）
  * 作品表带「本地状态」列（已下载 / 别处已有 / 未下载），可选自动过滤
  * 独立的「下载记录」页：库内状态统计、筛选、导出 CSV
"""
from __future__ import annotations

import csv
import os
import queue
import re
import threading
import time

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QProgressBar, QPushButton, QSpinBox,
    QSplitter, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ...core import backend, nav, paths
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from .. import theme
from ..widgets import Badge, Card, HLine, SearchInput, make_table, open_path
from .base import Page

try:
    from modules.tangxin.existing import norm_key as _norm_key
except Exception:  # noqa: BLE001
    def _norm_key(text):
        """兜底：与 ``existing.norm_key`` 行为对齐（NFKC + casefold + 去空白）。"""
        import re as _re
        import unicodedata

        s = unicodedata.normalize("NFKC", str(text or "")).casefold()
        return _re.sub(r"\s+", "", s).strip()


def _safe_name(text: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", (text or "").strip())
    return s.strip(" .") or "未命名"


def _status_colors() -> dict:
    """「本地状态」列三态配色（跟随主题，不写死深色）。

    已下载＝绿（ok）／别处已有＝琥珀（warn）／未下载＝灰（muted）。
    用户的核心诉求就是"扫完目录一眼看出哪些下过、哪些没下"，
    纯文字三态在两百行的表里等于没有视觉锚点，必须上色。
    """
    t = theme.tokens(settings().get("theme", "dark") != "light")
    return {
        "downloaded": t.get("ok", "#33c98b"),
        "elsewhere": t.get("warn", "#f0b429"),
        "none": t.get("muted", "#69748a"),
    }


class _PoolDownload(QThread):
    """并发下载池：N 个线程各自持有独立 downloader，共享一个任务队列。"""

    row_state = pyqtSignal(int, str, int)     # idx, 状态, 百分比
    finished_all = pyqtSignal(dict)

    def __init__(self, videos, out_base: str, actor: str, parallel: int,
                 seg_workers: int, stop_event: threading.Event,
                 paused_event: threading.Event):
        super().__init__()
        self._videos = list(videos)
        self._out = out_base
        self._actor = actor
        self._parallel = max(1, int(parallel or 1))
        self._seg = max(1, int(seg_workers or 1))
        self._stop = stop_event
        self._paused = paused_event
        self._lock = threading.Lock()
        self._stat = {"ok": 0, "skip": 0, "fail": 0, "stop": 0, "msgs": []}

    # ---------------------------------------------------------------- 统计
    def _bump(self, key: str, msg: str = "") -> None:
        with self._lock:
            self._stat[key] += 1
            if msg and len(self._stat["msgs"]) < 8:
                self._stat["msgs"].append(msg)

    # ---------------------------------------------------------------- 主流程
    def run(self) -> None:  # noqa: D401
        jobs: queue.Queue = queue.Queue()
        for idx, v in enumerate(self._videos):
            jobs.put((idx, v))
        target = os.path.join(self._out, _safe_name(self._actor))
        os.makedirs(target, exist_ok=True)

        threads = [threading.Thread(target=self._worker, args=(jobs, target),
                                    daemon=True)
                   for _ in range(min(self._parallel, max(1, len(self._videos))))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        msg = (f"完成 {self._stat['ok']} · 跳过 {self._stat['skip']} · "
               f"失败 {self._stat['fail']}")
        if self._stat["stop"]:
            msg += f" · 中止 {self._stat['stop']}"
        if self._stat["msgs"]:
            msg += " ｜ " + " / ".join(self._stat["msgs"][:2])
        self.finished_all.emit(dict(self._stat, text=msg))

    def _worker(self, jobs: queue.Queue, target: str) -> None:
        try:
            ns = backend.tangxin()
            dl = ns.downloader.TangXinDownloader(
                mode=ns.config.DOWNLOAD_MODE, proxy=ns.config.PROXY,
                stop_event=self._stop)
        except Exception as exc:  # noqa: BLE001
            self.finished_all.emit({"ok": 0, "skip": 0, "fail": 0,
                                    "stop": 0, "msgs": [], "error": str(exc),
                                    "text": f"下载器初始化失败：{exc}"})
            return

        try:
            while not self._stop.is_set():
                # 暂停：挂起但不退出，继续后接着干
                while self._paused.is_set() and not self._stop.is_set():
                    time.sleep(0.15)
                if self._stop.is_set():
                    break
                try:
                    idx, video = jobs.get_nowait()
                except queue.Empty:
                    break

                self.row_state.emit(idx, "下载中", 0)

                def _prog(cur=0, tot=0, phase=None, msg="", _i=idx):
                    pct = int(cur * 100 / tot) if tot else 0
                    label = {"skip": "跳过", "merge": "合并", "cover": "封面"}.get(
                        str(phase), "下载中")
                    self.row_state.emit(_i, label if not msg else f"{label} {msg}"[:26],
                                        max(0, min(100, pct)))

                try:
                    res = dl.download_video(video, target, progress=_prog,
                                            get_cover=True, stop_event=self._stop)
                except Exception as exc:  # noqa: BLE001
                    if self._stop.is_set():
                        self.row_state.emit(idx, "已停止", 0)
                        self._bump("stop")
                        break
                    self.row_state.emit(idx, f"失败：{exc}"[:40], 0)
                    self._bump("fail", str(exc)[:60])
                    continue

                if res.get("skipped"):
                    self.row_state.emit(idx, "已存在", 100)
                    self._bump("skip")
                elif res.get("ok"):
                    self.row_state.emit(idx, "完成", 100)
                    self._bump("ok")
                else:
                    self.row_state.emit(idx, f"失败：{res.get('error', '')}"[:40], 0)
                    self._bump("fail", str(res.get("error", ""))[:60])
        finally:
            try:
                dl.close()
            except Exception:
                pass


class TangxinPage(Page):
    key = "tangxin"
    title = "糖心 Vlog"
    subtitle = "导航页用代理读线路，站点直连；演员云 → 作品库 → 并发队列 → 下载记录"

    # 本地索引在后台线程建完后的通知。回调线程里不能直接碰 Qt 控件，
    # 必须过信号转回主线程再刷新「本地状态」列。
    _index_ready = pyqtSignal()

    # ---------------------------------------------------------------- 构建
    def build(self) -> None:
        s = settings()
        self._ns = None
        self._actors = []
        self._videos = []
        self._actor = None
        self._worker = None
        self._stop_event = threading.Event()
        self._paused_event = threading.Event()
        self._actors_cached = 0        # 本次展示来自缓存的演员数（断网兜底用）
        self._videos_cached = False    # 当前作品表是否先用缓存填的
        self._index_ready.connect(self._on_index_ready)
        self._status_colors = _status_colors()

        btn_out = QPushButton("打开输出目录")
        btn_out.setObjectName("Ghost")
        btn_out.clicked.connect(self._open_out)
        self.header.add_action(btn_out)
        btn_hist = QPushButton("下载记录")
        btn_hist.setObjectName("Ghost")
        btn_hist.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        self.header.add_action(btn_hist)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_crawl(), "采集与下载")
        self.tabs.addTab(self._tab_records(), "下载记录")
        # 2026-10-29：预览面板已屏蔽（同媒体库其它模块），宽度全部给封面墙。
        self.preview = None
        self.set_content(self.tabs)

        self._load_route_choices()
        self._update_pick()

    # ---------------------------------------------------------------- 采集页
    def _tab_crawl(self) -> QWidget:
        s = settings()
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        # 线路
        route_card = Card()
        rl = QVBoxLayout(route_card)
        rl.setContentsMargins(16, 12, 16, 12)
        rl.setSpacing(8)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("站点线路"))
        self.cmb_route = QComboBox()
        self.cmb_route.setMinimumWidth(360)
        row1.addWidget(self.cmb_route, 1)
        self.btn_nav = QPushButton("代理刷新导航")
        self.btn_nav.clicked.connect(self.refresh_nav)
        self.btn_apply = QPushButton("应用线路")
        self.btn_apply.setObjectName("Primary")
        self.btn_apply.clicked.connect(self.apply_route)
        row1.addWidget(self.btn_nav)
        row1.addWidget(self.btn_apply)
        rl.addLayout(row1)

        row2 = QHBoxLayout()
        self.chk_proxy = QCheckBox("站点请求走代理")
        self.chk_proxy.setChecked(bool(s.get("tangxin.proxy_enabled", False)))
        self.chk_proxy.stateChanged.connect(self._save_proxy_flag)
        self.badge_route = Badge("未设置")
        self.lbl_out = QLabel("输出：-")
        self.lbl_out.setObjectName("Muted")
        row2.addWidget(self.chk_proxy)
        row2.addWidget(self.badge_route)
        row2.addWidget(self.lbl_out, 1)
        rl.addLayout(row2)
        lay.addWidget(route_card)

        # 演员 / 作品
        split = QSplitter(Qt.Orientation.Horizontal)

        left = Card()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(14, 12, 14, 12)
        ll.setSpacing(8)
        lh = QHBoxLayout()
        t = QLabel("演员云")
        t.setObjectName("H2")
        self.badge_actors = Badge("0")
        lh.addWidget(t)
        lh.addWidget(self.badge_actors)
        self.chk_dl_only = QCheckBox("只看已下载")
        self.chk_dl_only.setToolTip("只显示「已下载数量 > 0」的演员（一眼看出谁下过）")
        self.chk_dl_only.stateChanged.connect(self._apply_actor_filter)
        lh.addWidget(self.chk_dl_only)
        lh.addStretch(1)
        self.btn_actors = QPushButton("加载演员云")
        self.btn_actors.setToolTip(
            "从本地缓存秒开，同时后台联网增量同步新演员。\n"
            "按住 Shift 点击＝忽略缓存强制重新抓取")
        self.btn_actors.clicked.connect(self._on_actors_clicked)
        lh.addWidget(self.btn_actors)
        ll.addLayout(lh)
        self.ed_filter = SearchInput("筛选演员…", width=0)
        self.ed_filter.textChanged.connect(self._filter_actors)
        ll.addWidget(self.ed_filter)
        self.list_actors = QListWidget()
        self.list_actors.currentRowChanged.connect(self._actor_changed)
        ll.addWidget(self.list_actors, 1)
        split.addWidget(left)

        right = Card()
        rrl = QVBoxLayout(right)
        rrl.setContentsMargins(14, 12, 14, 12)
        rrl.setSpacing(8)
        rh = QHBoxLayout()
        t2 = QLabel("作品库")
        t2.setObjectName("H2")
        self.lbl_actor = QLabel("未选择演员")
        self.lbl_actor.setObjectName("Muted")
        rh.addWidget(t2)
        rh.addWidget(self.lbl_actor, 1)
        self.btn_warm = QPushButton("预下载封面")
        self.btn_warm.setObjectName("Ghost")
        self.btn_warm.setToolTip(
            "把当前演员的全部封面提前抓下来，存到数据库同目录的 covers 缓存。\n"
            "下次打开（或断网时）直接读磁盘。")
        self.btn_warm.clicked.connect(self.warm_covers)
        rh.addWidget(self.btn_warm)
        self.btn_dirs = QPushButton("扫描目录…")
        self.btn_dirs.setObjectName("Ghost")
        self.btn_dirs.setToolTip(
            "管理「已知目录」（本机下载目录 + NAS/外盘）：列表存进 tangxin.db，\n"
            "换机器也不丢；已扫到的成品也记进库，下载前先查库，下过的直接跳过")
        self.btn_dirs.clicked.connect(self.manage_dirs)
        rh.addWidget(self.btn_dirs)
        self.btn_scan = QPushButton("重新扫描")
        self.btn_scan.setObjectName("Ghost")
        self.btn_scan.setToolTip(
            "重扫已知目录（本机下载目录 + 配置的 NAS/外盘），\n"
            "识别哪些作品已经下载、哪些别处已有")
        self.btn_scan.clicked.connect(self.scan_dirs)
        rh.addWidget(self.btn_scan)
        self.chk_hide_done = QCheckBox("隐藏已下载")
        self.chk_hide_done.stateChanged.connect(self._apply_video_filter)
        rh.addWidget(self.chk_hide_done)
        self.btn_all = QPushButton("全选未下")
        self.btn_all.setObjectName("Ghost")
        self.btn_all.clicked.connect(self._pick_undone)
        self.btn_none = QPushButton("清空")
        self.btn_none.setObjectName("Ghost")
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        rh.addWidget(self.btn_all)
        rh.addWidget(self.btn_none)
        rrl.addLayout(rh)
        rrl.addWidget(HLine())

        self.table_videos = make_table(
            ["选择", "标题", "时长", "本地状态", "编号"], [52, 350, 80, 96, 110])
        self.table_videos.itemChanged.connect(self._update_pick)
        self.table_videos.itemSelectionChanged.connect(self._preview_selected)

        # 封面墙：糖心一部作品一张封面，表格式的小缩略图看不清人。
        # 列表保留（勾选/状态/并发下载都在表上），封面墙作为并列视图。
        self._build_cover_wall()
        self.views_videos = QTabWidget()
        self.views_videos.setDocumentMode(True)
        self.views_videos.addTab(self.table_videos, "列表")
        self.views_videos.addTab(self.wall, "封面墙")
        rrl.addWidget(self.views_videos, 1)

        bottom = QHBoxLayout()
        self.lbl_pick = QLabel("已选 0")
        self.lbl_pick.setObjectName("Muted")
        self.sp_parallel = QSpinBox()
        self.sp_parallel.setRange(1, 8)
        self.sp_parallel.setValue(int(s.get("tangxin.parallel", 3)))
        self.sp_parallel.setToolTip("同时下载几部（每部内部按分片并发）")
        self.btn_download = QPushButton("开始下载")
        self.btn_download.setObjectName("Primary")
        self.btn_download.clicked.connect(self.start_download)
        bottom.addWidget(self.lbl_pick, 1)
        bottom.addWidget(QLabel("并发"))
        bottom.addWidget(self.sp_parallel)
        bottom.addWidget(self.btn_download)
        self.btn_wall_batch = QPushButton("批量选择")
        self.btn_wall_batch.setObjectName("Ghost")
        self.btn_wall_batch.setToolTip("进入封面墙多选模式：勾选多部作品后点底部「批量下载」")
        self.btn_wall_batch.clicked.connect(lambda: self.wall.enter_select_mode())
        bottom.addWidget(self.btn_wall_batch)
        rrl.addLayout(bottom)
        split.addWidget(right)
        split.setSizes([320, 860])
        lay.addWidget(split, 1)

        # 队列
        queue_card = Card()
        ql = QVBoxLayout(queue_card)
        ql.setContentsMargins(14, 12, 14, 12)
        ql.setSpacing(8)
        qh = QHBoxLayout()
        t3 = QLabel("下载队列")
        t3.setObjectName("H2")
        self.lbl_q = QLabel("空闲")
        self.lbl_q.setObjectName("Muted")
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setObjectName("Ghost")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_download)
        qh.addWidget(t3)
        qh.addWidget(self.lbl_q, 1)
        qh.addWidget(self.btn_pause)
        qh.addWidget(self.btn_stop)
        ql.addLayout(qh)
        self.table_queue = make_table(["视频", "状态", "进度"], [440, 200, 240])
        # 队列只留"看得见进度"的高度即可：主战场是上面的作品库和右侧预览，
        # 队列占太高会把两者挤扁。可折叠，展开时给到 240。
        self.table_queue.setMinimumHeight(88)
        self.table_queue.setMaximumHeight(160)
        ql.addWidget(self.table_queue)

        self.btn_queue_fold = QPushButton("展开")
        self.btn_queue_fold.setObjectName("Ghost")
        self.btn_queue_fold.clicked.connect(self._toggle_queue)
        qh.addWidget(self.btn_queue_fold)
        self._queue_expanded = False
        lay.addWidget(queue_card)
        return w

    def _toggle_queue(self) -> None:
        self._queue_expanded = not getattr(self, "_queue_expanded", False)
        if self._queue_expanded:
            self.table_queue.setMaximumHeight(240)
            self.btn_queue_fold.setText("收起")
        else:
            self.table_queue.setMaximumHeight(160)
            self.btn_queue_fold.setText("展开")

    # ---------------------------------------------------------------- 记录页
    def _tab_records(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(10)

        card = Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("下载记录（DATA/tangxin/tangxin.db）")
        t.setObjectName("H2")
        head.addWidget(t)
        head.addStretch(1)
        self.cmb_status = QComboBox()
        self.cmb_status.addItems(["全部", "done", "failed", "skipped", "started"])
        self.cmb_status.currentIndexChanged.connect(lambda _i: self.reload_records())
        head.addWidget(self.cmb_status)
        btn_refresh = QPushButton("刷新")
        btn_refresh.setObjectName("Ghost")
        btn_refresh.clicked.connect(self.reload_records)
        head.addWidget(btn_refresh)
        btn_export = QPushButton("导出 CSV")
        btn_export.setObjectName("Ghost")
        btn_export.clicked.connect(self.export_records)
        head.addWidget(btn_export)
        cl.addLayout(head)
        self.lbl_rec_stats = QLabel("读取中…")
        self.lbl_rec_stats.setObjectName("Muted")
        cl.addWidget(self.lbl_rec_stats)
        lay.addWidget(card)

        self.table_rec = make_table(
            ["状态", "演员", "标题", "大小", "时间", "错误"], [80, 130, 380, 90, 130, 220])
        lay.addWidget(self.table_rec, 1)
        return w

    # ---------------------------------------------------------------- 线路
    def on_show(self) -> None:
        if not self._ensure_backend():
            return
        self._load_route_choices()
        if not (settings().get("tangxin.route_url") or "").strip() \
                and self.cmb_route.currentData():
            self.apply_route()
        if self.tabs.currentIndex() == 1:
            self.reload_records()
        self._prime_from_cache()
        # 首次进入就自动拉演员云：否则页面一片空白，用户以为"没内容/坏了"。
        # 只自动跑一次，之后由「加载演员云」按钮决定是否刷新。
        if not getattr(self, "_auto_loaded", False):
            self._auto_loaded = True
            QTimer.singleShot(250, self.load_actors)
        # 进页面就保证本地索引是最新的（过期/空才真扫）。索引没建好时
        # 「本地状态」全是"未下载"，用户会以为功能没移植过来。
        self._ensure_index()

    # ------------------------------------------------------- 缓存优先 & 索引
    def _prime_from_cache(self) -> None:
        """进页面先用本地缓存把演员云填满（毫秒级，不联网）。

        DB 里存着 800+ 位演员，但之前界面从不读它 ⇒ 每次开软件都空白等网络。
        """
        if self.list_actors.count():
            return
        cached = self._cached_actors()
        if not cached:
            return
        self._fill_actors(cached)
        self._actors_cached = len(cached)
        self._sync_actors_btn(True)
        self.lbl_q.setText(f"缓存演员云：{len(cached)} 位")

    def _ensure_index(self) -> None:
        """需要时后台重建本地文件索引（用于判定"已下载/别处已有"）。

        ``ensure_ready`` 的 ``on_done`` 在**工作线程**里回调，直接刷表格会
        跨线程崩，所以用 ``_index_ready`` 信号转回主线程。
        """
        try:
            self._ns.existing.get_index().ensure_ready(
                max_age_hours=12,
                on_done=lambda *_a: self._index_ready.emit())
        except Exception:                                   # noqa: BLE001
            pass

    def _on_index_ready(self) -> None:
        """索引建完（或本来就是新的）：重算「本地状态」列 + 演员已下载标记。"""
        self._refresh_local_status()
        self._refresh_actor_dl()

    def _ensure_backend(self) -> bool:
        if self._ns is not None:
            return True
        try:
            self._ns = backend.tangxin()
        except Exception as exc:  # noqa: BLE001
            self.badge_route.set_tone("err")
            self.badge_route.setText("后端不可用")
            self.notify(f"糖心后端加载失败：{exc}", "err")
            return False
        self._refresh_route_label()
        return True

    def _load_route_choices(self) -> None:
        entries = nav.find_entries("tangxin")
        cur = settings().get("tangxin.route_url") or ""
        self.cmb_route.clear()
        for e in entries:
            if e.get("science_base_url"):
                self.cmb_route.addItem(
                    f"{e.get('name')} · 科学线路（推荐直连） {e['science_base_url']}",
                    e["science_base_url"])
            if e.get("base_url"):
                self.cmb_route.addItem(
                    f"{e.get('name')} · 主站（通常需代理） {e['base_url']}",
                    e.get("base_url"))
        if self.cmb_route.count() == 0:
            self.cmb_route.addItem(cur or "https://tangxinvlog.app（未从导航页解析）",
                                   cur or "https://tangxinvlog.app")
        if cur:
            for i in range(self.cmb_route.count()):
                if self.cmb_route.itemData(i) == cur:
                    self.cmb_route.setCurrentIndex(i)
                    break

    def refresh_nav(self) -> None:
        self.btn_nav.setEnabled(False)
        self.lbl_q.setText("读取导航页…")
        task = Task(nav.refresh, use_proxy=self.chk_proxy.isChecked(),
                    want_progress=True)
        task.done.connect(self._nav_done)
        task.failed.connect(lambda tb: (self.btn_nav.setEnabled(True),
                                        self.lbl_q.setText("导航页读取失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _nav_done(self, result: dict) -> None:
        self.btn_nav.setEnabled(True)
        self._load_route_choices()
        if result.get("ok"):
            self.lbl_q.setText(f"导航页已更新：{len(result.get('entries') or [])} 条")
            self.notify("导航页已更新，可点「应用线路」", "ok")
        else:
            self.lbl_q.setText("导航页读取失败（已保留缓存）")
            self.notify("导航页读取失败：" + (result.get("error") or "")[:140], "warn")

    def apply_route(self) -> None:
        url = (self.cmb_route.currentData() or "").strip()
        if not url:
            return
        s = settings()
        s.set("tangxin.route_url", url)
        s.set("tangxin.route_label", self.cmb_route.currentText())
        s.save()
        if self._ensure_backend():
            backend.apply_tangxin_settings(self._ns)
            self._refresh_route_label()
            self.notify(f"线路已切换：{url}", "ok")

    def _refresh_route_label(self) -> None:
        s = settings()
        route = s.get("tangxin.route_url") or getattr(
            getattr(self._ns, "config", None), "ORIGIN", "") or "未设置"
        proxy_on = s.get("tangxin.proxy_enabled", False)
        self.badge_route.set_tone("ok" if route else "warn")
        self.badge_route.setText(("代理" if proxy_on else "直连") + " · " + str(route)[:46])
        self.lbl_out.setText(f"输出：{paths.rel(s.get('tangxin.output_base', ''))}")

    def _save_proxy_flag(self) -> None:
        s = settings()
        s.set("tangxin.proxy_enabled", self.chk_proxy.isChecked())
        s.save()
        if self._ns is not None:
            backend.apply_tangxin_settings(self._ns)
        self._refresh_route_label()

    def _open_out(self) -> None:
        if not open_path(settings().get("tangxin.output_base", "")):
            self.notify("输出目录尚未创建（下载一次后自动建立）", "warn")

    # ---------------------------------------------------------------- 演员
    def _on_actors_clicked(self) -> None:
        """按钮入口：默认走缓存+增量，Shift+点击强制全量重抓。"""
        mods = QApplication.keyboardModifiers()
        force = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        self.load_actors(force=force)

    def _cached_actors(self) -> list:
        """读本地缓存里的演员云（毫秒级）。失败返回空列表。"""
        try:
            return list(self._ns.cache.get_actors() or [])
        except Exception:                                   # noqa: BLE001
            return []

    def _fill_actors(self, actors) -> None:
        """把演员列表填进左栏（缓存/联网结果共用），并补「已下 N」标记。"""
        self._actors = list(actors or [])
        self.badge_actors.setText(str(len(self._actors)))
        self.list_actors.blockSignals(True)
        self.list_actors.clear()
        for a in self._actors:
            item = QListWidgetItem(f"{a.name}  ({a.count})")
            item.setData(Qt.ItemDataRole.UserRole, a)
            self.list_actors.addItem(item)
        self.list_actors.blockSignals(False)
        # 索引就绪后（或此刻已有）补「已下 N」角标 + 强调色；索引没建好时先
        # 出列表，随后 _on_index_ready 会再刷一遍。
        self._refresh_actor_dl()

    def _refresh_actor_dl(self) -> None:
        """（重）计算每位演员的已下载数量，更新文案 + 强调色标记。

        与作品表的「本地状态」列同源：都来自 ``existing.downloaded_count_by_actor``
        （按规范化演员名统计）。已下过的演员在前面加「✓」并上绿色，方便一眼看出
        谁下过、谁没下过。
        """
        dl: dict = {}
        try:
            if self._ns is not None:
                dl = (self._ns.existing.get_index().downloaded_count_by_actor()
                      or {})
        except Exception:                                   # noqa: BLE001
            dl = {}
        self._dl_by_actor = dl
        for i in range(self.list_actors.count()):
            item = self.list_actors.item(i)
            if item is None:
                continue
            a = item.data(Qt.ItemDataRole.UserRole)
            if a is None:
                continue
            n = dl.get(_norm_key(a.name), 0)
            base = f"{a.name}  ({a.count})"
            if n > 0:
                item.setText(f"✓ {base}  已下 {n}")
                item.setForeground(QBrush(QColor("#36c275")))
            else:
                item.setText(base)
        self._apply_actor_filter()

    def _filter_actors(self, text: str) -> None:
        """演员搜索框输入：记录关键字后重算可见性。"""
        self._actor_kw = (text or "").strip().lower()
        self._apply_actor_filter()

    def _apply_actor_filter(self) -> None:
        """统一过滤：搜索关键字 + 「只看已下载」复选框同时生效。"""
        kw = getattr(self, "_actor_kw", "")
        dl = getattr(self, "_dl_by_actor", {}) or {}
        only_dl = bool(getattr(self, "chk_dl_only", None)
                       and self.chk_dl_only.isChecked())
        for i in range(self.list_actors.count()):
            item = self.list_actors.item(i)
            if item is None:
                continue
            a = item.data(Qt.ItemDataRole.UserRole)
            name = (a.name if a else item.text()).lower()
            hide_kw = bool(kw) and kw not in name
            hide_dl = bool(only_dl) and (a is None or dl.get(_norm_key(a.name), 0) == 0)
            item.setHidden(bool(hide_kw or hide_dl))

    def load_actors(self, force: bool = False) -> None:
        """加载演员云：**缓存优先**，后台再联网增量同步。

        之前每次进页面都要联网抓 800+ 位演员（几秒到几十秒空白等待），
        而 ``tangxin.cache`` 里其实早就存好了全量演员云——只是界面没去读。
        现在：缓存非空 ⇒ 先秒开填表，再后台抓一遍只补新增（只增不改，
        界面不闪）；force=True（Shift+点击）才忽略缓存重抓。
        """
        if not self._ensure_backend():
            return

        # 1) 缓存优先：先拿 DB 里的演员云秒开，用户不用盯着空列表
        shown = 0
        if not force:
            cached = self._cached_actors()
            if cached:
                self._fill_actors(cached)
                shown = len(cached)
        self._actors_cached = shown
        self._sync_actors_btn(shown > 0)

        self.btn_actors.setEnabled(False)
        self.lbl_q.setText(
            f"缓存 {shown} 位，后台增量同步…" if shown else "加载演员云…")

        ns = self._ns

        def _run(progress=None):
            crawler = ns.crawler.TangXinCrawler()
            try:
                actors = crawler.fetch_actor_cloud(progress=progress)
            finally:
                try:
                    crawler.close()
                except Exception:
                    pass
            # 联网结果写回缓存：下次打开就是秒开（只增不改）
            delta = {}
            try:
                delta = ns.cache.upsert_actors(actors) or {}
            except Exception:                               # noqa: BLE001
                delta = {}
            return actors, delta, shown

        task = Task(_run, want_progress=True)
        task.progress.connect(lambda p, t: self.lbl_q.setText(t or "加载演员云…"))
        task.done.connect(self._actors_done)
        task.failed.connect(self._actors_fail)
        CENTER.start(task)

    def _sync_actors_btn(self, has_cache: bool) -> None:
        self.btn_actors.setText("刷新演员云" if has_cache else "加载演员云")

    def _actors_done(self, payload) -> None:
        actors, delta, shown = payload
        self.btn_actors.setEnabled(True)
        self._fill_actors(actors)
        self._sync_actors_btn(True)
        added = int((delta or {}).get("added", 0) or 0)
        updated = int((delta or {}).get("updated", 0) or 0)
        self.lbl_q.setText(f"演员云：{len(self._actors)} 位")
        msg = f"演员云：{len(self._actors)} 位"
        if added or updated:
            msg += f"（新增 {added} · 更新 {updated}）"
        elif shown:
            msg += "（缓存一致）"
        self.notify(msg, "ok")

    def _actors_fail(self, tb: str) -> None:
        self.btn_actors.setEnabled(True)
        # 缓存兜底：网断了但本地有数据 ⇒ 保留已填好的列表，只提示同步失败
        if self._actors_cached:
            self.lbl_q.setText(f"增量同步失败，仍显示缓存 {self._actors_cached} 位")
            self.notify("联网同步失败，当前显示本地缓存", "warn")
            return
        self.lbl_q.setText("演员云加载失败")
        self.notify((tb or "").splitlines()[-1][:160] if tb else "加载失败", "err")

    def _actor_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self.list_actors.item(row)
        actor = item.data(Qt.ItemDataRole.UserRole) if item else None
        if actor is None:
            return
        self._actor = actor
        self.lbl_actor.setText(f"{actor.name} · 标注 {actor.count} 部")
        self.load_videos(actor)

    # ---------------------------------------------------------------- 封面墙
    def _build_cover_wall(self) -> None:
        """糖心作品封面墙（与黄果/九色同一套通用网格）。"""
        from ..cover_cache import CoverCache
        from ..cover_grid import CoverGrid

        origin = ""
        try:
            origin = (getattr(backend.tangxin().config, "ORIGIN", "") or "").rstrip("/")
        except Exception:                                   # noqa: BLE001
            origin = ""
        self._cover_referer = origin

        def _fetch(url: str):
            import requests

            try:
                r = requests.get(url, timeout=15, headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/126.0.0.0 Safari/537.36"),
                    "Referer": (self._cover_referer + "/") if self._cover_referer else url,
                })
                if r.status_code == 200 and r.content:
                    return r.content
            except Exception:                               # noqa: BLE001
                return None
            return None

        # 封面落在数据库同目录（DATA/tangxin/covers），跟黄果/九色一个规矩
        self._wall_cache = CoverCache(paths.data_dir("tangxin", "covers"),
                                      fetch=_fetch, size=(160, 224))
        self.wall = CoverGrid(
            theme.tokens(settings().get("theme", "dark") != "light"),
            (160, 224), self._wall_cache,
            key_field="vid", cover_field="cover_url", total_field="total",
            # 糖心一部作品就是"一集"：已下载就标 1/1，角标才显示得出来
            default_total=1,
            sub_fn=lambda v: str(getattr(v, "duration", "") or ""))
        self.wall.selected.connect(self._on_wall_selected)
        self.wall.activated.connect(self._on_wall_activated)
        self.wall.setMinimumHeight(260)
        # 封面墙多选下载：勾选多部作品 → 在列表里打勾 → 走既有下载流
        self.wall.batch_download_requested.connect(self._wall_batch_download)
        # 排序：按标题 / 按时长（时长形如 12:34，转成秒做数值排序）
        def _dur_sec(d):
            s = str(d.get("sub") or "").strip()
            parts = s.split(":")
            try:
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
                return int(s)
            except (TypeError, ValueError):
                return 0
        self.wall.set_sort_options([
            {"label": "按标题", "key": "title"},
            {"label": "按时长长→短", "key": "sub", "reverse": True, "fn": _dur_sec},
        ])

    def _on_wall_selected(self, item: dict) -> None:
        v = item.get("data") or item
        try:
            self._preview_video(v)
        except Exception:                                   # noqa: BLE001
            pass

    def _on_wall_activated(self, item: dict) -> None:
        """双击封面墙卡片 = 在列表里勾选它（下载仍走列表勾选）。"""
        vid = str(item.get("key") or "")
        for r in range(self.table_videos.rowCount()):
            cell = self.table_videos.item(r, 0)
            if cell is None:
                continue
            v = cell.data(Qt.ItemDataRole.UserRole)
            if v is not None and str(getattr(v, "vid", "")) == vid:
                cell.setCheckState(Qt.CheckState.Checked)
                self.table_videos.selectRow(r)
                break
        self._update_pick()

    def warm_covers(self) -> None:
        """把当前演员的全部封面提前抓到磁盘（数据库同目录的 covers）。"""
        vids = list(getattr(self, "_videos", []) or [])
        pairs = [(str(getattr(v, "vid", "")),
                  (getattr(v, "cover_url", "") or "").strip()) for v in vids]
        pairs = [(k, u) for k, u in pairs if k and u]
        if not pairs:
            self.notify("当前列表没有可下载的封面", "warn")
            return
        self.btn_warm.setEnabled(False)

        def _run(progress=None):
            return self._wall_cache.warm(pairs, on_progress=progress)

        task = Task(_run, want_progress=True)
        task.done.connect(self._warm_done)
        task.failed.connect(lambda tb: (self.btn_warm.setEnabled(True),
                                        self.notify("封面预下载失败", "err")))
        CENTER.start(task)

    def _warm_done(self, stat) -> None:
        self.btn_warm.setEnabled(True)
        stat = stat or {}
        self.notify(
            f"封面预下载完成：新下 {stat.get('ok', 0)} 张"
            f"（已有 {stat.get('skip', 0)} 张）", "ok")

    # ---------------------------------------------------------------- 作品
    def _status_item(self, st: str) -> QTableWidgetItem:
        """本地状态单元格：文字 + 配色 + 排序用的 UserRole。"""
        label = {"downloaded": "已下载", "elsewhere": "别处已有"}.get(st, "未下载")
        item = QTableWidgetItem(label)
        item.setForeground(QBrush(QColor(self._status_colors.get(st, "#69748a"))))
        item.setData(Qt.ItemDataRole.UserRole, "done" if st != "none" else "")
        return item

    def _cached_videos(self, actor) -> list:
        try:
            return list(self._ns.cache.get_videos(actor.slug) or [])
        except Exception:                                   # noqa: BLE001
            return []

    def _fill_videos(self, videos, status=None) -> None:
        """填作品表（缓存/联网结果共用）。status 为 None 时列显示「…」待定。"""
        self._videos = list(videos or [])
        # 封面墙同步填一份（列表 + 封面墙两个视图共用同一批数据）
        try:
            self.wall.set_items(
                self._videos,
                {k: (1 if v != "none" else 0)
                 for k, (v, _p) in (status or {}).items()} if status else None)
        except Exception:                                   # noqa: BLE001
            pass
        self.table_videos.blockSignals(True)
        self.table_videos.setRowCount(0)
        for v in self._videos:
            r = self.table_videos.rowCount()
            self.table_videos.insertRow(r)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, v)
            self.table_videos.setItem(r, 0, chk)
            self.table_videos.setItem(r, 1, QTableWidgetItem(v.title))
            self.table_videos.setItem(r, 2, QTableWidgetItem(v.duration or "-"))
            if status is None:
                item = QTableWidgetItem("…")
                item.setData(Qt.ItemDataRole.UserRole, "")
            else:
                st, _p = (status or {}).get(str(v.vid), ("none", None))
                item = self._status_item(st)
            self.table_videos.setItem(r, 3, item)
            self.table_videos.setItem(r, 4, QTableWidgetItem(str(v.vid)))
        self.table_videos.blockSignals(False)

    def load_videos(self, actor, force: bool = False) -> None:
        """加载某演员的作品：缓存优先秒开，后台联网增量补新作品。

        缓存里已经有该演员的作品时，先秒开填表并立刻算一次本地状态；
        后台抓完再把新增的补进去（只增不改，不闪烁）。
        """
        if not self._ensure_backend():
            return
        self.table_videos.setRowCount(0)
        ns = self._ns

        shown = 0
        if not force:
            cached = self._cached_videos(actor)
            if cached:
                self._fill_videos(cached)
                shown = len(cached)
        self._videos_cached = shown > 0
        self.lbl_q.setText(
            f"缓存 {shown} 部，后台增量同步…" if shown else f"抓取 {actor.name} 的作品…")
        # 缓存先填 ⇒ 本地状态马上就能算出来（不依赖网络）
        if shown:
            self._refresh_local_status()

        def _run(progress=None):
            crawler = ns.crawler.TangXinCrawler()
            try:
                videos = crawler.fetch_actor_videos(actor, 0, progress=progress)
            finally:
                try:
                    crawler.close()
                except Exception:
                    pass
            try:
                ns.cache.upsert_videos(actor.slug, actor.name, videos)
            except Exception:                               # noqa: BLE001
                pass
            status = {}
            try:
                out_base = settings().get("tangxin.output_base", "")
                status = ns.existing.get_index().status_map(videos, out_base)
            except Exception:                               # noqa: BLE001
                status = {}
            return videos, status, shown

        task = Task(_run, want_progress=True)
        task.progress.connect(lambda p, t: self.lbl_q.setText(t or "抓取作品…"))
        task.done.connect(self._videos_done)
        task.failed.connect(self._videos_fail)
        CENTER.start(task)

    def _videos_done(self, payload) -> None:
        videos, status, _shown = payload
        self._videos_cached = False
        self._fill_videos(videos, status)
        self._summarize_videos()

    def _videos_fail(self, tb: str) -> None:
        # 缓存兜底：已经用缓存填过表了，就别把表清掉
        if getattr(self, "_videos_cached", False) and self._videos:
            self.lbl_q.setText(f"增量同步失败，仍显示缓存 {len(self._videos)} 部")
            self._refresh_local_status()
            self._summarize_videos()
            self.notify("联网同步失败，当前显示本地缓存", "warn")
            return
        self.lbl_q.setText("作品抓取失败")
        self.notify((tb or "").splitlines()[-1][:160] if tb else "抓取失败", "err")

    def _summarize_videos(self) -> None:
        done = sum(1 for r in range(self.table_videos.rowCount())
                   if (self.table_videos.item(r, 3) or QTableWidgetItem()).data(
                       Qt.ItemDataRole.UserRole) == "done")
        self.lbl_q.setText(
            f"{self._actor.name if self._actor else ''} · {len(self._videos)} 部作品"
            f"（本地已有 {done}）")
        self._apply_video_filter()
        self._update_pick()

    def _apply_video_filter(self) -> None:
        hide = self.chk_hide_done.isChecked()
        for r in range(self.table_videos.rowCount()):
            item = self.table_videos.item(r, 3)
            done = item is not None and item.data(Qt.ItemDataRole.UserRole) == "done"
            self.table_videos.setRowHidden(r, bool(hide and done))

    # ------------------------------------------------------------ 扫描目录
    def scan_dirs(self) -> None:
        """重扫已知目录：本机下载目录 + 设置里配置的 NAS/外盘目录。

        为什么要能手动触发：索引虽然会懒加载，但用户刚拷进一批成品、或刚挂上
        NAS 时，界面上的「本地状态」还是旧的。这里做一次明确的全盘重扫，
        结果同时写库（下载记录）和本页状态列。
        """
        if not self._ensure_backend():
            return
        self.btn_scan.setEnabled(False)
        self.lbl_q.setText("扫描已知目录…")
        ns = backend.tangxin()

        def _run(progress=None):
            idx = ns.existing.get_index()
            dirs = list(getattr(idx, "known_dirs", []) or [])
            if progress:
                progress(0, max(1, len(dirs)),
                         "开始扫描：" + ("、".join(dirs) if dirs else "（未配置目录）"))
            return idx.refresh(on_progress=progress)

        task = Task(_run, want_progress=True)
        task.progress.connect(lambda p, t: self.lbl_q.setText((t or "扫描中…")[:120]))
        task.done.connect(self._scan_done)
        task.failed.connect(self._scan_failed)
        CENTER.start(task)

    def _scan_done(self, n) -> None:
        self.btn_scan.setEnabled(True)
        self.lbl_q.setText(f"扫描完成：索引 {int(n or 0)} 个文件")
        self.notify(f"扫描完成：本地已有 {int(n or 0)} 个文件", "ok")
        self._refresh_local_status()

    # ------------------------------------------------------ 已知目录管理
    def manage_dirs(self) -> None:
        """管理扫描根目录（存 tangxin.db，不再只存 settings.json）。

        为什么要能加：用户的成品常分散在本机 + NAS + 移动硬盘。没配目录 ⇒
        索引建不起来 ⇒ 每个作品都被判成"未下载" ⇒ 重复下载。
        """
        if not self._ensure_backend():
            return
        from PyQt6.QtWidgets import (
            QDialog, QDialogButtonBox, QFileDialog, QListWidget, QMessageBox,
        )

        idx = self._ns.existing.get_index()
        dirs = list(getattr(idx, "known_dirs", []) or [])

        dlg = QDialog(self)
        dlg.setWindowTitle("已知扫描目录（存进 tangxin.db）")
        dlg.resize(560, 360)
        lay = QVBoxLayout(dlg)
        tip = QLabel("这些目录下的成品会被扫进索引；下载前先查索引，已存在的直接跳过。")
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        lst = QListWidget()
        for d in dirs:
            lst.addItem(d)
        lay.addWidget(lst, 1)

        def _add() -> None:
            p = QFileDialog.getExistingDirectory(dlg, "选择要纳入扫描的目录", "")
            if not p:
                return
            np = os.path.normpath(p)
            for i in range(lst.count()):
                if os.path.normpath(lst.item(i).text()) == np:
                    return
            lst.addItem(np)

        def _remove() -> None:
            row = lst.currentRow()
            if row >= 0:
                lst.takeItem(row)

        row_b = QHBoxLayout()
        b_add = QPushButton("添加目录…")
        b_add.clicked.connect(_add)
        b_del = QPushButton("移除选中")
        b_del.setObjectName("Danger")
        b_del.clicked.connect(_remove)
        row_b.addWidget(b_add)
        row_b.addWidget(b_del)
        row_b.addStretch(1)
        lay.addLayout(row_b)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        lay.addWidget(box)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_dirs = [lst.item(i).text() for i in range(lst.count())]
        try:
            self._ns.existing.apply_known_dirs(new_dirs)
        except Exception as exc:                            # noqa: BLE001
            self.notify(f"保存目录失败：{exc}", "err")
            return
        self.notify(f"已保存 {len(new_dirs)} 个扫描目录到数据库", "ok")
        # 目录变了 ⇒ 旧索引不可信，立即重扫一次
        self.scan_dirs()

    def _scan_failed(self, tb: str) -> None:
        self.btn_scan.setEnabled(True)
        self.lbl_q.setText("扫描失败")
        text = (tb or "").strip()
        self.notify(text.splitlines()[-1][:160] if text else "扫描失败", "err")

    def _refresh_local_status(self) -> None:
        """只重算「本地状态」列（不重新抓网络列表）。"""
        if not self._videos:
            return
        videos = list(self._videos)
        ns = backend.tangxin()
        out_base = settings().get("tangxin.output_base", "")

        def _run(progress=None):
            try:
                return ns.existing.get_index().status_map(videos, out_base)
            except Exception:                       # noqa: BLE001
                return {}

        task = Task(_run)
        task.done.connect(self._apply_status_map)
        CENTER.start(task)

    def _apply_status_map(self, status) -> None:
        status = status or {}
        # 封面墙的「已下载」角标跟表格用同一份状态（网格按 vid 匹配）
        try:
            self.wall.refresh_status(
                {k: (1 if v != "none" else 0) for k, (v, _p) in status.items()})
        except Exception:                                   # noqa: BLE001
            pass
        self.table_videos.blockSignals(True)
        for r, v in enumerate(self._videos):
            if r >= self.table_videos.rowCount():
                break
            st, _p = status.get(str(v.vid), ("none", None))
            self.table_videos.setItem(r, 3, self._status_item(st))
        self.table_videos.blockSignals(False)
        self._summarize_videos()   # 内含 filter + 选中数统计

    def _visible_rows(self):
        for r in range(self.table_videos.rowCount()):
            if not self.table_videos.isRowHidden(r):
                yield r

    def _check_all(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self.table_videos.blockSignals(True)
        for r in self._visible_rows():
            item = self.table_videos.item(r, 0)
            if item:
                item.setCheckState(state)
        self.table_videos.blockSignals(False)
        self._update_pick()

    def _pick_undone(self) -> None:
        self.table_videos.blockSignals(True)
        for r in self._visible_rows():
            chk = self.table_videos.item(r, 0)
            st = self.table_videos.item(r, 3)
            done = st is not None and st.data(Qt.ItemDataRole.UserRole) == "done"
            if chk:
                chk.setCheckState(Qt.CheckState.Unchecked if done else Qt.CheckState.Checked)
        self.table_videos.blockSignals(False)
        self._update_pick()

    def _update_pick(self, *_a) -> None:
        n = len(self._picked())
        self.lbl_pick.setText(f"已选 {n}")
        self.btn_download.setText(f"开始下载 ({n})" if n else "开始下载")

    def _preview_selected(self) -> None:
        row = self.table_videos.currentRow()
        if row < 0:
            return
        item = self.table_videos.item(row, 0)
        if not item:
            return
        v = item.data(Qt.ItemDataRole.UserRole)
        if v:
            self._preview_video(v)

    def _preview_video(self, v) -> None:
        """占位：内置预览面板已按用户要求屏蔽（保留方法避免旧调用报错）。"""

    def _picked(self):
        out = []
        for r in range(self.table_videos.rowCount()):
            item = self.table_videos.item(r, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                v = item.data(Qt.ItemDataRole.UserRole)
                if v is not None:
                    out.append(v)
        return out

    # ---------------------------------------------------------------- 下载
    def start_download(self) -> None:
        videos = self._picked()
        if not videos:
            self.notify("请先勾选作品（可点「全选未下」）", "warn")
            return
        if not self._ensure_backend():
            return
        if self._worker is not None:
            self.notify("已有下载任务在跑，请先停止", "warn")
            return
        out_base = settings().get("tangxin.output_base", "")
        if not out_base:
            self.notify("请先在设置里指定输出目录", "warn")
            return
        os.makedirs(out_base, exist_ok=True)
        s = settings()
        s.set("tangxin.parallel", int(self.sp_parallel.value()))
        s.save()

        self.table_queue.setRowCount(0)
        for v in videos:
            r = self.table_queue.rowCount()
            self.table_queue.insertRow(r)
            self.table_queue.setItem(r, 0, QTableWidgetItem(v.title))
            self.table_queue.setItem(r, 1, QTableWidgetItem("等待"))
            bar = QProgressBar()
            bar.setRange(0, 100)
            self.table_queue.setCellWidget(r, 2, bar)

        self._stop_event = threading.Event()
        self._paused_event = threading.Event()
        self._worker = _PoolDownload(
            videos, out_base,
            self._actor.name if self._actor else "未分类",
            int(self.sp_parallel.value()),
            int(s.get("tangxin.workers", 8)),
            self._stop_event, self._paused_event)
        self._worker.row_state.connect(self._row_state)
        self._worker.finished_all.connect(self._dl_finished)

        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        self.btn_download.setEnabled(False)
        self.lbl_q.setText(f"下载中 · {len(videos)} 部 · 并发 {self.sp_parallel.value()}")
        self._worker.start()

    # --------------------------------------------------- 封面墙批量下载
    def _wall_batch_download(self, keys: list) -> None:
        """封面墙多选后点「批量下载」：把选中的作品在列表里打勾，再走下载流。"""
        want = {str(k) for k in keys}
        if not want:
            return
        self.table_videos.blockSignals(True)
        n = 0
        for r in range(self.table_videos.rowCount()):
            cell = self.table_videos.item(r, 0)
            if cell is None:
                continue
            v = cell.data(Qt.ItemDataRole.UserRole)
            if v is not None and str(getattr(v, "vid", "")) in want:
                cell.setCheckState(Qt.CheckState.Checked)
                n += 1
        self.table_videos.blockSignals(False)
        self._update_pick()
        if not n:
            self.notify("所选作品不在当前列表中", "warn")
            return
        self.start_download()

    def _row_state(self, idx: int, status: str, pct: int) -> None:
        if idx >= self.table_queue.rowCount():
            return
        item = self.table_queue.item(idx, 1)
        if item:
            item.setText(status)
        bar = self.table_queue.cellWidget(idx, 2)
        if isinstance(bar, QProgressBar):
            bar.setValue(max(0, min(100, pct)))

    def _dl_finished(self, stat: dict) -> None:
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_download.setEnabled(True)
        self.btn_pause.setText("暂停")
        text = stat.get("text") or "下载结束"
        self.lbl_q.setText(text)
        self.notify(text, "err" if stat.get("error") else
                    ("warn" if stat.get("fail") else "ok"))
        self._worker = None
        self._paused_event.clear()
        if self.tabs.currentIndex() == 1:
            self.reload_records()
        # 下完立刻重算「本地状态」：否则刚下好的作品还显示"未下载"，
        # 用户以为没下成、又点一遍（existing.record_downloaded 已经写库了，这里只是刷新界面）
        self._refresh_local_status()

    def toggle_pause(self) -> None:
        if self._worker is None:
            return
        if self._paused_event.is_set():
            self._paused_event.clear()
            self.btn_pause.setText("暂停")
            self.lbl_q.setText("已继续")
        else:
            self._paused_event.set()
            self.btn_pause.setText("继续")
            self.lbl_q.setText("已暂停（正在写的分片会写完当前块）")

    def stop_download(self) -> None:
        self._stop_event.set()
        self._paused_event.clear()
        self.btn_stop.setEnabled(False)
        self.lbl_q.setText("正在停止…")

    # ---------------------------------------------------------------- 记录
    def reload_records(self) -> None:
        if not self._ensure_backend():
            return
        status = None if self.cmb_status.currentIndex() == 0 else \
            self.cmb_status.currentText()

        def _run():
            return (self._ns.db.get_records(limit=800, status=status),
                    self._ns.db.count_by_status())

        task = Task(_run)
        task.done.connect(self._records_done)
        task.failed.connect(lambda tb: self.notify(tb.splitlines()[-1][:160], "err"))
        CENTER.start(task)

    def _records_done(self, payload) -> None:
        rows, counts = payload
        self.table_rec.setRowCount(0)
        for rec in rows or []:
            r = self.table_rec.rowCount()
            self.table_rec.insertRow(r)
            st = rec.get("status", "")
            self.table_rec.setItem(r, 0, QTableWidgetItem(st))
            self.table_rec.setItem(r, 1, QTableWidgetItem(rec.get("actor", "") or ""))
            self.table_rec.setItem(r, 2, QTableWidgetItem(rec.get("title", "") or ""))
            size = rec.get("size_bytes") or 0
            self.table_rec.setItem(r, 3, QTableWidgetItem(
                f"{size / 1024 / 1024:.1f} MB" if size else "-"))
            ts = rec.get("finished_at") or rec.get("started_at") or 0
            self.table_rec.setItem(r, 4, QTableWidgetItem(
                time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"))
            self.table_rec.setItem(r, 5, QTableWidgetItem(
                (rec.get("error") or "")[:120]))
        parts = " · ".join(f"{k} {v}" for k, v in sorted((counts or {}).items()))
        self.lbl_rec_stats.setText(f"共 {len(rows or [])} 条 ｜ {parts or '暂无记录'}")

    def export_records(self) -> None:
        if not self._ensure_backend():
            return
        out, _ = QFileDialog.getSaveFileName(
            self, "导出下载记录", str(paths.DATA / "tangxin_records.csv"),
            "CSV (*.csv)")
        if not out:
            return
        try:
            rows = self._ns.db.get_records(limit=100000)
            with open(out, "w", newline="", encoding="utf-8-sig") as fh:
                if rows:
                    w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
            self.notify(f"已导出 {len(rows)} 条：{paths.rel(out)}", "ok")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"导出失败：{exc}", "err")
