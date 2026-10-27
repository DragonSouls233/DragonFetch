# -*- coding: utf-8 -*-
"""版本与产品信息（打包、设置页、日志统一引用这里）。"""
from __future__ import annotations

APP_NAME = "DragonFetch"
APP_TITLE = "DragonFetch · 成人全能下载器"
APP_VERSION = "2.1.0"
APP_BUILD = "2026.10"
GITHUB_HINT = "DragonFetch"
COPYRIGHT = "DragonFetch"

# 各模块的中文名（用于界面展示，不做功能判断）——每个模块都独立
SUPPORT_MODULES = [
    ("tangxin", "糖心 Vlog"),
    ("huangguo", "黄果短剧"),
    ("jiuse", "九色视频"),
    ("sites", "站点 ×12（一域名一模块）"),
    ("madouqu", "麻豆磁力"),
    ("yttiktok", "映取 · YouTube / TikTok"),
]

VERSION_TEXT = f"{APP_NAME} {APP_VERSION} ({APP_BUILD})"
