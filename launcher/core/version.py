# -*- coding: utf-8 -*-
"""版本与产品信息（打包、设置页、日志统一引用这里）。"""
from __future__ import annotations

APP_NAME = "DragonFetch"
APP_TITLE = "DragonFetch · 成人全能下载器"
APP_VERSION = "0.0.1"
APP_BUILD = "2026.10"
GITHUB_HINT = "DragonFetch"
COPYRIGHT = "DragonFetch"

# 各后端支持的站点（用于界面展示，不做功能判断）
SUPPORT_MODULES = [
    ("tangxin", "糖心 Vlog"),
    ("huangguo", "黄果短剧"),
    ("sites", "多站点（JAVDB / Madouqu / 通用）"),
    ("yttiktok", "映取 · YouTube / TikTok"),
]

VERSION_TEXT = f"{APP_NAME} {APP_VERSION} ({APP_BUILD})"
