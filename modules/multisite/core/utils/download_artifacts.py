# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""下载产物识别 —— 「进行中的文件」与「视频文件」的统一判定

为什么单独一个模块
==================
这套规则原本在 ``content_dedupe``（内容去重）与 ``video_integrity``（完整性校验）
里**各写了一份**。两处判定必须完全一致，否则会出现：
去重认为某个文件"已完成"、完整性校验却认为它"还在下载" —— 互相矛盾。

集中在「下载产物」这一个概念上，两边共用：

* :func:`is_incomplete_download` —— 进行中的产物（``.part`` / ``-FragN`` / ``.ytdl``）
* :func:`is_video_file`          —— 参与媒体处理的扩展名
* :data:`SKIP_DIR_NAMES`         —— 扫描时应跳过的目录

⭐ ``-FragN`` 的判定为什么要用正则
=================================
yt-dlp 的 native HLS 分片文件名形如 ``xxx.mp4.part-Frag5``、``xxx-Frag12.part``。

* 用 ``name.endswith(".part")`` 判断 → **漏掉** ``xxx.mp4.part-Frag5``（结尾是 ``-Frag5``）
* 用裸子串 ``"frag" in name`` 判断   → **误杀** ``Fragment_Of_Love.mp4`` 这类正常文件

所以必须用精确正则 ``-Frag<数字>``。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Union

__all__ = [
    "VIDEO_EXTENSIONS",
    "INCOMPLETE_SUFFIXES",
    "SKIP_DIR_NAMES",
    "FRAG_RE",
    "is_incomplete_download",
    "is_video_file",
]

#: 参与媒体处理的视频扩展名
VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".flv",
     ".m4v", ".ts", ".mpg", ".mpeg"})

#: 进行中的下载产物后缀
INCOMPLETE_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp", ".download")

#: yt-dlp native HLS 分片：只认 ``-Frag<数字>``（分隔符可为 ``.`` ``-`` ``_``）
FRAG_RE = re.compile(r"(?:^|[.\-_])frag\d+(?:[.\-_]|$)", re.IGNORECASE)

#: 扫描时跳过的目录名（项目自身辅助目录 + 常见系统/回收站目录）
SKIP_DIR_NAMES = frozenset({
    ".actors", "temp", "_trash", "_trash_dups", "__pycache__",
    "$RECYCLE.BIN", "System Volume Information", ".git",
    "db_backups", "logs", "cache",
})


def is_incomplete_download(path: Union[str, Path]) -> bool:
    """是否是**进行中**的下载产物（内容还会变，不能改名/去重/修复）。

    识别三类：
    1. ``xxx.mp4.part`` —— 下载中的目标文件
    2. ``xxx.mp4.part-Frag5`` / ``xxx-Frag12.part`` —— yt-dlp native HLS 分片
    3. ``xxx.ytdl`` / ``.temp`` / ``.tmp`` / ``.download``

    Args:
        path: 文件名或路径

    Returns:
        bool: True = 进行中的产物
    """
    name = os.path.basename(str(path)).lower()
    if not name:
        return False
    if name.endswith(INCOMPLETE_SUFFIXES):
        return True
    return bool(FRAG_RE.search(name))


def is_video_file(path: Union[str, Path]) -> bool:
    """扩展名是否属于 :data:`VIDEO_EXTENSIONS`。"""
    return Path(str(path)).suffix.lower() in VIDEO_EXTENSIONS
