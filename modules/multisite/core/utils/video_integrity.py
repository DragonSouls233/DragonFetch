# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""视频完整性校验 + 三级修复

为什么需要
==========
``陷阱手册.md`` B15 修的是「**下完了但改不了名**」（``.part`` → ``.mp4`` 撞 WinError 32）。
本模块解决的是**另一层**问题：**下完了、名字也改好了，但文件本身是坏的** ——
缺 moov atom、被截断、音视频不同步、时长探测为 0。

两件事互补，不重叠：

======== ========================================== ==================
层面     症状                                        谁负责
======== ========================================== ==================
名称     ``Unable to rename``（文件其实是完整的）      B15 抢救分支
内容     能改名、能打开，但播放到一半卡死 / 无时长      **本模块**
======== ========================================== ==================

三级策略（**由便宜到贵**）
==========================
1. ``probe``    —— ``ffprobe`` 确认容器可解析、有视频流、``duration > 0``。
   快（<200 ms），不重编码。
2. ``remux``    —— ``ffmpeg -c copy -movflags +faststart``。
   修容器级损坏（缺 moov、时间戳乱、ts→mp4），**无画质损失**。
3. ``reencode`` —— ``-c:v libx264 -c:a aac``。最后手段，慢但能吞掉解码错误。

> 策略与命令参考 ``G:\\MDCX\\.references\\GitHub\\harvestr-main\\video_repair.py``
> （437 行，**MIT**，可自由取用）。

⭐ 相对参考实现的两处**安全性改进**
=================================
1. **替换顺序**。参考实现是 ``p.unlink()`` → ``fixed.rename(final)``：
   一旦 rename 失败，**原件已经没了**。
   本模块改用 ``os.replace(fixed, path)`` —— 同卷原子替换，
   **失败时原件原封不动**；并且默认先留一份 ``.bak``。
2. **确认闸门**。``repair_file()`` 默认 ``confirm=False`` → **只出计划不落地**。
   修复是破坏性操作，必须显式确认（CLI 上就是 ``--repair --confirm``）。

⚠️ 本模块**不会被任何自动流程调用**。默认入口 ``main()`` 只做只读体检。

用法
====
.. code-block:: bash

    # 只读体检（默认）：扫盘并列出损坏文件
    python -m modules.core.utils.video_integrity --root "O:\\"

    # 顺带体检下载临时区（.part 残留也能读，探测是只读的）
    python -m modules.core.utils.video_integrity --root "temp\\downloads" --include-incomplete

    # 真的修复（破坏性，必须双开关）
    python -m modules.core.utils.video_integrity --root "O:\\" --repair --confirm
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .download_artifacts import (
    INCOMPLETE_SUFFIXES as _SHARED_INCOMPLETE_SUFFIXES,
    SKIP_DIR_NAMES as _SHARED_SKIP_DIR_NAMES,
    VIDEO_EXTENSIONS as _SHARED_VIDEO_EXTENSIONS,
    is_incomplete_download,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ProbeResult",
    "RepairResult",
    "get_ffmpeg",
    "get_ffprobe",
    "check_playable",
    "repair_file",
    "sweep_folder",
    "build_report",
    "format_report_text",
    "is_file_locked",
]

#: 小于此大小直接判为坏（只有文件头，没有内容）
MIN_PLAUSIBLE_BYTES: int = 10_000

#: 参与体检的扩展名 / 进行中产物 / 跳过目录 —— 与内容去重共用同一份定义
VIDEO_EXTENSIONS = _SHARED_VIDEO_EXTENSIONS
INCOMPLETE_SUFFIXES = _SHARED_INCOMPLETE_SUFFIXES
SKIP_DIR_NAMES = _SHARED_SKIP_DIR_NAMES


# ======================================================================
# 二进制定位
# ======================================================================

def _find_binary(name: str) -> Optional[str]:
    """定位 ffmpeg / ffprobe。

    顺序：
    1. 环境变量 ``WORKBUDDY_FFMPEG`` / ``WORKBUDDY_FFPROBE``
    2. 项目根目录（``ffmpeg.exe`` 与 ``ffprobe.exe`` 都在这里，实测 9.0.1）
    3. PyInstaller 解包目录 ``sys._MEIPASS``
    4. 可执行文件同级目录
    5. ``PATH``
    """
    env_key = f"WORKBUDDY_{name.upper()}"
    explicit = os.environ.get(env_key)
    if explicit and os.path.isfile(explicit):
        return explicit

    exe = f"{name}.exe" if os.name == "nt" else name
    candidates: List[Path] = []

    # 项目根 = 本文件上溯 4 层（modules/core/utils/x.py -> 根）
    try:
        candidates.append(Path(__file__).resolve().parents[3] / exe)
    except IndexError:
        pass

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / exe)

    if getattr(sys, "executable", None):
        candidates.append(Path(sys.executable).parent / exe)

    for c in candidates:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue

    found = shutil.which(name)
    if found:
        return found
    logger.warning("未找到 %s —— 视频完整性校验不可用（%s 可覆盖）", name, env_key)
    return None


_FFMPEG_PATH: Optional[str] = None
_FFPROBE_PATH: Optional[str] = None
_BINARY_PROBED = False


def get_ffmpeg() -> Optional[str]:
    """取 ffmpeg 路径（缓存，只探测一次）。"""
    global _FFMPEG_PATH, _FFPROBE_PATH, _BINARY_PROBED
    if not _BINARY_PROBED:
        _FFPROBE_PATH = _find_binary("ffprobe")
        _FFMPEG_PATH = _find_binary("ffmpeg")
        _BINARY_PROBED = True
    return _FFMPEG_PATH


def get_ffprobe() -> Optional[str]:
    """取 ffprobe 路径（缓存，只探测一次）。"""
    if not _BINARY_PROBED:
        get_ffmpeg()
    return _FFPROBE_PATH


# ======================================================================
# 文件锁检测（B15 同源问题）
# ======================================================================

def is_file_locked(path: Path) -> bool:
    """检测文件是否被别的进程独占（Windows）。非 Windows 恒返回 False。

    用 ``CreateFileW`` + ``dwShareMode=0``（不共享）尝试打开：
    - 打不开（``ERROR_SHARING_VIOLATION`` 32 / ``ERROR_LOCK_VIOLATION`` 33）→ 被占
    - 能打开 → 立即关闭

    **比"试着改名"安全**：本函数不改动文件、不改名、不写内容。
    用途：修复前先确认没有别的进程（yt-dlp / 播放器 / 索引服务）正在读它。
    """
    if os.name != "nt":
        return False

    GENERIC_READ = 0x80000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    #: 32 = ERROR_SHARING_VIOLATION, 33 = ERROR_LOCK_VIOLATION
    LOCK_ERRORS = (32, 33)

    try:
        # use_last_error=True 才能用 ctypes.get_last_error() 拿到真实错误码
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        CreateFileW = kernel32.CreateFileW
        CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p]
        CreateFileW.restype = ctypes.c_void_p
    except Exception:
        logger.debug("加载 kernel32 失败，跳过文件锁检测", exc_info=True)
        return False

    handle = CreateFileW(str(path), GENERIC_READ, 0, None,
                         OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        if err in LOCK_ERRORS:
            return True
        # 其它错误（不存在 / 无权限 / 路径过长）不当作"被锁"
        logger.debug("文件锁检测遇到非锁错误 %s: %s", err, path)
        return False

    try:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        logger.debug("关闭句柄失败: %s", path, exc_info=True)
    return False


# ======================================================================
# Tier 1: probe
# ======================================================================

@dataclass
class ProbeResult:
    """一个文件的体检结果。"""

    path: str
    ok: bool
    reason: str = ""
    size_bytes: int = 0
    duration_s: float = 0.0
    video_streams: int = 0
    audio_streams: int = 0
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0
    container: str = ""
    locked: bool = False
    elapsed_ms: int = 0
    #: 是否做过深度解码校验（None = 未做）
    decode_ok: Optional[bool] = None
    #: 深度校验发现的解码错误（首行）
    decode_error: str = ""

    def to_dict(self) -> Dict:
        return {
            "path": self.path,
            "ok": self.ok,
            "reason": self.reason,
            "size_bytes": self.size_bytes,
            "size_mb": round(self.size_bytes / 1024 / 1024, 2),
            "duration_s": round(self.duration_s, 2),
            "video_streams": self.video_streams,
            "audio_streams": self.audio_streams,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "resolution": f"{self.width}x{self.height}" if self.width else "",
            "container": self.container,
            "locked": self.locked,
            "elapsed_ms": self.elapsed_ms,
            "decode_ok": self.decode_ok,
            "decode_error": self.decode_error,
        }


def _decode_check(path: Path, timeout: int = 600) -> Tuple[bool, str, int]:
    """**深度校验**：把整个文件解码到 ``null``，看是否有解码错误。

    为什么必须有这一层（实测 2026-09-22）：
    ``ffprobe`` **只看容器元数据**。一个 ``moov`` 在文件头、但**尾部被截断**的 mp4
    （= HLS 下载中途断流的典型产物）会被 ``ffprobe`` 报成
    ``duration=6.0``、``video_streams=1`` —— **完全"正常"**，实际播到一半就卡死。
    只有真正解码一遍才能发现（实测该文件会产生 6 条
    ``Invalid NAL unit size`` 错误）。

    代价：等于完整解码一次，慢（大致 = 视频时长 / 解码速度）。
    所以默认关闭，由 ``--deep`` 显式打开。

    Returns:
        (是否干净, 错误首行, 错误行数)
    """
    ff = get_ffmpeg()
    if not ff:
        return False, "ffmpeg not found", 0
    cmd = [ff, "-v", "error", "-i", str(path), "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "decode check timeout", 0
    except Exception as e:
        return False, f"decode check error: {e}", 0

    err = (proc.stderr or b"").decode(errors="replace").strip()
    lines = [x for x in err.splitlines() if x.strip()]
    if proc.returncode == 0 and not lines:
        return True, "", 0
    return False, (lines[0][:160] if lines else f"exit {proc.returncode}"), len(lines)


def check_playable(path: str | Path, timeout: int = 60,
                   check_lock: bool = False,
                   deep: bool = False,
                   deep_timeout: int = 600) -> ProbeResult:
    """用 ``ffprobe`` 判定文件是否「完整可播」；``deep=True`` 时再解码校验一遍。

    判坏条件：
    * 文件不存在 / 读不到
    * 体积 < :data:`MIN_PLAUSIBLE_BYTES`（只有文件头）
    * ``ffprobe`` 非零退出（容器解析失败，典型：``moov atom not found``）
    * ``duration <= 0``（截断 / 缺 moov / 裸流没有容器时长）
    * 无视频流
    * 时长 <0.5s 且体积 <50 KB（近乎空文件）
    * ``deep=True`` 且**解码过程中报错**（抓「moov 在、尾部被截断」）

    Args:
        path: 目标文件
        timeout: ffprobe 超时（秒）
        check_lock: 是否顺带检测文件占用（会多一次句柄操作）
        deep: 是否做深度解码校验（慢，但能抓到 ffprobe 看不出的截断）
        deep_timeout: 深度校验超时（秒）

    Returns:
        ProbeResult
    """
    started = time.time()
    p = Path(path)
    res = ProbeResult(path=str(p), ok=False)

    probe = get_ffprobe()
    if not probe:
        res.reason = "ffprobe not found"
        return res

    if not p.exists():
        res.reason = "file missing"
        return res
    try:
        res.size_bytes = p.stat().st_size
    except OSError as e:
        res.reason = f"stat failed: {e}"
        return res

    if check_lock:
        try:
            res.locked = is_file_locked(p)
        except Exception:
            logger.debug("文件锁检测失败: %s", p, exc_info=True)

    if res.size_bytes < MIN_PLAUSIBLE_BYTES:
        res.reason = f"too small ({res.size_bytes} B)"
        res.elapsed_ms = int((time.time() - started) * 1000)
        return res

    cmd = [probe, "-v", "error",
           "-show_streams", "-show_format",
           "-of", "json",
           "-analyzeduration", "5000000",
           "-probesize", "5000000",
           str(p)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        res.reason = "ffprobe timeout"
        res.elapsed_ms = int((time.time() - started) * 1000)
        return res
    except Exception as e:
        res.reason = f"ffprobe error: {e}"
        res.elapsed_ms = int((time.time() - started) * 1000)
        return res

    if proc.returncode != 0:
        err = (proc.stderr or b"").decode(errors="replace")
        one_line = " | ".join(x.strip() for x in err.strip().splitlines() if x.strip())[-160:]
        res.reason = f"ffprobe exit {proc.returncode}: {one_line}"
        res.elapsed_ms = int((time.time() - started) * 1000)
        return res

    try:
        data = json.loads((proc.stdout or b"").decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        res.reason = f"ffprobe JSON parse: {e}"
        res.elapsed_ms = int((time.time() - started) * 1000)
        return res

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    vstreams = [s for s in streams if s.get("codec_type") == "video"]
    astreams = [s for s in streams if s.get("codec_type") == "audio"]

    res.video_streams = len(vstreams)
    res.audio_streams = len(astreams)
    res.container = fmt.get("format_name", "") or ""
    if vstreams:
        res.video_codec = vstreams[0].get("codec_name", "") or ""
        try:
            res.width = int(vstreams[0].get("width") or 0)
            res.height = int(vstreams[0].get("height") or 0)
        except (TypeError, ValueError):
            pass
    if astreams:
        res.audio_codec = astreams[0].get("codec_name", "") or ""

    try:
        res.duration_s = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        res.duration_s = 0.0

    # 判定
    if res.duration_s <= 0:
        res.reason = "zero duration (truncated / missing moov / raw stream)"
    elif not vstreams:
        res.reason = "no video stream"
    elif res.duration_s < 0.5 and res.size_bytes < 50_000:
        res.reason = f"near-empty ({res.duration_s:.1f}s, {res.size_bytes} B)"
    else:
        res.ok = True
        res.reason = "ok"

    # Tier 1.5：深度解码校验（只在容器层判定为"好"时才需要补这一刀）
    if res.ok and deep:
        clean, derr, nlines = _decode_check(p, timeout=deep_timeout)
        res.decode_ok = clean
        if not clean:
            res.ok = False
            res.decode_error = derr
            res.reason = f"decode errors ({nlines}): {derr}"

    res.elapsed_ms = int((time.time() - started) * 1000)
    return res


# ======================================================================
# Tier 2 / 3: repair
# ======================================================================

@dataclass
class RepairResult:
    """一次修复的结果。"""

    path: str
    action: str = "failed"   # ok / remuxed / reencoded / would-* / skipped-* / failed
    reason: str = ""
    before_bytes: int = 0
    after_bytes: int = 0
    duration_s: float = 0.0
    elapsed_s: float = 0.0
    backup: str = ""

    def to_dict(self) -> Dict:
        return {
            "path": self.path,
            "action": self.action,
            "reason": self.reason,
            "before_bytes": self.before_bytes,
            "after_bytes": self.after_bytes,
            "duration_s": round(self.duration_s, 2),
            "elapsed_s": self.elapsed_s,
            "backup": self.backup,
        }


def _run_ffmpeg(cmd: List[str], timeout: int = 600) -> Tuple[int, str]:
    """跑一次 ffmpeg，返回 ``(returncode, 单行化的 stderr)``。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -1, str(e)
    err = (proc.stderr or b"").decode(errors="replace")
    one_line = " | ".join(x.strip() for x in err.strip().splitlines() if x.strip())[-220:]
    return proc.returncode, one_line


def _remux(src: Path, dst: Path, timeout: int = 300) -> Tuple[int, str]:
    """Tier 2：流拷贝重封装（无画质损失）。

    ``-fflags +genpts`` 重新生成缺失时间戳；
    ``-avoid_negative_ts make_zero`` 修音视频不同步；
    ``-movflags +faststart`` 把 moov 前置（流式播放必需）。
    """
    ff = get_ffmpeg()
    if not ff:
        return -1, "ffmpeg not found"
    cmd = [ff, "-y", "-fflags", "+genpts", "-i", str(src),
           "-map", "0", "-c", "copy",
           "-avoid_negative_ts", "make_zero",
           "-movflags", "+faststart",
           str(dst)]
    return _run_ffmpeg(cmd, timeout=timeout)


def _reencode(src: Path, dst: Path, timeout: int = 900) -> Tuple[int, str]:
    """Tier 3：全量重编码（慢，但能吞掉解码错误）。"""
    ff = get_ffmpeg()
    if not ff:
        return -1, "ffmpeg not found"
    cmd = [ff, "-y", "-err_detect", "ignore_err", "-i", str(src),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart",
           str(dst)]
    return _run_ffmpeg(cmd, timeout=timeout)


def repair_file(path: str | Path,
                confirm: bool = False,
                keep_backup: bool = True,
                allow_incomplete: bool = False,
                skip_if_locked: bool = True,
                remux_timeout: int = 300,
                reencode_timeout: int = 900,
                deep: bool = False,
                deep_timeout: int = 600) -> RepairResult:
    """对一个文件跑三级修复流水线。

    ⚠️ **破坏性操作**。``confirm=False``（默认）时**只出计划、不落地**。

    action 取值::

        ok              已经能播，未做任何事
        would-remux     计划用 remux 修（confirm=False）
        would-reencode  计划用 re-encode 修（confirm=False）
        remuxed         已用 remux 修好
        reencoded       已用 re-encode 修好
        skipped-locked  文件被其它进程占用，跳过
        skipped-incomplete 进行中的下载产物，跳过
        failed          三级都失败，**原件保留**

    ⭐ 替换顺序（相对参考实现的安全性改进）：
    参考实现先 ``unlink`` 原件再 ``rename`` 修复件 —— rename 一失败原件就没了。
    本模块用 ``os.replace``（同卷原子替换）：**失败时原件原封不动**。

    Args:
        path: 目标文件
        confirm: **必须显式传 True 才会真的修改文件**
        keep_backup: 替换前先留一份 ``.bak``（默认开）
        allow_incomplete: 允许处理 ``.part`` / ``-FragN`` 等进行中产物（默认否）
        skip_if_locked: 文件被占用时跳过（默认开）
        remux_timeout: remux 超时（秒）
        reencode_timeout: re-encode 超时（秒）
        deep: 是否做深度解码校验（能抓「moov 在、尾部被截断」）
        deep_timeout: 深度校验超时（秒）

    Returns:
        RepairResult
    """
    started = time.time()
    p = Path(path)
    res = RepairResult(path=str(p))

    if not p.exists():
        res.action = "failed"
        res.reason = "file not found"
        return res
    try:
        res.before_bytes = p.stat().st_size
    except OSError as e:
        res.action = "failed"
        res.reason = f"stat failed: {e}"
        return res

    # 进行中的产物默认不碰（内容还在变）。
    # ⚠️ 必须用共用判定：`endswith(".part")` 会漏掉 yt-dlp 的分片名
    #    `xxx.mp4.part-Frag5`（结尾是 -Frag5），那个也是进行中的产物。
    if is_incomplete_download(p):
        if not allow_incomplete:
            res.action = "skipped-incomplete"
            res.reason = "进行中的下载产物（用 allow_incomplete=True 强制处理）"
            return res

    # Tier 1（deep 时含解码校验）
    probe = check_playable(p, deep=deep, deep_timeout=deep_timeout)
    res.duration_s = probe.duration_s
    if probe.ok:
        res.action = "ok"
        res.reason = "playable"
        res.after_bytes = res.before_bytes
        res.elapsed_s = round(time.time() - started, 2)
        return res

    if skip_if_locked:
        try:
            if is_file_locked(p):
                res.action = "skipped-locked"
                res.reason = "文件被其它进程占用（可能是 yt-dlp / 播放器 / 索引服务）"
                return res
        except Exception:
            logger.debug("修复前锁检测失败: %s", p, exc_info=True)

    out_ext = ".mp4"
    fixed = p.with_name(p.stem + ".repaired" + out_ext)

    def _cleanup_fixed() -> None:
        try:
            if fixed.exists():
                fixed.unlink()
        except OSError:
            logger.debug("清理修复中间文件失败: %s", fixed, exc_info=True)

    def _commit(action: str, meta: ProbeResult) -> RepairResult:
        """把验证通过的修复件替换回去。"""
        backup = ""
        try:
            if keep_backup:
                backup_path = p.with_name(p.name + ".bak")
                try:
                    if backup_path.exists():
                        backup_path.unlink()
                    shutil.copy2(p, backup_path)
                    backup = str(backup_path)
                except OSError:
                    logger.warning("留备份失败，继续（原件将在替换时被覆盖）: %s", p,
                                   exc_info=True)
            # ⭐ os.replace：失败时原件仍在
            os.replace(fixed, p)
        except OSError as e:
            _cleanup_fixed()
            res.action = "failed"
            res.reason = f"{action} 成功但替换失败（原件已保留）: {e}"
            return res

        try:
            res.after_bytes = p.stat().st_size
        except OSError:
            res.after_bytes = 0
        res.action = action
        res.duration_s = meta.duration_s
        res.backup = backup
        res.reason = f"fixed via {action}"
        res.elapsed_s = round(time.time() - started, 2)
        return res

    # 未确认 → 只报告计划
    if not confirm:
        res.action = "would-remux"
        res.reason = f"计划 remux 修复（原因: {probe.reason}）—— 需要 confirm=True 才会执行"
        res.elapsed_s = round(time.time() - started, 2)
        return res

    # Tier 2: remux
    logger.info("[修复] %s: 尝试 remux（原因: %s）", p.name, probe.reason)
    _cleanup_fixed()
    rc, err = _remux(p, fixed, timeout=remux_timeout)
    if rc == 0 and fixed.exists():
        ok2 = check_playable(fixed, deep=deep, deep_timeout=deep_timeout)
        if ok2.ok:
            logger.info("[修复] %s: remux 成功，时长 %.1fs", p.name, ok2.duration_s)
            return _commit("remuxed", ok2)
        logger.info("[修复] %s: remux 产物仍不合格（%s）", p.name, ok2.reason)
    else:
        logger.info("[修复] %s: remux 失败 rc=%s %s", p.name, rc, err[:120])
    _cleanup_fixed()

    # Tier 3: re-encode
    logger.info("[修复] %s: 尝试 re-encode", p.name)
    rc, err = _reencode(p, fixed, timeout=reencode_timeout)
    if rc == 0 and fixed.exists():
        ok3 = check_playable(fixed, deep=deep, deep_timeout=deep_timeout)
        if ok3.ok:
            logger.info("[修复] %s: re-encode 成功，时长 %.1fs", p.name, ok3.duration_s)
            return _commit("reencoded", ok3)
        logger.info("[修复] %s: re-encode 产物仍不合格（%s）", p.name, ok3.reason)
    else:
        logger.info("[修复] %s: re-encode 失败 rc=%s %s", p.name, rc, err[:120])
    _cleanup_fixed()

    res.action = "failed"
    res.reason = f"三级修复均失败（原始原因: {probe.reason}）—— **原件已保留**"
    res.elapsed_s = round(time.time() - started, 2)
    logger.warning("[修复] %s: %s", p.name, res.reason)
    return res


# ======================================================================
# 批量体检
# ======================================================================

def _iter_files(root: Path, recursive: bool,
                include_incomplete: bool = False) -> List[Path]:
    """列出候选文件。"""
    out: List[Path] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES
                           and not d.startswith(".")]
            for fn in filenames:
                p = Path(dirpath) / fn
                if is_incomplete_download(fn):
                    if include_incomplete:
                        out.append(p)
                    continue
                if p.suffix.lower() in VIDEO_EXTENSIONS:
                    out.append(p)
    else:
        try:
            for p in root.iterdir():
                if not p.is_file():
                    continue
                if is_incomplete_download(p.name):
                    if include_incomplete:
                        out.append(p)
                elif p.suffix.lower() in VIDEO_EXTENSIONS:
                    out.append(p)
        except OSError:
            logger.debug("列目录失败: %s", root, exc_info=True)
    return out


def sweep_folder(root: str | Path,
                 recursive: bool = True,
                 include_incomplete: bool = False,
                 min_size_bytes: int = 0,
                 only_broken: bool = False,
                 repair: bool = False,
                 confirm: bool = False,
                 keep_backup: bool = True,
                 limit: int = 0,
                 skip_recent_seconds: int = 0,
                 progress_every: int = 200,
                 deep: bool = False,
                 deep_timeout: int = 600) -> Dict:
    """批量体检（默认只读）。

    Args:
        root: 根目录
        recursive: 是否递归
        include_incomplete: 是否把 ``.part`` 等中间产物也纳入（只读探测无害）
        min_size_bytes: 小于此大小跳过
        only_broken: 报告里只保留坏文件
        repair: 是否尝试修复（仍需 ``confirm=True`` 才真正落地）
        confirm: 修复的确认开关
        keep_backup: 修复时留 ``.bak``
        limit: >0 时最多处理这么多文件（试跑用）
        skip_recent_seconds: 跳过最近 N 秒内被修改的文件（避免碰正在下载的）
        progress_every: 每 N 个文件打一条进度
        deep: 是否做深度解码校验（慢，但能抓到 ffprobe 看不出的截断）
        deep_timeout: 深度校验超时（秒）

    Returns:
        dict: 报告
    """
    started = datetime.now()
    root_p = Path(root)
    report: Dict = {
        "generated_at": started.isoformat(timespec="seconds"),
        "mode": ("repair" if (repair and confirm) else
                 "repair-plan" if repair else "check"),
        "root": str(root_p),
        "recursive": recursive,
        "include_incomplete": include_incomplete,
        "results": [],
        "broken": [],
        "repaired": [],
        "stats": {},
    }

    if not root_p.exists():
        report["stats"] = {"error": "root not found"}
        logger.error("体检根目录不存在: %s", root_p)
        return report

    files = _iter_files(root_p, recursive, include_incomplete)
    if limit:
        files = files[:limit]
    logger.info("视频体检：%s 下找到 %d 个候选文件", root_p, len(files))

    checked = 0
    broken = 0
    repaired = 0
    skipped = 0
    now = time.time()

    for p in files:
        try:
            if min_size_bytes and p.stat().st_size < min_size_bytes:
                skipped += 1
                continue
        except OSError:
            skipped += 1
            continue

        if skip_recent_seconds:
            try:
                if now - p.stat().st_mtime < skip_recent_seconds:
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue

        probe = check_playable(p, deep=deep, deep_timeout=deep_timeout)
        checked += 1
        if checked % progress_every == 0:
            logger.info("体检进度：%d/%d（发现坏文件 %d）", checked, len(files), broken)

        if not probe.ok:
            broken += 1
            logger.warning("损坏文件: %s —— %s", p, probe.reason)
            if repair:
                r = repair_file(p, confirm=confirm, keep_backup=keep_backup,
                                allow_incomplete=include_incomplete,
                                deep=deep, deep_timeout=deep_timeout)
                report["repaired"].append(r.to_dict())
                if r.action in ("remuxed", "reencoded"):
                    repaired += 1
                    probe = check_playable(p, deep=deep,      # 修好后重新探测
                                           deep_timeout=deep_timeout)
                    if probe.ok:
                        broken -= 1

        if (not only_broken) or (not probe.ok):
            report["results"].append(probe.to_dict())
        if not probe.ok:
            report["broken"].append(probe.to_dict())

    report["stats"] = {
        "candidates": len(files),
        "checked": checked,
        "skipped": skipped,
        "broken": broken,
        "repaired": repaired,
        "elapsed_seconds": round((datetime.now() - started).total_seconds(), 2),
        "ffmpeg": get_ffmpeg() or "",
        "ffprobe": get_ffprobe() or "",
    }
    report["deep"] = deep
    report["note"] = ("体检为只读操作。修复是破坏性操作，需 repair=True 且 confirm=True；"
                      "本模块不会被任何自动流程调用。")
    return report


def build_report(*roots: str | Path, recursive: bool = True,
                 include_incomplete: bool = False,
                 min_size_bytes: int = 0, deep: bool = False,
                 **kwargs) -> Dict:
    """对多个根目录体检并合并成一个报告。"""
    merged: Dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        # ⚠️ 必须跟 sweep_folder 的取值一致，不能硬编码 "check"，
        #    否则 repair=True 合并出来的报告会谎报为只读体检
        "mode": ("repair" if kwargs.get("repair") and kwargs.get("confirm")
                 else "repair-plan" if kwargs.get("repair") else "check"),
        "roots": [str(r) for r in roots],
        "results": [],
        "broken": [],
        "stats": {"candidates": 0, "checked": 0, "skipped": 0,
                  "broken": 0, "repaired": 0, "elapsed_seconds": 0.0},
    }
    t0 = time.time()
    for r in roots:
        sub = sweep_folder(r, recursive=recursive,
                           include_incomplete=include_incomplete,
                           min_size_bytes=min_size_bytes,
                           deep=deep, **kwargs)
        merged["results"].extend(sub.get("results", []))
        merged["broken"].extend(sub.get("broken", []))
        for k in ("candidates", "checked", "skipped", "broken", "repaired"):
            merged["stats"][k] += sub.get("stats", {}).get(k, 0)
    merged["stats"]["elapsed_seconds"] = round(time.time() - t0, 2)
    merged["stats"]["ffmpeg"] = get_ffmpeg() or ""
    merged["stats"]["ffprobe"] = get_ffprobe() or ""
    merged["deep"] = deep
    merged["note"] = ("体检为只读操作。修复是破坏性操作，需 repair=True 且 confirm=True；"
                      "本模块不会被任何自动流程调用。")
    return merged


def format_report_text(report: Dict, max_items: int = 50) -> str:
    """把体检报告渲染成人类可读文本（**损坏文件按体积降序**）。"""
    s = report.get("stats", {}) or {}
    lines: List[str] = []
    add = lines.append

    add("=" * 78)
    add(f"视频完整性体检报告（模式: {report.get('mode', 'check')} —— 只读）")
    add("=" * 78)
    add(f"生成时间   : {report.get('generated_at')}")
    roots = report.get("roots") or ([report.get("root")] if report.get("root") else [])
    add(f"扫描根目录 : {', '.join(str(r) for r in roots)}")
    add(f"ffmpeg     : {s.get('ffmpeg') or '未找到'}")
    add(f"ffprobe    : {s.get('ffprobe') or '未找到'}")
    add(f"深度校验   : {'开启' if report.get('deep') else '关闭（--deep 开启）'}")
    add("-" * 78)
    add(f"候选文件   : {s.get('candidates', 0)}")
    add(f"已体检     : {s.get('checked', 0)}")
    add(f"跳过       : {s.get('skipped', 0)}")
    add(f"★ 损坏     : {s.get('broken', 0)}")
    add(f"已修复     : {s.get('repaired', 0)}")
    add(f"耗时       : {s.get('elapsed_seconds', 0)}s")
    add("-" * 78)

    broken = sorted(report.get("broken", []),
                    key=lambda x: x.get("size_bytes", 0), reverse=True)
    if not broken:
        add("没有发现损坏文件。")
    else:
        add(f"损坏文件（按体积降序，最多显示 {max_items} 个）：")
        for i, b in enumerate(broken[:max_items], 1):
            add(f"[{i}] {b.get('size_mb')} MB  {b.get('path')}")
            add(f"      原因: {b.get('reason')}")
            extra = []
            if b.get("duration_s"):
                extra.append(f"时长 {b['duration_s']}s")
            if b.get("resolution"):
                extra.append(b["resolution"])
            if b.get("video_codec"):
                extra.append(f"v:{b['video_codec']}")
            if b.get("locked"):
                extra.append("⚠ 被占用")
            if extra:
                add(f"      {' | '.join(extra)}")
        if len(broken) > max_items:
            add(f"... 还有 {len(broken) - max_items} 个（用 --json 拿全量）")

    if report.get("repaired"):
        add("-" * 78)
        add("修复动作：")
        for r in report["repaired"]:
            add(f"  {r.get('action'):16s} {r.get('path')}")
            if r.get("backup"):
                add(f"                 备份: {r.get('backup')}")

    add("=" * 78)
    add("⚠️ 体检只读。修复需 --repair --confirm（破坏性）。")
    add("=" * 78)
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。默认只读体检；修复需 ``--repair --confirm`` 双开关。"""
    parser = argparse.ArgumentParser(
        description="视频完整性体检（默认只读；修复需 --repair --confirm）")
    parser.add_argument("--root", action="append", default=[],
                        help="扫描根目录，可重复")
    parser.add_argument("--no-recursive", action="store_true", help="不递归")
    parser.add_argument("--include-incomplete", action="store_true",
                        help="把 .part / -FragN 等中间产物也纳入体检（探测为只读）")
    parser.add_argument("--min-size-mb", type=float, default=0.0,
                        help="小于此大小的文件跳过")
    parser.add_argument("--only-broken", action="store_true",
                        help="结果里只保留坏文件（省内存）")
    parser.add_argument("--repair", action="store_true",
                        help="尝试修复（仍需 --confirm 才真正落地）")
    parser.add_argument("--confirm", action="store_true",
                        help="⚠️ 确认执行破坏性修复")
    parser.add_argument("--no-backup", action="store_true",
                        help="修复时不留 .bak（不建议）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个文件（试跑）")
    parser.add_argument("--skip-recent-seconds", type=int, default=0,
                        help="跳过最近 N 秒内修改过的文件（避免碰正在下载的）")
    parser.add_argument("--show", type=int, default=50, help="文本报告最多打印多少条")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--deep", action="store_true",
                        help="深度解码校验（慢；唯一能抓「moov 在但尾部截断」的手段）")
    parser.add_argument("--deep-timeout", type=int, default=600,
                        help="深度校验超时（秒）")
    parser.add_argument("--verbose", action="store_true", help="DEBUG 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    if not args.root:
        print("请用 --root 指定至少一个扫描根目录。")
        return 2
    if args.repair and not args.confirm:
        print("⚠️ --repair 需要同时给出 --confirm 才会真正修改文件。")
        print("   不加 --confirm 时只输出修复计划（安全预览）。")

    report = build_report(
        *args.root,
        recursive=not args.no_recursive,
        include_incomplete=args.include_incomplete,
        min_size_bytes=int(args.min_size_mb * 1024 * 1024),
        only_broken=args.only_broken,
        repair=args.repair,
        confirm=args.confirm,
        keep_backup=not args.no_backup,
        limit=args.limit,
        skip_recent_seconds=args.skip_recent_seconds,
        deep=args.deep,
        deep_timeout=args.deep_timeout,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report_text(report, max_items=args.show))
    return 0


if __name__ == "__main__":
    sys.exit(main())
