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


def info_meta(url: str, proxy: str | None = None, timeout: int = 120) -> dict:
    """用 yt-dlp ``-J`` 取一条视频的完整元信息，并归一化成前端表格要的字段。

    为什么单独有这个函数
    --------------------
    「多站点」页的「解析直链」按钮对 **yt-dlp 有 extractor 的站点** 不该走
    ``unofficial-api``（那类站点本来就不需要站点包），但用户仍然希望点一下就能
    看到时长 / 作者 / 观看数 / 日期 / 分类，并拿到能直接下载的地址。
    这就是 yt-dlp 的 ``-J`` 能给的：一次请求，全部字段都有。

    Returns:
        ``{"title","duration","author","views","date","categories","direct_url",
        "direct_urls","thumbnail","site"}``，缺字段给空字符串。
    """
    u = extract_url(url)
    if not u.lower().startswith("http"):
        raise ValueError("请输入有效的视频链接")
    cmd = [ytdlp_path(), "-J", "--no-playlist", "--no-warnings"]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(u)
    out = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(_tail(out.stderr) or "解析失败")
    info = json.loads(out.stdout or "{}")
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]

    secs = int(info.get("duration") or 0)
    duration = ""
    if secs > 0:
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        duration = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    raw_date = str(info.get("upload_date") or "")
    date = (f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            if len(raw_date) == 8 else raw_date)

    cats = [c for c in (info.get("categories") or []) if c]
    if not cats:
        cats = [t for t in (info.get("tags") or [])[:4] if t]

    views = info.get("view_count")
    if isinstance(views, int) and views >= 0:
        views = f"{views / 1_000_000:.1f}M" if views >= 1_000_000 else (
            f"{views / 1000:.0f}K" if views >= 1000 else str(views))
    else:
        views = ""

    # 直链：优先「音视频合体」的最高画质（单独的视频流拿下来没声音），
    # 没有合体流时退回最高画质的视频流。
    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    progressive = [f for f in formats
                   if f.get("vcodec", "none") != "none"
                   and f.get("acodec", "none") != "none"]
    pool = progressive or [f for f in formats if f.get("vcodec", "none") != "none"] or formats
    pool.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    urls = [f["url"] for f in pool if f.get("url")]

    return {
        "title": info.get("title") or "未命名",
        "duration": duration,
        "author": (info.get("uploader") or info.get("creator")
                   or info.get("channel") or ""),
        "views": views,
        "date": date,
        "categories": cats,
        "direct_url": urls[-1] if urls else "",
        "direct_urls": urls,
        "thumbnail": info.get("thumbnail") or "",
        "site": info.get("extractor_key") or "",
    }


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
