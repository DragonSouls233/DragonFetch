# -*- coding: utf-8 -*-
"""内置浏览器：QtWebEngine 真站点浏览（多标签 / 下载接管 / 媒体嗅探 / 懒加载）。

为什么要有它：这些站点的播放页、图集、磁力链都必须在**同一会话**里操作
（Cookie、登录态、Referer 全在同一个 Profile 上），一跳系统浏览器就全丢了；
而且用户要的是"每个模块都有一片大面积的浏览区"，不是点一下就跳走。

对外契约（``tests/qa_browser.py`` 是这份契约的实操验收）：
  prepare_webengine()   必须早于 QApplication（AA_ShareOpenGLContexts + Chromium 代理）
  web_available() / web_error()
  is_media_url(url)     .m3u8/.mp4/... 判定
  sniffed(host) / clear_sniffed()   请求级媒体嗅探结果
  BrowserPanel          available / ensure_loaded() / load() / add_tab() /
                        current_view() / release() / external_link 信号
  WebWorkspace          「浏览」+「预览」双标签，mark_preview() 只在预览页打点
  MAX_LIVE_PANELS/_LIVE 同时存活的 WebEngine 面板上限（超了释放最旧的）

WebEngine 没装或初始化失败时不抛异常：``available`` 为 False，页面各自回退到
系统浏览器打开，功能不至于整块消失。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

__all__ = [
    "prepare_webengine", "web_error", "web_available",
    "is_media_url", "sniffed", "clear_sniffed",
    "BrowserPanel", "WebWorkspace", "MAX_LIVE_PANELS",
]

# 同时存活的浏览面板上限：每个面板 = 一个 Chromium 渲染进程（约 80~150MB）。
# 页面之间有多个模块各自持有一个面板，不管上限会把内存吃干净。
MAX_LIVE_PANELS = 4
_LIVE: "list[BrowserPanel]" = []
_LIVE_LOCK = threading.RLock()

_WEB_OK = False
_WEB_ERR = ""
_PROFILE_READY = False
_DL_HOOKED = False
_INTERCEPTOR = None                     # 必须保住 Python 引用，否则 GC 后崩溃

# 默认 UA：不少站点对 QtWebEngine 默认 UA 直接 403（实测 91porny）
_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_MEDIA_EXTS = (".m3u8", ".m3u", ".mpd", ".mp4", ".m4a", ".mp3", ".ts",
               ".webm", ".flv", ".mkv", ".mov", ".aac", ".m4v")

# 外部协议：不交给系统处理，抛给页面（磁力/迅雷/ed2k 归各模块自己消化）
_EXTERNAL_SCHEMES = ("magnet", "thunder", "ed2k", "ftp", "weixin", "alipays")

_ALLOWED_SCHEMES = ("http", "https", "about", "data", "blob", "file", "chrome-error")

_GUIDE_HTML = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#12161f;color:#98a4b8;font-family:'Microsoft YaHei UI','Segoe UI',sans-serif">
<div style="text-align:center;line-height:1.9">
<div style="font-size:15px;color:#e9eef8;margin-bottom:6px">内置浏览器</div>
<div style="font-size:12px">在上方地址栏输入网址回车，或从左侧列表点条目打开</div>
</div></body></html>"""

# 点击钩子：Chromium 会直接丢弃脚本里的 location.href='magnet:…'，
# 走不到导航回调，所以必须在**点击阶段**拦下来再报给 Python。
#
# 回传通道用 document.title 而不是 console.log：实测 QtWebEngine 里
# runJavaScript 的 console 消息不会稳定走到 javaScriptConsoleMessage
# （offscreen/某些构建下直接被吞），而 titleChanged 一定触发。
# 格式：DFEXT#<序号>#<url> / DFEXTWIN#<序号>#<url>（序号保证重复点同一链接也变）。
_CLICK_HOOK_JS = r"""
(function () {
  function report(href, kind) {
    try {
      var seq = (window.__dfExtSeq = (window.__dfExtSeq || 0) + 1);
      document.title = (kind === 'win' ? 'DFEXTWIN' : 'DFEXT') + '#' + seq + '#' + href;
    } catch (e) {}
  }
  // window.open 也接管：无用户手势时 Chromium 会直接拦掉弹窗，
  // 播放页的 target=_blank 就会"点了没反应"。
  try {
    window.open = function (u) { report(String(u || ''), 'win'); return null; };
  } catch (e) {}
  if (window.__dfExtHooked) { return; }
  window.__dfExtHooked = true;
  var RE = /^(magnet|thunder|ed2k|ftp|weixin|alipays):/i;
  function grab(e) {
    var el = e.target;
    while (el && el.tagName !== 'A') { el = el.parentElement; }
    if (!el || !el.href || !RE.test(el.href)) { return; }
    e.preventDefault();
    e.stopPropagation();
    report(el.href, 'link');
  }
  document.addEventListener('click', grab, true);
  document.addEventListener('auxclick', grab, true);
})();
"""


# ---------------------------------------------------------------- 初始化

def _proxy_server() -> str:
    """当前代理地址（Chromium 只认命令行参数，QNetworkProxy 对它无效）。"""
    try:
        from ..core.settings import settings

        s = settings()
        if not s.get("proxy.enabled", True):
            return ""
        return (s.proxy_url or "").strip()
    except Exception:                           # noqa: BLE001
        return ""


def _chromium_proxy(url: str) -> str:
    """把项目里的代理写法转成 Chromium 认的写法。

    ⚠️ Chromium **不认** ``socks5h://``（那是 curl 的约定，表示 DNS 也走代理），
    传进去会让它把代理当成无效地址、所有请求直接失败 —— 表现为整片浏览区
    只显示 ERR_ 错误页。Chromium 的 socks5 本来就是远端解析 DNS，语义一致。
    """
    u = (url or "").strip()
    for bad, good in (("socks5h://", "socks5://"), ("socks4a://", "socks4://")):
        if u.lower().startswith(bad):
            return good + u[len(bad):]
    return u


def prepare_webengine() -> bool:
    """一次性准备 WebEngine，**必须在 QApplication 实例化之前**调用。

    顺序错了的典型症状：右侧浏览区一片黑，或者进程静默退出、没有任何报错。
    """
    global _WEB_OK, _WEB_ERR
    try:
        from PyQt6.QtCore import QCoreApplication

        # ① 共享 GL 上下文：WebEngine 渲染进程要求，晚于 QApplication 会黑屏
        try:
            QCoreApplication.setAttribute(
                Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
        except Exception:                       # noqa: BLE001
            pass

        # ② 代理只认 Chromium 命令行；用户自己写过 flags 就不覆盖
        proxy = _chromium_proxy(_proxy_server())
        if proxy:
            flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
            if "--proxy-server" not in flags:
                os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
                    flags + f" --proxy-server={proxy}").strip()

        # ③ 模块必须在这里 import 完（渲染进程启动前）
        import PyQt6.QtWebEngineCore            # noqa: F401
        import PyQt6.QtWebEngineWidgets         # noqa: F401

        _WEB_OK = True
        _WEB_ERR = ""
        return True
    except Exception as exc:                    # noqa: BLE001
        _WEB_OK = False
        _WEB_ERR = f"{type(exc).__name__}: {exc}"
        return False


def web_error() -> str:
    return _WEB_ERR or "未知原因"


def web_available() -> bool:
    return _WEB_OK


# ---------------------------------------------------------------- 媒体嗅探

_SNIFF: "list[str]" = []
_SNIFF_LOCK = threading.Lock()
_SNIFF_MAX = 300


def is_media_url(url: str) -> bool:
    """是不是媒体直链（m3u8/mp4/...），嗅探与「解析下载」共用同一套判定。"""
    try:
        path = urlparse(url or "").path.lower()
    except Exception:                           # noqa: BLE001
        return False
    return path.endswith(_MEDIA_EXTS)


def sniffed(host: str | None = None) -> "list[str]":
    """最近捕获到的媒体请求（最新在前）；``host`` 给定时按域名过滤。"""
    with _SNIFF_LOCK:
        items = list(_SNIFF)
    if host:
        items = [u for u in items if host in u]
    return items


def clear_sniffed() -> None:
    with _SNIFF_LOCK:
        _SNIFF.clear()


def _remember_sniff(url: str) -> None:
    with _SNIFF_LOCK:
        if url in _SNIFF:
            return
        _SNIFF.insert(0, url)
        del _SNIFF[_SNIFF_MAX:]


def _install_interceptor(profile) -> None:
    """装一个请求级拦截器收集 .m3u8/.mp4（播放地址往往藏在 XHR 里）。"""
    global _INTERCEPTOR
    if _INTERCEPTOR is not None:
        return
    try:
        from PyQt6.QtWebEngineCore import QWebEngineUrlRequestInterceptor

        class _Sniffer(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):       # noqa: N802
                try:
                    _remember_sniff(info.requestUrl().toString())
                except Exception:                   # noqa: BLE001
                    pass

        _INTERCEPTOR = _Sniffer()
        profile.setUrlRequestInterceptor(_INTERCEPTOR)
    except Exception:                           # noqa: BLE001
        _INTERCEPTOR = None


# ---------------------------------------------------------------- Profile

def _active_panel() -> "BrowserPanel | None":
    """最近活跃的面板（全局只有一个 profile，下载要落到它的目录）。"""
    with _LIVE_LOCK:
        for p in reversed(_LIVE):
            if p._loaded:
                return p
    return None


def _global_download(item) -> None:
    panel = _active_panel()
    if panel is None:
        try:
            item.accept()
        except Exception:                       # noqa: BLE001
            pass
        return
    panel._apply_download(item)


def _ensure_profile() -> None:
    """把缓存/Cookie/UA 一次性配好（全局默认 Profile）。"""
    global _PROFILE_READY, _DL_HOOKED
    if not _WEB_OK:
        return
    from PyQt6.QtWebEngineCore import QWebEngineProfile

    prof = QWebEngineProfile.defaultProfile()
    if not _PROFILE_READY:
        try:
            from ..core import paths

            base = paths.data_dir("app", "webengine")
            prof.setPersistentStoragePath(str(base / "storage"))
            prof.setCachePath(str(base / "cache"))
            prof.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        except Exception:                       # noqa: BLE001
            pass
        try:
            prof.setHttpUserAgent(_CHROME_UA)
        except Exception:                       # noqa: BLE001
            pass
        _install_interceptor(prof)
        _PROFILE_READY = True

    if not _DL_HOOKED:
        try:
            prof.downloadRequested.connect(_global_download)
            _DL_HOOKED = True
        except Exception:                       # noqa: BLE001
            pass


def _make_page_class():
    """自定义 Page：外部协议拦截 + target=_blank 落新标签 + DFEXT 控制台回传。"""
    from PyQt6.QtWebEngineCore import QWebEnginePage

    class _Page(QWebEnginePage):
        def __init__(self, owner, parent=None):
            super().__init__(parent)
            self._owner = owner

        def createWindow(self, _type):          # noqa: N802
            try:
                return self._owner._new_page_for_window()
            except Exception:                   # noqa: BLE001
                return None

        def acceptNavigationRequest(self, url, nav_type, is_main):   # noqa: N802
            scheme = (url.scheme() or "").lower()
            if scheme and scheme not in _ALLOWED_SCHEMES:
                try:
                    self._owner.external_link.emit(url.toString())
                except Exception:               # noqa: BLE001
                    pass
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main)

        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            text = message if isinstance(message, str) else str(message)
            if text.startswith("DFEXT:"):
                try:
                    self._owner.external_link.emit(text[6:])
                except Exception:               # noqa: BLE001
                    pass
                return
            super().javaScriptConsoleMessage(level, message, line, source)

    return _Page


# ---------------------------------------------------------------- 浏览面板

class BrowserPanel(QWidget):
    """带地址栏与多标签的浏览区。第一次 ``ensure_loaded`` 才真正建渲染视图。"""

    url_changed = pyqtSignal(str)
    external_link = pyqtSignal(str)             # magnet: / thunder: / ed2k: …

    def __init__(self, home_url: str = "", download_dir: str = "", parent=None):
        super().__init__(parent)
        self._home = (home_url or "").strip()
        self._download_dir = (download_dir or "").strip()
        self._loaded = False
        self._page_cls = None
        self._title_cache: dict[int, str] = {}
        self._build()

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QWidget(self)
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(6, 6, 6, 6)
        bh.setSpacing(6)

        def _btn(text: str, tip: str, slot) -> QPushButton:
            b = QPushButton(text)
            b.setObjectName("Ghost")
            b.setToolTip(tip)
            b.setFixedSize(30, 28)
            b.clicked.connect(slot)
            return b

        self.btn_back = _btn("‹", "后退", self.go_back)
        self.btn_fwd = _btn("›", "前进", self.go_forward)
        self.btn_reload = _btn("⟳", "刷新", self.reload)
        bh.addWidget(self.btn_back)
        bh.addWidget(self.btn_fwd)
        bh.addWidget(self.btn_reload)

        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("输入网址后回车，或直接操作下方网页")
        self.ed_url.returnPressed.connect(self._on_url_entered)
        bh.addWidget(self.ed_url, 1)

        self.btn_new = _btn("+", "新建标签", lambda: self.add_tab(""))
        self.btn_ext = _btn("↗", "用系统浏览器打开", self.open_external)
        bh.addWidget(self.btn_new)
        bh.addWidget(self.btn_ext)
        lay.addWidget(bar)

        self.tabs = QTabWidget(self)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        lay.addWidget(self.tabs, 1)

        self.hint = QLabel("", self)
        self.hint.setObjectName("Muted")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        if _WEB_OK:
            self.hint.hide()
        else:
            self.hint.setText(
                "内置浏览器不可用（" + web_error() + "）\n"
                "页面里的「浏览」按钮会改用系统默认浏览器打开。")
            lay.addWidget(self.hint, 1)

    # ---------------------------------------------------------------- 能力

    @property
    def available(self) -> bool:
        return _WEB_OK

    def ensure_loaded(self) -> None:
        """懒加载：只有真正切到这一页才建第一个 WebEngineView。"""
        if self._loaded or not _WEB_OK:
            return
        with _LIVE_LOCK:
            if self in _LIVE:
                _LIVE.remove(self)
            _LIVE.append(self)
            # 超上限释放最旧的（release 会用同一把 RLock，可重入）
            while len(_LIVE) > MAX_LIVE_PANELS:
                oldest = _LIVE.pop(0)
                try:
                    oldest.release()
                except Exception:               # noqa: BLE001
                    pass
        self._loaded = True
        _ensure_profile()
        self._new_view(self._home)

    def release(self) -> None:
        """释放渲染视图（面板留着，下次 ensure_loaded 再建）。"""
        try:
            while self.tabs.count():
                w = self.tabs.widget(0)
                self.tabs.removeTab(0)
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
        except Exception:                       # noqa: BLE001
            pass
        self._loaded = False
        with _LIVE_LOCK:
            if self in _LIVE:
                _LIVE.remove(self)

    def current_view(self):
        idx = self.tabs.currentIndex()
        if idx < 0:
            return None
        return self.tabs.widget(idx)

    def load(self, url: str) -> None:
        """在当前标签打开（空 URL = 首页 / 引导页）。"""
        if not _WEB_OK:
            self._open_external(url or self._home)
            return
        url = (url or self._home or "").strip()
        self.ensure_loaded()
        view = self.current_view()
        if view is None:
            self._new_view(url)
        elif url:
            view.setUrl(QUrl(url))
        self.tabs.setCurrentIndex(0)

    def add_tab(self, url: str = "") -> None:
        if not _WEB_OK:
            if url:
                self._open_external(url)
            return
        self.ensure_loaded()
        self._new_view((url or self._home or "").strip())
        self.tabs.setCurrentIndex(self.tabs.count() - 1)

    def go_back(self) -> None:
        v = self.current_view()
        if v is not None:
            v.back()

    def go_forward(self) -> None:
        v = self.current_view()
        if v is not None:
            v.forward()

    def reload(self) -> None:
        v = self.current_view()
        if v is not None:
            v.reload()

    def open_external(self) -> None:
        self._open_external(self.current_url() or self._home)

    def current_url(self) -> str:
        v = self.current_view()
        return v.url().toString() if v is not None else ""

    # ---------------------------------------------------------------- 内部

    def _open_external(self, url: str) -> None:
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _on_url_entered(self) -> None:
        text = (self.ed_url.text() or "").strip()
        if not text:
            return
        if "://" not in text:
            text = "https://" + text
        self.load(text)

    def _new_page_for_window(self):
        """window.open / target=_blank：在面板里再开一个标签。"""
        self.add_tab("")
        view = self.current_view()
        return view.page() if view is not None else None

    def _new_view(self, url: str):
        from PyQt6.QtWebEngineCore import QWebEngineSettings
        from PyQt6.QtWebEngineWidgets import QWebEngineView

        if self._page_cls is None:
            self._page_cls = _make_page_class()

        view = QWebEngineView(self)
        try:
            view.setPage(self._page_cls(view, view))
            st = view.settings()
            # window.open / target=_blank 默认被拦（没有用户手势时），要显式放开，
            # 否则播放页的弹窗式地址会直接丢失、表现为"点了没反应"。
            st.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
            st.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
            st.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
            st.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
        except Exception:                       # noqa: BLE001
            pass
        view.urlChanged.connect(lambda u, v=view: self._on_view_url(v, u))
        view.titleChanged.connect(lambda t, v=view: self._on_view_title(v, t))
        view.loadFinished.connect(lambda okk, v=view: self._on_load_finished(v, okk))

        idx = self.tabs.addTab(view, "新标签")
        if url:
            view.setUrl(QUrl(url))
        else:
            # 空地址 → 引导页（不联网，也不留 about:blank 白屏）
            view.setHtml(_GUIDE_HTML, QUrl("about:blank"))
        self.tabs.setCurrentIndex(idx)
        return view

    def _on_load_finished(self, view, okk: bool) -> None:
        # 加载失败（错误页）也要注入：错误页上用户照样可能点到磁力链接，
        # 而且站点偶发 5xx 时页面内容其实已经渲染出来了。
        try:
            view.page().runJavaScript(_CLICK_HOOK_JS)
        except Exception:                       # noqa: BLE001
            pass

    def _close_tab(self, idx: int) -> None:
        if self.tabs.count() <= 1:
            view = self.tabs.widget(0)
            if view is not None:
                view.setHtml(_GUIDE_HTML, QUrl("about:blank"))
            return
        w = self.tabs.widget(idx)
        self.tabs.removeTab(idx)
        if w is not None:
            w.deleteLater()

    def _on_tab_changed(self, idx: int) -> None:
        view = self.tabs.widget(idx) if idx >= 0 else None
        if view is not None:
            self.ed_url.setText(view.url().toString())

    def _on_view_url(self, view, url: QUrl) -> None:
        if view is self.current_view():
            self.ed_url.setText(url.toString())
            self.url_changed.emit(url.toString())

    def _on_view_title(self, view, title: str) -> None:
        """标题有两用：正常显示标签名 + 作为「外部链接/弹窗」的回传通道。"""
        t = title or ""
        if t.startswith(("DFEXT#", "DFEXTWIN#")):
            parts = t.split("#", 2)
            tag = parts[0]
            payload = parts[2] if len(parts) > 2 else ""
            if tag == "DFEXTWIN":
                if payload:
                    self.add_tab(payload)
            elif payload:
                self.external_link.emit(payload)
            # 复原标签名，别把 payload 停在标签上
            keep = self._title_cache.get(id(view), "")
            idx = self.tabs.indexOf(view)
            if idx >= 0:
                self.tabs.setTabText(idx, (keep or "新标签")[:18])
            try:
                import json

                view.page().runJavaScript(f"document.title = {json.dumps(keep)};")
            except Exception:                   # noqa: BLE001
                pass
            return

        self._title_cache[id(view)] = t
        idx = self.tabs.indexOf(view)
        if idx >= 0:
            short = t.strip() or "新标签"
            self.tabs.setTabText(idx, short[:18] + ("…" if len(short) > 18 else ""))

    def _apply_download(self, item) -> None:
        """浏览器里点下载时，落到本模块的下载目录（不是系统下载夹）。"""
        try:
            target = Path(self._download_dir) if self._download_dir else None
            if target is None:
                from ..core import paths

                target = paths.data_dir("app", "downloads")
            target.mkdir(parents=True, exist_ok=True)
            item.setDownloadDirectory(str(target))
            item.setDownloadFileName(item.downloadFileName() or "download")
            item.accept()
        except Exception:                       # noqa: BLE001
            try:
                item.accept()
            except Exception:                   # noqa: BLE001
                pass


class WebWorkspace(QTabWidget):
    """「浏览」+「预览」两页共用右侧空间（大面积给浏览，播放按需切换）。"""

    def __init__(self, preview: QWidget | None = None, home_url: str = "",
                 download_dir: str = "", parent=None):
        super().__init__(parent)
        self.setDocumentMode(True)
        self.browser = BrowserPanel(home_url=home_url, download_dir=download_dir, parent=self)
        self.addTab(self.browser, "浏览")
        self.preview = preview
        if preview is not None:
            self.addTab(preview, "预览")
        self.setCurrentIndex(0)

    def mark_preview(self) -> None:
        """有人在列表里选中了条目：只在「预览」标签上打个点，**不抢焦点**。

        用户正在浏览网页时被强行切走会打断操作，所以只提示「这里有新东西」。
        """
        if self.preview is None:
            return
        idx = self.indexOf(self.preview)
        if idx < 0:
            return
        text = self.tabText(idx).replace("●", "").strip() or "预览"
        self.setTabText(idx, "● " + text)
