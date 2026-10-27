# -*- coding: utf-8 -*-
"""媒体库自检：扫描已下载视频、跳过完整文件、识别损坏/部分文件。

核心规则:
  - 通过 ffprobe 验证视频是否完整可播放（返回正常时长 > 0）
  - 完整文件直接标记为 DONE，避免重复下载
  - 存在但损坏/时长为 0 的文件视为“断续残留”，下载前会被删除重下
  - 未下载集正常进入队列

ffmpeg 已升级 9.1，本模块使用同目录的 ffprobe 做校验。
"""

import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config
from .utils import build_output_path

# 文件校验缓存：避免同一文件被反复 ffprobe。
# 一次 ffprobe 约 0.3s，若在 UI 线程对 36 集逐个校验会阻塞 10s 以上，
# 因此必须缓存；key=文件绝对路径, value=(mtime, size, 是否有效)。
_validate_cache: Dict[str, Tuple[float, int, bool]] = {}
_cache_lock = threading.Lock()


def _startupinfo():
    """Windows 下隐藏控制台窗口。"""
    if sys.platform != "win32":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return info


def invalidate(path: "Path | str") -> None:
    """使某个文件的校验缓存失效（下载完成/删除后调用）。"""
    key = str(Path(path))
    with _cache_lock:
        _validate_cache.pop(key, None)


def _probe_duration_ok(p: Path) -> bool:
    """真正调用 ffprobe 判断时长 > 0。"""
    if not config.FFPROBE_PATH.is_file():
        # ffprobe 缺失时只能按存在且非空判断（兜底）
        return True

    cmd = [
        str(config.FFPROBE_PATH),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(p),
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=30,
            startupinfo=_startupinfo(),
        )
        if proc.returncode != 0:
            return False
        first = proc.stdout.strip().splitlines()
        if not first:
            return False
        duration = float(first[0])
        return duration > 0
    except Exception:                       # noqa: BLE001 - 校验失败不阻塞
        return False


def validate_video(path: "Path | str") -> bool:
    """用 ffprobe 验证视频文件是否完整可播放（带 mtime+size 缓存）。

    返回 True: 文件存在、ffprobe 退出码 0、且 duration > 0。
    任一失败返回 False（含 ffprobe 不存在时退化为"文件存在且大小>0"）。

    性能：单次 ffprobe 约 0.3s，同一文件按 (mtime, size) 命中缓存后
    直接返回，避免在 UI 线程对整部剧重复校验造成卡死。
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return False
    if not p.is_file() or st.st_size == 0:
        return False

    key = str(p)
    mtime, size = st.st_mtime, st.st_size
    with _cache_lock:
        hit = _validate_cache.get(key)
    if hit is not None and hit[0] == mtime and hit[1] == size:
        return hit[2]

    ok = _probe_duration_ok(p)
    with _cache_lock:
        _validate_cache[key] = (mtime, size, ok)
    return ok


def _resolve_category(title: str, category: str) -> str:
    """未显式给出分类时，查归属记忆（换脸/魔改 落在独立子目录，路径不同）。"""
    if category:
        return category
    try:
        from . import category_rules
        cat = category_rules.of(config.ACTIVE_SITE_ID, title=title)
        if not cat:
            cat = category_rules.infer_category_from_title(title)
        return cat
    except Exception:                       # noqa: BLE001 - 查不到就按常规目录
        return ""


def video_path(title: str, ep: int, download_dir: Optional[Path] = None,
               category: str = "") -> Path:
    """返回某集视频的预期路径（默认当前站点下载目录）。

    category 为空时会查一次归属记忆，保证「换脸/魔改」这类独立子目录里的
    已下载文件也能被认出来（否则会被当成没下过、反复重下）。
    """
    cat = _resolve_category(title, category)
    return build_output_path(download_dir or config.DOWNLOAD_SITE_DIR, title, ep,
                             category=cat)


def is_done(title: str, ep: int, download_dir: Optional[Path] = None,
            category: str = "") -> bool:
    """判断某集是否已完整下载（可能触发 ffprobe，勿在 UI 线程对整剧调用）。"""
    return validate_video(video_path(title, ep, download_dir, category))


def peek_done(title: str, ep: int, download_dir: Optional[Path] = None,
              category: str = "") -> bool:
    """仅查缓存判断是否已下载，绝不触发 ffprobe；未知一律视为未下载。

    用于 UI 线程的快速判定：命中缓存的集直接跳过，未命中的交给下载线程
    在真正下载前校验（run_download 会跳过完整文件），从而杜绝界面卡死。
    """
    p = video_path(title, ep, download_dir, category)
    try:
        st = p.stat()
    except OSError:
        return False
    if st.st_size == 0:
        return False
    with _cache_lock:
        hit = _validate_cache.get(str(p))
    if hit is not None and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    return False


def scan_drama(title: str, eps: List[int],
               download_dir: Optional[Path] = None) -> Dict[int, Path]:
    """扫描一部剧的指定集数，返回 {集号: 路径} 的已下载完整集。"""
    result: Dict[int, Path] = {}
    for ep in eps:
        p = video_path(title, ep, download_dir)
        if validate_video(p):
            result[ep] = p
    return result


def remove_if_broken(path: "Path | str") -> bool:
    """如果文件损坏/不完整则删除，返回是否执行了删除。"""
    p = Path(path)
    if not p.is_file():
        return False
    if validate_video(p):
        return False
    try:
        p.unlink()
        invalidate(p)
        return True
    except Exception:                       # noqa: BLE001
        return False


def status_text(title: str, ep: int,
                download_dir: Optional[Path] = None,
                category: str = "") -> str:
    """返回某集在磁盘上的状态文本（用于 UI 提示）。"""
    p = video_path(title, ep, download_dir, category)
    if validate_video(p):
        return f"已下载 ({p.stat().st_size // (1024*1024)} MB)"
    if p.is_file():
        return "文件不完整（将重新下载）"
    return "未下载"
