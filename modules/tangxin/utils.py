# -*- coding: utf-8 -*-
"""通用工具：文件名清洗、匹配键归一化、ffmpeg 路径、字节辅助。"""
import os
import re
import subprocess
import unicodedata

from . import config


_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_TRIM = re.compile(r'\s+')


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """清洗成 Windows 合法文件名：去非法字符、压缩空白、去首尾点/空格、限长。"""
    if not name:
        return "untitled"
    name = _ILLEGAL.sub(" ", name)
    name = _TRIM.sub(" ", name).strip()
    name = name.strip(". ")
    if not name:
        return "untitled"
    # Windows 保留名处理
    if name.upper() in ("CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3",
                        "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6",
                        "LPT7", "LPT8", "LPT9"):
        name = name + "_"
    if len(name) > max_len:
        name = name[:max_len].rstrip(". ")
    return name


def norm_key(text) -> str:
    """标准化匹配键：NFKC（全角→半角）+ casefold + 去所有空白。

    ⚠️ 放在 utils（而不是 existing）的原因：``db`` 也要用它做"同一演员不同写法"
       的归并，而 existing 依赖 db —— 放 existing 会形成循环导入。
       ``existing.norm_key`` 保留为转发，行为完全一致。

    目的是让「測試影片」「測試 影片」「測试影片」这类差异仍能匹配，
    同时避免把明显不同的片子误判成同一个。
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text))
    s = s.casefold()
    s = _TRIM.sub("", s)
    return s.strip()


class DownloadCancelled(Exception):
    """下载被用户中止。下载器在检测到 stop_event 后抛出，由调用方收敛为『已取消』。"""


def find_ffmpeg() -> str:
    return config.FFMPEG


def run_ffmpeg(args: list, timeout: int = 1800) -> subprocess.CompletedProcess:
    """运行 ffmpeg，返回 CompletedProcess。args 不含可执行名。"""
    cmd = [find_ffmpeg()] + args
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout)
