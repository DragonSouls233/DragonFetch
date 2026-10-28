# -*- coding: utf-8 -*-
"""设计令牌 + 全局 QSS。

风格：深色石墨底 + 蓝色强调，卡片化布局、克制的描边与阴影。
切换亮色只改 ``TOKENS_LIGHT``，页面代码不感知颜色。

QSS 里大量 ``{}``，因此用 ``string.Template``（``$var``）而不是 f-string，
避免每个花括号都要转义。
"""
from __future__ import annotations

from string import Template

TOKENS_DARK = {
    "bg": "#0c0f15",
    "bg_alt": "#0f131b",
    "surface": "#141924",
    "surface2": "#1a202c",
    "surface3": "#202836",
    "border": "#252d3c",
    "border_soft": "#1d242f",
    "text": "#e9eef8",
    "sub": "#98a4b8",
    "muted": "#69748a",
    "accent": "#4d8dff",
    "accent_hover": "#659dff",
    "accent_soft": "#1b2a44",
    "accent_text": "#ffffff",
    "ok": "#33c98b",
    "warn": "#f0b429",
    "err": "#f2564d",
    "ok_soft": "#12291f",
    "warn_soft": "#2b2213",
    "err_soft": "#2b1615",
}

TOKENS_LIGHT = {
    "bg": "#f4f6fb",
    "bg_alt": "#eef1f8",
    "surface": "#ffffff",
    "surface2": "#f7f9fd",
    "surface3": "#eef2f9",
    "border": "#dde4f0",
    "border_soft": "#e8edf6",
    "text": "#111827",
    "sub": "#556072",
    "muted": "#8993a4",
    "accent": "#2f6fe4",
    "accent_hover": "#3f7ef0",
    "accent_soft": "#e6eefc",
    "accent_text": "#ffffff",
    "ok": "#12996b",
    "warn": "#b8791a",
    "err": "#d43d35",
    "ok_soft": "#e4f7ef",
    "warn_soft": "#fdf3e0",
    "err_soft": "#fdecea",
}

_QSS = Template("""
* { outline: none; }

QWidget {
    background: transparent;
    color: $text;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}

QWidget#Root { background: $bg; }

/* ---------------- 侧栏 ---------------- */
QWidget#Sidebar {
    background: $bg_alt;
    border-right: 1px solid $border_soft;
}
QScrollArea#NavScroll, QWidget#NavInner { background: transparent; border: none; }
QScrollArea#NavScroll QScrollBar:vertical { width: 6px; margin: 0; }
QLabel#BrandTitle { font-size: 17px; font-weight: 700; letter-spacing: 0.5px; }
QLabel#BrandSub { color: $muted; font-size: 11px; }
QLabel#NavSection {
    color: $muted; font-size: 11px; font-weight: 600;
    padding: 14px 10px 6px 12px; letter-spacing: 1px;
}
QPushButton#NavBtn {
    background: transparent; border: none; border-radius: 9px;
    padding: 10px 12px; text-align: left; color: $sub; font-size: 13px;
}
QPushButton#NavBtn:hover { background: $surface2; color: $text; }
QPushButton#NavBtn:checked {
    background: $accent_soft; color: $text; font-weight: 600;
    border-left: 3px solid $accent;
}
QLabel#Version { color: $muted; font-size: 11px; }

/* ---------------- 文本层级 ---------------- */
QLabel#H1 { font-size: 21px; font-weight: 700; }
QLabel#H2 { font-size: 15px; font-weight: 600; }
QLabel#Sub { color: $sub; font-size: 12px; }
QLabel#Muted { color: $muted; font-size: 12px; }
QLabel#StatValue { font-size: 26px; font-weight: 700; }
QLabel#StatLabel { color: $sub; font-size: 12px; }
QLabel#StatHint { color: $muted; font-size: 11px; }

/* ---------------- 卡片 ---------------- */
QFrame#Card {
    background: $surface;
    border: 1px solid $border_soft;
    border-radius: 14px;
}
QFrame#CardFlat {
    background: $surface2;
    border: 1px solid $border_soft;
    border-radius: 12px;
}
QFrame#Banner {
    background: $accent_soft;
    border: 1px solid $border;
    border-radius: 12px;
}
QFrame#HLine { background: $border_soft; max-height: 1px; border: none; }

/* ---------------- 按钮 ---------------- */
QPushButton {
    background: $surface2; border: 1px solid $border;
    border-radius: 9px; padding: 7px 14px; color: $text;
}
QPushButton:hover { background: $surface3; border-color: $accent; }
QPushButton:pressed { background: $surface; }
QPushButton:disabled { color: $muted; border-color: $border_soft; }

QPushButton#Primary {
    background: $accent; border: 1px solid $accent; color: $accent_text;
    font-weight: 600;
}
QPushButton#Primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#Primary:disabled { background: $surface2; color: $muted; border-color: $border; }

QPushButton#Ghost { background: transparent; border: 1px solid $border; color: $sub; }
QPushButton#Ghost:hover { color: $text; border-color: $accent; }

QPushButton#Danger { background: $err_soft; border: 1px solid $err; color: $err; }
QPushButton#Danger:hover { background: $err; color: #fff; }
QPushButton#Danger:disabled {
    background: $surface2; border-color: $border_soft; color: $muted;
}

QPushButton#Chip {
    background: $surface2; border: 1px solid $border_soft;
    border-radius: 14px; padding: 5px 13px; color: $sub; font-size: 12px;
}
QPushButton#Chip:checked {
    background: $accent_soft; border-color: $accent; color: $text;
}

/* ---------------- 输入 ---------------- */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QComboBox {
    background: $surface2; border: 1px solid $border;
    border-radius: 9px; padding: 7px 10px; selection-background-color: $accent;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border-color: $accent;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: $surface2; border: 1px solid $border;
    selection-background-color: $accent_soft; selection-color: $text;
    padding: 4px; outline: none;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 15px; height: 15px; border-radius: 4px;
    border: 1px solid $border; background: $surface2;
}
QCheckBox::indicator:checked {
    background: $accent; border-color: $accent;
}

/* ---------------- 列表 / 表格 ---------------- */
QListWidget, QTableWidget, QTreeWidget {
    background: $surface; border: 1px solid $border_soft;
    border-radius: 10px; gridline-color: $border_soft;
    alternate-background-color: $surface2;
}
QListWidget::item { padding: 7px 8px; border-radius: 7px; min-height: 22px; }
QListWidget::item:hover { background: $surface2; }
QListWidget::item:selected { background: $accent_soft; color: $text; }

QTableWidget::item { padding: 6px; border: none; }
QTableWidget::item:selected { background: $accent_soft; color: $text; }
QHeaderView::section {
    background: $surface2; color: $sub; border: none;
    border-bottom: 1px solid $border; padding: 8px 6px; font-weight: 600;
}
QTableCornerButton::section { background: $surface2; border: none; }

/* ---------------- 进度条 ---------------- */
QProgressBar {
    background: $surface3; border: none; border-radius: 5px;
    min-height: 8px; max-height: 8px; text-align: center;
    color: transparent; font-size: 1px;
}
QProgressBar::chunk { background: $accent; border-radius: 5px; }
/* 大号进度条：下载页用，需要看得见百分比 */
QProgressBar#Big {
    min-height: 18px; max-height: 18px; border-radius: 9px;
    background: $border; color: $sub; font-size: 11px;
}
QProgressBar#Big::chunk { border-radius: 9px; }

/* ---------------- 标签页 ---------------- */
QTabWidget::pane { border: 1px solid $border_soft; border-radius: 12px; top: -1px; }
QTabBar::tab {
    background: transparent; color: $sub; padding: 8px 16px;
    border-bottom: 2px solid transparent; margin-right: 4px;
}
QTabBar::tab:selected { color: $text; border-bottom: 2px solid $accent; }
QTabBar::tab:hover { color: $text; }

/* ---------------- 内置浏览器 ---------------- */
QWidget#BrowserBar {
    background: $surface2;
    border-bottom: 1px solid $border_soft;
}
QPushButton#BrowserTool {
    background: transparent; border: 1px solid transparent;
    border-radius: 8px; color: $sub; font-size: 13px; padding: 0;
}
QPushButton#BrowserTool:hover {
    background: $surface3; color: $text; border-color: $border;
}
QPushButton#BrowserTool:disabled { color: $muted; background: transparent; }
QLineEdit#BrowserAddr {
    background: $bg; border: 1px solid $border; border-radius: 8px;
    padding: 5px 9px; font-size: 12px; color: $sub;
}
QLineEdit#BrowserAddr:focus { border-color: $accent; color: $text; }
QFrame#SniffBar {
    background: $accent_soft;
    border-top: 1px solid $border_soft;
}
/* 工作区（浏览/预览）与内层浏览器标签：去掉双层圆角，网页贴边才好看 */
QTabWidget#Workspace::pane {
    border: 1px solid $border_soft; border-radius: 12px;
    background: $surface; top: -1px;
}
QTabWidget#BrowserTabs::pane {
    border: none; border-radius: 0; background: $bg; top: 0;
}
QTabWidget#BrowserTabs QTabBar::tab {
    padding: 6px 13px; font-size: 12px; max-width: 190px;
}

/* ---------------- 滚动条 ---------------- */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical {
    background: $surface3; border-radius: 5px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: $muted; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: $surface3; border-radius: 5px; min-width: 30px; }

/* ---------------- 分割器 / 状态栏 ---------------- */
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 10px; }
QSplitter::handle:vertical { height: 10px; }

QStatusBar {
    background: $bg_alt; border-top: 1px solid $border_soft; color: $muted;
}
QStatusBar QLabel { color: $muted; font-size: 11px; padding: 0 8px; }

QMenu {
    background: $surface2; border: 1px solid $border; border-radius: 10px; padding: 6px;
}
QMenu::item { padding: 6px 20px; border-radius: 6px; }
QMenu::item:selected { background: $accent_soft; }

QToolTip {
    background: $surface3; color: $text; border: 1px solid $border;
    padding: 5px 8px; border-radius: 6px;
}

/* ---------------- 提示条 ---------------- */
QFrame#Toast { background: $surface3; border: 1px solid $border; border-radius: 10px; }
QFrame#ToastOk { background: $ok_soft; border: 1px solid $ok; border-radius: 10px; }
QFrame#ToastWarn { background: $warn_soft; border: 1px solid $warn; border-radius: 10px; }
QFrame#ToastErr { background: $err_soft; border: 1px solid $err; border-radius: 10px; }

QLabel#Badge {
    background: $surface3; color: $sub; border-radius: 9px;
    padding: 2px 9px; font-size: 11px;
}
QLabel#BadgeOk { background: $ok_soft; color: $ok; border-radius: 9px; padding: 2px 9px; font-size: 11px; }
QLabel#BadgeWarn { background: $warn_soft; color: $warn; border-radius: 9px; padding: 2px 9px; font-size: 11px; }
QLabel#BadgeErr { background: $err_soft; color: $err; border-radius: 9px; padding: 2px 9px; font-size: 11px; }

/* ---------------- 对话框 / 表单 ---------------- */
QDialog, QWidget#DialogBody { background: $bg; }
QGroupBox {
    border: 1px solid $border_soft; border-radius: 10px;
    margin-top: 10px; padding: 10px 12px 8px 12px;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; color: $sub; }
QFormLayout QLabel { color: $sub; }
QDoubleSpinBox {
    background: $surface2; border: 1px solid $border; border-radius: 8px;
    padding: 6px 8px; color: $text;
}
QTabWidget::tab-bar { left: 2px; }
QTextEdit { background: $surface2; border: 1px solid $border_soft; border-radius: 10px; }
QLabel#StatValueSm { font-size: 18px; font-weight: 700; }
QLabel#Warn { color: $warn; font-size: 12px; }
QLabel#Ok { color: $ok; font-size: 12px; }
QLabel#Err { color: $err; font-size: 12px; }

/* ---------------- 主内容 / 预览分栏 ---------------- */
QSplitter#MainSplit::handle:horizontal {
    background: $border_soft;
    width: 4px;
    margin: 14px 4px;
    border-radius: 2px;
}
QSplitter#MainSplit::handle:horizontal:hover { background: $accent; }

/* ---------------- 内置预览面板 ---------------- */
QFrame#PreviewPane {
    background: $surface2;
    border: 1px solid $border_soft;
    border-radius: 12px;
}
QLabel#PreviewThumb {
    background: $bg_alt;
    border: 1px solid $border_soft;
    border-radius: 8px;
    color: $muted; font-size: 12px; padding: 6px;
}
""")


def tokens(dark: bool = True) -> dict:
    return TOKENS_DARK if dark else TOKENS_LIGHT


def build_qss(dark: bool = True) -> str:
    return _QSS.substitute(tokens(dark))
