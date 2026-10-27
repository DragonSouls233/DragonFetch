# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""多站点注册表

为什么需要它
------------
本项目原先只认 PornHub：下载链路的 URL 是 PornHub 页面地址，刮削器也写死了
PornHub 的 DOM 结构。要支持"其他网站的视频"，第一步不是写解析器，而是先让
代码**知道某个 URL 属于哪个站点、这个站点该怎么处理**。

三条处理路径（按优先级）
------------------------
1. ``native``  —— 本项目原有链路。**只有 PornHub 走这条，且本模块不参与其中的
   任何解析/提取逻辑**。PornHub 的行为与接入本模块之前完全一致。
2. ``ytdlp``   —— yt-dlp 有可用 extractor。直接把页面 URL 交给现有 yt-dlp 下载
   链路即可，无需任何站点特定代码。
3. ``eaf``     —— yt-dlp 没有 extractor，改用 ``unofficial-api-for-<site>``
   包（PyPI，作者 EchterAlsFake）解析出直链，再把直链交给现有下载引擎。

实测依据（2026-09-20，yt-dlp 2026.08.19）
-----------------------------------------
``yt-dlp.exe --list-extractors`` 共 1752 个 extractor，与 Porn_Fetch 支持的
13 站逐一对照：10 站有 extractor，``porntrex`` / ``xfreehd`` / ``thumbzilla``
三站没有。故本表按该实测结果标注 ``ytdlp`` 字段。

许可
----
``unofficial-api-for-*`` 与 Porn_Fetch 同为 EchterAlsFake 出品，GPL-3.0-or-later，
与本项目（GPL-3.0-or-later）兼容。本文件**不复制其任何代码**，只做模块名登记与
惰性导入。
"""

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

__all__ = ["SiteSpec", "SITES", "detect_site", "get_site", "list_sites",
           "looks_like_list_page", "looks_like_direct_media", "LIST_PATH_HINTS"]



@dataclass(frozen=True)
class SiteSpec:
    """单个站点的静态描述。

    Attributes:
        key: 站点短名，同时用作日志/数据库里的站点标识。
        label: 人类可读名。
        domains: 域名后缀（匹配 ``host == d`` 或 ``host.endswith('.' + d)``）。
        ytdlp: yt-dlp 是否有可用 extractor（实测得出，不是猜的）。
        ytdlp_note: 实测备注（例如 tube8 已损坏）。
        eaf_module: ``unofficial-api-for-*`` 的 **import 名**（不是包名）。
        native: 是否由本项目原有链路处理（仅 PornHub）。
        list_paths: 形如列表页的 URL 路径片段，用于判断"这是不是列表页"。
        homepage_url: 站点主页。留空则用 ``homepage`` 从 ``domains[0]`` 推导。
    """

    key: str
    label: str
    domains: tuple
    ytdlp: bool = True
    ytdlp_note: str = ""
    eaf_module: Optional[str] = None
    native: bool = False
    list_paths: tuple = ()
    homepage_url: str = ""

    @property
    def homepage(self) -> str:
        """站点主页 URL（给"打开网站"按钮用）。

        默认 ``https://www.<domains[0]>``。这个默认值是**实测过**的
        （2026-09-20，13 站逐个 HEAD）：带 ``www.`` 全部可达，部分站 301
        到裸域名，浏览器会自动跟随。xhamster / beeg 的裸域名更直接，
        但带 www 也能通，故不必逐站特例。
        """
        if self.homepage_url:
            return self.homepage_url
        domain = self.domains[0] if self.domains else ""
        if not domain:
            return ""
        if domain.startswith("www."):
            return f"https://{domain}"
        return f"https://www.{domain}"

    @property
    def needs_eaf_resolution(self) -> bool:
        """是否需要先用 unofficial-api 解析出直链。

        仅当 yt-dlp 没有该站的 extractor、且该站不由原有链路处理时才为真。
        """
        return (not self.ytdlp) and (not self.native) and bool(self.eaf_module)


# 通用列表页路径提示：命中任一即认为"可能是一个视频列表页"
LIST_PATH_HINTS = (
    "/pornstar/", "/model/", "/models/", "/channel/", "/channels/",
    "/user/", "/users/", "/profile/", "/profiles/", "/amateur/",
    "/search", "/category/", "/categories/", "/tag/", "/tags/",
    "/pornstars/", "/studio/", "/studios/", "/playlist/",
)


SITES = (
    # ===== 本项目原有链路：任何字段的改动都不允许影响它的下载行为 =====
    SiteSpec(
        key="pornhub", label="PornHub",
        domains=("pornhub.com",),
        ytdlp=True, native=True,
        eaf_module="pornhub_api",
        list_paths=("/pornstar/", "/model/", "/channels/", "/users/",
                    "/playlist/", "/video/", "/view_video.php"),
    ),
    # ===== yt-dlp 原生支持（10 站） =====
    SiteSpec(key="xvideos", label="XVideos", domains=("xvideos.com", "xvideos.es"),
             ytdlp=True, eaf_module="xvideos_api",
             list_paths=("/profiles/", "/channels/", "/model/", "/pornstar/")),
    SiteSpec(key="xnxx", label="XNXX", domains=("xnxx.com",),
             ytdlp=True, eaf_module="xnxx_api",
             list_paths=("/profiles/", "/pornstar/", "/model/", "/channels/")),
    SiteSpec(key="eporner", label="Eporner", domains=("eporner.com",),
             ytdlp=True, eaf_module="eporner_api",
             list_paths=("/profile/", "/pornstar/", "/channel/")),
    SiteSpec(key="xhamster", label="xHamster", domains=("xhamster.com", "xhamster.desi"),
             ytdlp=True, eaf_module="xhamster_api",
             list_paths=("/pornstars/", "/channels/", "/creators/", "/users/")),
    SiteSpec(key="spankbang", label="SpankBang", domains=("spankbang.com",),
             ytdlp=True, eaf_module="spankbang_api",
             list_paths=("/profile/", "/pornstar/", "/channel/")),
    SiteSpec(key="youporn", label="YouPorn", domains=("youporn.com",),
             ytdlp=True, eaf_module="youporn_api",
             list_paths=("/pornstar/", "/channel/", "/model/")),
    SiteSpec(key="beeg", label="Beeg", domains=("beeg.com",),
             ytdlp=True, eaf_module="beeg_api",
             list_paths=("/profile/", "/channel/", "/pornstar/")),
    SiteSpec(key="redtube", label="RedTube", domains=("redtube.com",),
             ytdlp=True, eaf_module="redtube_api",
             list_paths=("/pornstar/", "/channel/", "/model/")),
    SiteSpec(key="tube8", label="Tube8", domains=("tube8.com",),
             ytdlp=True, ytdlp_note="yt-dlp extractor 标记为 CURRENTLY BROKEN，"
                                    "实际下载优先走 unofficial-api 兜底",
             eaf_module="tube8_api",
             list_paths=("/pornstar/", "/channel/", "/model/")),
    # ===== yt-dlp 无 extractor，必须走 unofficial-api（3 站） =====
    SiteSpec(key="porntrex", label="PornTrex", domains=("porntrex.com",),
             ytdlp=False, eaf_module="porntrex_api",
             list_paths=("/model/", "/channels/", "/channel/", "/pornstar/")),
    SiteSpec(key="xfreehd", label="xFreeHD", domains=("xfreehd.com",),
             ytdlp=False, eaf_module="xfreehd_api",
             list_paths=("/pornstar/", "/channels/", "/model/", "/channel/")),
    SiteSpec(key="thumbzilla", label="Thumbzilla", domains=("thumbzilla.com",),
             ytdlp=False, eaf_module="thumbzilla_api",
             list_paths=("/pornstar/", "/channels/", "/model/", "/channel/")),
)

_BY_KEY = {s.key: s for s in SITES}


def _host_matches(host: str, domain: str) -> bool:
    """域名后缀匹配，避免 ``notpornhub.com`` 被误判成 ``pornhub.com``。"""
    return host == domain or host.endswith("." + domain)


def detect_site(url: str) -> Optional[SiteSpec]:
    """从 URL 识别站点。

    Args:
        url: 任意 URL。

    Returns:
        命中的 :class:`SiteSpec`；未收录或无法解析时返回 ``None``
        （调用方应保持原有行为，不要因为识别失败就改变处理方式）。
    """
    if not url or not isinstance(url, str):
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    for spec in SITES:
        for domain in spec.domains:
            if _host_matches(host, domain):
                return spec
    return None


def get_site(key: str) -> Optional[SiteSpec]:
    """按 key 取站点描述。"""
    return _BY_KEY.get(key)


def list_sites() -> tuple:
    """返回全部站点描述（只读元组）。"""
    return SITES


def looks_like_list_page(url: str) -> bool:
    """粗略判断 URL 像不像"列表页"（模特/频道/搜索/分类）。

    只做路径片段匹配，不抓页面。识别不出来时返回 ``False``——
    宁可少报，也不要让单视频 URL 被当成列表去翻页。
    """
    spec = detect_site(url)
    if spec is None:
        return False
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        return False
    hints = tuple(spec.list_paths) + tuple(LIST_PATH_HINTS)
    return any(h in path for h in hints)


# 直接媒体地址的扩展名（**已归一化掉结尾的 `/`** 再比对）
_DIRECT_MEDIA_EXTS = (
    ".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".flv", ".wmv",
    ".m3u8", ".mpd", ".ts",
)

# CDN / 直链的路径特征。有些直链不带扩展名（如带签名的 /get_file/...），
# 靠这些片段兜住。
_DIRECT_MEDIA_PATH_HINTS = (
    "/get_file/",            # porntrex / xfreehd 这类 CDN
    "/media/", "/hls/", "/dash/", "/stream/",
    "/video/download", "/download/", "/playlist.m3u8",
)


def looks_like_direct_media(url: str) -> bool:
    """URL 是否**已经**是可直接下载的媒体地址（因此无需再解析直链）。

    为什么需要它
    ------------
    ``SiteService.needs_resolution()`` 只看"站点是否需要站点包解析"，
    不看"这个 URL 本身是什么"。于是当下载链路拿到**已经是直链**的 URL
    （例如用户直接粘了 ``..._1080p.mp4``）时，仍会把它当成页面 URL 送去
    ``unofficial-api`` 解析 —— 上游会把 144MB 的 mp4 当 HTML 去 load，
    超时 30s × 4 次重试全部失败（白等 ~2 分钟），最后才回退。

    ⚠️ 真实事故：2026-09-20 端到端测试中发现，porntrex 直链因此白白多花了约 2 分钟。

    判定刻意偏保守：只要"看起来就是媒体文件"就返回 ``True`` —— 返回 True 的
    代价只是"跳过一次没必要的解析"，返回 False 的代价是"白等两分钟"。
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    # 结尾的 `/` 要先去掉：porntrex 的直链形如 `..._1080p.mp4/`
    # （实测其 CDN 接受这种形式，**不要**改动 URL 本身，这里只用于判断）。
    path = (parsed.path or "").rstrip("/").lower()
    if not path:
        return False
    if path.endswith(_DIRECT_MEDIA_EXTS):
        return True
    return any(hint in path for hint in _DIRECT_MEDIA_PATH_HINTS)
