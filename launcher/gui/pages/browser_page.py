# -*- coding: utf-8 -*-
"""独立浏览器模块（侧栏单独一个 TAB）。

2026-10-29 起，媒体库各模块**不再内嵌** Chromium：每个模块塞一个浏览器实例
既吃内存，又让"媒体库"看起来像浏览器壳。浏览器收敛到这里，作为独立入口：
想逛站点就切到本页，想下载/管理就回各模块页。

功能：
  * 多标签浏览（QtWebEngine 真渲染，能登录、能点播）
  * 站点快捷入口（糖心 / 黄果 / 九色 / 麻豆，地址取地址同步中心的结果）
  * 下载目录可指定（内置浏览器里点下载就落到这），记在设置里
  * 媒体直链嗅探：页面上出现的视频直链会收集起来，可一键复制
"""
from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from ...core.settings import settings
from .. import browser as browser_mod
from ..widgets import Badge, Card, HLine
from .base import Page


class BrowserPage(Page):
    key = "browser"
    title = "浏览器"
    subtitle = "内置 Chromium 真站点浏览：登录、点播、找资源；媒体库各模块不再内嵌浏览器"

    def build(self) -> None:
        s = settings()
        self._panel = None          # 懒加载：切到本页才建渲染视图

        card = Card()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        t = QLabel("站点")
        t.setObjectName("H2")
        row1.addWidget(t)

        self.cmb_site = QComboBox()
        self.cmb_site.setMinimumWidth(240)
        self.cmb_site.setToolTip("从地址同步中心读到的可用地址；选完点「打开」")
        row1.addWidget(self.cmb_site, 1)

        btn_open = QPushButton("打开")
        btn_open.setObjectName("Primary")
        btn_open.clicked.connect(self._open_site)
        row1.addWidget(btn_open)

        btn_go = QPushButton("跳转地址栏")
        btn_go.setObjectName("Ghost")
        btn_go.clicked.connect(self._go_addr)
        row1.addWidget(btn_go)

        self.ed_addr = QLineEdit()
        self.ed_addr.setPlaceholderText("直接输入网址后回车（留空则打开所选站点）")
        self.ed_addr.returnPressed.connect(self._go_addr)
        row1.addWidget(self.ed_addr, 1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.badge_web = Badge("未加载")
        row2.addWidget(self.badge_web)
        lbl_dir = QLabel("下载目录")
        lbl_dir.setObjectName("Muted")
        row2.addWidget(lbl_dir)
        self.ed_dir = QLineEdit()
        self.ed_dir.setText(str(s.get("browser.download_dir", "") or ""))
        self.ed_dir.setPlaceholderText("内置浏览器里下载的文件落到这里（留空＝各模块默认目录）")
        self.ed_dir.setMinimumWidth(280)
        row2.addWidget(self.ed_dir, 1)
        btn_dir = QPushButton("选择…")
        btn_dir.setObjectName("Ghost")
        btn_dir.clicked.connect(self._pick_dir)
        row2.addWidget(btn_dir)
        self.btn_sniff = QPushButton("复制嗅探到的直链")
        self.btn_sniff.setObjectName("Ghost")
        self.btn_sniff.setToolTip("把浏览过程中嗅探到的视频直链复制到剪贴板")
        self.btn_sniff.clicked.connect(self._copy_sniffed)
        row2.addWidget(self.btn_sniff)
        row2.addStretch(1)
        lay.addLayout(row2)

        lay.addWidget(HLine())

        # 浏览器本体占位（真正建视图推迟到 on_show，避免开软件就拉起 Chromium）
        self.host = QWidget()
        hl = QVBoxLayout(self.host)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        self.lbl_hint = QLabel("切换到本页后加载浏览器…")
        self.lbl_hint.setObjectName("Muted")
        self.lbl_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hl.addWidget(self.lbl_hint)
        lay.addWidget(self.host, 1)

        self.body.addWidget(card, 1)
        self._fill_sites()

    # ---------------------------------------------------------------- 站点表
    def _fill_sites(self) -> None:
        """站点下拉：优先用地址同步中心的结果，读不到就用各模块配置的地址。"""
        self.cmb_site.clear()
        items: list[tuple[str, str]] = []
        try:
            from ...core import navsync

            for row in navsync.summary():
                url = str(row.get("url") or "").strip()
                if not url:
                    continue
                label = str(row.get("label") or row.get("key") or url)
                if not row.get("ok"):
                    label += "（未校验通过）"
                items.append((label, url))
        except Exception:                                    # noqa: BLE001
            items = []
        if not items:
            s = settings()
            for label, key in (("糖心 Vlog", "tangxin.route_url"),
                               ("九色视频", "jiuse.active_url"),
                               ("麻豆磁力", "madouqu.base_url")):
                url = str(s.get(key, "") or "").strip()
                if url:
                    items.append((label, url))
            try:
                meta = __import__("launcher.core.backend", fromlist=["x"]).huangguo_site_meta()
                url = str(meta.get("base_url") or "").strip()
                if url:
                    items.append(("黄果短剧", url))
            except Exception:                                # noqa: BLE001
                pass
        for label, url in items:
            self.cmb_site.addItem(f"{label} · {url}", url)
        if not items:
            self.cmb_site.addItem("（地址同步中心暂无结果，请先跑一次同步）", "")

    # ---------------------------------------------------------------- 动作
    def _ensure_panel(self):
        """第一次切到本页才真正建 Chromium（开软件时不拖慢启动）。"""
        if self._panel is not None:
            return self._panel
        if not browser_mod.web_available():
            self.badge_web.setText("QtWebEngine 不可用")
            self.badge_web.set_tone("err")
            self.lbl_hint.setText(
                f"内置浏览器不可用：{browser_mod.web_error() or '缺少 QtWebEngine'}\n"
                "可用系统浏览器打开上面的地址。")
            return None
        home = str(self.cmb_site.currentData() or "").strip()
        panel = browser_mod.BrowserPanel(
            home_url=home,
            download_dir=str(settings().get("browser.download_dir", "") or ""))
        self._panel = panel
        self.lbl_hint.hide()
        self.host.layout().addWidget(panel, 1)
        self.badge_web.setText("已加载")
        self.badge_web.set_tone("ok")
        panel.url_changed.connect(self._on_url_changed)
        return panel

    def _on_url_changed(self, url: str) -> None:
        if url and not self.ed_addr.hasFocus():
            self.ed_addr.setText(url)

    def _open_site(self) -> None:
        url = str(self.cmb_site.currentData() or "").strip()
        if not url:
            self.notify("该站点暂无可用地址，请先到「地址同步」跑一次", "warn")
            return
        panel = self._ensure_panel()
        if panel is None:
            self._open_external(url)
            return
        panel.load(url)

    def _go_addr(self) -> None:
        url = self.ed_addr.text().strip()
        if not url:
            self._open_site()
            return
        if "://" not in url:
            url = "https://" + url
        panel = self._ensure_panel()
        if panel is None:
            self._open_external(url)
            return
        panel.load(url)

    def _open_external(self, url: str) -> None:
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:                                    # noqa: BLE001
            pass

    def _pick_dir(self) -> None:
        cur = self.ed_dir.text().strip() or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "选择内置浏览器的下载目录", cur)
        if not d:
            return
        self.ed_dir.setText(d)
        s = settings()
        s.set("browser.download_dir", d)
        s.save()
        if self._panel is not None:
            self._panel._download_dir = d

    def _copy_sniffed(self) -> None:
        urls = browser_mod.sniffed()
        if not urls:
            self.notify("还没有嗅探到媒体直链", "warn")
            return
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText("\n".join(urls))
        self.notify(f"已复制 {len(urls)} 条直链", "ok")

    # ---------------------------------------------------------------- 生命周期
    def on_show(self) -> None:
        self._fill_sites()
        panel = self._ensure_panel()
        if panel is not None and not getattr(panel, "_loaded", False):
            try:
                panel.ensure_loaded()
            except Exception:                                # noqa: BLE001
                pass

    def shutdown(self) -> None:
        """关闭软件时释放渲染视图（不释放会有残留进程）。"""
        try:
            if self._panel is not None:
                self._panel.release()
        except Exception:                                    # noqa: BLE001
            pass
