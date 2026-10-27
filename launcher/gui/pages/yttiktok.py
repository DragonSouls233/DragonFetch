# -*- coding: utf-8 -*-
"""映取 · YouTube / TikTok：批量链接 → 解析 → 选画质 → 并发下载。

重建要点（2026-10）：
  * 支持**一次粘贴多条链接**（自动抽链接、去重、逐条解析）
  * 画质档位（最佳 / 1080 / 720 / 480）与「仅音频 MP3」「同时存封面」
  * 并发队列 + 行级进度 + 停止；结果可再次下载、可复制链接
  * 下载器就是共享的 bin/yt-dlp.exe，随包分发无需 pip
"""
from __future__ import annotations

import os
import queue
import threading
import time

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QProgressBar, QPushButton,
    QPlainTextEdit, QSpinBox, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ...core import backend, paths
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from ..widgets import Badge, Card, HLine, make_table, open_path
from .base import Page

QUALITY = [
    ("最佳（自动）", None),
    ("1080p 以内", "bv*[height<=1080]+ba/b[height<=1080]"),
    ("720p 以内", "bv*[height<=720]+ba/b[height<=720]"),
    ("480p 以内", "bv*[height<=480]+ba/b[height<=480]"),
]


class _DownloadPool(QThread):
    row_state = pyqtSignal(int, str, int)
    finished_all = pyqtSignal(dict)

    def __init__(self, jobs: list[tuple[int, str]], outdir: str, proxy: str,
                 parallel: int, quality: str | None, audio_only: bool,
                 thumb: bool, stop_event: threading.Event):
        super().__init__()
        self._jobs = list(jobs)
        self._out = outdir
        self._proxy = proxy
        self._parallel = max(1, int(parallel or 1))
        self._fmt = quality
        self._audio = audio_only
        self._thumb = thumb
        self._stop = stop_event
        self._lock = threading.Lock()
        self._stat = {"ok": 0, "fail": 0, "stop": 0, "msgs": []}

    def _bump(self, key: str, msg: str = "") -> None:
        with self._lock:
            self._stat[key] += 1
            if msg and len(self._stat["msgs"]) < 6:
                self._stat["msgs"].append(msg)

    def run(self) -> None:  # noqa: D401
        tasks: queue.Queue = queue.Queue()
        for item in self._jobs:
            tasks.put(item)
        threads = [threading.Thread(target=self._worker, args=(tasks,), daemon=True)
                   for _ in range(min(self._parallel, max(1, len(self._jobs))))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        msg = f"完成 {self._stat['ok']} · 失败 {self._stat['fail']}"
        if self._stat["stop"]:
            msg += f" · 中止 {self._stat['stop']}"
        if self._stat["msgs"]:
            msg += " ｜ " + " / ".join(self._stat["msgs"][:2])
        self.finished_all.emit(dict(self._stat, text=msg))

    def _worker(self, tasks: queue.Queue) -> None:
        try:
            svc = backend.ytdlp().service
        except Exception as exc:  # noqa: BLE001
            self.finished_all.emit({"ok": 0, "fail": 0, "stop": 0, "msgs": [],
                                    "text": f"yt-dlp 不可用：{exc}"})
            return
        while not self._stop.is_set():
            try:
                idx, url = tasks.get_nowait()
            except queue.Empty:
                break
            self.row_state.emit(idx, "下载中", 0)

            def _prog(pct=0.0, line="", _i=idx):
                self.row_state.emit(_i, str(line)[:40], int(max(0, min(100, pct))))

            try:
                path = svc.download(url, self._out, proxy=self._proxy or None,
                                    progress=_prog, fmt=self._fmt,
                                    audio_only=self._audio,
                                    write_thumbnail=self._thumb)
                self.row_state.emit(idx, "完成", 100)
                self._bump("ok")
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    self.row_state.emit(idx, "已停止", 0)
                    self._bump("stop")
                    break
                self.row_state.emit(idx, f"失败：{exc}"[:44], 0)
                self._bump("fail", str(exc)[:60])


class YtTiktokPage(Page):
    key = "yttiktok"
    title = "映取 · YouTube / TikTok"
    subtitle = "批量粘贴链接 → 解析元信息 → 选画质 → 并发下载（共享 bin/yt-dlp.exe）"

    def build(self) -> None:
        s = settings()
        self._items: list = []          # [(url, VideoInfo|None, error)]
        self._worker = None
        self._stop_event = threading.Event()

        btn_out = QPushButton("打开下载目录")
        btn_out.setObjectName("Ghost")
        btn_out.clicked.connect(self._open_out)
        self.header.add_action(btn_out)
        self.badge_ver = Badge(f"yt-dlp {self._ver()}")
        self.header.add_action(self.badge_ver)

        # ---------------- 输入
        in_card = Card()
        il = QVBoxLayout(in_card)
        il.setContentsMargins(16, 12, 16, 12)
        il.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("链接（每行一条，可以直接粘整段分享文案）")
        t.setObjectName("H2")
        head.addWidget(t)
        head.addStretch(1)
        self.btn_parse = QPushButton("解析全部")
        self.btn_parse.setObjectName("Primary")
        self.btn_parse.clicked.connect(self.parse_all)
        btn_clear = QPushButton("清空")
        btn_clear.setObjectName("Ghost")
        btn_clear.clicked.connect(self._clear)
        head.addWidget(btn_clear)
        head.addWidget(self.btn_parse)
        il.addLayout(head)
        self.ed_urls = QPlainTextEdit()
        self.ed_urls.setPlaceholderText(
            "https://www.youtube.com/watch?v=…\nhttps://www.tiktok.com/@user/video/…")
        self.ed_urls.setMinimumHeight(88)
        self.ed_urls.setMaximumHeight(130)
        il.addWidget(self.ed_urls)

        opt = QHBoxLayout()
        self.cmb_quality = QComboBox()
        for label, _fmt in QUALITY:
            self.cmb_quality.addItem(label)
        idx = max(0, [q[0] for q in QUALITY].index(s.get("yttiktok.format", "最佳（自动）"))
                  if s.get("yttiktok.format", "最佳（自动）") in [q[0] for q in QUALITY] else 0)
        self.cmb_quality.setCurrentIndex(idx)
        self.chk_audio = QCheckBox("仅音频 MP3")
        self.chk_audio.setChecked(bool(s.get("yttiktok.audio_only", False)))
        self.chk_thumb = QCheckBox("同时存封面")
        self.chk_thumb.setChecked(bool(s.get("yttiktok.write_thumbnail", False)))
        self.sp_parallel = QSpinBox()
        self.sp_parallel.setRange(1, 6)
        self.sp_parallel.setValue(int(s.get("yttiktok.parallel", 2)))
        opt.addWidget(QLabel("画质"))
        opt.addWidget(self.cmb_quality)
        opt.addWidget(self.chk_audio)
        opt.addWidget(self.chk_thumb)
        opt.addStretch(1)
        opt.addWidget(QLabel("并发"))
        opt.addWidget(self.sp_parallel)
        il.addLayout(opt)
        self.body.addWidget(in_card)

        # ---------------- 结果
        res_card = Card()
        rl = QVBoxLayout(res_card)
        rl.setContentsMargins(16, 12, 16, 12)
        rl.setSpacing(8)
        rh = QHBoxLayout()
        t2 = QLabel("解析结果")
        t2.setObjectName("H2")
        self.lbl_parse = QLabel("空闲")
        self.lbl_parse.setObjectName("Muted")
        self.btn_all = QPushButton("全选")
        self.btn_all.setObjectName("Ghost")
        self.btn_all.clicked.connect(lambda: self._check_all(True))
        self.btn_none = QPushButton("清空选择")
        self.btn_none.setObjectName("Ghost")
        self.btn_none.clicked.connect(lambda: self._check_all(False))
        self.btn_download = QPushButton("开始下载")
        self.btn_download.setObjectName("Primary")
        self.btn_download.clicked.connect(self.start_download)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_download)
        rh.addWidget(t2)
        rh.addWidget(self.lbl_parse, 1)
        rh.addWidget(self.btn_all)
        rh.addWidget(self.btn_none)
        rh.addWidget(self.btn_stop)
        rh.addWidget(self.btn_download)
        rl.addLayout(rh)
        rl.addWidget(HLine())

        self.table = make_table(
            ["选择", "标题", "站点", "时长", "上传者", "状态"],
            [52, 400, 90, 80, 160, 120])
        rl.addWidget(self.table, 1)
        self.body.addWidget(res_card, 1)

        # ---------------- 队列
        q_card = Card()
        ql = QVBoxLayout(q_card)
        ql.setContentsMargins(16, 12, 16, 12)
        ql.setSpacing(8)
        qh = QHBoxLayout()
        t3 = QLabel("下载队列")
        t3.setObjectName("H2")
        self.lbl_q = QLabel("空闲")
        self.lbl_q.setObjectName("Muted")
        qh.addWidget(t3)
        qh.addWidget(self.lbl_q, 1)
        ql.addLayout(qh)
        self.table_q = make_table(["视频", "状态", "进度"], [460, 220, 220])
        self.table_q.setMinimumHeight(120)
        ql.addWidget(self.table_q)
        self.body.addWidget(q_card)

    # ---------------------------------------------------------------- 工具
    @staticmethod
    def _ver() -> str:
        try:
            return backend.ytdlp_version()
        except Exception:
            return "不可用"

    def _open_out(self) -> None:
        if not open_path(settings().get("yttiktok.download_dir", "")):
            self.notify("下载目录尚未创建（下载一次后自动建立）", "warn")

    def on_show(self) -> None:
        self.badge_ver.setText(f"yt-dlp {self._ver()}")

    def _urls(self) -> list[str]:
        raw = [x.strip() for x in self.ed_urls.toPlainText().splitlines() if x.strip()]
        out, seen = [], set()
        import re
        for line in raw:
            m = re.search(r"https?://[^\s'\"<>]+", line)
            u = (m.group(0) if m else line).strip().rstrip(".,;)")
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _clear(self) -> None:
        self.ed_urls.clear()
        self._items = []
        self.table.setRowCount(0)
        self.lbl_parse.setText("空闲")

    # ---------------------------------------------------------------- 解析
    def parse_all(self) -> None:
        urls = self._urls()
        if not urls:
            self.notify("请先粘贴链接", "warn")
            return
        self.btn_parse.setEnabled(False)
        self.lbl_parse.setText(f"解析中… 0/{len(urls)}")
        proxy = settings().proxy_url if settings().get("proxy.enabled", True) else ""

        def _run(progress=None):
            svc = backend.ytdlp().service
            out = []
            for i, u in enumerate(urls, start=1):
                if progress:
                    progress(i, len(urls), u[:48])
                try:
                    out.append((u, svc.probe(u, proxy=proxy or None), ""))
                except Exception as exc:  # noqa: BLE001
                    out.append((u, None, f"{type(exc).__name__}: {exc}"[:160]))
            return out

        task = Task(_run, want_progress=True)
        task.progress.connect(
            lambda p, t: self.lbl_parse.setText(f"解析中… {max(0, p)}%  {t[:60]}"))
        task.done.connect(self._parse_done)
        task.failed.connect(lambda tb: (self.btn_parse.setEnabled(True),
                                        self.lbl_parse.setText("解析失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _parse_done(self, items) -> None:
        self.btn_parse.setEnabled(True)
        self._items = list(items or [])
        ok = sum(1 for _u, v, e in self._items if v is not None)
        self.table.setRowCount(0)
        for url, info, err in self._items:
            r = self.table.rowCount()
            self.table.insertRow(r)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if info else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, url)
            self.table.setItem(r, 0, chk)
            self.table.setItem(r, 1, QTableWidgetItem(
                getattr(info, "title", "") if info else url))
            self.table.setItem(r, 2, QTableWidgetItem(
                getattr(info, "site", "") if info else "-"))
            self.table.setItem(r, 3, QTableWidgetItem(
                getattr(info, "duration_text", "") if info else "-"))
            self.table.setItem(r, 4, QTableWidgetItem(
                getattr(info, "uploader", "") if info else "-"))
            self.table.setItem(r, 5, QTableWidgetItem(
                "就绪" if info else f"失败：{err[:60]}"))
        self.lbl_parse.setText(f"解析完成：成功 {ok} / 共 {len(self._items)}")
        self.notify(f"解析完成：{ok} 条可用", "ok" if ok else "warn")

    def _check_all(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item:
                item.setCheckState(state)

    # ---------------------------------------------------------------- 下载
    def start_download(self) -> None:
        if self._worker is not None:
            self.notify("已有下载任务在跑，请先停止", "warn")
            return
        jobs = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                status = self.table.item(r, 5)
                url = item.data(Qt.ItemDataRole.UserRole)
                if url and status and status.text() != "完成":
                    jobs.append((r, url))
        if not jobs:
            self.notify("请先勾选要下载的条目（解析失败的不能下载）", "warn")
            return

        outdir = settings().get("yttiktok.download_dir", "")
        if not outdir:
            self.notify("请先在设置里指定下载目录", "warn")
            return
        os.makedirs(outdir, exist_ok=True)

        s = settings()
        s.set("yttiktok.format", self.cmb_quality.currentText())
        s.set("yttiktok.audio_only", self.chk_audio.isChecked())
        s.set("yttiktok.write_thumbnail", self.chk_thumb.isChecked())
        s.set("yttiktok.parallel", int(self.sp_parallel.value()))
        s.save()

        self.table_q.setRowCount(0)
        for row, _url in jobs:
            r = self.table_q.rowCount()
            self.table_q.insertRow(r)
            self.table_q.setItem(r, 0, QTableWidgetItem(
                self.table.item(row, 1).text() if self.table.item(row, 1) else "-"))
            self.table_q.setItem(r, 1, QTableWidgetItem("等待"))
            bar = QProgressBar()
            bar.setRange(0, 100)
            self.table_q.setCellWidget(r, 2, bar)

        # 队列行号 → 结果表行号（进度同步回写「状态」列）
        self._qmap = {i: row for i, (row, _u) in enumerate(jobs)}
        proxy = settings().proxy_url if settings().get("proxy.enabled", True) else ""
        self._stop_event = threading.Event()
        self._worker = _DownloadPool(
            [(i, u) for i, (_row, u) in enumerate(jobs)],
            outdir, proxy, int(self.sp_parallel.value()),
            QUALITY[self.cmb_quality.currentIndex()][1],
            self.chk_audio.isChecked(), self.chk_thumb.isChecked(),
            self._stop_event)
        self._worker.row_state.connect(self._row_state)
        self._worker.finished_all.connect(self._dl_finished)
        self.btn_stop.setEnabled(True)
        self.btn_download.setEnabled(False)
        self.lbl_q.setText(f"下载中 · {len(jobs)} 条")
        self._worker.start()

    def _row_state(self, idx: int, status: str, pct: int) -> None:
        if idx >= self.table_q.rowCount():
            return
        item = self.table_q.item(idx, 1)
        if item:
            item.setText(status)
        bar = self.table_q.cellWidget(idx, 2)
        if isinstance(bar, QProgressBar):
            bar.setValue(max(0, min(100, pct)))
        # 回写结果表状态
        try:
            row = self._qmap.get(idx)
            if row is not None and self.table.item(row, 5):
                self.table.item(row, 5).setText(status)
        except Exception:
            pass

    def _dl_finished(self, stat: dict) -> None:
        self.btn_stop.setEnabled(False)
        self.btn_download.setEnabled(True)
        text = stat.get("text") or "下载结束"
        self.lbl_q.setText(text)
        self.notify(text, "ok" if not stat.get("fail") else "warn")
        self._worker = None

    def stop_download(self) -> None:
        self._stop_event.set()
        self.btn_stop.setEnabled(False)
        self.lbl_q.setText("正在停止…")
