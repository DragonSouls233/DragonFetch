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
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QProgressBar, QPushButton, QSpinBox, QSplitter,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from ...core import backend, nav, paths
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from ..widgets import Badge, Card, HLine, SearchInput, make_table, open_path
from .base import Page
from ..preview import PreviewPane


def _safe_name(text: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", (text or "").strip())
    return s.strip(" .") or "未命名"


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
        self.preview = PreviewPane()
        self.preview.setMinimumHeight(320)
        self.attach_preview(self.tabs, self.preview)

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
        lh.addStretch(1)
        self.btn_actors = QPushButton("加载演员云")
        self.btn_actors.clicked.connect(self.load_actors)
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
        rrl.addWidget(self.table_videos, 1)

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
        # 首次进入就自动拉演员云：否则页面一片空白，用户以为"没内容/坏了"。
        # 只自动跑一次，之后由「加载演员云」按钮决定是否刷新。
        if not getattr(self, "_auto_loaded", False):
            self._auto_loaded = True
            if self.list_actors.count() == 0:
                QTimer.singleShot(250, self.load_actors)

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
    def load_actors(self) -> None:
        if not self._ensure_backend():
            return
        self.btn_actors.setEnabled(False)
        self.lbl_q.setText("加载演员云…")

        def _run(progress=None):
            ns = backend.tangxin()
            crawler = ns.crawler.TangXinCrawler()
            try:
                return crawler.fetch_actor_cloud(progress=progress)
            finally:
                try:
                    crawler.close()
                except Exception:
                    pass

        task = Task(_run, want_progress=True)
        task.progress.connect(lambda p, t: self.lbl_q.setText(t or "加载演员云…"))
        task.done.connect(self._actors_done)
        task.failed.connect(self._actors_fail)
        CENTER.start(task)

    def _actors_done(self, actors) -> None:
        self.btn_actors.setEnabled(True)
        self._actors = list(actors or [])
        self.badge_actors.setText(str(len(self._actors)))
        self.list_actors.clear()
        for a in self._actors:
            item = QListWidgetItem(f"{a.name}  ({a.count})")
            item.setData(Qt.ItemDataRole.UserRole, a)
            self.list_actors.addItem(item)
        self.lbl_q.setText(f"演员云：{len(self._actors)} 位")
        self.notify(f"演员云：{len(self._actors)} 位", "ok")

    def _actors_fail(self, tb: str) -> None:
        self.btn_actors.setEnabled(True)
        self.lbl_q.setText("演员云加载失败")
        self.notify(tb.splitlines()[-1][:160], "err")

    def _filter_actors(self, text: str) -> None:
        kw = (text or "").strip().lower()
        for i in range(self.list_actors.count()):
            item = self.list_actors.item(i)
            item.setHidden(bool(kw) and kw not in item.text().lower())

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

    # ---------------------------------------------------------------- 作品
    def load_videos(self, actor) -> None:
        if not self._ensure_backend():
            return
        self.table_videos.setRowCount(0)
        self.lbl_q.setText(f"抓取 {actor.name} 的作品…")

        def _run(progress=None):
            ns = backend.tangxin()
            crawler = ns.crawler.TangXinCrawler()
            try:
                videos = crawler.fetch_actor_videos(actor, 0, progress=progress)
            finally:
                try:
                    crawler.close()
                except Exception:
                    pass
            status = {}
            try:
                out_base = settings().get("tangxin.output_base", "")
                status = ns.existing.get_index().status_map(videos, out_base)
            except Exception:
                status = {}
            return videos, status

        task = Task(_run, want_progress=True)
        task.progress.connect(lambda p, t: self.lbl_q.setText(t or "抓取作品…"))
        task.done.connect(self._videos_done)
        task.failed.connect(lambda tb: (self.lbl_q.setText("作品抓取失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _videos_done(self, payload) -> None:
        videos, status = payload
        self._videos = list(videos or [])
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
            st, _p = (status or {}).get(str(v.vid), ("none", None))
            label = {"downloaded": "已下载", "elsewhere": "别处已有"}.get(st, "未下载")
            item = QTableWidgetItem(label)
            if st != "none":
                item.setData(Qt.ItemDataRole.UserRole, "done")
            self.table_videos.setItem(r, 3, item)
            self.table_videos.setItem(r, 4, QTableWidgetItem(str(v.vid)))
        self.table_videos.blockSignals(False)
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
        if not v:
            return
        # 糖心的 Video 自带 m3u8_url（CDN 拼接，不联网），直接当预览直链用；
        # 面板检测到 .m3u8 会自动用 ffmpeg 截前 60 秒转封装后内嵌播。
        m3u8 = ""
        try:
            m3u8 = v.m3u8_url or ""
        except Exception:  # noqa: BLE001
            m3u8 = ""
        # 注意不要把 m3u8 直接塞进 video：那会在"选中行"时就触发 ffmpeg 转封装
        # （等于自动下 60 秒），改成只给 resolver，用户点「在线播放」才准备。
        self.preview.preview(
            title=getattr(v, "title", "") or "",
            thumb=getattr(v, "cover_url", "") or None,
            local_dir=settings().get("tangxin.output_base", "") or None,
            local_hint=getattr(v, "title", "") or "",
            resolver=(lambda u=m3u8: u) if m3u8 else None,
        )

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
