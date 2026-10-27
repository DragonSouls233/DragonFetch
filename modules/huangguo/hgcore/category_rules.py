# -*- coding: utf-8 -*-
"""分类规则：单集类作品的独立存放 + 归属记忆。

背景:
  黄果AI 的「换脸 / 魔改」是**单集作品**（一部剧只有一集）。它们和多集短剧
  混在同一个下载根目录下时，既不好找也不好管理。用户希望：这两类下载后自动
  落进同名的独立文件夹，例如::

      <下载目录>/换脸/A/A.mp4
      <下载目录>/魔改/B/B.mp4

归属如何确定:
  采用「来源 TAB + 记忆」。用户在「换脸 / 魔改」TAB 里浏览时，列表里的剧会被
  打上该分类；同时写入本地映射（DATA/category_map.json，按 站点+did 与
  站点+标题 双索引）。之后即使从搜索、更新检测、批量等入口下载，也能自动落回
  正确的文件夹。

映射只影响落盘目录，不影响取址与播放。
"""

import json
import re
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import config

# 分类名 -> 独立子目录名。只列「单集类」分类；其余分类返回 "" 走常规剧集目录。
# 值就是子目录名（与分类同名），便于用户一眼找到。
SINGLE_EP_FOLDERS: Dict[str, str] = {
    "换脸": "换脸",
    "魔改": "魔改",
}

_LOCK = threading.RLock()
_MAP_FILE = "category_map.json"

_by_did: Dict[str, str] = {}      # "site:did"   -> 分类名
_by_title: Dict[str, str] = {}    # "site:title" -> 分类名
_loaded = False
_dirty = False


# ---------------------------------------------------------------- 规则查询

def subdir_for(category: Optional[str]) -> str:
    """分类对应的独立子目录名；不属于单集类分类时返回空串。"""
    if not category:
        return ""
    return SINGLE_EP_FOLDERS.get(str(category).strip(), "")


def is_single_ep(category: Optional[str]) -> bool:
    """该分类是否为「单集作品」（需要独立子目录 + 文件名不带集号）。"""
    return bool(subdir_for(category))


# ---------------------------------------------------------------- 标题推断
#
# 兜底：某些入口（搜索 / 上新 / 跨 TAB 的「下载本页全部」等）拿不到分类，
# 但剧名本身就带了「换脸 / 魔改」字样（如「AI换脸 XXX」「AI魔改 XXX」，
# 偶见「AI换 脸」这种中间夹空格的写法）。这些剧名是站点的「单集类」作品，
# 直接按标题关键词归类，避免它们被误放进 huangguoai 站点根目录。
# 这两个词极罕见出现在普通短剧标题里，误判风险可忽略。

_FACE_RE = re.compile(r"换\s*脸")     # 兼容「换脸」「换 脸」
_MAGIC_RE = re.compile(r"魔改")


def infer_category_from_title(title: str) -> str:
    """从剧名推断单集类分类：含「魔改」→ 魔改，含「换脸」→ 换脸，否则返回空串。"""
    if not title:
        return ""
    t = str(title)
    if _MAGIC_RE.search(t):
        return "魔改"
    if _FACE_RE.search(t):
        return "换脸"
    return ""


def single_ep_categories() -> List[str]:
    return list(SINGLE_EP_FOLDERS)


# ---------------------------------------------------------------- 归属记忆

def _map_path() -> Path:
    return config.DATA_DIR / _MAP_FILE


def _key(site_id: str, kind: str, value) -> str:
    return f"{site_id or ''}:{kind}:{value}"


def load(force: bool = False) -> None:
    """从磁盘载入映射（进程内只载入一次）。"""
    global _loaded
    with _LOCK:
        if _loaded and not force:
            return
        _by_did.clear()
        _by_title.clear()
        try:
            data = json.loads(_map_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _by_did.update(data.get("by_did") or {})
                _by_title.update(data.get("by_title") or {})
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        _loaded = True


def flush() -> None:
    """把内存中的映射写回磁盘（仅在有变更时写）。"""
    global _dirty
    with _LOCK:
        if not _dirty:
            return
        payload = {"by_did": _by_did, "by_title": _by_title}
        path = _map_path()
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(path)
            _dirty = False
        except OSError:
            pass


def remember(site_id: str, did, title: str, category: str) -> None:
    """记住一部剧的归属分类（仅单集类分类需要记，普通分类不占空间）。"""
    global _dirty                       # 必须声明：否则下面的赋值会变成局部变量，落盘失效
    if not category or not is_single_ep(category):
        return
    load()
    changed = False
    with _LOCK:
        if did is not None:
            k = _key(site_id, "did", did)
            if _by_did.get(k) != category:
                _by_did[k] = category
                changed = True
        if title:
            k = _key(site_id, "title", str(title).strip())
            if _by_title.get(k) != category:
                _by_title[k] = category
                changed = True
        if changed:
            _dirty = True
            # 同步权威库：浏览/下载时学到的分类也写进 dramas.category，
            # 这样搜索、更新检测、有更新 TAB 等入口也能据此正确归类。
            try:
                import hgcore.library_db as _ldb
                _ldb.set_category(site_id, did, category)
            except Exception:                      # noqa: BLE001
                pass


def remember_many(site_id: str, items: Iterable[Tuple]) -> None:
    """批量记忆 [(did, title, category), ...]，结束后统一落盘一次。"""
    for did, title, category in items:
        remember(site_id, did, title, category)
    flush()


def of(site_id: str, did=None, title: Optional[str] = None) -> str:
    """查询一部剧的归属分类：先按 did 精确匹配，再按标题兜底。查不到返回 ""。"""
    if not site_id:
        return ""
    load()
    with _LOCK:
        if did is not None and str(did) != "":
            hit = _by_did.get(_key(site_id, "did", did))
            if hit:
                return hit
        if title:
            hit = _by_title.get(_key(site_id, "title", str(title).strip()))
            if hit:
                return hit
    return ""


def forget(site_id: str, did=None, title: Optional[str] = None) -> None:
    """删除一条归属记录（供测试/清理用）。"""
    global _dirty
    load()
    with _LOCK:
        if did is not None:
            _by_did.pop(_key(site_id, "did", did), None)
        if title:
            _by_title.pop(_key(site_id, "title", str(title).strip()), None)
        _dirty = True
    flush()
