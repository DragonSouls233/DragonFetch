# -*- coding: utf-8 -*-
"""NFO 生成（Kodi/Emby 风格 <movie>），原子写。只写真实存在的字段。"""
import os
import xml.sax.saxutils as sax

from .models import Video


def _esc(text: str) -> str:
    return sax.escape(text or "")


def build_movie_nfo(video: Video, poster_name: str = None) -> str:
    """生成 Kodi <movie> NFO 文本。"""
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    parts.append("<movie>")
    parts.append("  <title>%s</title>" % _esc(video.title))
    parts.append("  <sorttitle>%s</sorttitle>" % _esc(video.title))
    # 站点视频 id —— 已下载索引靠它做跨目录精确去重（existing.py 扫描时解析）
    if video.vid:
        parts.append('  <uniqueid type="vid">%s</uniqueid>' % _esc(str(video.vid)))
    if video.runtime_minutes:
        parts.append("  <runtime>%d</runtime>" % video.runtime_minutes)
    if poster_name:
        parts.append("  <thumb>%s</thumb>" % _esc(poster_name))
    if video.actor:
        parts.append("  <actor>")
        parts.append("    <name>%s</name>" % _esc(video.actor))
        parts.append("  </actor>")
    # 来源标签，便于识别
    parts.append("  <source>糖心Vlog</source>")
    parts.append("</movie>")
    return "\n".join(parts) + "\n"


def write_nfo(path: str, content: str):
    """原子写：先写 .tmp 再 os.replace，避免半截文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def write_video_nfo(video: Video, mp4_path: str, poster_path: str = None) -> str:
    """根据 mp4 路径生成同名 .nfo。返回 nfo 路径。"""
    base = os.path.splitext(mp4_path)[0]
    nfo_path = base + ".nfo"
    poster_name = os.path.basename(poster_path) if poster_path else None
    write_nfo(nfo_path, build_movie_nfo(video, poster_name))
    return nfo_path
