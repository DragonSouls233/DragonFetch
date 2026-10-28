# -*- coding: utf-8 -*-
"""内置预览面板：缩略图 + 内嵌视频播放（QtMultimedia）。

需求：选中列表条目 → 显示封面/缩略图；点击「播放」→ 在当前面板内嵌播放。
策略：本地已下载文件优先，其次远程直链；没有 QtMultimedia 时优雅降级为「仅缩略图 + 系统播放器打开」。
"""
from __future__ import annotations

import hashlib
import os
import re

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

try:  # 运行环境可能未启用多媒体模块
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QVideoWidget
    _HAS_MM = True
except Exception:  # pragma: no cover
    _HAS_MM = False
    QMediaPlayer = QAudioOutput = QVideoWidget = None  # type: ignore[assignment]

_VIDEO_EXT = (".mp4", ".mkv", ".webm", ".flv", ".mov", ".avi", ".m4v", ".ts")

# 内嵌预览 HLS 时只截前面这么多秒，避免为了"看一眼"把整片拉下来
_HLS_PREVIEW_SECONDS = 60


def _is_hls(s: str) -> bool:
    """判断是不是 HLS 播放列表（.m3u8）。"""
    if not s:
        return False
    return s.split("?")[0].split("#")[0].lower().endswith(".m3u8")


def _to_url(s: str) -> QUrl:
    if os.path.isfile(s):
        return QUrl.fromLocalFile(os.path.abspath(s))
    return QUrl(s)


def _find_local(directory: str, hint: str) -> str | None:
    """在 directory 里模糊匹配文件名包含 hint 的最新视频文件。"""
    if not directory or not os.path.isdir(directory) or not hint:
        return None
    hint = re.sub(r'[\\/:*?"<>|]', "_", hint).lower()
    best: str | None = None
    try:
        for name in os.listdir(directory):
            if name.lower().endswith(_VIDEO_EXT) and hint in name.lower():
                p = os.path.join(directory, name)
                if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                    best = p
    except OSError:
        return None
    return best


class PreviewPane(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("PreviewPane")
        self._last_src: str | None = None
        self._img_token = 0
        self._reply = None
        self._resolver = None        # 页面注入的直链解析回调
        self._resolve_task = None    # 解析任务引用（防 QThread 被 GC 导致 abort）
        self._hls_task = None        # HLS 转封装任务引用
        self._net = QNetworkAccessManager(self)
        self._net.finished.connect(self._on_image)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        head = QHBoxLayout()
        lbl = QLabel("内置预览")
        lbl.setObjectName("H2")
        head.addWidget(lbl)
        self.lbl_title = QLabel("")
        self.lbl_title.setObjectName("Muted")
        self.lbl_title.setWordWrap(True)
        head.addWidget(self.lbl_title, 1)
        # 「在线播放」：页面传入 resolver 回调，后台解析出直链后就地播放。
        # 没有 resolver（或解析不出）时按钮隐藏，避免点了没反应。
        self.btn_online = QPushButton("在线播放")
        self.btn_online.setObjectName("Ghost")
        self.btn_online.clicked.connect(self._resolve_and_play)
        self.btn_online.setVisible(False)
        head.addWidget(self.btn_online)
        self.btn_open = QPushButton("系统播放器打开")
        self.btn_open.setObjectName("Ghost")
        self.btn_open.clicked.connect(self._open_file)
        head.addWidget(self.btn_open)
        self.btn_toggle = QPushButton("收起")
        self.btn_toggle.setObjectName("Ghost")
        self.btn_toggle.clicked.connect(self._toggle)
        head.addWidget(self.btn_toggle)
        root.addLayout(head)

        # ⚠️ 折叠必须作用于 QWidget 而不是 QLayout：QLayout 没有 isVisible()/
        # setVisible()，对布局调用会抛 AttributeError；而 Qt 会把槽函数里的
        # Python 异常当致命错误直接 abort 整个进程（用户表现为"点一下就闪退"）。
        # 竖版布局：预览在右侧常驻栏里，视频要占主要面积，封面缩到下方一行。
        self._body_widget = QWidget(self)
        self._body = QVBoxLayout(self._body_widget)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(10)
        self.lbl_thumb = QLabel()
        self.lbl_thumb.setObjectName("PreviewThumb")
        self.lbl_thumb.setMinimumHeight(150)
        self.lbl_thumb.setMaximumHeight(210)
        self.lbl_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_thumb.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Preferred)

        if _HAS_MM:
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
            self._video = QVideoWidget(self)
            self._video.setMinimumSize(QSize(320, 200))
            self._player.setVideoOutput(self._video)
            self._player.errorOccurred.connect(self._on_player_error)
            self._player.playbackStateChanged.connect(self._on_state)
            self._player_host = self._video
        else:  # 降级：仅缩略图 + 提示
            self._player = None
            self._video = None
            self._audio = None
            note = QLabel("当前环境未启用 QtMultimedia：仅显示封面，可点「系统播放器打开」用外部播放器观看。")
            note.setObjectName("Muted")
            note.setWordWrap(True)
            self._player_host = note
        # 视频在上（拉伸），封面在下（固定小高度）
        self._body.addWidget(self._player_host, 1)
        self._body.addWidget(self.lbl_thumb, 0)
        root.addWidget(self._body_widget, 1)

    # ----------------------------------------------------------- 公共入口
    def preview(self, title: str = "", thumb: str | None = None,
                video: str | None = None, local_dir: str | None = None,
                local_hint: str | None = None,
                resolver=None) -> None:
        """显示一条内容。

        resolver: 可选 ``() -> str|None``，点击「在线播放」时在后台调用，
        返回可播放的直链（远程 URL 或本地路径）。用于"还没下载也想看"的场景。
        """
        self.lbl_title.setText(title or "（未命名）")
        self.show_thumbnail(thumb)
        self._resolver = resolver if callable(resolver) else None
        self.btn_online.setVisible(self._resolver is not None)
        self.btn_online.setEnabled(True)
        self.btn_online.setText("在线播放")
        src: str | None = None
        if local_dir and local_hint:
            src = _find_local(local_dir, local_hint)
        if not src and video:
            src = video
        self.play(src)

    # ----------------------------------------------------------- 在线解析播放
    def _resolve_and_play(self) -> None:
        if not callable(getattr(self, "_resolver", None)):
            return
        self.btn_online.setEnabled(False)
        self.btn_online.setText("解析中…")
        from ..core.tasks import CENTER, Task

        task = Task(self._resolver)
        task.done.connect(self._on_resolved)
        task.failed.connect(self._on_resolve_failed)
        # ⚠️ 必须保引用：QThread 被 GC 而仍在运行会让 Qt abort 整个进程
        self._resolve_task = task
        CENTER.start(task)

    def _on_resolved(self, url) -> None:
        self.btn_online.setEnabled(True)
        self.btn_online.setText("在线播放")
        self._resolve_task = None
        if not url:
            self.lbl_title.setText(self.lbl_title.text() + " · 解析不到直链")
            return
        self.play(str(url))

    def _on_resolve_failed(self, tb: str) -> None:
        self.btn_online.setEnabled(True)
        self.btn_online.setText("在线播放")
        self._resolve_task = None
        last = (tb or "").strip().splitlines()[-1] if (tb or "").strip() else "未知错误"
        self.lbl_title.setText(self.lbl_title.text() + f" · 解析失败：{last[:80]}")

    def show_thumbnail(self, url_or_path: str | None) -> None:
        self.lbl_thumb.setText("")
        self.lbl_thumb.setPixmap(QPixmap())
        if not url_or_path:
            self.lbl_thumb.setText("无封面")
            return
        if os.path.isfile(url_or_path):
            pm = QPixmap(url_or_path)
            if not pm.isNull():
                self._set_thumb(pm)
                return
            self.lbl_thumb.setText("封面读取失败")
            return
        # 远程：异步拉取（带过期令牌，避免旧请求覆盖新图）
        self._img_token += 1
        req = QNetworkRequest(QUrl(url_or_path))
        reply = self._net.get(req)
        reply.setProperty("tok", self._img_token)
        self._reply = reply

    def _on_image(self, reply: QNetworkReply) -> None:
        if reply.property("tok") != self._img_token:
            return  # 过期请求
        self._reply = None
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self.lbl_thumb.setText("封面加载失败")
            return
        data = reply.readAll()
        pm = QPixmap()
        if pm.loadFromData(data):
            self._set_thumb(pm)
        else:
            self.lbl_thumb.setText("封面无法解析")

    def _set_thumb(self, pm: QPixmap) -> None:
        # 按当前面板宽度自适应（右侧栏宽度可变，固定 216 会留大片空白）
        w = self.lbl_thumb.width()
        if w < 200:
            w = 380
        scaled = pm.scaled(max(200, w - 6), 200,
                           Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self.lbl_thumb.setPixmap(scaled)

    def play(self, url_or_path: str | None) -> None:
        self._last_src = url_or_path
        if not _HAS_MM or self._player is None:
            return
        if not url_or_path:
            self._player.stop()
            return
        # Windows 的 QtMultimedia(WMF) 不支持 HLS(m3u8)，直接 setSource 会静默失败
        # （面板全黑、看起来像"没反应"）。这里先用 ffmpeg 截前 60 秒转封装成本地 mp4
        # 再内嵌播，实测 5.7s / 26MB，足够当"预览"。
        if _is_hls(url_or_path):
            self._prepare_hls(url_or_path)
            return
        self._player.setSource(_to_url(url_or_path))
        self._player.play()

    def _prepare_hls(self, url: str) -> None:
        """把 m3u8 截前 N 秒转成缓存 mp4，然后内嵌播放。"""
        from ..core import paths

        cache = paths.APP_DATA / "preview_cache"
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        key = hashlib.md5(url.split("?")[0].encode("utf-8", "ignore")).hexdigest()[:16]
        out = cache / f"{key}.mp4"
        if out.exists() and out.stat().st_size > 1024:
            self._player.setSource(_to_url(str(out)))
            self._player.play()
            return

        ffmpeg = paths.bin_path("ffmpeg.exe")
        if not os.path.isfile(ffmpeg):
            self.lbl_title.setText(self.lbl_title.text() + " · 缺 ffmpeg，无法预览 HLS")
            return
        self.lbl_title.setText(self.lbl_title.text() + " · 正在准备播放…")

        def _run():
            import subprocess

            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", url,
                 "-t", str(_HLS_PREVIEW_SECONDS), "-c", "copy",
                 "-bsf:a", "aac_adtstoasc", str(out)],
                capture_output=True, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return str(out) if out.exists() and out.stat().st_size > 1024 else ""

        from ..core.tasks import CENTER, Task

        task = Task(_run)
        task.done.connect(self._on_hls_ready)
        task.failed.connect(self._on_hls_failed)
        self._hls_task = task
        CENTER.start(task)

    def _on_hls_ready(self, path: str) -> None:
        self._hls_task = None
        if not path:
            self.lbl_title.setText(self.lbl_title.text() + " · HLS 转封装失败")
            return
        if self._player is not None:
            self._player.setSource(_to_url(path))
            self._player.play()

    def _on_hls_failed(self, tb: str) -> None:
        self._hls_task = None
        last = (tb or "").strip().splitlines()[-1] if (tb or "").strip() else "未知错误"
        self.lbl_title.setText(self.lbl_title.text() + f" · HLS 失败：{last[:60]}")

    def _open_file(self) -> None:
        src = getattr(self, "_last_src", None)
        if not src:
            return
        if os.path.isfile(src):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(src)))
        else:
            QDesktopServices.openUrl(QUrl(src))

    def _toggle(self) -> None:
        was_visible = self._body_widget.isVisible()
        self._body_widget.setVisible(not was_visible)
        self.btn_toggle.setText("展开" if was_visible else "收起")

    # ----------------------------------------------------------- 播放器回调
    def _on_player_error(self, _err, _msg: str = "") -> None:
        if self._player is not None:
            self._player.stop()

    def _on_state(self, _state) -> None:
        pass
