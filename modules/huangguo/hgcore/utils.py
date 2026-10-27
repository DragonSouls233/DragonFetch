# -*- coding: utf-8 -*-
"""通用工具函数：文件名清洗、文本解析、格式化。"""

import re
import math
from pathlib import Path
from typing import Optional

# Windows 文件名非法字符
_INVALID_CHARS = r'[\\/:*?"<>|\r\n\t]'

# 全角标点替换为半角，避免部分文件系统异常
_REPLACE_MAP = {
    "：": "：",   # 保留全角冒号（Windows 允许），仅作占位说明
}


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """把任意字符串清洗成合法的 Windows 文件/目录名。

    - 去掉非法字符与首尾空白、末尾的点
    - 压缩连续空白
    - 超长截断
    """
    if not name:
        return "untitled"
    s = re.sub(_INVALID_CHARS, "", str(name))
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")
    if not s:
        s = "untitled"
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    return s


# 站点常给同一部剧套上不同 SEO 词（"全集在线观看" / "在线看" / "高清完整版" …），
# 这些词不影响"是哪部剧"，落盘时统一剔除，避免同一部剧生成多个文件夹。
#
# ⚠ 设计原则（2026-10-21 复盘踩坑）：
#   1) **只剥「纯 SEO 尾巴」**——即标题结尾处、由固定词表拼出来的后缀。
#      因为「视频」「高清」在换脸/魔改类里是**实义词**：
#      「AI换脸 纪文君 在家拍性爱视频」「AI魔改 迪丽热巴 图生视频」
#      「AI魔改 包上恩 高清PMV」——剥掉就毁掉了剧名信息。
#   2) 用**正则模式**匹配「(全集|完整版|高清版|免费|无删减)?(在线观看|在线播放|在线看)」
#      这类组合，而不是穷举词表——站点随时会换说法（实测出现过
#      「全集在线观看」「在线观看」「在线看」三种）。
#   3) 剥完若尾部留下多余空格/标点，一并清掉。
_TAIL_SEO_RE = re.compile(
    r"(?:"
    r"(?:全集|完整版|高清版|免费版?|无删减|未删减|高清|全集高清)?"
    r"(?:在线观看|在线播放|在线看|免费观看|免费视频)"
    r"|全集在线|在线观看|在线看"
    r")\s*$"
)
# 标题开头的宣传性标签（同样是纯噪音）
_LEAD_TITLE_TAG = re.compile(
    r"^\s*(?:最新|热播|热门|精选|推荐|火爆|高清无码|高清|HD|完整版|全集)"
    r"\s*[:：]?\s*"
)
# 独立的「高清」「全集」等紧跟剧名结尾时也属噪音（如 "斩神高清"）
_TRAIL_TITLE_NOISE = re.compile(r"\s*(?:高清版|全集高清|完整版|高清|全集)\s*$")

# 括号里"纯 SEO 词"才删；带实义的括号必须保留！
# 例如 美丽新世界（漫剧版）/ 少年阿宾（改编版）/ 滴滴代操（奇幻版） 是【不同版本】，
# 若无差别删括号会把它们和"美丽新世界 / 少年阿宾 / 滴滴代操"错误合并。
_SEO_WORDS = (
    "在线观看", "在线播放", "在线看", "免费观看", "免费视频",
    "全集", "完整版", "高清", "HD",
    "热播", "最新", "热门", "精选", "推荐", "火爆", "无删减", "未删减",
)
_BRACKET_ANY = re.compile(r"[【\[（(]([^】\]）)]*)[】\]）)]")


def _strip_seo_only_brackets(s: str) -> str:
    """只删除「内容全是 SEO 词」的括号；含实义的括号（版本/季/集数等）原样保留。"""
    def repl(m: "re.Match") -> str:
        inner = m.group(1)
        rest = inner
        for w in _SEO_WORDS:
            rest = rest.replace(w, "")
        if rest.strip() == "":
            return ""            # 纯噪音（如 (全集) / 【热播】）-> 整块删除
        return m.group(0)        # 含实义（如 (漫剧版) / (全20集)）-> 保留
    return _BRACKET_ANY.sub(repl, s)


def normalize_title(title: str) -> str:
    """把剧集标题归一化，去掉站点常见的 SEO 噪音词。

    目的：让 "苏老师的裸贷人生" 与 "苏老师的裸贷人生全集在线观看" 这类
    同一部剧因站点改标题产生的变体，落盘到同一个文件夹，而不是生成多个。

    规则（保守策略，**只动确定的 SEO 噪音**）：
    - 结尾的「(全集|完整版|高清版|免费)?(在线观看|在线播放|在线看)」组合 -> 删
    - 结尾独立的「高清 / 全集 / 完整版」-> 删
    - 开头的「最新 / 热播 / 高清 / HD」等宣传标签 -> 删
    - 只含 SEO 词的括号（(全集) / 【热播】）-> 删

    保留（**有实义，剥了就毁剧名**）：
    - 「视频」「高清」「在线」等在标题中间/作为描述词时
      （如 "AI换脸 纪文君 在家拍性爱视频"、"AI魔改 图生视频"）
    - 版本/季/集数等括号（（漫剧版）/（改编版）/（全20集））
    """
    if not title:
        return ""
    s = str(title)
    s = _strip_seo_only_brackets(s)
    # 剥尾巴（可能叠多层，如 "全集在线观看" + "全集"）
    for _ in range(3):
        new = _TAIL_SEO_RE.sub("", s)
        new = _TRAIL_TITLE_NOISE.sub("", new)
        if new == s:
            break
        s = new
    s = _LEAD_TITLE_TAG.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    # 剥完可能留下悬空的标点/空格，如 "剧名 -" / "剧名 "
    s = re.sub(r"[\s\-—_|/·、,，]+$", "", s).strip()
    return s


def parse_episode_count(text: str) -> int:
    """从 '更新至 36 集' / '更新至36集' / '36集' 中解析集数。

    解析失败返回 0。
    """
    if not text:
        return 0
    m = re.search(r"(\d+)\s*集", str(text))
    return int(m.group(1)) if m else 0


def parse_score(text: str) -> str:
    """从 '9.9分' 中提取评分文本，失败返回空串。"""
    if not text:
        return ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*分", str(text))
    return m.group(1) if m else ""


def ep_suffix(ep: int) -> str:
    """把集数格式化为 S01E01 形式。"""
    return f"S01E{ep:02d}"


def season_dir_name(season: int = 1) -> str:
    """季目录名，遵循 Kodi/Emby/Jellyfin 约定的 "Season 01"。"""
    return f"Season {season:02d}"


def _subdir_for(category: str) -> str:
    """分类对应的独立子目录（延迟导入，避免与 config 形成循环依赖）。"""
    if not category:
        return ""
    from .category_rules import subdir_for
    return subdir_for(category)


def _category_base(download_dir: Path, category: str) -> Path:
    """单集类分类的落盘根：直接挂在「用户设定的下载根目录」，不套站点子目录。

    换脸 / 魔改 是黄果AI 独有分类，不会与其他站点重名；放在根目录下
    用户一眼就能找到（J:\\AI剧\\换脸\\）。调用方显式传入了别的目录时
    （迁移 / 测试场景）尊重调用方。
    """
    if not _subdir_for(category):
        return download_dir
    from . import config as _config
    root = getattr(_config, "DOWNLOAD_DIR", None)
    if not root:
        return download_dir
    try:
        if Path(download_dir).resolve() == Path(_config.DOWNLOAD_SITE_DIR).resolve():
            return Path(root)
    except OSError:
        pass
    return download_dir


def drama_folder(download_dir: Path, title: str, season: Optional[int] = None,
                 category: str = "") -> Path:
    """剧集目录：常规 <下载目录>/<剧名>[/Season 01]；
    单集类 <下载根目录>/<分类>/<剧名>。

    season=None 时读 config.SEASON_FOLDER 决定是否加季子目录。
    传入具体季号（>0）则强制使用该层。
    """
    safe = sanitize_filename(normalize_title(title))
    sub = _subdir_for(category)
    base = _category_base(download_dir, category)
    folder = base / sub / safe if sub else base / safe
    if sub:
        return folder          # 单集作品不再嵌套 Season 层
    if season is None:
        from . import config as _config
        use_season = bool(getattr(_config, "SEASON_FOLDER", False))
    else:
        use_season = season > 0
    if use_season:
        folder = folder / season_dir_name(season or 1)
    return folder


def build_output_path(download_dir: Path, title: str, ep: int, ext: str = "mp4",
                      category: str = "") -> Path:
    """构造输出路径。

    常规剧集: <download_dir>/<剧名>[/Season 01]/<剧名> S01E01.mp4
    单集类（换脸/魔改）: <download_dir>/<分类>/<剧名>/<剧名>.mp4
    """
    safe = sanitize_filename(normalize_title(title))
    folder = drama_folder(download_dir, title, category=category)
    if _subdir_for(category):
        return folder / f"{safe}.{ext.lstrip('.')}"
    return folder / f"{safe} {ep_suffix(ep)}.{ext.lstrip('.')}"


def format_size(num_bytes: float) -> str:
    """字节数格式化为 KB / MB / GB。"""
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = min(int(math.log(num_bytes, 1024)), len(units) - 1)
    return f"{num_bytes / (1024 ** idx):.2f} {units[idx]}"


def format_speed(bytes_per_sec: float) -> str:
    """速度格式化。"""
    if not bytes_per_sec or bytes_per_sec <= 0:
        return "--"
    return format_size(bytes_per_sec) + "/s"


def format_duration(seconds: float) -> str:
    """秒格式化为 HH:MM:SS 或 MM:SS。"""
    if not seconds or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def strip_html(text: str) -> str:
    """去除 HTML 标签并压缩空白。"""
    if not text:
        return ""
    s = re.sub(r"<[^>]+>", "", str(text))
    return re.sub(r"\s+", " ", s).strip()
