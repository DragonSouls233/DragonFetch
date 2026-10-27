"""数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VideoInfo:
    url: str
    title: str = ""
    uploader: str = ""
    duration: int = 0
    thumbnail: str = ""
    ext: str = ""
    webpage_url: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def duration_text(self) -> str:
        s = int(self.duration or 0)
        h, m, sec = s // 3600, (s % 3600) // 60, s % 60
        return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    @property
    def site(self) -> str:
        u = (self.webpage_url or self.url or "").lower()
        if "youtube" in u or "youtu.be" in u:
            return "YouTube"
        if "tiktok" in u:
            return "TikTok"
        if "douyin" in u:
            return "抖音"
        return "其他"
