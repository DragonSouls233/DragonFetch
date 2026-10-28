# -*- coding: utf-8 -*-
"""页面基类：统一 24px 留白、页头、通知出口。"""
from __future__ import annotations

from PyQt6.QtWidgets import QVBoxLayout, QWidget

from ..widgets import PageHeader


class Page(QWidget):
    key: str = ""
    title: str = ""
    subtitle: str = ""

    def __init__(self):
        super().__init__()
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(26, 20, 26, 18)
        self.root.setSpacing(16)
        self.header = PageHeader(self.title, self.subtitle)
        self.root.addWidget(self.header)
        self.body = QVBoxLayout()
        self.body.setSpacing(14)
        self.root.addLayout(self.body, 1)
        self.build()

    # 子类实现
    def build(self) -> None:  # pragma: no cover
        pass

    def attach_preview(self, content, preview, width: int = 430):
        """把主内容与预览面板装成「左内容 / 右预览」可拖拽分栏。

        预览从原来的"底部一条"改成右侧常驻大栏：预览是主要诉求，
        下载列表也要看得清，两者用 QSplitter 让用户自己拖比例。
        """
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QSplitter

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("MainSplit")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)
        split.addWidget(content)
        split.addWidget(preview)
        split.setStretchFactor(0, 1)   # 内容区随窗口拉伸
        split.setStretchFactor(1, 0)   # 预览保持设定宽度
        preview.setMinimumWidth(300)
        split.setSizes([1200, width])
        self.body.addWidget(split, 1)
        self._main_split = split
        return split

    def on_show(self) -> None:
        """切换到本页时调用（做一次轻量刷新即可）。"""

    # 工具
    def notify(self, text: str, kind: str = "info") -> None:
        """把提示交给主窗口的浮动提示；独立窗口/无主窗口时退化为写日志。

        注意：页面自己就是顶层窗口时 ``window()`` 返回自身，必须避免自调用，
        否则会无限递归（已实测踩到）。
        """
        win = self.window()
        if win is not None and win is not self and hasattr(win, "notify"):
            win.notify(text, kind)
            return
        from ...core.logbus import LOG
        LOG.emit_log("warn" if kind in ("warn", "err") else "info", text)

    def switch_page(self, key: str) -> None:
        win = self.window()
        if win is not None and win is not self and hasattr(win, "switch_page"):
            win.switch_page(key)
