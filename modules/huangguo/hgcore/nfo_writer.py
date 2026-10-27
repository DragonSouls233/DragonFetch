# -*- coding: utf-8 -*-
"""元数据归纳：为下载的剧自动生成媒体库友好的元数据文件。

采用的格式标准：**Kodi NFO（XML）** —— 同时被 Emby / Jellyfin / Plex 原生识别，
是当前事实上的主流刮削格式（剧集用 <tvshow> + <episodedetails>）。

目录产出（<downloads>/<site>/<剧名>/）:
    tvshow.nfo        剧集级信息（标题/简介/类型/评分/uniqueid/链接）
    poster.jpg        竖版封面
    fanart.jpg        横版背景图（有则写，无则跳过）
    library.json      自用清单（标题、集数列表、下载状态、文件路径）
    Season 01/        可选：季子目录（config.SEASON_FOLDER=True 时启用）
      ├─ <剧名> S01E01.mp4
      └─ <剧名> S01E01.nfo   每集信息（下载完成后写入）

媒体服务器识别规则（务必遵守）:
    * 文件名必须含 SxxExx
    * 推荐放在 "Season 01" 子目录；若平铺在剧集根目录也会被当作第 1 季
    * tvshow.nfo / poster.jpg / fanart.jpg 必须放在**剧集根目录**（不是季目录内）
"""

import json
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape

from . import config
from .models import Drama, DownloadTask
from .utils import drama_folder as _drama_folder, sanitize_filename, season_dir_name


def drama_folder(title: str, season: Optional[int] = None,
                 category: str = "") -> Path:
    """剧集根目录（按当前站点分目录存储，便于区分来源）。

    注意：返回的是**剧集根目录**（tvshow.nfo 所在层）。
    需要季子目录时用 episode_folder()。

    换脸 / 魔改 等单集类分类会多一层分类子目录，与视频落盘位置保持一致。
    """
    return _drama_folder(config.DOWNLOAD_SITE_DIR, title, season=0,
                         category=category)


def episode_folder(title: str, category: str = "") -> Path:
    """单集视频/NFO 所在目录（依据 config.SEASON_FOLDER 决定是否含 Season 01）。"""
    return _drama_folder(config.DOWNLOAD_SITE_DIR, title, season=None,
                         category=category)


def _xml(text) -> str:
    return escape(str(text or ""), {"\"": "&quot;", "'": "&apos;"})


# ---------------------------------------------------------------- 剧集级

def write_tvshow_nfo(drama: Drama, episode_count: int) -> Optional[Path]:
    """写 tvshow.nfo（放在剧集根目录）。"""
    folder = drama_folder(drama.title, category=getattr(drama, "category", ""))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        genres = "".join(f"    <genre>{_xml(t)}</genre>\n" for t in (drama.tags or [])[:8])
        unique = f"{config.ACTIVE_SITE_ID}:{drama.did}"
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            "<tvshow>\n"
            f"  <title>{_xml(drama.title)}</title>\n"
            f"  <showtitle>{_xml(drama.title)}</showtitle>\n"
            f"  <sorttitle>{_xml(drama.title)}</sorttitle>\n"
            f"  <plot>{_xml(drama.desc)}</plot>\n"
            f"  <outline>{_xml((drama.desc or '')[:120])}</outline>\n"
            f"{genres}"
            f"  <rating>{_xml(drama.score)}</rating>\n"
            "  <season>1</season>\n"
            f"  <episode>{episode_count}</episode>\n"
            "  <status>Continuing</status>\n"
            f"  <studio>{_xml(config.ACTIVE_SITE.get('name', '黄果短剧'))}</studio>\n"
            '  <uniqueid type="hg" default="true">'
            f'{_xml(unique)}</uniqueid>\n'
            f"  <id>{_xml(unique)}</id>\n"
            f"  <source>{_xml(drama.url)}</source>\n"
            "</tvshow>\n"
        )
        path = folder / "tvshow.nfo"
        path.write_text(xml, encoding="utf-8")
        return path
    except OSError:
        return None


def save_poster(drama: Drama, image_bytes: bytes) -> Optional[Path]:
    """保存封面为 poster.jpg（需要已解密的明文图）。"""
    return _write_image(
        drama_folder(drama.title, category=getattr(drama, "category", "")),
        "poster.jpg", image_bytes,
    )


def save_fanart(drama: Drama, image_bytes: bytes) -> Optional[Path]:
    """保存横版背景图为 fanart.jpg（无横版素材时上层可跳过）。"""
    return _write_image(
        drama_folder(drama.title, category=getattr(drama, "category", "")),
        "fanart.jpg", image_bytes,
    )


def write_movie_nfo(drama: Drama) -> Optional[Path]:
    """单集类作品（换脸/魔改）写 movie.nfo，与 <剧名>.mp4 同目录。

    Kodi/Emby 对「平铺的单集视频」按电影刮削，movie.nfo 能被正确配对；
    若继续写 tvshow.nfo，播放器会把文件夹当剧集处理却找不到分集。
    """
    folder = drama_folder(drama.title, category=getattr(drama, "category", ""))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        genres = "".join(f"    <genre>{_xml(t)}</genre>\n" for t in (drama.tags or [])[:8])
        unique = f"{config.ACTIVE_SITE_ID}:{drama.did}"
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            "<movie>\n"
            f"  <title>{_xml(drama.title)}</title>\n"
            f"  <plot>{_xml(drama.desc)}</plot>\n"
            f"{genres}"
            f'  <uniqueid type="hg" default="true">{_xml(unique)}</uniqueid>\n'
            f"  <id>{_xml(unique)}</id>\n"
            "</movie>\n"
        )
        path = folder / "movie.nfo"
        path.write_text(xml, encoding="utf-8")
        return path
    except OSError:
        return None


def _write_image(folder: Path, name: str, data: bytes) -> Optional[Path]:
    if not data:
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        return path
    except OSError:
        return None


def cached_cover_bytes(site_id: str, did) -> Optional[bytes]:
    """读取本地已缓存的封面（DATA/covers/<site>/<did>.jpg）。

    应用内网格封面走的就是这条通道（已解密明文 JPEG），因此这里读到的
    字节可直接用作媒体库 poster.jpg，无需再次网络抓取 / 解密。
    """
    if not site_id or did is None:
        return None
    p = config.CACHE_DIR / str(site_id) / f"{did}.jpg"
    try:
        if p.is_file() and p.stat().st_size > 1000:
            return p.read_bytes()
    except OSError:
        return None
    return None


def save_poster_bytes(folder: Path, data: bytes) -> Optional[Path]:
    """把封面字节直接写入指定剧集目录的 poster.jpg。"""
    return _write_image(folder, "poster.jpg", data)


# ---------------------------------------------------------------- 单集级

def write_episode_nfo(task: DownloadTask) -> Optional[Path]:
    """某集下载完成后写 {S01E01}.nfo（与视频同目录）。"""
    folder = episode_folder(task.drama_title,
                            category=getattr(task, "category", ""))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        from .utils import ep_suffix
        unique = f"{config.ACTIVE_SITE_ID}:{task.did}:{task.ep}"
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            "<episodedetails>\n"
            f"  <title>第{task.ep:02d}集</title>\n"
            f"  <showtitle>{_xml(task.drama_title)}</showtitle>\n"
            "  <season>1</season>\n"
            f"  <episode>{task.ep}</episode>\n"
            f"  <plot>{_xml(task.drama_title)} 第 {task.ep} 集</plot>\n"
            "  <status>finished</status>\n"
            '  <uniqueid type="hg-ep" default="true">'
            f'{_xml(unique)}</uniqueid>\n'
            "</episodedetails>\n"
        )
        path = folder / f"{ep_suffix(task.ep)}.nfo"
        path.write_text(xml, encoding="utf-8")
        return path
    except OSError:
        return None


# ---------------------------------------------------------------- 清单

def update_library_json(drama: Drama,
                        episodes: List[int],
                        done: Dict[int, str] = None) -> Optional[Path]:
    """更新剧集根目录下的 library.json 清单（标题 + 集数 + 状态）。"""
    folder = drama_folder(drama.title)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        done = done or {}
        payload = {
            "id": drama.did,
            "site": config.ACTIVE_SITE_ID,
            "title": drama.title,
            "score": drama.score,
            "total_episodes": len(episodes),
            "episodes": [
                {
                    "ep": ep,
                    "label": f"S01E{ep:02d}",
                    "downloaded": ep in done,
                    "file": done.get(ep, ""),
                }
                for ep in sorted(episodes)
            ],
            "tags": drama.tags,
            "description": drama.desc,
            "source": drama.url,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = folder / "library.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
    except OSError:
        return None


# ---------------------------------------------------------------- 目录整理

def _media_files(folder: Path) -> Iterable[Path]:
    """列出目录下的媒体与 sidecar 文件（不含子目录）。"""
    if not folder.is_dir():
        return []
    out = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() in (".mp4", ".mkv", ".ts", ".nfo", ".jpg", ".png"):
            out.append(p)
    return out


def organize_season_folders(titles: Iterable[str],
                            to_season: bool = True) -> Tuple[int, int]:
    """把剧集目录整理进/移出 "Season 01" 子目录。

    仅移动**单集文件与其同名 sidecar**（SxxExx.mp4 / SxxExx.nfo），
    tvshow.nfo / poster.jpg / fanart.jpg / library.json 始终留在剧集根目录。

    返回 (移动文件数, 出错数)。用于用户主动切换 SEASON_FOLDER 后的数据迁移。
    """
    moved = errors = 0
    for title in titles:
        root = drama_folder(title)
        season = root / season_dir_name(1)
        src, dst = (root, season) if to_season else (season, root)
        if not src.is_dir():
            continue
        try:
            dst.mkdir(parents=True, exist_ok=True)
        except OSError:
            errors += 1
            continue
        for f in _media_files(src):
            # 只搬单集文件（文件名含 S01E）
            if "S01E" not in f.name:
                continue
            target = dst / f.name
            if target.exists():
                continue
            try:
                f.replace(target)
                moved += 1
            except OSError:
                try:
                    import shutil
                    shutil.move(str(f), str(target))
                    moved += 1
                except OSError:
                    errors += 1
        # 季目录空了就删掉
        if not to_season and season.is_dir():
            try:
                if not any(season.iterdir()):
                    season.rmdir()
            except OSError:
                pass
    return moved, errors


def expected_episode_path(title: str, ep: int, ext: str = "mp4",
                          category: str = "") -> Path:
    """预测某集文件的规范路径（供整理/自检使用）。"""
    from .utils import ep_suffix
    folder = episode_folder(title, category=category)
    if category:
        return folder / f"{sanitize_filename(title)}.{ext}"
    return folder / f"{sanitize_filename(title)} {ep_suffix(ep)}.{ext}"


# ---------------------------------------------------------------- 补刮封面

def _folder_did(folder: Path) -> Optional[str]:
    """从剧集目录里推断 did（library.json → tvshow.nfo / movie.nfo 的 uniqueid）。"""
    lib = folder / "library.json"
    if lib.is_file():
        try:
            return str(json.loads(lib.read_text(encoding="utf-8")).get("id"))
        except Exception:                      # noqa: BLE001
            pass
    nfo = folder / "tvshow.nfo"
    if not nfo.is_file():
        nfo = folder / "movie.nfo"             # 单集类（换脸/魔改）写的是 movie.nfo
    if nfo.is_file():
        try:
            m = re.search(
                r"<uniqueid[^>]*>([^<:]+):([^<]+)</uniqueid>",
                nfo.read_text(encoding="utf-8", errors="ignore"),
            )
            if m:
                return m.group(2)
        except OSError:
            pass
    return None


def backfill_missing_metadata(site_id: str = None,
                              on_log=None) -> int:
    """为已下载但缺少 poster.jpg 的剧补全封面（优先用本地缓存封面）。

    适用场景：早期下载的剧没有走刮削，或 _ensure_tvshow 的实时抓取偶发失败，
    导致磁盘上只有 mp4 / tvshow.nfo 却缺 poster.jpg。本函数遍历下载目录，
    对「有视频但无 poster」的剧，用 DATA/covers 里已缓存的封面补齐，无需联网。

    返回成功补全的剧数量。on_log 可选，用于回传进度（主线程日志）。
    """
    root = config.DOWNLOAD_DIR
    if not root.is_dir():
        return 0
    site_dirs = [root / site_id] if site_id else [p for p in root.iterdir() if p.is_dir()]
    done = 0
    for sdir in site_dirs:
        if not sdir.is_dir():
            continue
        sid = site_id or sdir.name
        for folder in sdir.iterdir():
            if not folder.is_dir():
                continue
            # 必须有视频才算「已下载的剧」
            if not any(folder.rglob("*.mp4")):
                continue
            if (folder / "poster.jpg").exists():
                continue
            did = _folder_did(folder)
            if not did:
                if on_log:
                    on_log(f"[跳过] {folder.name}：无法识别 did")
                continue
            data = cached_cover_bytes(sid, did)
            if not data:
                if on_log:
                    on_log(f"[跳过] {folder.name}：本地无缓存封面")
                continue
            if save_poster_bytes(folder, data):
                done += 1
                if on_log:
                    on_log(f"[补全] {folder.name}：poster.jpg")
    return done
