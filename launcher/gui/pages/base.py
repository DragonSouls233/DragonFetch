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

    # ---------------------------------------------------------------- 钩子
    def home_url(self) -> str:
        """本模块内置浏览器的首页。

        返回空串表示"这个模块没有对应站点"（总览/设置/库管理这类页面），
        此时右侧只留预览，不会摆一个空浏览器占地方。
        """
        return ""

    def browse_download_dir(self) -> str:
        """内置浏览器里点「下载」时的落地目录（一般就是本模块的下载目录）。"""
        return ""

    # ---------------------------------------------------------------- 布局
    def attach_preview(self, content, preview, width: int = 430):
        """把主内容与右侧工作区装成可拖拽分栏。

        右侧默认是 **「浏览」+「预览」两个标签**：浏览是 QtWebEngine 真站点
        （可直接登录/点播/下载），预览是解析直链后的内嵌播放。两者共用一块
        纵向空间，用标签切换而不是硬挤成两栏——用户要的是"一片大面积浏览"。
        """
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QSplitter, QTabWidget

        from ..browser import BrowserPanel, WebWorkspace

        if isinstance(preview, (WebWorkspace, QTabWidget)):
            # 页面自己已经组织好右侧(黄果/糖心：「网页」+「播放器」)，直接沿用；
            # 顺手把里面的浏览器找出来，供 switch_page 触发懒加载。
            right = preview
            for i in range(preview.count()):
                w = preview.widget(i)
                if isinstance(w, BrowserPanel):
                    self.browser_panel = w
                    break
        else:
            right = WebWorkspace(preview, home_url=self.home_url(),
                                 download_dir=self.browse_download_dir())
            self.browser_panel = right.browser
        self.workspace = right

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("MainSplit")
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)
        split.addWidget(content)
        split.addWidget(right)
        # QSplitter 会尊重子控件的 minimumSizeHint：站点页那张 8 列表格的最小宽度
        # 实测约 1300px，不显式压一下的话浏览区永远只有 37%，拖都拖不大（已踩）。
        # 压到 340 后表格会出横向滚动条，这是可接受的代价。
        content.setMinimumWidth(340)
        # 浏览区略大于内容区：用户的原话是"每个模块都要一片大面积的浏览"。
        # 默认 760:860（窗口 1440 宽时约 46:54），拖一下分隔条就能改，比例会记住。
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        right.setMinimumWidth(520)
        split.setSizes([max(560, width + 330), max(700, int((width + 330) * 1.13))])
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
