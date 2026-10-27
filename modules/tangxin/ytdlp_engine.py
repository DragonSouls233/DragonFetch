# -*- coding: utf-8 -*-
"""yt-dlp 兜底引擎。

定位：**只在自制 m3u8 下载器失败时**才降级调用，不作为主路径。
原因：自制下载器已实测跑通（代理、AES-128 解密、ffmpeg concat 全验证），
且可控可诊断；yt-dlp 是通用引擎，对糖心这种自建站点的适配未必更好，
但它支持的格式/站点更广，作为兜底能提高整体成功率。

代理透传：统一使用项目内置代理（socks5h://127.0.0.1:10808 或节点专属
入口 10809+），与自制下载器走同一出口，行为一致。

容错：yt-dlp 未安装 / 导入失败时静默禁用（``available()`` 返回 False），
**绝不阻断主下载流程**。
"""
import os
import re
import subprocess

from . import config, logger
from .utils import DownloadCancelled

_yt_dlp = None
_checked = False


def find_ytdlp_exe() -> str:
    """定位**独立的** yt-dlp 可执行文件（用户要求：外部程序统一放 ``bin/``）。

    顺序：``TX_YTDLP_EXE`` → ``{RUNTIME_ROOT}/bin/yt-dlp.exe``
    → ``{RUNTIME_ROOT}/yt-dlp.exe``（老布局兼容）→ PATH。
    """
    cands = []
    env = os.environ.get("TX_YTDLP_EXE")
    if env:
        cands.append(env)
    try:
        cands.append(os.path.join(config.BIN_DIR, "yt-dlp.exe"))
        cands.append(os.path.join(config.RUNTIME_ROOT, "yt-dlp.exe"))
    except Exception:
        pass
    cands.extend(["yt-dlp.exe", "yt-dlp"])
    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        exe = c
        if os.path.basename(c).lower() in ("yt-dlp", "yt-dlp.exe"):
            try:
                import shutil
                exe = shutil.which(c) or ""
            except Exception:
                exe = ""
            if not exe:
                continue
        if os.path.isfile(exe):
            return exe
    return ""


def _import_yt_dlp():
    """延迟导入 yt-dlp；只尝试一次，失败则缓存结果。"""
    global _yt_dlp, _checked
    if _checked:
        return _yt_dlp
    _checked = True
    try:
        import yt_dlp as _mod
        _yt_dlp = _mod
    except Exception as e:
        logger.get_logger().warning("yt-dlp 模块不可用：%s", e)
        _yt_dlp = None
    return _yt_dlp


def available() -> bool:
    """yt-dlp 是否可用（Python 模块 **或** 独立 exe，任一即可）。"""
    if _import_yt_dlp() is not None:
        return True
    return bool(find_ytdlp_exe())


def _progress_hook(progress, stop_event=None):
    """把 yt-dlp 的进度回调转成项目统一的 progress(done, total, phase, msg)。

    若传入 stop_event，用户中止时主动抛出 DownloadCancelled（由调用方收敛）。
    """
    def hook(d):
        if stop_event and stop_event.is_set():
            raise DownloadCancelled("用户中止下载")
        if not progress:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0
            progress(int(done / 1024), int(total / 1024), "download",
                     "%.1f MB / %.1f MB  %.0f KB/s"
                     % (done / 1048576, total / 1048576, (speed or 0) / 1024),
                     speed=speed, bytes_done=done, bytes_total=total, eta=None)
        elif status == "finished":
            progress(1, 1, "merge", "下载完成，转封装中…")
    return hook


def download(url: str, output_file: str, proxy: str = None, progress=None,
             stop_event=None) -> dict:
    """用 yt-dlp 下载到 ``output_file``（应为本地临时文件）。

    优先用 **Python 模块**（进度回调最准）；模块不可用时降级用
    **独立 exe**（``bin/yt-dlp.exe``）。

    返回 ``{ok, path, error}``。失败**不抛异常**，交由调用方决定是否再降级。
    """
    mod = _import_yt_dlp()
    if mod is None:
        return _download_by_exe(url, output_file, proxy=proxy, progress=progress,
                                stop_event=stop_event)

    out_dir = os.path.dirname(output_file)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    stem, _ext = os.path.splitext(os.path.basename(output_file))

    opts = {
        "outtmpl": os.path.join(out_dir, stem + ".%(ext)s"),
        "format": "bestvideo+bestaudio/best/best",   # 最高清优先
        "concurrent_fragment_downloads": max(1, min(config.SEGMENT_WORKERS, 3)),
        "retries": config.RETRIES,
        "fragment_retries": config.RETRIES,
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook(progress, stop_event)],
    }
    if proxy:
        opts["proxy"] = proxy

    try:
        with mod.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadCancelled:
        return {"ok": False, "error": "已取消"}
    except Exception as e:
        logger.get_logger().warning("yt-dlp 兜底下载失败: %s", e)
        return {"ok": False, "error": str(e)[:500]}

    # yt-dlp 会按实际容器给扩展名，这里找回落盘文件
    if os.path.isfile(output_file):
        return {"ok": True, "path": output_file}
    for name in sorted(os.listdir(out_dir)):
        if os.path.splitext(name)[0] == stem:
            p = os.path.join(out_dir, name)
            if os.path.getsize(p) > 0:
                return {"ok": True, "path": p}
    return {"ok": False, "error": "yt-dlp 未产生输出文件"}


# ---------- 独立 exe 兜底（模块不可用时）----------
_PCT_RE = re.compile(r"([\d.]+)%")
_SPEED_RE = re.compile(r"at\s+([\d.]+)(KiB|MiB|GiB|B)/s")


def _download_by_exe(url: str, output_file: str, proxy=None, progress=None,
                     stop_event=None) -> dict:
    """用独立 ``yt-dlp.exe`` 下载（Python 模块不可用时的兜底）。"""
    exe = find_ytdlp_exe()
    if not exe:
        return {"ok": False, "error": "yt-dlp 不可用（模块与 bin/yt-dlp.exe 都没有）"}
    out_dir = os.path.dirname(output_file)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    stem, _ext = os.path.splitext(os.path.basename(output_file))
    cmd = [exe, "--newline", "--no-warnings", "--no-playlist",
           "-f", "bestvideo+bestaudio/best/best",
           "--merge-output-format", "mp4",
           "-o", os.path.join(out_dir, stem + ".%(ext)s"),
           url]
    if proxy:
        cmd += ["--proxy", proxy]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True, encoding="utf-8",
                                errors="replace")
    except Exception as e:
        return {"ok": False, "error": "yt-dlp 启动失败: %s" % e}

    def _pump():
        """读一行输出，解析进度；用户中止则杀掉进程。"""
        try:
            for line in proc.stdout:
                if stop_event and stop_event.is_set():
                    raise DownloadCancelled("用户中止下载")
                if not progress:
                    continue
                m = _PCT_RE.search(line)
                if not m:
                    continue
                try:
                    pct = float(m.group(1))
                except ValueError:
                    continue
                sp = 0.0
                ms = _SPEED_RE.search(line)
                if ms:
                    mult = {"B": 1, "KiB": 1024, "MiB": 1048576,
                            "GiB": 1073741824}.get(ms.group(2), 1)
                    try:
                        sp = float(ms.group(1)) * mult
                    except ValueError:
                        sp = 0.0
                progress(int(pct), 100, "download",
                         "%s（yt-dlp.exe）" % line.strip()[:80], speed=sp)
        except DownloadCancelled:
            raise
        except Exception:
            pass

    try:
        _pump()
        proc.wait(timeout=60)
    except DownloadCancelled:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "error": "已取消"}
    except Exception as e:
        return {"ok": False, "error": "yt-dlp 执行异常: %s" % e}

    if os.path.isfile(output_file) and os.path.getsize(output_file) > 0:
        return {"ok": True, "path": output_file}
    try:
        for name in sorted(os.listdir(out_dir)):
            if os.path.splitext(name)[0] == stem:
                p = os.path.join(out_dir, name)
                if os.path.getsize(p) > 0:
                    return {"ok": True, "path": p}
    except Exception:
        pass
    return {"ok": False, "error": "yt-dlp(exe) 未产生输出文件"}
