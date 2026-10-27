# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""PornHub 模特链接归一化 —— 供「快速加模特」去重比对使用。

为什么需要
==========
同一个模特在库里的 URL 与列表页抓到的 URL **形态并不相同**，实测（2026-10-17）::

    数据库里已收录的（678 条）：
        /model/<slug>/videos      642 条
        /pornstar/<slug>/videos    31 条
        /model/<slug>               2 条
    列表页抓到的（个人主页）：
        /pornstar/<slug>   或   /model/<slug>

若直接用 ``url.rsplit("/", 1)[-1]`` 取 slug，那 **674 条（99.4%）取到的都是
字面量 ``"videos"``** —— slug 键退化成单值、去重形同虚设，只能退化到靠名字匹配。
一旦名字写法有差异（``Jetsu4K`` vs ``jetsu 4k``）就会重复入库。

统一口径
========
本模块给出 **canonical slug**：

1. 去 ``#`` 锚点、``?`` 查询串、尾斜杠，统一小写；
2. 取 ``/pornstar/`` 或 ``/model/`` 之后的路径；
3. 若首段是纯数字（PornHub 的 ``/model/<id>/<slug>`` 形态）则跳过它，取真正的 slug；
4. 只取第一段 —— 丢弃 ``/videos``、``/photos`` 等功能尾段。

于是 ``/model/jetsu4k/videos`` 与 ``/pornstar/jetsu4k`` 都归一为 ``jetsu4k``，
GUI 与 WEB 两端用同一份实现比对，不会再出现口径漂移。
"""
import re

__all__ = ["canonical_slug", "canonical_url", "clean_model_name", "normalize_pornhub_url"]

_MODEL_PATH_RE = re.compile(r"/(?:pornstar|model)/(.+)$", re.I)


def canonical_slug(url: str) -> str:
    """把任意形态的 PornHub 模特链接归一为可比较的 slug（小写）。

    >>> canonical_slug("https://cn.pornhub.com/model/Jetsu4K/videos")
    'jetsu4k'
    >>> canonical_slug("https://cn.pornhub.com/pornstar/jetsu4k")
    'jetsu4k'
    >>> canonical_slug("https://cn.pornhub.com/model/12345/sweetie-fox")
    'sweetie-fox'
    >>> canonical_slug("")
    ''
    """
    if not url:
        return ""
    p = str(url).split("#")[0].split("?")[0].rstrip("/").lower()
    m = _MODEL_PATH_RE.search(p)
    segs = [s for s in (m.group(1) if m else p).split("/") if s]
    # /model/<数字id>/<slug> 形态：跳过纯数字段，拿真正的 slug
    if len(segs) >= 2 and segs[0].isdigit():
        segs = segs[1:]
    return segs[0] if segs else ""


def canonical_url(url: str) -> str:
    """归一化完整链接（去锚点/查询串/尾斜杠并小写），用于「完全相同」这一级比对。"""
    if not url:
        return ""
    return str(url).split("#")[0].split("?")[0].rstrip("/").lower()


# ---------------------------------------------------------------- 名字清洗
# PornHub 的模特名会混进站点固定文案尾巴，中英文都有。实测踩过的两种：
#   "Amber Griz的色情片"        ← 中文尾巴（cn.pornhub 简体站）
#   "Jane Doe's Porn Videos"   ← 英文尾巴
# 不清洗就直接入库，名字会污染 local_directory（目录名也跟着变成
# ``[Channel] Amber Griz的色情片``），并且和站点真实 slug 对不上、影响去重。

# 站方文案标记（从右往左定位到最后一个出现处，直接切掉其后全部内容）
_TAIL_MARKERS = (
    "的色情片", "的視頻", "的视频", "的影片", "的成人视频",
    "色情片", "視頻", "视频",
)

# 剥离尾巴后可能残留的年份/编号尾巴（如 "…色情片2026" 被剥掉中文后剩下的数字）
_TAIL_NUM_RE = re.compile(r"\s*\d{1,4}$")

# 标题里可能出现的 HTML 实体（抓回来的页面常见 &amp; &#124; 等）
_HTML_ENTITIES = (
    ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
    ("&#39;", "'"), ("&apos;", "'"), ("&nbsp;", " "),
    ("&#124;", "|"), ("&hellip;", "..."), ("&mdash;", "-"), ("&ndash;", "-"),
)

# 站方通用后缀（出现在分隔符之后，整段丢弃）
_NAME_TITLES = (
    "Pornhub.com", "Pornhub", "Pornhub 官网", "Pornstar Videos",
    "Porn Videos", "Videos", "Video", "HD", "4K",
)


def clean_model_name(raw: str) -> str:
    """把网页标题 / alt 文本清洗成**纯模特名**。

    处理链路：
      1. 去首尾空白与 ``-`` / ``|`` 分隔符；
      2. 命中分隔符时只保留第一段（``"Jane Doe - Pornstar Videos"`` → ``"Jane Doe"``）；
      3. 剥离中英文尾巴（``"Amber Griz的色情片"`` → ``"Amber Griz"``）；
      4. 站方通用词做**整词**剔除，但**保留名字内部单词**
         （``"Amina Toy"``/``"Eva Angel"`` 等不能误伤）；
      5. 结果为空则原样返回调用方兜底。

    >>> clean_model_name("Amber Griz的色情片")
    'Amber Griz'
    >>> clean_model_name("Jane Doe - Pornstar Videos, ...")
    'Jane Doe'
    >>> clean_model_name("Little Caprice")
    'Little Caprice'
    """
    if not raw:
        return ""
    t = str(raw).strip()

    # 0) 先解 HTML 实体（抓回来的页面里常有 &amp; / &#124; / &nbsp;）
    for ent, ch in _HTML_ENTITIES:
        if ent in t:
            t = t.replace(ent, ch)
    t = t.strip()

    # 1) 分隔符截断：只保留第一段。
    #    ⚠️ 必须包含**无空格**的 " | " 变体：站方标题常是「名字 | Pornhub.com」，
    #    解码实体后可能是「名字| Pornhub.com」，带空格切不掉。
    for sep in (" - ", " | ", " – ", " — ", "_-_", "|", " -"):
        if sep in t:
            head = t.split(sep)[0].strip()
            if head:
                t = head
                break

    # 2) 剥离尾巴。
    #    思路：**从右往左找站方文案的起始标记**，一次性切掉（含年份）。
    #    比堆正则可靠 —— 站点文案会变（"的色情片" / "的新色情片2026" /
    #    "'s Porn Videos"…），但「名字 + 站点文案」的结构不变。
    for marker in _TAIL_MARKERS:
        i = t.lower().rfind(marker)
        if i > 0:                      # i>0 保证不把纯文案当成名字
            t = t[:i].strip()
            break
    # 固定串尾巴（英文为主）
    for tail in ("'s Porn Videos", "'s Videos", "'s Porn", "'s",
                 " - Porn Videos", " Porn Videos", " Pornstar Videos",
                 " Videos", " Video"):
        if len(t) > len(tail) and t.endswith(tail):
            t = t[: -len(tail)].strip()
    # 残留年份/编号尾巴（"…2026"）
    m = _TAIL_NUM_RE.search(t)
    if m and len(t) > m.start() and len(t[: m.start()].strip()) >= 2:
        t = t[: m.start()].strip()

    # 3) 整词剔除站方词：按空格切分，只丢完全相等的词，保留名字内部单词
    words = [w for w in t.split() if w.lower().strip(".,:;·|-_")
             not in {x.lower() for x in _NAME_TITLES}]
    if words:
        t = " ".join(words).strip()

    # 4) 收尾清理
    t = re.sub(r"\s{2,}", " ", t).strip(" -|·,，、")
    # 剥离中文尾巴后可能留下孤立的「的 / 新 / 全 / 最新 / 全新」等虚词，
    # 例如 "…的新色情片2026" 切完剩 "…的新" ⇒ 一并去掉。
    t = re.sub(r"(的|新|全|最新|全新)+$", "", t).strip()
    return t


def normalize_pornhub_url(url: str) -> str:
    """把模特链接规范成本项目统一的 ``/videos`` 形态。

    库里 99% 的记录是 ``/model/<slug>/videos`` 或 ``/pornstar/<slug>/videos``，
    而列表页抓到的是**个人主页** ``/model/<slug>``（无 /videos）。
    直接入库会让同一模特出现两种 URL、按 URL 匹配随之失效。

    >>> normalize_pornhub_url("https://cn.pornhub.com/model/milamuse")
    'https://cn.pornhub.com/model/milamuse/videos'
    >>> normalize_pornhub_url("https://cn.pornhub.com/model/x/videos")
    'https://cn.pornhub.com/model/x/videos'
    >>> normalize_pornhub_url("https://javdb.com/actors/wm9B")
    'https://javdb.com/actors/wm9B'
    """
    if not url:
        return ""
    u = str(url).split("#")[0].split("?")[0].rstrip("/")
    if "pornhub.com" not in u:
        return u                       # 非 PornHub（javdb 等）保持原样
    if re.search(r"/videos$", u, re.I):
        return u
    if re.search(r"/(?:model|pornstar)/[^/]+$", u, re.I):
        return u + "/videos"
    return u