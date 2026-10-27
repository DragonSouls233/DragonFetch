# -*- coding: utf-8 -*-
"""内置预览面板：缩略图 + 内嵌视频播放（QtMultimedia）。

需求：选中列表条目 → 显示封面/缩略图；点击「播放」→ 在当前面板内嵌播放。
策略：本地已下载文件优先，其次远程直链；没有 QtMultimedia 时优雅降级为「仅缩略图 + 系统播放器打开」。
"""
from __future__ import annotations

import os
import re

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QPixmap
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

try:  # 运行环境可能未启用多媒体模块
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QVideoWidget
    _HAS_MM = True
except Exception:  # pragma: no cover
    _HAS_MM = False
    QMediaPlayer = QAudioOutput = QVideoWidget = None  # type: ignore[assignment]

_VIDEO_EXT = (".mp4", ".mkv", ".webm", ".flv", ".mov", ".avi", ".m4v", ".ts")


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
        self.btn_open = QPushButton("系统播放器打开")
        self.btn_open.setObjectName("Ghost")
        self.btn_open.clicked.connect(self._open_file)
        head.addWidget(self.btn_open)
        self.btn_toggle = QPushButton("收起")
        self.btn_toggle.setObjectName("Ghost")
        self.btn_toggle.clicked.connect(self._toggle)
        head.addWidget(self.btn_toggle)
        root.addLayout(head)

        self._body = QHBoxLayout()
        self._body.setSpacing(12)
        self.lbl_thumb = QLabel()
        self.lbl_thumb.setObjectName("PreviewThumb")
        self.lbl_thumb.setFixedWidth(220)
        self.lbl_thumb.setMinimumHeight(280)
        self.lbl_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._body.addWidget(self.lbl_thumb)

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
        self._body.addWidget(self._player_host, 1)
        root.addLayout(self._body, 1)

    # ----------------------------------------------------------- 公共入口
    def preview(self, title: str = "", thumb: str | None = None,
                video: str | None = None, local_dir: str | None = None,
                local_hint: str | None = None) -> None:
        self.lbl_title.setText(title or "（未命名）")
        self.show_thumbnail(thumb)
        src: str | None = None
        if local_dir and local_hint:
            src = _find_local(local_dir, local_hint)
        if not src and video:
            src = video
        self.play(src)

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
        scaled = pm.scaledToWidth(216, Qt.TransformationMode.SmoothTransformation)
        if scaled.height() > 280:
            scaled = pm.scaled(216, 280, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        self.lbl_thumb.setPixmap(scaled)

    def play(self, url_or_path: str | None) -> None:
        self._last_src = url_or_path
        if not _HAS_MM or self._player is None:
            return
        if not url_or_path:
            self._player.stop()
            return
        self._player.setSource(_to_url(url_or_path))
        self._player.play()

    def _open_file(self) -> None:
        src = getattr(self, "_last_src", None)
        if not src:
            return
        if os.path.isfile(src):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(src)))
        else:
            QDesktopServices.openUrl(QUrl(src))

    def _toggle(self) -> None:
        hidden = self._body.isVisible()
        self._body.setVisible(not hidden)
        self.btn_toggle.setText("展开" if hidden else "收起")

    # ----------------------------------------------------------- 播放器回调
    def _on_player_error(self, _err, _msg: str = "") -> None:
        if self._player is not None:
            self._player.stop()

    def _on_state(self, _state) -> None:
        pass
