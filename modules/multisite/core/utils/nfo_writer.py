# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""NFO 生成 —— Emby / Kodi 媒体库元数据

为什么需要
==========
刮削链路（``modules/scraper/``）把演员资料与视频列表写成了
``data/scraper/performers/{名}/info.json`` + ``videos.json``，
但**媒体库（Emby / Jellyfin / Kodi）根本不读 JSON** —— 它读的是
**与视频文件同名的 ``.nfo``**。

实测全仓 ``grep -rniI nfo`` **0 处真实命中**：本功能此前**从未实现**。
后果是媒体库里所有视频都是「文件名当标题、无演员、无标签、无时长」的裸状态，
刮削出来的 189 个字段一个都没进库。

本模块做三件事
==============
1. :func:`build_movie_nfo` —— 视频字典 → ``<movie>`` XML（Kodi/Emby 规范）
2. :func:`build_person_nfo` —— 演员字典 → ``<person>`` XML
3. :func:`write_nfo` —— **原子写**（``.tmp`` + ``os.replace``）

以及两种落盘模式（见 :class:`NfoGenerator`）：

======================  ==========================================  ==================
模式                    输出                                            适用
======================  ==========================================  ==================
``scraped``（默认）     ``performers/{名}/person.nfo``                 备份 / 二次加工
                        ``performers/{名}/nfo/{viewkey}.nfo``
``media DIR``           ``DIR/**/<视频文件名>.nfo``                     媒体库直接可用
======================  ==========================================  ==================

⚠️ 本项目踩过的坑，本模块逐条设防
================================
**1. 只写真实存在的字段，缺就不写标签。**
   本项目的上次事故（手册 N2）正是「页面重定向到聚合列表页 → 把别人的数据当自己的存库」。
   宁可真值少，也不要编造。**不写空标签、不写占位符、不拿标题冒充简介。**

**2. ``<rating>`` 单位必须换算。**
   PornHub 的评分是 **0~100 百分制**，而 Kodi / Emby 的 ``<rating>`` 是 **0~10**。
   直接写 85 会让库里显示 **850%**。本模块 ``/10`` 后再写。

**3. ``<runtime>`` 单位是「分钟」。**
   数据源给的是 ``"12:34"`` 字符串，必须解析成秒再换算，不能直接塞字符串。

**4. ``<uniqueid>`` 必须带 ``type`` 且恰有一个 ``default="true"``。**
   缺 ``default`` 时 Emby 会把它当「无外部 ID」，同一视频重复入库。

**5. XML 1.0 非法控制字符必须先剥掉。**
   PornHub 标题里偶尔混入 ``\\x00-\\x08`` 等字符，
   直接 ``html.escape`` 仍然会产出**解析不了的 XML**（Emby 直接报错拒绝整个文件）。

**6. 默认 dry-run、默认不覆盖。**
   ``scraped`` 模式写的是本项目自己的目录，风险低；
   但 ``media`` 模式写的是**用户的媒体库**，所以两者统一：
   **不加 ``--confirm`` 一律只出计划不落盘**；
   **不加 ``--overwrite`` 一律跳过已存在的 NFO**。

> 标签集合参照 Kodi 官方 NFO 模板（``kodi.wiki/view/NFO_files/Templates``）与
> Emby 的 movie/person NFO 解析器。**未复制任何参考项目代码**：
> ``G:\\MDCX\\.references`` 下的 ``ref22-mdcx-diy/mdcx/core/nfo.py`` 是
> JAV 专用标签集（``num``/``label``/``tmdbid``/``series`` 等），字段对不上，
> 只作「有这些标签可用」的清单参考；``ref24-CommunityScrapers`` 是 AGPL-3.0，
> 与本项目 GPL-3.0-or-later 组合会触发 AGPL §13 网络条款，**一律不取用**。

用法
====
::

    # 1) 先看计划（不落盘）—— 永远是第一步
    python -m modules.core.utils.nfo_writer --all

    # 2) 确认无误后真写（刮削目录）
    python -m modules.core.utils.nfo_writer --performer riley-reid --confirm

    # 3) 写到媒体库，与视频文件同名
    python -m modules.core.utils.nfo_writer --all --media "O:/男模" --confirm

    # 4) 在线增强（补简介/上传日期/评分，需代理）
    python -m modules.core.utils.nfo_writer --performer riley-reid --online --confirm

⚠️ 本模块**只创建 ``.nfo`` 文件**，不修改、不移动、不删除任何视频文件。
   已存在且未加 ``--overwrite`` 的 NFO 一律原样保留。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as _xml_escape

logger = logging.getLogger(__name__)

__all__ = [
    "NFO_EXT",
    "MOVIE_ROOT",
    "PERSON_ROOT",
    "NfoReport",
    "write_nfo",
    "build_movie_nfo",
    "build_person_nfo",
    "NfoGenerator",
    "main",
]

#: NFO 扩展名（Emby / Kodi 都认这个小写形式）
NFO_EXT = ".nfo"

#: 两个根标签
MOVIE_ROOT = "movie"
PERSON_ROOT = "person"

#: ``<uniqueid type="...">`` 的类型名 —— 数据全来自 PornHub 的 viewkey
UNIQUEID_TYPE = "pornhub"

#: 本项目的内容分级（固定值，不是从数据源猜的）
MPAA_RATING = "XXX"

#: 一次性写入的上限（防止 ``--media`` 误指到整个盘）
DEFAULT_MAX_WRITES = 500

#: 文件名安全长度（Windows 单段上限 255，留出 ``.nfo`` 与同名视频的余量）
MAX_STEM_LEN = 180

#: XML 1.0 **不允许**的控制字符。
#: 注意保留 ``\\t \\n \\r``（``\\x09 \\x0a \\x0d``）—— 它们是合法的。
_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: 从视频文件名里捞 viewkey 候选用的 token 正则
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{8,20}")

#: ``mm:ss`` / ``hh:mm:ss`` / ``m:ss``
_DURATION_RE = re.compile(r"^(?:(\d{1,3}):)?(\d{1,3}):(\d{1,2})$")

#: ``2019-05-08`` / ``2019/05/08`` / ``2019-05-08T12:00:00+00:00``
_DATE_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")

#: 站点 key → 展示名（``<studio>`` 用）
_STUDIO_NAMES = {
    "pornhub": "PornHub",
    "porntrex": "PornTrex",
    "xfreehd": "xFreeHD",
    "thumbzilla": "Thumbzilla",
    "tube8": "Tube8",
    "youporn": "YouPorn",
    "redtube": "RedTube",
    "xhamster": "xHamster",
    "spankbang": "SpankBang",
    "beeg": "Beeg",
    "eporner": "EPorner",
    "tnaflix": "TNAFlix",
    "hqporner": "HQPorner",
}

#: ``info.attributes`` 里能对上 Kodi ``<person>`` 标准标签的键
#: （PornHub 的 infoPiece 键名见 ``pornhub_scraper._INFO_PIECE_KEY_MAP``）
_PERSON_ATTR_TAGS = {
    "birth_place": "birthplace",
    "birthplace": "birthplace",
    "birth_date": "birthday",
    "birthday": "birthday",
    "date_of_birth": "birthday",
    "gender": "gender",
    "height": "height",
    "weight": "weight",
    "measurements": "measurements",
    "ethnicity": "ethnicity",
    "hair_color": "haircolor",
    "eye_color": "eyecolor",
    "eyes": "eyecolor",
    "nationality": "nationality",
    "deathday": "deathday",
}

#: 演员主页 URL 尾巴上**不是 slug** 的路径段。
#: ⭐ 实测：``info.json`` 里存的常常是
#: ``https://cn.pornhub.com/model/ada-kham/videos`` ——
#: 直接 ``url.rsplit("/", 1)[-1]`` 会得到 ``"videos"``，
#: 于是所有演员的 ``<uniqueid>`` 全变成 ``videos``（媒体库里会互相覆盖）。
_TRAILING_NON_SLUG = frozenset({
    "videos", "photos", "about", "playlists", "favorites", "gifs",
    "channels", "pornstars", "models", "subscribers", "upload",
})

#: 演员**资料稀疏**的判据字段（用于 ``--skip-sparse`` 与报告标注）
_SPARSE_FIELDS = ("bio", "avatar_url", "cover_url", "country",
                  "subscribers", "video_count", "views", "joined_date")

logger.addHandler(logging.NullHandler())


# ==================== 文本与转义 ====================


def _clean_text(value: Any) -> str:
    """把任意值转成「可直接进 XML」的文本。

    做三件事，缺一不可：

    1. ``None`` / 空 → 空串（调用方据此决定**不写这个标签**）
    2. 剥掉 XML 1.0 非法控制字符（``\\x00-\\x08`` 等）
    3. 统一换行、去掉首尾空白

    ⚠️ 只做转义前处理，**不做** ``&``/``<``/``>`` 转义 ——
    那是 :func:`_escape` 的职责，分开才能让调用方先判断「是不是空」。
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [_clean_text(v) for v in value]
        return ", ".join(p for p in parts if p)
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str):
        value = str(value)

    value = _XML_ILLEGAL_RE.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value.strip()


def _escape(value: str) -> str:
    """XML 转义。

    ⚠️ 不能只 ``html.escape``：``html.escape`` 默认会把 ``'`` 和 ``"`` 也转成
    实体（``&#x27;`` / ``&quot;``），对**文本节点**来说无害、对**属性值**是必要的；
    但对 ``&#x27;`` 这类实体，Kodi 老版本解析器偶有兼容问题。
    这里只转三个必需字符（``& < >``），属性值由 :func:`_attr` 单独处理引号。
    """
    return _xml_escape(value, {'"': "&quot;", "'": "&apos;"})


def _attr(value: str) -> str:
    """属性值转义（把 `"` 也转掉）。"""
    return _xml_escape(value, {'"': "&quot;", "'": "&apos;"})


def _tag(name: str, value: Any, indent: str = "  ") -> str:
    """有值才产出一行标签；空值返回空串。

    ⭐ 这是「**只写真实存在的字段**」这条纪律的落地点：
    全模块所有标签都走这里，因此不可能出现 ``<plot></plot>`` 这种空壳 ——
    Emby 对空标签的处理是「覆盖掉已联网抓到的简介」，比不写还糟。
    """
    text = _clean_text(value)
    if not text:
        return ""
    return f"{indent}<{name}>{_escape(text)}</{name}>\n"


def _strip_number_prefix(text: str) -> str:
    """去掉 ``123. `` / ``12) `` 这类编号前缀。

    PornHub 的 infoPiece 偶尔带序号（``1. Blonde``），进媒体库不好看。

    ⭐ 本函数的判据有一个**实测踩到的坑**（S6 回归脚本 A12b 抓出）：
    写成 ``^\\d{1,3}\\s*[.)、:]`` 会把 ``5.5 inches`` 砍成 ``5 inches`` ——
    因为 ``5`` + ``.`` 也满足「数字 + 句点」。
    所以分隔符后**必须跟一个「非数字且非空白」字符**（``(?=[^\\d\\s])``）。

    ⚠️ 不能只写 ``(?=\\D)``：``\\D`` 把**空格**也算通过，
    于是 ``1. 2 Blondes`` 会匹配到 ``"1."``（``\\s*`` 回退成空），
    照样被砍成 ``2 Blondes``。必须同时排除空白。

    另外**只在「前缀后仍有内容」时剥离**，否则 ``"3."`` 会变成空。
    """
    m = re.match(r"^\s*\d{1,3}\s*[.)、:]\s*(?=[^\d\s])", text)
    if not m:
        return text
    stripped = text[m.end():].strip()
    return stripped or text


# ==================== 单位换算 ====================


def parse_duration_seconds(value: Any) -> int:
    """``"12:34"`` / ``"1:02:03"`` / ``754`` / ``"754s"`` → 秒。

    无解析则返回 0（调用方据此不写 ``<runtime>``）。
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))

    text = _clean_text(value)
    if not text:
        return 0

    m = _DURATION_RE.match(text)
    if m:
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2))
        seconds = int(m.group(3))
        return hours * 3600 + minutes * 60 + seconds

    m = re.match(r"^(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?$", text, re.IGNORECASE)
    if m:
        return int(float(m.group(1)))

    # 纯数字：当作秒。⚠️ 只在字符串本身长得像秒数时才接受，
    # 否则 "2024" 这种年份会被当成 33 分钟。
    if re.fullmatch(r"\d{1,7}", text):
        n = int(text)
        if n >= 60:
            return n
    return 0


def duration_to_minutes(value: Any) -> int:
    """→ ``<runtime>`` 用的**分钟**（Kodi / Emby 规范）。

    ⚠️ 取整而不是截断：``754`` 秒 → ``13`` 分钟（不是 ``12``）。
    短视频也至少给 1 分钟，避免 Emby 判成「0 分钟」而显示为未知。
    """
    seconds = parse_duration_seconds(value)
    if seconds <= 0:
        return 0
    return max(1, int(round(seconds / 60.0)))


def parse_rating_10(value: Any) -> Optional[float]:
    """PornHub 评分 → **0~10**（Kodi / Emby 的 ``<rating>`` 量纲）。

    ⭐ ⭐ 这是本模块最容易错的一处：PornHub 是**百分制**。
    直接写 ``85`` → 媒体库显示 **850%**。

    规则：
    * ``> 10`` 的值视为百分制 → ``/10``
    * ``0 < v <= 10`` 原样保留（数据源可能本来就是十分制）
    * ``0`` / 负值 / 解析失败 → ``None``（**不写标签**，
      因为「0 分」与「没有评分」在媒体库里是完全不同的两件事）
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    if num > 10:
        num = num / 10.0
    num = min(10.0, num)
    return round(num, 2)


def normalize_date(value: Any) -> str:
    """→ ``YYYY-MM-DD``；解析不出返回空串。"""
    text = _clean_text(value)
    if not text:
        return ""
    m = _DATE_RE.search(text)
    if not m:
        return ""
    year, month, day = (int(g) for g in m.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def _positive_int(value: Any) -> int:
    """非负整数；失败返回 0（0 = 不写标签）。"""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(float(str(value).strip().replace(",", ""))))
    except (TypeError, ValueError):
        return 0


# ==================== 文件名安全 ====================


def extract_slug(url: str) -> str:
    """从演员主页 URL 里取出**真正的 slug**。

    ⭐ 为什么不能直接取最后一段：``info.json`` 里存的常常是
    ``https://cn.pornhub.com/model/ada-kham/videos`` ——
    ``rsplit("/", 1)[-1]`` 会得到 ``"videos"``。
    这样写出来的 ``<uniqueid>`` 全库同名，媒体库会把所有演员当成同一个人。
    （手册 N2 的同类问题：**看着像标识符的东西，先验证它真的是标识符**。）

    末尾的 ``/videos`` ``/photos`` ``/about`` 等一律剥掉，再取最后一段。
    """
    text = _clean_text(url)
    if not text:
        return ""
    path = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    # 去掉 scheme://host
    path = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+", "", path)
    segments = [seg for seg in path.split("/") if seg]
    while segments and segments[-1].lower() in _TRAILING_NON_SLUG:
        segments.pop()
    return segments[-1] if segments else ""


def canonical_profile_url(url: str) -> str:
    """去掉 ``/videos`` 之类的尾巴，得到演员**主页** URL（没尾巴就原样返回）。"""
    text = _clean_text(url)
    if not text:
        return ""
    slug = extract_slug(text)
    if not slug:
        return text
    idx = text.rfind(slug)
    if idx < 0:
        return text
    return text[: idx + len(slug)]


def is_sparse(info: Dict[str, Any]) -> bool:
    """演员资料是否**稀疏**（``info.json`` 里所有关键字段都空）。

    实测本机 6 个演员目录里 5 个是这样的（抓取失败/页面被重定向时留下的空壳），
    若照样产出 ``person.nfo``，媒体库里会多出一堆只有名字的空条目。
    CLI 的 ``--skip-sparse`` 用它决定是否跳过（**默认不跳过** ——
    稀疏是数据问题，不是 NFO 的问题，交给用户判断）。
    """
    info = info or {}
    if _clean_text(info.get("bio")):
        return False
    for key in _SPARSE_FIELDS:
        value = info.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > 0:
                return False
        elif _clean_text(value):
            return False
    return True


def safe_filename(name: str, fallback: str = "unknown") -> str:
    """→ 可安全落盘的**单段**文件名。

    比 ``scraper_storage._get_performer_dir`` 更严：那里只过滤 ``<>:"/\\|?*``，
    但对 ``CON``/``NUL`` 这类 **Windows 保留设备名**、结尾的点与空格没有处理 ——
    后者在 Windows 上会**静默去掉尾点**，导致「写出来的文件名和预期不一样」。
    """
    text = _clean_text(name)
    if not text:
        return fallback

    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
    text = text.strip(". ")
    text = re.sub(r"\s+", " ", text)

    stem_upper = text.split(".")[0].upper()
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if stem_upper in reserved:
        text = f"_{text}"

    if len(text) > MAX_STEM_LEN:
        text = text[:MAX_STEM_LEN].rstrip(". ")
    return text or fallback


# ==================== XML 构造：movie ====================


def _actor_block(names: Iterable[str], indent: str = "  ") -> str:
    """``<actor>`` 块。第一个是主演（``<order>0</order>``）。

    Emby 用 ``<order>`` 决定演员在详情页的排序；不写则按出现顺序，也可接受，
    但显式写更稳（不同版本 Emby 行为不一致）。
    """
    out: List[str] = []
    seen: set = set()
    order = 0
    for raw in names:
        name = _clean_text(raw)
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{indent}<actor>\n")
        out.append(f"{indent}  <name>{_escape(name)}</name>\n")
        out.append(f"{indent}  <order>{order}</order>\n")
        out.append(f"{indent}</actor>\n")
        order += 1
    return "".join(out)


def _multi_tag(name: str, values: Iterable[Any], indent: str = "  ") -> str:
    """一个值一个标签（``<genre>``/``<tag>`` 的规范写法），去重保序。"""
    out: List[str] = []
    seen: set = set()
    for raw in values or []:
        text = _strip_number_prefix(_clean_text(raw))
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{indent}<{name}>{_escape(text)}</{name}>\n")
    return "".join(out)


def build_movie_nfo(video: Dict[str, Any], performer: Optional[Dict[str, Any]] = None,
                    *, online: Optional[Dict[str, Any]] = None) -> str:
    """视频字典 → ``<movie>`` NFO 文本。

    Args:
        video: 来自 ``videos.json`` 的字典（或 :class:`VideoSummary` 的 ``__dict__``）。
            认得的键：``title/url/viewkey/source/thumbnail_url/duration/views``
            /``performer_name``，以及**在线增强**才有的
            ``plot/upload_date/rating/likes/is_hd/tags/categories/pornstars``。
        performer: ``info.json`` 的字典（可选）。用于补
            ``display_name``、``avatar_url``、``country``，以及**主演**顺序。
        online: :meth:`PornHubScraper.get_video_detail` 的补充字段（可选）。
            与 ``video`` 合并，``online`` 优先 —— 因为在线拿到的是详情页，
            比列表页抓的字段更全。

    Returns:
        UTF-8 XML 文本（含 XML 声明，**不带 BOM**）。

    ⚠️ 返回的是**完整文件内容**，本函数不碰磁盘（可离线单测）。
    """
    merged: Dict[str, Any] = dict(video or {})
    if online:
        for key, value in online.items():
            if value not in (None, "", [], {}, 0):
                merged[key] = value

    title = _strip_number_prefix(_clean_text(merged.get("title"))) or ""
    viewkey = _clean_text(merged.get("viewkey"))
    url = _clean_text(merged.get("url"))
    source = (_clean_text(merged.get("source")) or "pornhub").lower()

    performer = performer or {}
    performer_name = _clean_text(merged.get("performer_name")) or _clean_text(
        performer.get("display_name")) or _clean_text(performer.get("name"))

    if not title and viewkey:
        # ⚠️ 用 viewkey 拼标题**只是避免产出无名条目**，不是「已抓到的标题」。
        # 记录到日志，便于事后发现「列表解析漏了标题」这类回归。
        title = f"Video {viewkey}"
        logger.warning("视频 %s 无标题，回退为 %s（检查列表解析）", viewkey, title)
    if not title:
        title = "Unknown"

    body: List[str] = []

    body.append(_tag("title", title))
    # ``originaltitle`` / ``sorttitle`` 与 title 同值是有意为之：
    # 我们没有「原文标题 / 译名」的区分，写同值可让 Emby 的排序与去重稳定。
    body.append(_tag("originaltitle", title))
    body.append(_tag("sorttitle", title))

    plot = _clean_text(merged.get("plot") or merged.get("description"))
    if plot:
        body.append(_tag("plot", plot))
        outline = plot.split("\n", 1)[0]
        if len(outline) > 300:
            outline = outline[:297].rstrip() + "..."
        body.append(_tag("outline", outline))

    body.append(_tag("runtime", duration_to_minutes(
        merged.get("duration_seconds") or merged.get("duration")) or None))

    premiered = normalize_date(
        merged.get("upload_date") or merged.get("premiered") or merged.get("releasedate"))
    body.append(_tag("premiered", premiered))
    body.append(_tag("releasedate", premiered))
    body.append(_tag("year", premiered[:4] if premiered else None))

    body.append(_tag("studio", _STUDIO_NAMES.get(source, source.title() if source else "")))
    body.append(_tag("mpaa", MPAA_RATING))
    # ⚠️ **不写 ``<director>``**：数据源里没有导演字段，把演员名塞进 ``<director>``
    # 会凭空造出一个角色（媒体库详情页会显示「导演: Riley Reid」）——
    # 正是本项目三令五申的「静默错数据」。宁缺毋滥。

    # ``categories`` → genre（Emby 的「类型」），``tags`` → tag（关键词）
    body.append(_multi_tag("genre", merged.get("categories") or []))
    tags: List[Any] = list(merged.get("tags") or [])
    if merged.get("is_hd"):
        tags.append("HD")
    body.append(_multi_tag("tag", tags))

    rating = parse_rating_10(merged.get("rating"))
    if rating is not None:
        body.append(_tag("rating", f"{rating:g}"))
    body.append(_tag("votes", _positive_int(merged.get("votes")) or None))

    actors: List[str] = []
    if performer_name:
        actors.append(performer_name)
    for extra in merged.get("pornstars") or []:
        actors.append(extra)
    body.append(_actor_block(actors))

    thumb = _clean_text(merged.get("thumbnail_url") or performer.get("avatar_url"))
    if thumb:
        body.append(f'  <thumb aspect="poster">{_escape(thumb)}</thumb>\n')
    cover = _clean_text(performer.get("cover_url"))
    if cover:
        body.append("  <fanart>\n")
        body.append(f"    <thumb>{_escape(cover)}</thumb>\n")
        body.append("  </fanart>\n")

    body.append(_tag("trailer", merged.get("preview_url")))

    if viewkey:
        # ⚠️ ``default="true"`` 必须**有且只有一个** —— 见模块 docstring 第 4 条。
        body.append(
            f'  <uniqueid type="{_attr(UNIQUEID_TYPE)}" default="true">'
            f"{_escape(viewkey)}</uniqueid>\n")
    if url:
        body.append(_tag("website", url))

    # 项目扩展标签：真实数据但没有对应标准标签，写出来供媒体库外的工具读取。
    # Emby / Kodi 会忽略未知标签，故无损。
    body.append(_tag("views", _positive_int(merged.get("views")) or None))

    return _wrap(MOVIE_ROOT, body)


def _wrap(root: str, body: Sequence[str]) -> str:
    """拼成完整 XML 文档。"""
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    return f"{head}<{root}>\n{''.join(body)}</{root}>\n"


# ==================== XML 构造：person ====================


def build_person_nfo(performer: Dict[str, Any]) -> str:
    """演员字典 → ``<person>`` NFO 文本。

    ⚠️ 文件名约定为 ``person.nfo``，即 Kodi 20 的
    ``.actors/{演员名}/person.nfo`` 规范文件名 ——
    本模块**只产出 NFO**（头像 ``.actors/{名}.jpg`` 按用户决定**暂不做**），
    但文件名对齐后，将来补头像时整个 ``.actors/{名}/`` 可直接搬过去。
    """
    performer = performer or {}
    name = _clean_text(performer.get("name"))
    display = _clean_text(performer.get("display_name")) or name
    if not display:
        display = "Unknown"

    body: List[str] = []
    body.append(_tag("title", display))
    body.append(_tag("name", name or display))
    body.append(_tag("sorttitle", display))

    body.append(_tag("biography", performer.get("bio")))

    # 描述型档案字段 → 标准标签
    attributes = performer.get("attributes") or {}
    mapped: Dict[str, str] = {}
    for key, value in attributes.items():
        text = _strip_number_prefix(_clean_text(value))
        if not text:
            continue
        tag_name = _PERSON_ATTR_TAGS.get(str(key).strip().lower())
        if tag_name and tag_name not in mapped:
            mapped[tag_name] = text

    # 国籍 / 出生地：优先 attributes（更精确），回落 country
    country = _clean_text(performer.get("country"))
    city = _clean_text(performer.get("city"))
    if "birthplace" not in mapped:
        birthplace = ", ".join(p for p in (city, country) if p)
        if birthplace:
            mapped["birthplace"] = birthplace
    if "nationality" not in mapped and country:
        mapped["nationality"] = country

    for tag_name in ("birthday", "birthplace", "deathday", "gender", "nationality",
                     "ethnicity", "haircolor", "eyecolor", "height", "weight",
                     "measurements"):
        if tag_name in mapped:
            body.append(_tag(tag_name, mapped[tag_name]))

    joined = normalize_date(performer.get("joined_date"))
    if joined:
        # 不是标准字段，但**是真实数据**：写在扩展标签里，不冒充 birthday。
        body.append(_tag("joined", joined))

    avatar = _clean_text(performer.get("avatar_url"))
    if avatar:
        body.append(f"  <thumb>{_escape(avatar)}</thumb>\n")
    cover = _clean_text(performer.get("cover_url"))
    if cover:
        body.append("  <fanart>\n")
        body.append(f"    <thumb>{_escape(cover)}</thumb>\n")
        body.append("  </fanart>\n")

    body.append(_multi_tag("tag", performer.get("tags") or []))

    # 未映射到标准标签的 infoPiece 条目 → 项目扩展标签（无损保留）
    for key, value in attributes.items():
        key_l = str(key).strip().lower()
        if key_l in _PERSON_ATTR_TAGS:
            continue
        text = _strip_number_prefix(_clean_text(value))
        if not text:
            continue
        body.append(
            f'  <attribute name="{_attr(str(key).strip())}">{_escape(text)}</attribute>\n')

    url = _clean_text(performer.get("url"))
    profile_url = canonical_profile_url(url)
    if profile_url:
        body.append(_tag("website", profile_url))
    slug = extract_slug(url) or name
    if slug:
        body.append(
            f'  <uniqueid type="{_attr(UNIQUEID_TYPE)}" default="true">'
            f"{_escape(slug)}</uniqueid>\n")

    body.append(_tag("subscribers", _positive_int(performer.get("subscribers")) or None))
    body.append(_tag("video_count", _positive_int(performer.get("video_count")) or None))
    body.append(_tag("views", _positive_int(performer.get("views")) or None))

    for link in performer.get("social_links") or []:
        if not isinstance(link, dict):
            continue
        platform = _clean_text(link.get("platform"))
        link_url = _clean_text(link.get("url"))
        if not link_url:
            continue
        if platform:
            body.append(
                f'  <social platform="{_attr(platform)}">{_escape(link_url)}</social>\n')
        else:
            body.append(f"  <social>{_escape(link_url)}</social>\n")

    return _wrap(PERSON_ROOT, body)


# ==================== 写盘 ====================


@dataclass
class NfoReport:
    """单个 NFO 的计划 / 结果。"""

    #: ``written`` 已写 / ``planned`` 待写（dry-run）/ ``skipped`` 已存在 /
    #: ``orphan`` 视频文件找不到元数据 / ``failed`` 写失败
    action: str
    path: str
    viewkey: str = ""
    performer: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.action in ("written", "planned")


def write_nfo(path: str, content: str, *, overwrite: bool = False,
              dry_run: bool = True) -> NfoReport:
    """把 NFO 内容写到 ``path``。

    ⭐ **原子写**：先写同目录 ``xxx.nfo.tmp`` 再 ``os.replace``。
    ``os.replace`` 在 Windows 上是原子的（同卷），因此不会出现
    「媒体库扫到一个半截 XML」的情况 —— 这是媒体库场景的实际故障源：
    Emby 的文件监视器会在写入过程中就尝试解析。

    ⭐ **默认不覆盖**：已存在的 NFO 一律 ``skipped``。
    理由见模块 docstring 第 6 条 —— 用户可能手工修过 NFO，
    而 ``--media`` 模式打的是**用户自己的媒体库**，不是我们的目录。

    Args:
        path: 目标 ``.nfo`` 路径
        content: 完整 XML 文本
        overwrite: True 才覆盖已存在的文件
        dry_run: True 只检查不落盘

    Returns:
        :class:`NfoReport`
    """
    target = Path(path)
    if target.exists() and not overwrite:
        return NfoReport("skipped", str(target), reason="已存在（未加 --overwrite）")

    if dry_run:
        return NfoReport("planned", str(target), reason="dry-run")

    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp, target)
    except OSError as exc:
        logger.error("写 NFO 失败 %s: %s", target, exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            logger.debug("清理临时文件失败: %s", tmp, exc_info=True)
        return NfoReport("failed", str(target), reason=str(exc))

    return NfoReport("written", str(target))


# ==================== 数据源 ====================


def default_scraped_root() -> Path:
    """``data/scraper/performers``（**单一真源**：``scraper_storage.PERFORMERS_DIR``）。

    ⚠️ 延迟导入：``modules.scraper`` 的 ``__init__`` 会拉起
    ``PornHubScraper``（bs4 / curl_cffi），而本模块的离线单测
    完全不需要网络栈。延迟到真正要路径时才 import。
    """
    try:
        from multisite.scraper.scraper_storage import PERFORMERS_DIR  # 延迟导入
        # ⚠️ 必须 resolve()：``scraper_storage`` 是用 ``".."`` 拼出来的相对路径
        # （``modules/scraper/../../data/...``），直接打印会得到一长串 ``\..\``，
        # 既难读，也会让「同一目录两种写法」的判等失效。
        return Path(PERFORMERS_DIR).resolve()
    except Exception:
        logger.debug("回退到本地推导路径", exc_info=True)

    base = Path(__file__).resolve().parents[2] / "data"
    return (base / "scraper" / "performers").resolve()


@dataclass
class PerformerDataset:
    """一个演员目录的完整数据集。"""

    name: str
    directory: Path
    info: Dict[str, Any] = field(default_factory=dict)
    videos: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return _clean_text(self.info.get("display_name")) or self.name


def _read_json(path: Path, default: Any) -> Any:
    """读 JSON；坏文件返回 ``default`` 并记日志（**不抛异常**，一个坏目录不该中断整批）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取失败 %s: %s", path, exc)
        return default


def load_performer_dir(directory: Path) -> Optional[PerformerDataset]:
    """从 ``performers/{名}/`` 读 ``info.json`` + ``videos.json``。

    只有 ``info.json`` 存在才认这是演员目录；
    ``videos.json`` 缺失时返回空列表（仍然可以产出 ``person.nfo``）。
    """
    info_path = directory / "info.json"
    if not info_path.is_file():
        return None

    info = _read_json(info_path, {})
    if not isinstance(info, dict):
        logger.warning("info.json 不是对象 %s", info_path)
        info = {}

    raw_videos = _read_json(directory / "videos.json", [])
    if isinstance(raw_videos, dict):
        # 兼容未来可能的 {"videos": [...]} 包装
        raw_videos = raw_videos.get("videos") or []
    videos = [v for v in raw_videos if isinstance(v, dict)] if isinstance(raw_videos, list) else []

    name = _clean_text(info.get("name")) or directory.name
    return PerformerDataset(name=name, directory=directory, info=info, videos=videos)


def iter_performer_datasets(root: Optional[Path] = None) -> Iterator[PerformerDataset]:
    """遍历刮削根目录下所有演员。

    ⚠️ **没有 ``info.json`` 的目录会被跳过并记一条 WARNING**：
    实测本机 ``performers/DiogoMatterazo/`` 就是这样一个空壳目录。
    静默跳过会让「6 个目录只处理了 5 个」变成无从解释的数字差。
    """
    base = Path(root) if root else default_scraped_root()
    if not base.is_dir():
        logger.warning("刮削根目录不存在: %s", base)
        return
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        dataset = load_performer_dir(child)
        if dataset is None:
            logger.warning("跳过（缺 info.json）: %s", child)
            continue
        yield dataset


def build_viewkey_index(root: Optional[Path] = None) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
    """``viewkey`` → ``(video_dict, performer_info)`` 全量索引。

    ``--media`` 模式的全部依据：媒体库里的视频文件**只能靠文件名里的 viewkey**
    与刮削数据对上。索引里没有的 viewkey → 判为孤儿，**不写 NFO**
    （宁可没有元数据，也不要张冠李戴）。
    """
    index: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    for dataset in iter_performer_datasets(root):
        for video in dataset.videos:
            key = _clean_text(video.get("viewkey")).lower()
            if not key or key in index:
                continue
            index[key] = (video, dataset.info)
    return index


def _candidate_viewkeys(stem: str) -> List[str]:
    """从视频文件名 stem 里捞出 viewkey 候选（保序去重）。

    覆盖本项目 ``FileNameFormatter`` 的两种命名：``[Artist] Title (viewkey)``
    与 ``[Artist] 2024-03-09 15:30; Title``（后者没有 viewkey，返回空）。

    ⚠️ 只产**候选**：调用方必须拿去索引里查，查不到就丢弃。
    单靠正则形状判断（比如「13 位十六进制」）会误伤标题里的普通单词。
    """
    out: List[str] = []
    seen: set = set()

    m = re.search(r"\(([A-Za-z0-9]{6,20})\)", stem)
    if m:
        token = m.group(1).lower()
        seen.add(token)
        out.append(token)

    for token in _TOKEN_RE.findall(stem):
        low = token.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(low)

    return out


# ==================== 生成器 ====================


class NfoGenerator:
    """NFO 生成器 —— 计划与执行分离。

    ``plan_*`` 一律**只读**（不碰磁盘），``execute`` 才写。
    这样 ``--confirm`` 前的 dry-run 报告与最终写入走的是**同一份计划**，
    不会出现「报告说 12 个、实际写了 13 个」的不一致。
    """

    def __init__(self, scraped_root: Optional[Path] = None):
        self.scraped_root = Path(scraped_root) if scraped_root else default_scraped_root()
        self._index: Optional[Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]] = None

    # ---------- 索引 ----------

    @property
    def index(self) -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
        if self._index is None:
            self._index = build_viewkey_index(self.scraped_root)
        return self._index

    def datasets(self, names: Optional[Sequence[str]] = None) -> List[PerformerDataset]:
        """取演员数据集；``names`` 为空 = 全部。"""
        wanted = {n.strip().lower() for n in (names or []) if n and n.strip()}
        out: List[PerformerDataset] = []
        for dataset in iter_performer_datasets(self.scraped_root):
            if wanted and dataset.name.lower() not in wanted \
                    and dataset.display_name.lower() not in wanted:
                continue
            out.append(dataset)
        return out

    def missing_names(self, names: Sequence[str],
                      datasets: Optional[Sequence[PerformerDataset]] = None) -> List[str]:
        """请求了但磁盘上没有的演员名（用于 CLI 明确告知，而不是静默少写）。

        Args:
            names: 用户请求的名字
            datasets: 已经算好的数据集（避免重复遍历目录 —— 每次遍历都会
                对缺 ``info.json`` 的目录打一遍 WARNING，重复遍历会刷屏）
        """
        pool = list(datasets) if datasets is not None else self.datasets(names)
        found = {d.name.lower() for d in pool} | {d.display_name.lower() for d in pool}
        return [n for n in names if n and n.strip().lower() not in found]

    # ---------- 刮削目录模式 ----------

    def plan_scraped(self, names: Optional[Sequence[str]] = None, *,
                     overwrite: bool = False,
                     dry_run: bool = True,
                     skip_sparse: bool = False) -> Tuple[List[Tuple[str, NfoReport]], List[NfoReport]]:
        """规划刮削目录输出。

        Returns:
            ``(person_plans, movie_plans)``。

            * ``person_plans``: ``(dir_path, report)`` —— 文件名叫 ``person.nfo``
            * ``movie_plans``: ``NfoReport`` —— 文件名叫 ``nfo/{viewkey}.nfo``

        ⚠️ 为什么视频 NFO 放 ``nfo/`` 子目录、而不是直接平铺：
        ``performers/{名}/`` 已经放了 ``info.json``/``videos.json``/图片，
        再平铺几十上百个 ``.nfo`` 会把「原始数据」和「派生数据」混在一起。
        子目录让整个 ``nfo/`` 可一次性删除重建（幂等）。
        """
        person_plans: List[Tuple[str, NfoReport]] = []
        movie_plans: List[NfoReport] = []

        for dataset in self.datasets(names):
            person_path = dataset.directory / "person.nfo"

            if skip_sparse and is_sparse(dataset.info):
                person_plans.append((str(dataset.directory), NfoReport(
                    "skipped", str(person_path), performer=dataset.name,
                    reason="资料稀疏（--skip-sparse）")))
                continue

            content = build_person_nfo(dataset.info)
            report = write_nfo(str(person_path), content,
                               overwrite=overwrite, dry_run=dry_run)
            report.performer = dataset.name
            if is_sparse(dataset.info):
                report.reason = "资料稀疏（无简介/头像/统计）"
            person_plans.append((str(dataset.directory), report))

            nfo_dir = dataset.directory / "nfo"
            for video in dataset.videos:
                viewkey = _clean_text(video.get("viewkey"))
                if not viewkey:
                    movie_plans.append(NfoReport(
                        "orphan", str(nfo_dir), performer=dataset.name,
                        reason="videos.json 条目缺 viewkey"))
                    continue
                target = nfo_dir / safe_filename(f"{viewkey}{NFO_EXT}", "video.nfo")
                movie_content = build_movie_nfo(video, dataset.info)
                rep = write_nfo(str(target), movie_content,
                                overwrite=overwrite, dry_run=dry_run)
                rep.viewkey = viewkey
                rep.performer = dataset.name
                movie_plans.append(rep)

        return person_plans, movie_plans

    # ---------- 媒体库模式 ----------

    def plan_media(self, media_dir: str, *, overwrite: bool = False,
                   dry_run: bool = True, recursive: bool = True,
                   limit: int = DEFAULT_MAX_WRITES,
                   online: Optional[Dict[str, Dict[str, Any]]] = None) -> List[NfoReport]:
        """规划媒体库输出：给每个**能认出来**的视频文件写同名 ``.nfo``。

        认出来的条件（**两者缺一不可**）：

        1. 文件名 stem 里能捞出 token，且该 token 在 :attr:`index` 里
        2. 该 token 对应到具体 video 字典

        ⚠️ 不满足就报 ``orphan``，**绝不猜**。

        Args:
            media_dir: 媒体库根目录
            overwrite: 允许覆盖已存在的 NFO
            dry_run: 只出计划
            recursive: 是否递归子目录
            limit: 上限（``<=0`` 视为不限）
            online: ``viewkey`` → 详情页字段（由 :func:`_online_enrich` 产出）。
                给定时**优先于** ``videos.json`` 的列表页字段。
        """
        from .download_artifacts import is_video_file  # 单一真源（约定 #16）

        base = Path(media_dir)
        if not base.is_dir():
            logger.error("媒体库目录不存在: %s", base)
            return []

        cap = limit if limit and limit > 0 else 10 ** 9
        online = online or {}
        reports: List[NfoReport] = []
        walker: Iterable[Path] = base.rglob("*") if recursive else base.glob("*")

        for path in sorted(walker):
            if len(reports) >= cap:
                logger.warning("达到写入上限 %d，已截断（用 --max 调整）", cap)
                break
            try:
                if not path.is_file() or not is_video_file(path):
                    continue
            except OSError:
                logger.debug("跳过不可读条目: %s", path, exc_info=True)
                continue

            hit: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None
            matched_key = ""
            for candidate in _candidate_viewkeys(path.stem):
                if candidate in self.index:
                    hit = self.index[candidate]
                    matched_key = candidate
                    break

            target = path.with_suffix(NFO_EXT)
            stem = target.name
            if len(stem) > 200:
                # Windows 单段上限 255：长标题 + .nfo 有溢出风险。
                # 但 NFO **必须**与视频同名才能被识别，所以这里不截断，
                # 只记警告（截断会让 NFO 变成孤儿文件，比不写更糟）。
                logger.warning("文件名过长（%d 字符），Emby 可能无法关联: %s",
                               len(stem), target.name)

            if hit is None:
                reports.append(NfoReport("orphan", str(target),
                                         reason="文件名里没有能对上刮削数据的 viewkey"))
                continue

            video, performer = hit
            content = build_movie_nfo(video, performer, online=online.get(matched_key))
            report = write_nfo(str(target), content,
                               overwrite=overwrite, dry_run=dry_run)
            report.viewkey = matched_key
            report.performer = _clean_text(performer.get("name"))
            reports.append(report)

        return reports


# ==================== 报告 ====================


def _summarize(reports: Sequence[NfoReport]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for rep in reports:
        counts[rep.action] = counts.get(rep.action, 0) + 1
    return counts


def _print_report(title: str, reports: Sequence[NfoReport], *, show: int = 20,
                  verbose: bool = False) -> None:
    counts = _summarize(reports)
    total = len(reports)
    print(f"\n=== {title} ===")
    if not total:
        print("  （无）")
        return

    order = ("written", "planned", "skipped", "orphan", "failed")
    labels = {
        "written": "已写入",
        "planned": "待写入(dry-run)",
        "skipped": "跳过（已存在）",
        "orphan": "无匹配（未写）",
        "failed": "失败",
    }
    for key in order:
        if counts.get(key):
            print(f"  {labels[key]:<16} {counts[key]}")

    for key in ("failed", "orphan", "skipped"):
        if key not in counts:
            continue
        if key != "failed" and not verbose:
            continue
        print(f"\n  --- {labels[key]} ---")
        shown = 0
        for rep in reports:
            if rep.action != key:
                continue
            print(f"    {rep.path}" + (f"  [{rep.reason}]" if rep.reason else ""))
            shown += 1
            if shown >= show:
                print(f"    ...（共 {counts[key]} 条）")
                break

    if "planned" in counts or "written" in counts:
        print("\n  --- 样例 ---")
        shown = 0
        for rep in reports:
            if rep.action not in ("planned", "written"):
                continue
            suffix = f"  [{rep.reason}]" if (verbose and rep.reason) else ""
            print(f"    {rep.path}{suffix}")
            shown += 1
            if shown >= show:
                print(f"    ...（共 {counts.get('planned', 0) + counts.get('written', 0)} 条）")
                break


# ==================== CLI ====================


def _default_proxy() -> str:
    """尝试从项目配置里拿代理；拿不到返回空串（= 直连）。"""
    try:
        from multisite.core.utils.proxy_config import get_global_proxy_config as _get_global_proxy_config
        value = _get_global_proxy_config()
        if isinstance(value, str):
            return value
    except Exception:
        logger.debug("读取全局代理配置失败", exc_info=True)
    return ""


def _online_enrich(viewkeys: Sequence[str], proxy: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """用 :meth:`PornHubScraper.get_video_detail` 增强视频字段。

    ⚠️ ``proxy`` 为 None = 直连 = **一定失败**（PornHub 在墙外）。
    CLI 里未显式给 ``--proxy`` 时会尝试读项目配置，仍为空则**明确警告并继续**
    （离线字段照常写），而不是静默产出半空的 NFO。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not viewkeys:
        return out
    if not proxy:
        logger.warning("--online 已开启但未配置代理，PornHub 将不可达；"
                       "仅写离线字段。请用 --proxy socks5://127.0.0.1:10808")
        return out

    try:
        from multisite.scraper import PornHubScraper
    except Exception as exc:
        logger.error("无法导入刮削器，跳在线增强: %s", exc)
        return out

    try:
        with PornHubScraper(proxy=proxy, use_cn=False) as scraper:
            for idx, viewkey in enumerate(viewkeys, 1):
                try:
                    detail = scraper.get_video_detail(viewkey)
                except Exception as exc:
                    logger.warning("[%d/%d] 取详情失败 %s: %s", idx, len(viewkeys), viewkey, exc)
                    continue
                if detail is None:
                    logger.warning("[%d/%d] 无详情 %s", idx, len(viewkeys), viewkey)
                    continue
                out[viewkey] = {
                    "title": getattr(detail, "title", None),
                    "duration": getattr(detail, "duration", None),
                    "duration_seconds": getattr(detail, "duration_seconds", None),
                    "views": getattr(detail, "views", 0),
                    "rating": getattr(detail, "rating", None),
                    "likes": getattr(detail, "likes", 0),
                    "is_hd": getattr(detail, "is_hd", False),
                    "upload_date": getattr(detail, "upload_date", None),
                    "tags": list(getattr(detail, "tags", []) or []),
                    "categories": list(getattr(detail, "categories", []) or []),
                    "pornstars": list(getattr(detail, "pornstars", []) or []),
                    "thumbnail_url": getattr(detail, "thumbnail_url", None),
                    "preview_url": getattr(detail, "preview_url", None),
                }
                logger.info("[%d/%d] 已增强 %s", idx, len(viewkeys), viewkey)
    except Exception as exc:
        logger.error("在线增强整体失败: %s", exc)

    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m modules.core.utils.nfo_writer",
        description="生成 Emby/Kodi NFO（默认 dry-run，只出计划不落盘）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python -m modules.core.utils.nfo_writer --all\n"
            "  python -m modules.core.utils.nfo_writer --performer riley-reid --confirm\n"
            "  python -m modules.core.utils.nfo_writer --all --media \"O:/男模\" --confirm\n"
        ),
    )
    parser.add_argument("--performer", action="append", default=[],
                        help="演员名（可重复）；不给则需 --all")
    parser.add_argument("--all", action="store_true", help="处理刮削目录下全部演员")
    parser.add_argument("--scraped-root", default="",
                        help="刮削根目录（默认 data/scraper/performers）")
    parser.add_argument("--scraped", action="store_true",
                        help="启用刮削目录模式（未给 --media 时自动启用）")
    parser.add_argument("--media", action="append", default=[],
                        help="媒体库目录（可重复）。给定时启用媒体库模式")
    parser.add_argument("--no-recursive", action="store_true",
                        help="媒体库模式不递归子目录")
    parser.add_argument("--confirm", action="store_true",
                        help="真正写盘（不加则 dry-run）")
    parser.add_argument("--overwrite", action="store_true",
                        help="允许覆盖已存在的 NFO（默认跳过）")
    parser.add_argument("--skip-sparse", action="store_true",
                        help="跳过资料稀疏的演员（info.json 里简介/头像/统计全空）")
    parser.add_argument("--online", action="store_true",
                        help="用 PornHub 详情页在线增强（需代理，慢）")
    parser.add_argument("--proxy", default="",
                        help="代理地址，如 socks5://127.0.0.1:10808")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_WRITES,
                        help=f"单次写入上限（默认 {DEFAULT_MAX_WRITES}）")
    parser.add_argument("--show", type=int, default=20, help="报告里每类最多打印多少条")
    parser.add_argument("--verbose", "-v", action="store_true", help="打开 DEBUG 日志")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.all and not args.performer:
        print("错误：请给 --performer NAME 或 --all", file=sys.stderr)
        return 2

    names = None if args.all else list(args.performer)
    root = Path(args.scraped_root) if args.scraped_root else None
    generator = NfoGenerator(root)

    # 默认模式：未指定 --media 时走刮削目录
    do_scraped = args.scraped or not args.media
    do_media = bool(args.media)
    dry_run = not args.confirm

    if dry_run:
        print("⚠️ dry-run 模式：只出计划，不落盘。确认无误后加 --confirm")

    online_map: Dict[str, Dict[str, Any]] = {}
    if args.online and do_media:
        # 媒体库模式才需要在线增强：那里的元数据只有 viewkey，
        # 离线 videos.json 的字段本就够支撑 scraped 模式的 NFO。
        keys: List[str] = []
        for media_dir in args.media:
            for rep in generator.plan_media(media_dir, dry_run=True,
                                           recursive=not args.no_recursive,
                                           limit=args.max):
                if rep.viewkey and rep.viewkey not in keys:
                    keys.append(rep.viewkey)
        online_map = _online_enrich(keys, args.proxy or _default_proxy() or None)
        print(f"在线增强: {len(online_map)}/{len(keys)} 个视频取到详情")

    exit_code = 0

    if do_scraped:
        datasets = generator.datasets(names)
        if names:
            for missing in generator.missing_names(names, datasets):
                print(f"⚠️ 刮削目录里没有演员: {missing}")
                exit_code = 1
        if not datasets:
            print("没有可处理的演员（刮削目录为空或名字不匹配）")
            exit_code = 1
        else:
            person_plans, movie_plans = generator.plan_scraped(
                names, overwrite=args.overwrite, dry_run=dry_run,
                skip_sparse=args.skip_sparse)
            person_reports = [rep for _, rep in person_plans]
            _print_report(f"person.nfo（{len(datasets)} 个演员）", person_reports,
                          show=args.show, verbose=args.verbose)
            _print_report(f"movie NFO（{len(movie_plans)} 条视频）", movie_plans,
                          show=args.show, verbose=args.verbose)
            if any(rep.action == "failed" for rep in person_reports + movie_plans):
                exit_code = 1

    for media_dir in args.media:
        reports = generator.plan_media(
            media_dir, overwrite=args.overwrite, dry_run=dry_run,
            recursive=not args.no_recursive, limit=args.max, online=online_map)
        _print_report(f"媒体库 NFO: {media_dir}", reports,
                      show=args.show, verbose=args.verbose)
        if any(rep.action == "failed" for rep in reports):
            exit_code = 1

    if dry_run:
        print("\n（未落盘。确认后重跑并加 --confirm）")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
