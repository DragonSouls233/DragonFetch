# -*- coding: utf-8 -*-
"""下载引擎：调用 yt-dlp 拉取 m3u8 并合并为 mp4。

设计要点:
  - 核心层不依赖 UI，只通过回调上报进度，方便在 QThread / 线程池中运行
  - AES-128 加密的 HLS 由 yt-dlp + ffmpeg 自动解密合并，无需手工处理
  - auth_key 有时效，因此播放地址必须在下载前即时获取（由调用方保证）
  - 支持取消：通过 task.proc 暴露子进程句柄

落盘策略（借鉴 nexus-media 的 staging 思想 + Ghost-Downloader-3 的加速思路）:
  - 片段下载与音视频合并全部在「暂存目录」(config.TEMP_DIR) 完成，该目录应位于
    高速 SSD（项目本地磁盘），避免机械硬盘上的随机 IO 密集合并拖慢速度
  - 合并完成后，把最终 MP4 移动到「下载目录」(config.DOWNLOAD_SITE_DIR，通常为 HDD)
  - 失败 / 取消时清理暂存残留，避免占用空间
  - 加速：通过 --concurrent-fragments 并行拉取分片（已由 config 接入），并支持
    --rate-limit 限速（config.DOWNLOAD_RATE_LIMIT），避免占满带宽
"""

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from . import applog, config
from .library_scan import invalidate, remove_if_broken, validate_video
from .models import DownloadTask, TaskStatus
from .utils import build_output_path, normalize_title, sanitize_filename

# 进度行示例（实测）:
#   [download]   0.0% of ~ 310.24MiB at    3.23KiB/s ETA Unknown (frag 0/165)
#   [download]  45.2% of ~145.00MiB at  3.20MiB/s ETA 00:25
#   [download] 100% of 145.00MiB in 00:42
#
# 注意: 波浪号与数字之间可能有空格（ "~ 310.24MiB" ），不能写成 "~?[\d.]"
_PROGRESS_RE = re.compile(
    r"\[download\]\s+"
    r"(?P<pct>[\d.]+)%\s+of\s+"
    r"(?:~\s*)?(?P<size>[\d.]+\s*[KMGT]?i?B)"
    r"(?:\s+at\s+(?P<speed>[\d.]+\s*[KMGT]?i?B/s|Unknown))?"
    r"(?:\s+ETA\s+(?P<eta>[\d:]+|Unknown))?"
    r"(?:\s+\(frag\s+(?P<frag>\d+/\d+)\))?"
)

# 合并/转封装阶段
_MERGING_HINTS = ("[Merger]", "[Fixup", "[ffmpeg]", "Merging formats")

# 进度回调的最小间隔 / 最小百分比变化（节流用，界面本就 300ms 刷一次）
_PROGRESS_MIN_INTERVAL = 0.3
_PROGRESS_MIN_PCT = 0.5

# yt-dlp 失败时的关键行特征：用于把「真实原因」留存进日志与 task.error
_ERROR_HINTS = (
    "ERROR", "Unable to", "HTTP Error", "Forbidden", "404", "403",
    "timed out", "Connection", "SSL", "no video", "Unsupported",
)


def _extract_error(tail) -> str:
    """从 yt-dlp 输出尾部提炼失败原因（优先 ERROR 行；最多 3 条 / 500 字）。

    旧版只记录「退出码 N」，真实报错（403/超时/m3u8 解析失败…）全被丢弃，
    导致失败无法定位。这里把关键行带回，写进 task.error 与日志文件。
    """
    lines = [s.strip() for s in tail if s and s.strip()]
    hits = [s for s in lines if any(h in s for h in _ERROR_HINTS)]
    picked = hits[-3:] if hits else lines[-3:]
    return " | ".join(dict.fromkeys(picked))[:500]


def parse_progress(line: str) -> Optional[dict]:
    """解析 yt-dlp 的 --newline 进度行，失败返回 None。

    返回 dict: percent / size / speed / eta / frag
    其中 speed、eta 可能是 "Unknown"，frag 形如 "12/165"。
    """
    m = _PROGRESS_RE.search(line)
    if not m:
        return None

    def _clean(v, placeholder="Unknown"):
        v = (v or "").strip()
        return "" if v == placeholder else v

    return {
        "percent": float(m.group("pct")),
        "size": (m.group("size") or "").strip(),
        "speed": _clean(m.group("speed")),
        "eta": _clean(m.group("eta")),
        "frag": (m.group("frag") or "").strip(),
    }


def build_command(task: DownloadTask, m3u8_url: str, out_path) -> list:
    """构造 yt-dlp 命令行。

    out_path 此时指向「暂存目录」(SSD)：片段下载与合并都在该目录完成。
    合并完成后由调用方负责把文件移动到下载目录（HDD）。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(config.YTDLP_PATH),
        *config.YTDLP_COMMON_ARGS,
        # 暂存目录（SSD）：分片下载与中间文件都落在高速盘，避免 HDD 随机 IO 密集
        "-P", f"temp:{config.TEMP_DIR}",
        "--ffmpeg-location", str(config.BIN_DIR),
        "-o", str(out_path),
    ]
    # 可选代理（设置面板或环境变量 HG_PROXY 配置）
    if config.PROXY_URL:
        cmd += ["--proxy", config.PROXY_URL]
    # 限速（KB/s）：0 / 空 = 不限速
    if config.DOWNLOAD_RATE_LIMIT:
        cmd += ["--rate-limit", f"{config.DOWNLOAD_RATE_LIMIT}K"]
    cmd.append(m3u8_url)
    return cmd


def _cleanup_staging(temp_out_path: Path) -> None:
    """删除暂存主文件及其所有派生物（*-FragN / *.part / *.ytdl 等）。

    失败 / 取消时回收 SSD 暂存空间；成功时主文件已 move 走，这里只清残留。
    """
    prefix = temp_out_path.name
    try:
        for child in temp_out_path.parent.iterdir():
            if child.name.startswith(prefix):
                try:
                    child.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def cleanup_orphaned_staging(max_age_min: int = 10) -> int:
    """启动时回收暂存目录（SSD）里的孤儿文件，返回删除数量。

    正常流程下每个任务的 finally 都会清掉自己的暂存文件；但进程被强杀 /
    崩溃时 finally 不会执行，会遗留 *.mp4 / *-FragN / *.ytdl（曾见 119MB
    整集残留）。启动时不可能有下载在进行，正好安全回收。

    max_age_min：只清「最后修改早于 N 分钟」的文件，避免误删同时运行的
    另一实例正在下载的暂存文件。
    """
    import time
    root = config.TEMP_DIR
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_min * 60
    n = 0
    freed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for p in entries:
        try:
            if not p.is_file():
                continue
            st = p.stat()
            if st.st_mtime < cutoff:
                freed += st.st_size
                p.unlink()
                n += 1
        except OSError:
            pass
    if n:
        applog.info("启动清理暂存孤儿: %d 个文件 / %.1f MB", n, freed / 1024 / 1024)
    return n


def run_download(task: DownloadTask,
                 m3u8_url: str,
                 on_progress: Callable[[DownloadTask], None] = None,
                 on_log: Callable[[str], None] = None) -> bool:
    """执行单个下载任务（阻塞）。

    参数:
        task:         下载任务，会就地更新 status / percent / 进度文本
        m3u8_url:     真实播放地址（需即时获取）
        on_progress:  进度变化回调，参数为 task
        on_log:       日志回调，参数为一行文本

    返回:
        True 成功，False 失败（task.error 记录原因）
    """
    if not config.YTDLP_PATH.is_file():
        task.status = TaskStatus.FAILED
        task.error = f"未找到 yt-dlp: {config.YTDLP_PATH}"
        return False

    if not m3u8_url:
        task.status = TaskStatus.FAILED
        task.error = "播放地址为空（可能已过期，请重试）"
        return False

    # 最终落盘路径（HDD 下载目录）
    # 换脸/魔改 等单集类分类会落到 <下载目录>/<分类>/<剧名>/<剧名>.mp4
    out_path = build_output_path(
        config.DOWNLOAD_SITE_DIR, task.drama_title, task.ep,
        category=getattr(task, "category", "") or "",
    )

    # 暂存路径（SSD 项目本地目录）：合并在此完成，再移动到 HDD。
    # 用唯一文件名避免多任务并发时撞名（同剧多集也可能同时下载）。
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_name = (
        f"{sanitize_filename(normalize_title(task.drama_title))}"
        f"__ep{task.ep}__{uuid.uuid4().hex[:10]}.mp4"
    )
    temp_out_path = config.TEMP_DIR / tmp_name

    # 已完整存在则跳过；损坏/部分文件删除后重新下载
    if out_path.is_file():
        if validate_video(out_path):
            task.status = TaskStatus.DONE
            task.percent = 100.0
            task.output_path = str(out_path)
            if on_progress:
                on_progress(task)
            if on_log:
                on_log(f"[跳过] 已存在 {out_path.name}")
            return True
        else:
            if remove_if_broken(out_path):
                if on_log:
                    on_log(f"[断续] 检测到不完整文件，删除重下: {out_path.name}")
            # 若删除失败（无权限等），直接覆盖写入
            elif on_log:
                on_log(f"[断续] 不完整文件无法删除，将覆盖: {out_path.name}")

    # yt-dlp 先把片段下载并合并到 SSD 暂存文件
    cmd = build_command(task, m3u8_url, temp_out_path)

    # 进度回调节流：yt-dlp 每拉到一个分片就打印一行，批量下载时这些回调会
    # 跨线程涌向 UI 事件队列（几百个任务时足以把界面拖到卡顿）。界面本来就
    # 按 300ms 刷新一次，这里把回调也压到同一量级，肉眼无差别但事件量降一个
    # 数量级。状态跃迁（取址中/下载中/合并中/完成）一律直通，不丢帧。
    _last = {"t": 0.0, "pct": -1.0}

    def _progress(force: bool = False) -> None:
        if on_progress is None:
            return
        now = time.monotonic()
        if (not force and now - _last["t"] < _PROGRESS_MIN_INTERVAL
                and abs(task.percent - _last["pct"]) < _PROGRESS_MIN_PCT):
            return
        _last["t"] = now
        _last["pct"] = task.percent
        on_progress(task)

    # 子进程环境：强制 UTF-8 输出，隐藏控制台窗口
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    task.status = TaskStatus.FETCHING
    _progress(force=True)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            startupinfo=startupinfo,
            text=True,
            encoding="utf-8",
            errors="ignore",
            bufsize=1,
        )
        task.proc = proc

        task.status = TaskStatus.DOWNLOADING
        _progress(force=True)

        tail = deque(maxlen=40)      # 保留输出尾部，失败时提炼真实原因
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            tail.append(line)

            # 合并阶段提示
            if any(h in line for h in _MERGING_HINTS):
                # 取消优先：不要因为一条收尾日志把 CANCELLED 覆盖回 MERGING
                if task.status != TaskStatus.CANCELLED:
                    task.status = TaskStatus.MERGING
                _progress(force=True)
                if on_log:
                    on_log(line)
                continue

            info = parse_progress(line)
            if info:
                # 进度行只驱动任务对象更新，不再逐行写日志（避免刷屏/卡顿）
                task.percent = info["percent"]
                task.size_text = info["size"]
                task.speed_text = info["speed"]
                task.eta_text = info["eta"]
                # 取消优先：取消信号到来后，不能再被进度行覆盖回 DOWNLOADING，
                # 否则 task.status 最终不是 CANCELLED，上层会把它误判成失败。
                if task.status != TaskStatus.CANCELLED and task.status != TaskStatus.MERGING:
                    task.status = TaskStatus.DOWNLOADING
                _progress()
                continue

            # 其余（错误/警告/提示）才进日志
            if on_log:
                on_log(line)

        proc.wait()

        if task.status == TaskStatus.CANCELLED:
            return False

        # 合并产物应落在 SSD 暂存目录；不存在则视为失败
        if proc.returncode != 0 or not temp_out_path.is_file():
            task.status = TaskStatus.FAILED
            detail = _extract_error(tail)
            base = (
                f"yt-dlp 退出码 {proc.returncode}"
                if proc.returncode != 0
                else "未生成合并文件"
            )
            task.error = f"{base}: {detail}" if detail else base
            if detail:
                # 真实原因落到当天日志，便于回看定位（旧版只有退出码，查不出原因）
                applog.error("yt-dlp 失败明细 did=%s ep=%s: %s",
                             task.did, task.ep, detail)
            return False

        # ---- 从 SSD 暂存移动到 HDD 下载目录（落盘）----
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 同盘为 rename（瞬时），跨盘为 copy+delete；机械盘落盘仅一次顺序写
            shutil.move(str(temp_out_path), str(out_path))
        except Exception as exc:                # noqa: BLE001
            task.status = TaskStatus.FAILED
            task.error = f"合并文件转存失败: {exc}"
            return False

        # 转存成功：刷新校验缓存（旧缓存可能指向暂存文件）
        invalidate(out_path)

        task.status = TaskStatus.DONE
        task.percent = 100.0
        task.output_path = str(out_path)
        _progress(force=True)
        if on_log:
            on_log(f"[落盘] {out_path.name} (暂存->下载目录)")
        return True

    except FileNotFoundError:
        task.status = TaskStatus.FAILED
        task.error = f"无法启动 yt-dlp: {config.YTDLP_PATH}"
        return False
    except Exception as exc:                    # noqa: BLE001
        if task.status == TaskStatus.CANCELLED:
            return False
        task.status = TaskStatus.FAILED
        task.error = str(exc)
        return False
    finally:
        task.proc = None
        # 清理暂存残留：成功时已移动走；失败/取消时删除。
        # 注意不能只删主 mp4 —— yt-dlp 并发分片会派生 *.mp4-FragN、*.part、*.ytdl 等
        # 同名兄弟文件，旧版漏删它们，失败一次就在 SSD 暂存目录留下一堆垃圾。
        _cleanup_staging(temp_out_path)


def cancel(task: DownloadTask) -> None:
    """取消任务：终止子进程。"""
    task.status = TaskStatus.CANCELLED
    proc = getattr(task, "proc", None)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:                        # noqa: BLE001
            pass
