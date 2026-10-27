# -*- coding: utf-8 -*-
"""数据模型：剧集、分集、下载任务。

只放纯数据结构与状态常量，不承载业务逻辑，方便 UI 与下载器共享。
"""

from dataclasses import dataclass, field
from typing import List, Optional
from enum import Enum, auto


# ---------------------------------------------------------------- 剧集

@dataclass
class Drama:
    """一部剧（首页卡片 + 详情页补充信息）。"""
    did: int                      # 站点 ID，如 117
    title: str = ""               # 标题
    cover: str = ""               # 封面 URL（懒加载的 data-src）
    score: str = ""               # 评分，如 "9.9"
    episode_text: str = ""        # 原始集数文案，如 "更新至 36 集"
    desc: str = ""                # 简介（卡片摘要 / 详情页简介）
    tags: List[str] = field(default_factory=list)
    category: str = ""            # 归属分类（如「换脸」「魔改」）；单集类分类会独立建目录

    # 更新监测（「有更新」TAB 用；非空表示这是一条待补记录）
    update_eps: List[int] = field(default_factory=list)   # 待补的集号
    update_site: str = ""         # 该剧所属站点 id（跨站展示时提示用）

    # 详情页补充（懒加载）
    episode_count: int = 0        # 总集数，解析自 episode_text
    detail_loaded: bool = False   # 是否已加载过详情

    def __post_init__(self):
        if not self.episode_count and self.episode_text:
            from .utils import parse_episode_count
            self.episode_count = parse_episode_count(self.episode_text)

    @property
    def url(self) -> str:
        from .config import URL_DETAIL
        return URL_DETAIL.format(did=self.did)

    @property
    def display_score(self) -> str:
        return f"{self.score}分" if self.score else ""


# ---------------------------------------------------------------- 专题

@dataclass
class Topic:
    """一个专题（/topics/ 页的卡片）。"""
    slug: str                     # 路径片段，如 "hot-aiduanju"
    title: str = ""
    cover: str = ""
    count_text: str = ""          # 如 "28部剧情"

    @property
    def url(self) -> str:
        from .config import URL_TOPIC
        return URL_TOPIC.format(slug=self.slug)


# ---------------------------------------------------------------- 分集

@dataclass
class Episode:
    """单集。"""
    ep: int                       # 集号，从 1 开始
    title: str = ""               # 分集标题（可为空）
    play_url: str = ""            # m3u8 地址（需现抓现用，auth_key 有时效）

    @property
    def label(self) -> str:
        return f"第{self.ep:02d}集"


# ---------------------------------------------------------------- 下载任务

class TaskStatus(Enum):
    PENDING = auto()      # 排队中
    FETCHING = auto()     # 正在获取播放地址
    DOWNLOADING = auto()  # 下载中
    MERGING = auto()      # 合并/转封装中
    DONE = auto()         # 完成
    FAILED = auto()       # 失败
    CANCELLED = auto()    # 已取消


STATUS_TEXT = {
    TaskStatus.PENDING: "排队中",
    TaskStatus.FETCHING: "获取地址",
    TaskStatus.DOWNLOADING: "下载中",
    TaskStatus.MERGING: "合并中",
    TaskStatus.DONE: "完成",
    TaskStatus.FAILED: "失败",
    TaskStatus.CANCELLED: "已取消",
}


@dataclass
class DownloadTask:
    """一个下载任务 = 一集。

    site_id 在**入队时固化**：任务的取址、写库、落盘目录都必须按它自己的站点来，
    不能读「此刻的全局站点」。否则用户在队列运行中切换站点，剩余任务就会拿
    另一个站点的后端去请求另一个域名（表现为整片 404）。
    """
    did: int
    ep: int
    drama_title: str
    cover: str = ""                  # 卡片封面 URL（用于生成 poster.jpg 的兜底源）
    site_id: str = ""                # 任务所属站点（空 = 跟随当前站点，兼容旧流程）
    category: str = ""               # 归属分类（换脸/魔改 等单集类会落到独立子目录）
    # 批量准备阶段已经抓到的详情页结果（标题/简介/封面/集数）。
    # 带着它入队，下载线程的元数据归纳就无需**再抓一次详情页**：
    # 200 部剧可省下约 110 秒的请求，避免下载线程被元数据请求堵住。
    detail: dict = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING

    # 进度
    percent: float = 0.0          # 0~100
    size_text: str = ""           # 已下载/总大小
    speed_text: str = ""          # 速度
    eta_text: str = ""            # 剩余时间

    # 结果
    output_path: str = ""
    error: str = ""

    # 运行时
    proc: Optional[object] = field(default=None, repr=False)  # subprocess.Popen

    @property
    def name(self) -> str:
        from .utils import ep_suffix
        return f"{self.drama_title} {ep_suffix(self.ep)}"

    @property
    def status_text(self) -> str:
        return STATUS_TEXT.get(self.status, "")
