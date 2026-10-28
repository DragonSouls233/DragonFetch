# -*- coding: utf-8 -*-
"""内置预览面板：缩略图 + 内嵌视频播放（QtMultimedia）。

需求：选中列表条目 → 显示封面/缩略图；点击「播放」→ 在当前面板内嵌播放。
策略：本地已下载文件优先，其次远程直链；没有 QtMultimedia 时优雅降级为「仅缩略图 + 系统播放器打开」。
"""
from __future__ import annotations

import hashlib
import os
import re

from PyQt6.QtCore import QSize, Qt, QTimer, QUrl
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

# 内嵌预览 HLS 时只截前面这么多秒：既要在 8 秒内出画面，又要够看几眼。
# 实测（2026-10-28，本机 ~1.1MB/s）：-t 10 ≈ 4.1s、-t 20 ≈ 5.6s、-t 60 ≈ 8s+。
# 所以取 12 秒 —— 加上解析（九色快路 2.6s）总时长仍能压到 8 秒内。
_HLS_PREVIEW_SECONDS = 12


def _is_hls(s: str) -> bool:
    """判断是不是 HLS 播放列表（.m3u8）。"""
    if not s:
        return False
    return s.split("?")[0].split("#")[0].lower().endswith(".m3u8")


def _to_url(s: str) -> QUrl:
    if os.path.isfile(s):
        return QUrl.fromLocalFile(os.path.abspath(s))
    return QUrl(s)


_MAX_SCAN = 6000          # 兜底：超大 NAS 目录扫到这个数就停，避免卡界面

# 封面/CDN 请求用的 UA：不少图床对空 UA 直接 403
_UA = (b"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       b"(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _find_local(directory: str, hint: str, actor: str = "") -> str | None:
    """在 directory 里模糊匹配文件名包含 hint 的最新视频文件。

    ⚠️ 为什么不能只在根目录 ``listdir``：糖心的落盘布局是
    ``{root}/{演员}/{标题}/{标题}.mp4``（三层），早期版本还有两层
    （``{root}/{演员}/{标题}.mp4``）。只在根目录找 ⇒ 文件其实早就下好了，
    预览面板却一直显示"无本地文件"，用户以为功能没移植过来。
    这里优先扫演员子目录（快且能消歧义），再浅扫全目录，深度封顶 3 层。
    """
    if not directory or not os.path.isdir(directory) or not hint:
        return None
    key = re.sub(r'[\\/:*?"<>|]', "_", hint).lower()
    best: str | None = None
    seen = [0]

    def _mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    def _scan(root: str, depth: int) -> None:
        nonlocal best
        if best is not None or depth > 3 or seen[0] > _MAX_SCAN:
            return
        try:
            names = os.listdir(root)
        except OSError:
            return
        for name in names:
            if seen[0] > _MAX_SCAN:
                return
            seen[0] += 1
            p = os.path.join(root, name)
            if os.path.isdir(p):
                _scan(p, depth + 1)
                continue
            if name.lower().endswith(_VIDEO_EXT) and key in name.lower():
                if best is None or _mtime(p) > _mtime(best):
                    best = p

    # 1) 演员子目录优先：同名标题在不同演员下很常见，先定位能少扫一大片
    if actor:
        safe_actor = re.sub(r'[\\/:*?"<>|]', "_", (actor or "").strip())
        sub = os.path.join(directory, safe_actor)
        if os.path.isdir(sub):
            _scan(sub, 0)
            if best:
                return best
    # 2) 兜底：整个输出目录浅扫（最多 3 层）
    _scan(directory, 0)
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
        self._hls_src = None         # 正在尝试"原生播放"的 m3u8
        # 预解析：选中条目就后台把直链解析出来，用户点「在线播放」时几乎零等待
        self._pf_token = 0           # 当前预览条目的令牌
        self._pf_done: tuple[int, str] | None = None   # (令牌, 直链)
        self._pf_task = None
        self._pf_token_running = 0   # 正在预解析的令牌（0 = 没有在跑）
        self._pf_click_pending = False   # 用户在解析期间就点了「在线播放」
        self._warmup_task = None
        self._referer = ""       # 封面请求的 Referer（防盗链）
        # 原生 HLS 起播看门狗：超时还没出画面就退回 ffmpeg 转封装
        self._hls_watch = QTimer(self)
        self._hls_watch.setSingleShot(True)
        self._hls_watch.timeout.connect(self._hls_fallback)
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
    def set_referer(self, url: str) -> None:
        """设置远程封面请求带的 Referer（站点防盗链用）。

        糖心/多站点的封面 CDN 会校验来源，不带 Referer 一律 403 —— 表现就是
        「封面一张都出不来」。页面在 on_show 时把站点首页设进来即可。
        """
        self._referer = (url or "").strip()

    def preview(self, title: str = "", thumb: str | None = None,
                video: str | None = None, local_dir: str | None = None,
                local_hint: str | None = None, local_actor: str = "",
                resolver=None, prefetch: bool = False) -> None:
        """显示一条内容。

        resolver: 可选 ``() -> str|None``，点击「在线播放」时在后台调用，
        返回可播放的直链（远程 URL 或本地路径）。用于"还没下载也想看"的场景。

        prefetch: 选中条目就先在后台解析一次直链（点播放时几乎零等待）。
        只有**解析便宜**的页面才该开（九色一次 GET 2 秒）；像多站点那种要跑
        yt-dlp 的，乱预解析等于每次划过列表都发一次 8 秒请求，反而拖慢。
        """
        self.lbl_title.setText(title or "（未命名）")
        self.show_thumbnail(thumb)
        self._resolver = resolver if callable(resolver) else None
        self.btn_online.setVisible(self._resolver is not None)
        self.btn_online.setEnabled(True)
        self.btn_online.setText("在线播放")
        # 换条目 → 令牌自增，旧的预解析结果作废
        self._pf_token += 1
        self._pf_done = None
        self._pf_click_pending = False
        if self._resolver is not None and prefetch:
            # 选中就预解析：把"解析"那 2~3 秒提前花掉，点播放时只剩起播。
            # 延迟 400ms 是为了避开"快速划过列表"时的空跑。
            QTimer.singleShot(400, self._prefetch)
        src: str | None = None
        if local_dir and local_hint:
            src = _find_local(local_dir, local_hint, local_actor or "")
        if not src and video:
            src = video
        self.play(src)

    # ----------------------------------------------------------- 预解析
    def _prefetch(self) -> None:
        """后台先把直链解析好（失败静默，不影响用户点按钮）。"""
        resolver = getattr(self, "_resolver", None)
        if not callable(resolver):
            return
        token = self._pf_token

        def _run():
            return resolver()

        from ..core.tasks import CENTER, Task

        task = Task(_run)
        task.done.connect(lambda url, t=token: self._on_prefetched(t, url))
        task.failed.connect(lambda tb, t=token: self._pf_failed(t))
        self._pf_task = task
        self._pf_token_running = token
        CENTER.start(task)

    def _on_prefetched(self, token: int, url) -> None:
        self._pf_task = None
        self._pf_token_running = 0
        if token != self._pf_token or not url:
            return
        self._pf_done = (token, str(url))
        # 解析出来之后再预热（顺序很重要）：先把直链交给「在线播放」按钮，
        # 用户点下去就能起播；预热在后台继续跑，只是为了把 DNS/TLS 提前建好。
        if _is_hls(self._pf_done[1]):
            from ..core import backend as _bk
            from ..core.tasks import CENTER, Task as _Task

            try:
                t = _Task(lambda u=str(url): _bk.warmup_hls(u))
                self._warmup_task = t
                CENTER.start(t)
            except Exception:  # noqa: BLE001
                self._warmup_task = None
        # 用户在解析期间就点了「在线播放」→ 现在直接起播
        if self._pf_click_pending:
            self._pf_click_pending = False
            self.btn_online.setEnabled(True)
            self.btn_online.setText("在线播放")
            self.play(str(url))

    def _pf_failed(self, token: int) -> None:
        self._pf_task = None
        self._pf_token_running = 0
        if self._pf_click_pending and token == self._pf_token:
            # 预解析失败 → 退回"点按钮再解析一次"的常规路径
            self._pf_click_pending = False
            self._resolve_and_play()

    # ----------------------------------------------------------- 在线解析播放
    def _resolve_and_play(self) -> None:
        if not callable(getattr(self, "_resolver", None)):
            return
        # 预解析已经拿到直链 → 直接起播（用户感知就是"点了就有"）
        pf = getattr(self, "_pf_done", None)
        if pf and pf[0] == self._pf_token and pf[1]:
            self.btn_online.setText("在线播放")
            self.play(pf[1])
            return
        # 预解析还在跑 → 等它的结果，别再发一次重复请求（否则解析两遍，
        # 首帧时间直接翻倍，实测会从 4.8s 掉到 8.5s）
        if self._pf_task is not None and self._pf_token_running == self._pf_token:
            self._pf_click_pending = True
            self.btn_online.setEnabled(False)
            self.btn_online.setText("解析中…")
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
        # 封面 CDN 普遍按 Referer/UA 防盗链：不带 Referer 直接 403，
        # 表现就是「封面一个都不显示」。这里带上站点来源和完整 UA。
        if getattr(self, "_referer", ""):
            req.setRawHeader(b"Referer", str(self._referer).encode("utf-8"))
        req.setRawHeader(b"User-Agent", _UA)
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
        self._hls_watch.stop()
        self._hls_src = None
        if not _HAS_MM or self._player is None:
            return
        if not url_or_path:
            self._player.stop()
            return
        # HLS(m3u8) 曾经在 Windows 上播不了（QtMultimedia 走 WMF 时确像"黑屏没反应"），
        # 于是老做法是先用 ffmpeg 截 60 秒转封装成 mp4 再播 —— 代价是点一下要等 6~9 秒。
        # 实测 2026-10-28：本机 Qt 6.11 的 QtMultimedia **已经用 FFmpeg 后端**
        # （播放时能直接看到 ffmpeg 的 hls 日志），m3u8 原生起播 **3.5 秒**就出画面，
        # 比转封装快一倍多。所以改成：**先原生播**，看门狗超时 / 播放器报错
        # 才退回转封装 —— 快路径优先，慢路径兜底。
        if _is_hls(url_or_path):
            self._play_hls(url_or_path)
            return
        self._player.setSource(_to_url(url_or_path))
        self._player.play()

    def _play_hls(self, url: str) -> None:
        """先试原生 HLS；6 秒内没出画面就转封装兜底。"""
        self._hls_src = url
        self._player.setSource(_to_url(url))
        self._player.play()
        self._hls_watch.start(6000)

    def _hls_fallback(self) -> None:
        """原生播放超时（老环境/解码器不支持）→ 走 ffmpeg 截一小段转封装。"""
        src = self._hls_src
        self._hls_src = None
        if not src or self._player is None:
            return
        if self._player.position() > 0:
            return
        self._prepare_hls(src)

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
        # 原生 HLS 报错 → 立刻走转封装兜底，别让面板停在黑屏
        if self._hls_src:
            src, self._hls_src = self._hls_src, None
            self._hls_watch.stop()
            self._prepare_hls(src)
            return
        if self._player is not None:
            self._player.stop()

    def _on_state(self, _state) -> None:
        pass
