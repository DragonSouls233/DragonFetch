# -*- coding: utf-8 -*-
"""导航页 · 黄视频：先代理读 daohang.pro 入口表，再用解析出的地址直连。

需求对应：
  * 「糖心 / 黄果 先使用代理读取导航页，然后再不用代理正常使用」——本页负责
    「读导航」，并把解析结果写给糖心线路；黄果的发布页刷新在其页面内。
  * 「导航页的黄视频也要做进来」——黄视频入口在这里可直接解析下载。
"""
from __future__ import annotations

import datetime as _dt
import os

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from ...core import backend, nav, paths
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from ..widgets import Badge, Card, HLine, make_table, open_path, cell
from .base import Page


class NavPage(Page):
    key = "nav"
    title = "导航页 · 黄视频"
    subtitle = "daohang.pro 的链接表需要代理读取；解析出的站点默认直连使用"

    # ---------------------------------------------------------------- 内置浏览器
    def home_url(self) -> str:
        """导航页模块没有固定站点，浏览区留空给用户自己输网址。"""
        return ""

    def browse_download_dir(self) -> str:
        return settings().get("yttiktok.download_dir", "") or ""

    def build(self) -> None:
        # ---------------- 顶部：刷新 + 状态
        top = Card()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(18, 16, 18, 16)
        tl.setSpacing(10)
        row = QHBoxLayout()
        self.lbl_state = QLabel("尚未读取导航页")
        self.lbl_state.setObjectName("Sub")
        self.badge = Badge("未读取")
        btn_refresh = QPushButton("代理刷新导航")
        btn_refresh.setObjectName("Primary")
        btn_refresh.clicked.connect(self.refresh)
        row.addWidget(self.lbl_state, 1)
        row.addWidget(self.badge)
        row.addWidget(btn_refresh)
        tl.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("导航页地址"))
        self.ed_nav_url = QLineEdit(settings().get("nav.url", nav.DEFAULT_URL))
        row2.addWidget(self.ed_nav_url, 1)
        self.chk_proxy = QCheckBox("通过代理读取")
        self.chk_proxy.setChecked(bool(settings().get("proxy.enabled", True)))
        row2.addWidget(self.chk_proxy)
        tl.addLayout(row2)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(10)
        lv.addWidget(top)

        # ---------------- 入口表
        table_card = Card()
        cl = QVBoxLayout(table_card)
        cl.setContentsMargins(18, 16, 18, 16)
        cl.setSpacing(10)
        head = QHBoxLayout()
        title = QLabel("导航入口")
        title.setObjectName("H2")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_open = QPushButton("打开")
        self.btn_open.setObjectName("Ghost")
        self.btn_open.clicked.connect(self._open_selected)
        self.btn_route = QPushButton("设为糖心线路")
        self.btn_route.setObjectName("Ghost")
        self.btn_route.clicked.connect(self._apply_route)
        head.addWidget(self.btn_open)
        head.addWidget(self.btn_route)
        cl.addLayout(head)
        cl.addWidget(HLine())

        self.table = make_table(["名称", "说明", "地址", "线路"], [150, 240, 300, 200])
        self.table.doubleClicked.connect(lambda _i: self._open_selected())
        cl.addWidget(self.table, 1)
        lv.addWidget(table_card, 2)

        # ---------------- 黄视频
        hv = Card()
        hl = QVBoxLayout(hv)
        hl.setContentsMargins(18, 16, 18, 16)
        hl.setSpacing(10)
        h1 = QHBoxLayout()
        t2 = QLabel("黄视频")
        t2.setObjectName("H2")
        self.lbl_hv = QLabel("入口地址来自导航页（huang.video）")
        self.lbl_hv.setObjectName("Muted")
        h1.addWidget(t2)
        h1.addWidget(self.lbl_hv, 1)
        hl.addLayout(h1)
        hl.addWidget(HLine())

        r1 = QHBoxLayout()
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("粘贴黄视频的具体视频链接；也可直接点「打开站点」浏览")
        r1.addWidget(self.ed_url, 1)
        btn_site = QPushButton("打开站点")
        btn_site.setObjectName("Ghost")
        btn_site.clicked.connect(self._open_huangvideo)
        r1.addWidget(btn_site)
        hl.addLayout(r1)

        r2 = QHBoxLayout()
        self.ed_out = QLineEdit(settings().get("yttiktok.download_dir", ""))
        btn_dir = QPushButton("下载目录")
        btn_dir.setObjectName("Ghost")
        btn_dir.clicked.connect(self._pick_dir)
        self.btn_parse = QPushButton("解析")
        self.btn_parse.clicked.connect(self._parse_hv)
        self.btn_dl = QPushButton("下载")
        self.btn_dl.setObjectName("Primary")
        self.btn_dl.clicked.connect(self._download_hv)
        r2.addWidget(self.ed_out, 1)
        r2.addWidget(btn_dir)
        r2.addWidget(self.btn_parse)
        r2.addWidget(self.btn_dl)
        hl.addLayout(r2)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        hl.addWidget(self.bar)
        self.lbl_hv_status = QLabel("空闲")
        self.lbl_hv_status.setObjectName("Muted")
        hl.addWidget(self.lbl_hv_status)
        lv.addWidget(hv)

        # 右侧：导航页里的入口点开就能在应用内逛，不用跳系统浏览器
        from ..preview import PreviewPane
        self.preview = PreviewPane()
        self.preview.setMinimumHeight(320)
        self.attach_preview(left, self.preview)

        self._entries = []
        self._fill(nav.cached_entries(), nav.cached_payload())

    # -------------------------------------------------------------- 导航
    def on_show(self) -> None:
        if not self._entries:
            self._fill(nav.cached_entries(), nav.cached_payload())

    def refresh(self) -> None:
        url = self.ed_nav_url.text().strip()
        use_proxy = self.chk_proxy.isChecked()
        s = settings()
        s.set("nav.url", url or nav.DEFAULT_URL)
        s.save()
        self.lbl_state.setText("读取中（代理…）")
        task = Task(nav.refresh, use_proxy=use_proxy, url=url, want_progress=True)
        task.done.connect(self._refresh_done)
        task.failed.connect(lambda tb: (self.lbl_state.setText("读取失败"),
                                        self.badge.set_tone("err"),
                                        self.badge.setText("失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _refresh_done(self, result: dict) -> None:
        entries = result.get("entries") or []
        if result.get("ok"):
            self.badge.set_tone("ok")
            self.badge.setText("已更新")
            self.notify(f"导航页已更新：{len(entries)} 条", "ok")
        else:
            self.badge.set_tone("warn")
            self.badge.setText("使用缓存" if entries else "失败")
            self.notify("导航页读取失败：" + (result.get("error") or "")[:140], "warn")
        self._fill(entries, result)

    def _fill(self, entries, payload: dict) -> None:
        self._entries = list(entries or [])
        ts = (payload or {}).get("at") or 0
        when = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "未知"
        via = (payload or {}).get("via") or "-"
        self.lbl_state.setText(f"{len(self._entries)} 条入口 · 更新于 {when} · 通道 {via}")
        if self._entries and self.badge.text() in ("未读取", ""):
            self.badge.set_tone("warn")
            self.badge.setText("缓存")
        self.table.setRowCount(0)
        for e in self._entries:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, cell(e.get("name", ""), e))
            self.table.setItem(r, 1, cell(e.get("desc", "")))
            self.table.setItem(r, 2, cell(e.get("base_url") or e.get("url", "")))
            line = e.get("science_base_url", "")
            self.table.setItem(r, 3, cell(line or "—"))
        hv = [e for e in self._entries if e.get("kind") == "huangvideo"]
        if hv:
            site = hv[0].get("base_url") or hv[0].get("url")
            self.lbl_hv.setText(f"入口：{site}")
            if not self.ed_url.text().strip():
                self.ed_url.setPlaceholderText(f"粘贴 {site} 下的具体视频链接")

    def _selected_entry(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._entries):
            self.notify("请先选中一条入口", "warn")
            return None
        return self._entries[row]

    def open_in_browser(self, url: str) -> None:
        """在右侧「浏览」标签里打开；没有 WebEngine 才退回系统浏览器。"""
        url = (url or "").strip()
        if not url:
            return
        ws = getattr(self, "workspace", None)
        bp = getattr(self, "browser_panel", None)
        if bp is not None and bp.available:
            if ws is not None:
                ws.setCurrentIndex(0)
            bp.load(url)
            return
        QDesktopServices.openUrl(QUrl(url))

    def _open_selected(self) -> None:
        e = self._selected_entry()
        if e:
            self.open_in_browser(e.get("url") or e.get("base_url") or "")

    def _apply_route(self) -> None:
        e = self._selected_entry()
        if not e:
            return
        url = e.get("base_url") or e.get("url")
        s = settings()
        s.set("tangxin.route_url", url)
        s.set("tangxin.cdn", "")
        s.set("tangxin.route_label", e.get("name", ""))
        s.save()
        try:
            backend.apply_tangxin_settings()
            self.notify(f"糖心线路已设为 {url}（直连）", "ok")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"已保存，但糖心后端尚未就绪：{exc}", "warn")

    # -------------------------------------------------------------- 黄视频
    def _open_huangvideo(self) -> None:
        hv = [e for e in self._entries if e.get("kind") == "huangvideo"]
        url = (hv[0].get("url") if hv else "") or "https://huang.video/"
        self.open_in_browser(url)

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择下载目录",
                                             self.ed_out.text() or str(paths.DATA))
        if d:
            self.ed_out.setText(d)

    def _proxy_arg(self):
        s = settings()
        return s.proxy_url if (self.chk_proxy.isChecked() and s.proxy_url) else None

    def _parse_hv(self) -> None:
        url = self.ed_url.text().strip()
        if not url:
            self.notify("请粘贴黄视频链接", "warn")
            return
        self.btn_parse.setEnabled(False)
        self.lbl_hv_status.setText("解析中…")
        proxy = self._proxy_arg()

        def _run():
            ns = backend.ytdlp()
            return ns.service.probe(url, proxy=proxy)

        task = Task(_run)
        task.done.connect(lambda info: (
            self.btn_parse.setEnabled(True),
            self.lbl_hv_status.setText(f"{info.title or '(无标题)'} · {info.duration_text}"),
            self.notify("解析成功", "ok")))
        task.failed.connect(lambda tb: (self.btn_parse.setEnabled(True),
                                        self.lbl_hv_status.setText("解析失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)

    def _download_hv(self) -> None:
        url = self.ed_url.text().strip()
        out = self.ed_out.text().strip()
        if not url or not out:
            self.notify("请填写链接与下载目录", "warn")
            return
        os.makedirs(out, exist_ok=True)
        settings().set("yttiktok.download_dir", out)
        settings().save()
        self.btn_dl.setEnabled(False)
        self.bar.setValue(0)
        proxy = self._proxy_arg()

        def _run(progress=None):
            ns = backend.ytdlp()
            return ns.service.download(url, out, proxy=proxy, progress=progress)

        task = Task(_run, want_progress=True)
        task.progress.connect(lambda pct, text: (
            self.bar.setValue(max(0, pct)) if pct >= 0 else None,
            self.lbl_hv_status.setText(text[:110]) if text else None))
        task.done.connect(lambda path: (self.btn_dl.setEnabled(True),
                                        self.bar.setValue(100),
                                        self.lbl_hv_status.setText(
                                            f"完成：{paths.rel(path)}" if path else "完成"),
                                        self.notify("下载完成" if path else "下载结束", "ok")))
        task.failed.connect(lambda tb: (self.btn_dl.setEnabled(True),
                                        self.lbl_hv_status.setText("下载失败"),
                                        self.notify(tb.splitlines()[-1][:160], "err")))
        CENTER.start(task)
