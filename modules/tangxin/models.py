# -*- coding: utf-8 -*-
"""糖心数据模型：演员(Actor) 与 视频(Video)。"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class Video:
    vid: str                 # 视频数字 id，如 "25576"
    title: str               # 标题（已清洗）
    cover_url: str = ""      # 封面图 URL（CDN cover.jpg）
    duration: str = ""       # 原始时长字符串，如 "140:17"
    actor: str = ""          # 所属演员名（用于 NFO）

    @property
    def m3u8_url(self) -> str:
        from . import config
        return f"{config.CDN}/videos/{self.vid}/index.m3u8"

    @property
    def page_url(self) -> str:
        """视频详情页地址（yt-dlp 兜底引擎用它作为输入）。"""
        from . import config
        return f"{config.ORIGIN}{config.LOCALE}/v/{self.vid}/"

    @property
    def key_url(self) -> str:
        from . import config
        return f"{config.CDN}/videos/{self.vid}/enc.key"

    @property
    def runtime_minutes(self) -> int:
        """把 '140:17' 或 '7:30' 解析成整数分钟（Kodi NFO 用）。"""
        if not self.duration:
            return 0
        parts = [int(p) for p in self.duration.split(":") if p.isdigit()]
        if not parts:
            return 0
        total_seconds = 0
        for p in parts:
            total_seconds = total_seconds * 60 + p
        return max(1, round(total_seconds / 60))


@dataclass
class Actor:
    slug: str                # URL 编码后的 slug，用于拼接接口地址
    name: str                # 展示名（解码后的中文/日文）
    count: int = 0           # 演员云标注的作品数
    videos: List[Video] = field(default_factory=list)
