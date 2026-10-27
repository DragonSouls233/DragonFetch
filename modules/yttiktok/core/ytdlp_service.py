"""yt-dlp 封装：解析（probe）与下载（download）。

- 可执行文件优先用项目 bin/yt-dlp.exe，其次系统 PATH。
- 支持 YouTube 与 TikTok（yt-dlp 原生支持），另外也兼容抖音等。
- 下载进度按 `[download] 12.3%` 行解析并回调。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from shutil import which

from .models import VideoInfo

_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def extract_url(text: str) -> str:
    """从整段分享文案里抽出第一个链接。"""
    m = _URL_RE.search(text or "")
    if m:
        return m.group(0).rstrip(".,;)}\"'")
    return (text or "").strip()


def _project_root() -> Path:
    # modules/yttiktok/core/this.py -> parents[3] == DragonFetch
    return Path(__file__).resolve().parents[3]


def ytdlp_path() -> str:
    p = _project_root() / "bin" / "yt-dlp.exe"
    if p.exists():
        return str(p)
    exe = which("yt-dlp")
    if exe:
        return exe
    raise FileNotFoundError("未找到 yt-dlp，请把 yt-dlp.exe 放到项目 bin/ 目录")


def _tail(text: str | None, n: int = 500) -> str:
    text = (text or "").strip()
    return text[-n:] if len(text) > n else text


def probe(url: str, proxy: str | None = None) -> VideoInfo:
    """解析视频元信息（不下载）。"""
    u = extract_url(url)
    if not u.lower().startswith("http"):
        raise ValueError("请输入有效的视频链接")
    cmd = [ytdlp_path(), "-J", "--no-playlist", "--no-warnings"]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(u)
    out = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if out.returncode != 0:
        raise RuntimeError(_tail(out.stderr) or "解析失败")
    info = json.loads(out.stdout or "{}")
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]
    if not info:
        raise RuntimeError("解析结果为空")
    return VideoInfo(
        url=u,
        title=info.get("title") or "未命名",
        uploader=info.get("uploader") or info.get("creator") or info.get("channel") or "",
        duration=int(info.get("duration") or 0),
        thumbnail=info.get("thumbnail") or "",
        ext=info.get("ext") or "",
        webpage_url=info.get("webpage_url") or u,
        raw=info,
    )


def download(
    url: str,
    outdir: str,
    proxy: str | None = None,
    progress=None,
    fmt: str | None = None,
    audio_only: bool = False,
    write_thumbnail: bool = False,
    playlist: bool = False,
) -> str:
    """下载到 outdir，返回目标文件路径（可能为空字符串）。

    fmt            yt-dlp 的 -f 表达式；None = "bv*+ba/b"
    audio_only     True 时只取音频并转 mp3
    write_thumbnail 同时保存封面（转 jpg）
    playlist       True 时允许整条播放列表（默认只下单个视频）
    """
    u = extract_url(url)
    if not u.lower().startswith("http"):
        raise ValueError("请输入有效的视频链接")
    Path(outdir).mkdir(parents=True, exist_ok=True)
    cmd = [ytdlp_path(), "--newline"]
    if not playlist:
        cmd += ["--no-playlist"]
    if audio_only:
        cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3"]
    else:
        cmd += ["-f", fmt or "bv*+ba/b", "--merge-output-format", "mp4"]
    if write_thumbnail:
        cmd += ["--write-thumbnail", "--convert-thumbnails", "jpg"]
    cmd += ["-o", str(Path(outdir) / "%(title)s [%(id)s].%(ext)s")]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(u)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    pct = 0.0
    last_path = ""
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            pct = float(m.group(1))
            if progress:
                progress(pct, line)
            continue
        if line.startswith("[Merger]") or line.startswith("[download] Destination:"):
            m2 = re.search(r"(?:Destination:|Merging formats into)\s*\"?(.+?)\"?$", line)
            if m2:
                last_path = m2.group(1).strip().strip('"')
        if progress and line:
            progress(pct, line)
    code = proc.wait()
    if code != 0:
        raise RuntimeError(f"下载失败（退出码 {code}）")
    if progress:
        progress(100.0, "下载完成")
    return last_path
