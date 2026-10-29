# -*- coding: utf-8 -*-
"""通用封面网格：媒体库各模块（黄果 / 短剧平台 / 九色 / 糖心）共用。

为什么抽出来
------------
之前只有黄果有封面网格（``_DramaGrid``），九色是表格缩略图、糖心干脆没有，
于是"媒体库下面有的模块有封面、有的没有"。这里把它做成通用组件，
各模块只要把列表转成 item 字典塞进来即可。

item 约定（dict）::

    {"key": 唯一键, "title": 标题, "cover": 封面URL,
     "sub": 副标题(如 "8.5分 · 24集"), "total": 总集数/总数, "data": 原始对象}

关键能力
--------
* **分块建卡**：一次建 12 张，避免上百张卡把界面卡死
* **加载更多**：默认只显示 ``PAGE`` 张，点底部按钮再追加（列表动辄上千条）
* **下载角标**：``refresh_status({key: 已完成数})`` 直接在封面左上标「已下 N/M」
* **封面缓存**：走 ``CoverCache``（内存 LRU + 磁盘 + 可选解密），全程后台线程
"""
from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from .cover_cache import CoverCache

__all__ = ["CoverCard", "CoverGrid", "make_item"]

PAGE = 60          # 首屏显示多少张（点「加载更多」再追加 PAGE 张）
CHUNK = 12         # 每轮建几张卡（分块，避免卡界面）


def make_item(key, title: str, cover: str = "", sub: str = "",
              total: int = 0, data=None) -> dict:
    """把任意对象转成网格认识的 item 字典。"""
    return {"key": str(key), "title": title or "", "cover": cover or "",
            "sub": sub or "", "total": int(total or 0), "data": data}


class CoverCard(QFrame):
    """一张封面卡片：封面 + 下载角标 + 标题 + 副标题。"""

    clicked = pyqtSignal(dict)
    double_clicked = pyqtSignal(dict)

    def __init__(self, item: dict, tokens: dict, cover: tuple[int, int],
                 parent=None):
        super().__init__(parent)
        self.setObjectName("CoverCard")
        self.item = item
        self._t = tokens
        self._w, self._h = cover
        self._selected = False
        self._checked = False
        self._done = 0
        self._total = int(item.get("total") or 0)

        self.setFixedWidth(self._w + 16)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build()
        self._apply_style()

    # ------------------------------------------------------------------ 构建

    def _build(self) -> None:
        t = self._t
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        wrap = QFrame(self)
        wrap.setFixedSize(self._w, self._h)
        wrap.setStyleSheet("background: transparent;")

        self.cover = QLabel(wrap)
        self.cover.setGeometry(0, 0, self._w, self._h)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setText("加载中")
        self.cover.setStyleSheet(
            f"background:{t['surface3']}; border-radius:6px;"
            f" color:{t['muted']}; font-size:12px;")

        # 下载角标叠在封面左上角（不是布局子控件，必须自己 move/adjustSize）
        self.badge = QLabel(wrap)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setVisible(False)

        # 多选勾选框（右上角），仅在选择模式可见
        self.check = QLabel(wrap)
        self.check.setText("☐")
        self.check.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.check.setStyleSheet(
            "background:rgba(0,0,0,0.45); color:#ffffff; border-radius:4px;"
            " font-size:13px; padding:1px 4px;")
        self.check.move(max(0, self._w - 30), 6)
        self.check.setVisible(False)

        lay.addWidget(wrap)

        text = (self.item.get("title") or "").strip() or "（无标题）"
        self.title = QLabel(text)
        self.title.setWordWrap(True)
        self.title.setFixedWidth(self._w)
        self.title.setMaximumHeight(34)
        self.title.setToolTip(text)
        self.title.setStyleSheet(
            f"color:{t['text']}; font-size:12px; background:transparent;")
        lay.addWidget(self.title)

        sub = (self.item.get("sub") or "").strip()
        self.meta = QLabel(sub)
        self.meta.setStyleSheet(
            f"color:{t['sub']}; font-size:11px; background:transparent;")
        lay.addWidget(self.meta)

    def _apply_style(self) -> None:
        t = self._t
        if self._selected or self._checked:
            border, bg = t["accent"], t["accent_soft"]
        elif self._done > 0 and self._total > 0 and self._done >= self._total:
            border, bg = t["ok"], t["ok_soft"]
        elif self._done > 0:
            border, bg = t["warn"], t["warn_soft"]
        else:
            border, bg = t["border"], t["surface2"]
        self.setStyleSheet(
            f"QFrame#CoverCard {{ background:{bg}; border:1px solid {border};"
            f" border-radius:8px; }}")

    # ------------------------------------------------------------------ 对外

    def key(self) -> str:
        return str(self.item.get("key"))

    def set_selected(self, value: bool) -> None:
        if self._selected == value:
            return
        self._selected = value
        self._apply_style()

    def set_multi_select(self, value: bool) -> None:
        self.check.setVisible(bool(value))
        if not value:
            self.set_checked(False)

    def set_checked(self, value: bool) -> None:
        if self._checked == bool(value):
            return
        self._checked = bool(value)
        self.check.setText("☑" if self._checked else "☐")
        self._apply_style()

    def set_status(self, done: int, total: Optional[int] = None) -> None:
        if total is not None:
            self._total = int(total or 0)
        self._done = int(done or 0)
        self._render_badge()
        self._apply_style()

    def set_cover(self, image) -> None:
        from PyQt6.QtGui import QPixmap

        if image is None or image.isNull():
            self.cover.setPixmap(QPixmap())
            self.cover.setText("无封面")
            return
        self.cover.setPixmap(QPixmap.fromImage(image))
        self.cover.setText("")

    # ------------------------------------------------------------------ 内部

    def _render_badge(self) -> None:
        if self._done <= 0 or self._total <= 0:
            self.badge.setVisible(False)
            return
        t = self._t
        full = self._done >= self._total
        text = f"已下 {self._done}/{self._total}" if not full else "已下载"
        color = t["ok"] if full else t["warn"]
        self.badge.setText(text)
        self.badge.setStyleSheet(
            f"background:{color}; color:{self._t.get('on_accent', '#0b0e14')};"
            f" border-radius:4px; font-size:11px; padding:2px 6px;"
            f" font-weight:600;")
        self.badge.adjustSize()
        self.badge.move(6, 6)
        self.badge.raise_()
        self.badge.setVisible(True)

    def mousePressEvent(self, event):            # noqa: N802
        self.clicked.emit(self.item)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):      # noqa: N802
        self.double_clicked.emit(self.item)
        super().mouseDoubleClickEvent(event)


class CoverGrid(QScrollArea):
    """封面网格容器：分块建卡 + 加载更多 + 封面缓存 + 下载角标。"""

    selected = pyqtSignal(dict)
    activated = pyqtSignal(dict)                 # 双击
    load_more_requested = pyqtSignal()           # 用户点了「加载更多」但已无本地数据
    clear_filter_requested = pyqtSignal()        # Esc：请求上层清除筛选
    selection_changed = pyqtSignal(list)         # 多选集合变化（key 列表）
    batch_download_requested = pyqtSignal(list)  # 批量下载（key 列表）

    def __init__(self, tokens: dict, cover: tuple[int, int] = (160, 224),
                 cache: Optional[CoverCache] = None, page: int = PAGE,
                 key_field: str = "key", cover_field: str = "cover",
                 total_field: str = "total", sub_fn: Optional[Callable[[dict], str]] = None,
                 default_total: int = 0, parent=None):
        """字段映射：各模块列表的字段名不一样（黄果 did/cover_url/episode_count，
        糖心是 Video 对象），这里统一规范化成 item 字典，卡片只认标准字段。"""
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._t = tokens
        self._w, self._h = cover
        self._cache = cache
        self._page = max(1, int(page or PAGE))
        self._key_field = key_field
        self._cover_field = cover_field
        self._total_field = total_field
        self._sub_fn = sub_fn
        self._default_total = int(default_total or 0)

        self._host = QWidget()
        outer = QVBoxLayout(self._host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        # 排序条（可选）：页面调用 set_sort_options 后才显示
        self._sort_combo = QComboBox()
        self._sort_combo.setMinimumWidth(150)
        self._sort_combo.setVisible(False)
        self._sort_combo.currentIndexChanged.connect(self._on_sort_idx)
        outer.addWidget(self._sort_combo)

        self._holder = QWidget()
        self._gl = QGridLayout(self._holder)
        self._gl.setContentsMargins(10, 10, 10, 10)
        self._gl.setSpacing(10)
        self._gl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self._holder, 1)

        # 空态 / 零结果态：库为空或筛选无命中时给出明确指引，而不是一片空白。
        self._empty = QLabel("")
        self._empty.setObjectName("EmptyHint")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setMinimumHeight(160)
        self._empty.hide()
        outer.addWidget(self._empty, 1)

        self.btn_more = QPushButton("加载更多")
        self.btn_more.setObjectName("Ghost")
        self.btn_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_more.clicked.connect(self._on_more)
        self.lbl_tip = QLabel("")
        self.lbl_tip.setObjectName("Muted")
        self.lbl_tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.btn_more)
        outer.addWidget(self.lbl_tip)
        self.setWidget(self._host)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # 多选工具条（覆盖在视口底部，仅在选择模式下显示）
        self._sel_bar = QWidget(self)
        sel_lay = QHBoxLayout(self._sel_bar)
        sel_lay.setContentsMargins(8, 6, 8, 6)
        sel_lay.setSpacing(6)
        self._sel_label = QLabel("已选 0")
        sel_lay.addWidget(self._sel_label)
        sel_lay.addStretch(1)
        b_all = QPushButton("全选"); b_all.setObjectName("Ghost")
        b_all.clicked.connect(self.select_all)
        b_none = QPushButton("取消选择"); b_none.setObjectName("Ghost")
        b_none.clicked.connect(self.clear_selection)
        b_dl = QPushButton("批量下载"); b_dl.setObjectName("Primary")
        b_dl.clicked.connect(self._on_batch_dl)
        b_exit = QPushButton("退出"); b_exit.setObjectName("Ghost")
        b_exit.clicked.connect(self.exit_select_mode)
        for b in (b_all, b_none, b_dl, b_exit):
            sel_lay.addWidget(b)
        self._sel_bar.hide()
        self._position_sel_bar()

        self._cards: dict[str, CoverCard] = {}
        self._all: list[dict] = []
        self._visible: list[dict] = []
        self._pending: list[dict] = []
        self._token = 0
        self._status: dict[str, int] = {}
        self._current: dict | None = None
        self._shown = 0
        self._on_more_cb: Optional[Callable[[], None]] = None
        self._kw = ""
        self._field = "title"
        self._loading = False
        self._select_mode = False
        self._selected: set = set()
        self._sort_key = ""
        self._sort_reverse = False
        self._sort_fn = None
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self._apply_filter)

    # ------------------------------------------------------------------ 数据

    def set_cache(self, cache: CoverCache) -> None:
        """封面缓存要等后端就绪（知道当前站点）才能建，所以允许后置。"""
        self._cache = cache
        for key, card in self._cards.items():
            url = (card.item.get("cover") or "").strip()
            if url:
                cache.request(key, url, self._on_cover)

    def set_more_loader(self, cb: Optional[Callable[[], None]]) -> None:
        """「加载更多」的回调：本地还有数据时自动翻页，没有则由页面去抓下一页。"""
        self._on_more_cb = cb

    def _normalize(self, d) -> dict:
        """把任意列表元素（dict / 数据类对象）转成标准 item 字典。

        糖心传的是 ``Video`` 数据类对象（vid/cover_url/title），黄果/九色传
        的是 dict —— 两种都要认，否则所有卡片 key 都是空串、最后只剩一张卡。
        """
        if not isinstance(d, dict):
            obj = d
            out = {
                "key": str(getattr(obj, self._key_field, "")
                           or getattr(obj, "id", "") or ""),
                "cover": str(getattr(obj, self._cover_field, "")
                             or getattr(obj, "cover_url", "")
                             or getattr(obj, "cover", "") or ""),
                "title": str(getattr(obj, "title", "") or ""),
                "total": int(getattr(obj, self._total_field, 0) or 0)
                         or self._default_total,
                "sub": "",
                "data": obj,
            }
            if self._sub_fn is not None:
                try:
                    out["sub"] = self._sub_fn(obj) or ""
                except Exception:                           # noqa: BLE001
                    out["sub"] = ""
            return out
        if "key" in d and "cover" in d:
            return d
        out = dict(d)
        out["key"] = str(d.get(self._key_field)
                         or d.get("key") or d.get("id") or "")
        out["cover"] = str(d.get(self._cover_field)
                           or d.get("cover_url") or d.get("cover") or "")
        out["title"] = str(d.get("title") or "")
        out["total"] = int(d.get(self._total_field) or 0) or self._default_total
        if self._sub_fn is not None:
            try:
                out["sub"] = self._sub_fn(d) or ""
            except Exception:                               # noqa: BLE001
                out["sub"] = ""
        out.setdefault("sub", "")
        out["data"] = d if "_obj" not in d else d["_obj"]
        return out

    def set_items(self, items: list[dict], status: dict | None = None) -> None:
        """整体替换列表（会重置分页、整表重建）。"""
        self._all = [self._normalize(d) for d in (items or [])]
        self._status = dict(status or {})
        self._current = None
        self._shown = 0
        self._apply_filter(full=True)

    def append_items(self, items: list[dict], status: dict | None = None) -> None:
        """追加一批（「加载更多」抓到新数据后用），已有的 key 会去重。"""
        have = {str(d.get("key")) for d in self._all}
        added = 0
        for d in items or []:
            nd = self._normalize(d)
            k = str(nd.get("key"))
            if k in have:
                continue
            have.add(k)
            self._all.append(nd)
            added += 1
        if status:
            self._status.update(status)
        if added:
            # 关键：把新抓到的**显示出来**。只补进 _all 而不推进 _shown 的话，
            # 用户点完「加载更多」界面纹丝不动（数据抓到了却看不见）——
            # 这跟"没效果"没区别（已实测）。至少露出新增量，再多给一页的量。
            self._shown = min(self._shown + max(added, self._page),
                              len(self._all))
            self._apply_filter()

    def set_filter(self, text: str, field: str = "title") -> None:
        """筛选（带 250ms 防抖）：连续输入只重建一次网格，避免逐字卡顿。"""
        self._kw = (text or "").strip().lower()
        self._field = field
        self._filter_timer.start(250)

    # ------------------------------------------------------------------ 排序
    def set_sort_options(self, options: list[dict]) -> None:
        """页面提供排序项后显示排序条。每项：{"label","key","reverse","fn"}。"""
        self._sort_combo.blockSignals(True)
        self._sort_combo.clear()
        self._sort_options = list(options or [])
        for opt in self._sort_options:
            self._sort_combo.addItem(opt.get("label", ""), opt.get("key", ""))
        self._sort_combo.blockSignals(False)
        self._sort_combo.setVisible(bool(self._sort_options))

    def _on_sort_idx(self, idx: int) -> None:
        key = self._sort_combo.itemData(idx)
        self._on_sort(key)

    def _on_sort(self, key) -> None:
        opt = next((o for o in getattr(self, "_sort_options", [])
                    if o.get("key") == key), None)
        if opt is None:
            return
        self._sort_key = str(key or "")
        self._sort_reverse = bool(opt.get("reverse", False))
        self._sort_fn = opt.get("fn")
        self._apply_filter()

    def refresh_status(self, status: dict) -> None:
        self._status = dict(status or {})
        for key, card in self._cards.items():
            card.set_status(self._status.get(key, 0), card.item.get("total"))

    def select_first(self) -> None:
        if self._visible:
            self._select(self._visible[0])

    def current(self) -> dict | None:
        return self._current

    def count(self) -> int:
        return len(self._all)

    def shown(self) -> int:
        return min(self._shown, len(self._visible))

    def has_more(self) -> bool:
        return len(self._visible) > self._shown

    # ------------------------------------------------------------------ 构建

    def _apply_filter(self, full: bool = False) -> None:
        kw = getattr(self, "_kw", "") or ""
        field = getattr(self, "_field", "title")
        self._visible = [d for d in self._all
                         if not kw or kw in str(d.get(field) or "").lower()]
        # 排序（按 key 取字段；若提供 fn 则用它，便于按数值/日期排序）
        if self._sort_key:
            fn = self._sort_fn or (lambda d: str(d.get(self._sort_key) or ""))
            try:
                self._visible.sort(key=fn, reverse=self._sort_reverse)
            except Exception:                       # noqa: BLE001
                pass
        if self._shown == 0:
            self._shown = min(self._page, len(self._visible))
        else:
            self._shown = min(self._shown, len(self._visible))
        if full:
            self._rebuild()
        else:
            self._relayout_incremental()

    def _relayout_incremental(self) -> None:
        """筛选/排序变化时复用已有卡片：隐藏不可见的、补全缺失的、再布局。

        不再像 _rebuild 那样销毁全部卡片——避免千条列表逐字筛选时每次重建
        卡 200ms+，也保住已加载的封面与下载角标。
        """
        vis_keys = {str(d.get("key")) for d in self._visible[:self._shown]}
        for k, card in list(self._cards.items()):
            if k not in vis_keys:
                card.setVisible(False)
        self._pending = [d for d in self._visible[:self._shown]
                         if str(d.get("key")) not in self._cards]
        self._build_chunk(self._token)

    def _rebuild(self) -> None:
        self._token += 1
        for card in self._cards.values():
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        while self._gl.count():
            item = self._gl.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        self._pending = list(self._visible[:self._shown])
        self._build_chunk(self._token)

    def _build_chunk(self, token: int) -> None:
        if token != self._token:
            return
        if not self._pending:
            self._relayout()
            return
        chunk, self._pending = self._pending[:CHUNK], self._pending[CHUNK:]
        for d in chunk:
            k = str(d.get("key"))
            if k in self._cards:
                continue
            card = CoverCard(d, self._t, (self._w, self._h), self._holder)
            card.clicked.connect(self._on_click)
            card.double_clicked.connect(self.activated.emit)
            card.set_status(self._status.get(k, 0), d.get("total"))
            card.set_multi_select(self._select_mode)
            card.set_checked(k in self._selected)
            self._cards[k] = card
            url = (d.get("cover") or "").strip()
            if url and self._cache is not None:
                self._cache.request(k, url, self._on_cover)
            elif url:
                card.cover.setText("待加载")
            else:
                card.set_cover(None)
        self._relayout()
        QTimer.singleShot(0, lambda t=token: self._build_chunk(t))

    def _relayout(self) -> None:
        while self._gl.count():
            item = self._gl.takeAt(0)
            if item.widget():
                self._gl.removeWidget(item.widget())

        avail = self.viewport().width() - 20
        each = self._w + 16 + 10
        cols = max(1, avail // each) if avail > 0 else 1
        for idx, d in enumerate(self._visible[:self._shown]):
            card = self._cards.get(str(d.get("key")))
            if card is not None:
                card.setVisible(True)
                self._gl.addWidget(card, idx // cols, idx % cols)
        self._update_more_btn()

    def _update_more_btn(self) -> None:
        self._update_empty_state()
        more = self.has_more()
        self.btn_more.setVisible(True)
        self.btn_more.setEnabled(True)
        if self._loading:
            self.btn_more.setText("加载中…")
            self.btn_more.setEnabled(False)
        elif more:
            rest = len(self._visible) - self._shown
            self.btn_more.setText(f"加载更多（还有 {rest} 条）")
        else:
            self.btn_more.setText("加载更多" if self._on_more_cb else "已全部显示")
            self.btn_more.setEnabled(bool(self._on_more_cb))
        total_part = (f"（库内共 {len(self._all)}）"
                      if len(self._all) != len(self._visible) else "")
        kw_part = (f" · 筛选「{self._kw}」" if self._kw else "")
        self.lbl_tip.setText(
            f"已显示 {self.shown()}/{len(self._visible)}" + total_part + kw_part)

    def _update_empty_state(self) -> None:
        """库为空 / 筛选零命中时给出明确指引，而不是一片空白。"""
        if len(self._visible):
            self._empty.hide()
            return
        t = self._t
        if self._kw:
            title = f"没有匹配「{self._kw}」的内容"
            sub = "换个关键词，或清除筛选看看全部内容"
        elif len(self._all):
            title = "没有可显示的内容"
            sub = "当前条件下没有条目"
        else:
            title = "这里还没有内容"
            sub = "去对应模块抓取或下载后，作品会显示在这里"
        self._empty.setText(f"<div style='text-align:center;'>"
                            f"<div style='font-size:15px;color:{t['text']};"
                            f"margin-bottom:6px;'>{title}</div>"
                            f"<div style='color:{t['muted']};'>{sub}</div></div>")
        self._empty.show()

    def set_loading(self, value: bool) -> None:
        """抓取/翻页进行时给出加载态，避免用户以为卡死。"""
        self._loading = bool(value)
        self._update_more_btn()

    # ------------------------------------------------------------------ 交互

    def _on_more(self) -> None:
        if self.has_more():
            self._shown = min(self._shown + self._page, len(self._visible))
            self._pending = [d for d in self._visible[:self._shown]
                             if str(d.get("key")) not in self._cards]
            self._build_chunk(self._token)
            return
        # 本地已经显示完 ⇒ 交给页面去抓下一页（联网）
        self.load_more_requested.emit()
        if self._on_more_cb is not None:
            self._on_more_cb()

    def _on_cover(self, key, image) -> None:
        card = self._cards.get(str(key))
        if card is not None:
            card.set_cover(image)

    def _on_click(self, item: dict) -> None:
        if self._select_mode:
            self._toggle_select(str(item.get("key")))
        else:
            self._select(item)

    # ------------------------------------------------------------------ 多选
    def enter_select_mode(self) -> None:
        if self._select_mode:
            return
        self._select_mode = True
        for card in self._cards.values():
            card.set_multi_select(True)
        self._sel_bar.show()
        self._position_sel_bar()
        self._sel_label.setText(f"已选 {len(self._selected)}")

    def exit_select_mode(self) -> None:
        if not self._select_mode:
            return
        self._select_mode = False
        self._selected.clear()
        for card in self._cards.values():
            card.set_multi_select(False)
            card.set_checked(False)
        self._sel_bar.hide()
        self.selection_changed.emit([])

    def select_all(self) -> None:
        self._selected = {str(d.get("key")) for d in self._visible}
        for k, card in self._cards.items():
            card.set_checked(k in self._selected)
        self._sel_label.setText(f"已选 {len(self._selected)}")
        self.selection_changed.emit(list(self._selected))

    def clear_selection(self) -> None:
        self._selected.clear()
        for card in self._cards.values():
            card.set_checked(False)
        self._sel_label.setText("已选 0")
        self.selection_changed.emit([])

    def _toggle_select(self, key: str) -> None:
        if key in self._selected:
            self._selected.discard(key)
        else:
            self._selected.add(key)
        card = self._cards.get(key)
        if card is not None:
            card.set_checked(key in self._selected)
        self._sel_label.setText(f"已选 {len(self._selected)}")
        self.selection_changed.emit(list(self._selected))

    def _on_batch_dl(self) -> None:
        if self._selected:
            self.batch_download_requested.emit(list(self._selected))

    def _position_sel_bar(self) -> None:
        vw, vh = self.width(), self.height()
        w = min(440, max(280, vw - 24))
        h = 40
        self._sel_bar.setGeometry((vw - w) // 2, vh - h - 14, w, h)

    def _select(self, item: dict) -> None:
        self._current = item
        key = str(item.get("key"))
        for k, card in self._cards.items():
            if not self._select_mode:
                card.set_selected(k == key)
        self.selected.emit(item)

    def resizeEvent(self, event):                # noqa: N802
        super().resizeEvent(event)
        if self._all:
            self._relayout()
        if self._sel_bar.isVisible():
            self._position_sel_bar()

    # ------------------------------------------------------------------ 键盘
    def keyPressEvent(self, event):              # noqa: N802
        if not self._visible:
            return super().keyPressEvent(event)
        cols = self._cols()
        cur_idx = -1
        cur_key = str(self._current.get("key")) if self._current else None
        for i, d in enumerate(self._visible[:self.shown()]):
            if str(d.get("key")) == cur_key:
                cur_idx = i
                break
        k = event.key()
        if k == Qt.Key.Key_Escape:
            if self._kw:
                self.clear_filter_requested.emit()
            return super().keyPressEvent(event)
        if cur_idx < 0:
            if k in (Qt.Key.Key_Right, Qt.Key.Key_Left, Qt.Key.Key_Up,
                     Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._select(self._visible[0])
                event.accept()
                return
            return super().keyPressEvent(event)
        n = self.shown()
        nxt = cur_idx
        if k == Qt.Key.Key_Right:
            nxt = min(n - 1, cur_idx + 1)
        elif k == Qt.Key.Key_Left:
            nxt = max(0, cur_idx - 1)
        elif k == Qt.Key.Key_Down:
            nxt = min(n - 1, cur_idx + cols)
        elif k == Qt.Key.Key_Up:
            nxt = max(0, cur_idx - cols)
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._current is not None:
                self.activated.emit(self._current)
                event.accept()
                return
        else:
            return super().keyPressEvent(event)
        if nxt != cur_idx:
            self._select(self._visible[nxt])
            card = self._cards.get(str(self._visible[nxt].get("key")))
            if card is not None:
                self.ensureWidgetVisible(card, 24, 24)
            event.accept()

    def _cols(self) -> int:
        avail = self.viewport().width() - 20
        each = self._w + 16 + 10
        return max(1, avail // each) if avail > 0 else 1
