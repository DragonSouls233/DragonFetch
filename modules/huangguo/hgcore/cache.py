# -*- coding: utf-8 -*-
"""三级缓存：加速列表/TAB 切换，避免每次重新抓页面。

分层设计（越靠前越快）:
    L1 内存 LRU   进程内 OrderedDict，key = 站点 + 类型 + URL，容量 50（毫秒级）
    L2 磁盘 JSON  DATA/cache/lists/<sha1>.json，带 TTL（默认 10 分钟）
    L3 SQLite     dramas 表（由 UI 层使用，stale-while-revalidate，见 main_window）

关键约定:
    * 缓存 key **必须带 site_id**，否则两站的 did/slug 会互相污染
    * 返回的对象是**深拷贝后的新列表**，调用方可自由修改而不影响缓存
    * TTL 到期不删除，只是标记为过期；UI 可先用过期数据渲染、后台再刷新

用法:
    from hgcore import cache
    hit = cache.get_list(url, kind="category")      # 命中返回 list，未命中 None
    if hit is None:
        data = site_api.list_category(url)
        cache.put_list(url, "category", data)
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

# ---------------------------------------------------------------- 配置

MEMORY_CAPACITY = 50           # L1 最多缓存多少个列表
DEFAULT_TTL = 600              # L2 默认有效期（秒），10 分钟
STALE_GRACE = 7 * 24 * 3600    # 过期后仍保留的宽限（用于离线兜底），7 天

_lock = threading.Lock()
_memory: "OrderedDict[str, tuple]" = OrderedDict()   # key -> (ts, payload)
_stats = {"l1_hit": 0, "l2_hit": 0, "miss": 0}


# ---------------------------------------------------------------- 序列化

def _drama_to_dict(d) -> dict:
    return {
        "did": d.did, "title": d.title, "cover": d.cover, "score": d.score,
        "episode_text": getattr(d, "episode_text", ""), "desc": d.desc,
        "tags": list(d.tags or []), "episode_count": getattr(d, "episode_count", 0),
    }


def _dict_to_drama(x: dict):
    from .models import Drama
    d = Drama(
        did=x.get("did"), title=x.get("title", ""), cover=x.get("cover", ""),
        score=x.get("score", ""), episode_text=x.get("episode_text", ""),
        desc=x.get("desc", ""), tags=list(x.get("tags") or []),
    )
    d.episode_count = int(x.get("episode_count") or d.episode_count or 0)
    return d


def _topic_to_dict(t) -> dict:
    return {"slug": t.slug, "title": t.title, "cover": t.cover,
            "count_text": getattr(t, "count_text", "")}


def _dict_to_topic(x: dict):
    from .models import Topic
    return Topic(slug=x.get("slug"), title=x.get("title", ""),
                 cover=x.get("cover", ""), count_text=x.get("count_text", ""))


def _serialize(items: List) -> list:
    out = []
    for it in items or []:
        if hasattr(it, "slug") and hasattr(it, "count_text"):        # Topic
            out.append({"__t": "topic", **_topic_to_dict(it)})
        elif hasattr(it, "did"):                                      # Drama
            out.append({"__t": "drama", **_drama_to_dict(it)})
        elif isinstance(it, dict):
            out.append({"__t": "dict", **it})
    return out


def _deserialize(raw: list) -> List:
    out = []
    for x in raw or []:
        t = x.get("__t")
        if t == "topic":
            out.append(_dict_to_topic(x))
        elif t == "dict":
            out.append({k: v for k, v in x.items() if k != "__t"})
        else:
            out.append(_dict_to_drama(x))
    return out


# ---------------------------------------------------------------- key / 路径

def _key(url: str, kind: str, site_id: Optional[str] = None) -> str:
    site = site_id or config.ACTIVE_SITE_ID or "default"
    return f"{site}|{kind}|{url}"


def _disk_path(key: str) -> Path:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return config.LIST_CACHE_DIR / f"{h}.json"


# ---------------------------------------------------------------- L1 / L2 读写

def _l1_get(key: str) -> Optional[list]:
    with _lock:
        item = _memory.get(key)
        if item is None:
            return None
        _memory.move_to_end(key)          # LRU：命中即提到队尾
        return item[1]


def _l1_put(key: str, payload: list) -> None:
    with _lock:
        _memory[key] = (time.time(), payload)
        _memory.move_to_end(key)
        while len(_memory) > MEMORY_CAPACITY:
            _memory.popitem(last=False)


def _l2_get(key: str) -> Optional[tuple]:
    """返回 (ts, payload) 或 None（文件不存在/损坏）。"""
    p = _disk_path(key)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if raw.get("key") != key:         # 哈希碰撞防御
            return None
        return float(raw.get("ts") or 0), raw.get("items") or []
    except Exception:                     # noqa: BLE001
        return None


def _l2_put(key: str, payload: list) -> None:
    p = _disk_path(key)
    try:
        config.LIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"key": key, "ts": time.time(), "items": payload},
            ensure_ascii=False,
        ), encoding="utf-8")
        _l2_prune()
    except Exception:                     # noqa: BLE001
        pass


def _l2_prune() -> None:
    """清理超过 STALE_GRACE 的磁盘缓存文件。"""
    try:
        now = time.time()
        for f in config.LIST_CACHE_DIR.glob("*.json"):
            try:
                if now - f.stat().st_mtime > STALE_GRACE:
                    f.unlink()
            except OSError:
                pass
    except Exception:                     # noqa: BLE001
        pass


# ---------------------------------------------------------------- 对外 API

def get_list(url: str, kind: str = "category",
             site_id: Optional[str] = None,
             ttl: int = DEFAULT_TTL,
             allow_stale: bool = False) -> Optional[List]:
    """取缓存列表。

    命中（未过期）返回 list；allow_stale=True 时即使过期也返回（供离线兜底）。
    未命中返回 None。
    """
    key = _key(url, kind, site_id)

    payload = _l1_get(key)
    if payload is not None:
        _stats["l1_hit"] += 1
        return _deserialize(payload)

    disk = _l2_get(key)
    if disk is not None:
        ts, payload = disk
        fresh = (time.time() - ts) <= ttl
        if fresh or allow_stale:
            _l1_put(key, payload)               # 回填 L1
            _stats["l2_hit"] += 1
            return _deserialize(payload)

    _stats["miss"] += 1
    return None


def put_list(url: str, kind: str, items: List,
             site_id: Optional[str] = None) -> None:
    """写入 L1 + L2。"""
    key = _key(url, kind, site_id)
    payload = _serialize(items)
    _l1_put(key, payload)
    _l2_put(key, payload)


def is_fresh(url: str, kind: str = "category", site_id: Optional[str] = None,
             ttl: int = DEFAULT_TTL) -> bool:
    """该 key 的缓存是否仍在有效期内（用于决定是否需要后台刷新）。"""
    key = _key(url, kind, site_id)
    disk = _l2_get(key)
    if disk is None:
        return False
    return (time.time() - disk[0]) <= ttl


def invalidate(url: Optional[str] = None, kind: Optional[str] = None,
               site_id: Optional[str] = None) -> int:
    """清缓存。全部参数为空 = 清空所有（内存 + 磁盘）。

    返回清掉的键数量。
    """
    global _memory
    n = 0
    with _lock:
        if url is None and kind is None and site_id is None:
            n = len(_memory)
            _memory = OrderedDict()
        else:
            site = site_id or config.ACTIVE_SITE_ID or "default"
            victims = []
            for k in _memory:
                parts = k.split("|", 2)
                if len(parts) != 3:
                    continue
                s, kd, u = parts
                if site_id is not None and s != site:
                    continue
                if kind is not None and kd != kind:
                    continue
                if url is not None and u != url:
                    continue
                victims.append(k)
            for k in victims:
                _memory.pop(k, None)
                n += 1

    # 磁盘：无参 = 全删；有参 = 删除对应文件
    try:
        if url is None and kind is None and site_id is None:
            for f in config.LIST_CACHE_DIR.glob("*.json"):
                try:
                    f.unlink()
                except OSError:
                    pass
        else:
            site = site_id or config.ACTIVE_SITE_ID or "default"
            # 磁盘文件按 key 哈希命名，无法反查；只清已知组合
            if url is not None and kind is not None:
                try:
                    _disk_path(_key(url, kind, site)).unlink()
                except OSError:
                    pass
    except Exception:                        # noqa: BLE001
        pass
    return n


def stats() -> Dict[str, Any]:
    """缓存统计（命中率等），供调试/状态栏展示。"""
    with _lock:
        mem_n = len(_memory)
    try:
        disk_n = len(list(config.LIST_CACHE_DIR.glob("*.json")))
    except Exception:                        # noqa: BLE001
        disk_n = 0
    total = _stats["l1_hit"] + _stats["l2_hit"] + _stats["miss"]
    hit = _stats["l1_hit"] + _stats["l2_hit"]
    return {
        "memory_items": mem_n,
        "disk_items": disk_n,
        "l1_hit": _stats["l1_hit"],
        "l2_hit": _stats["l2_hit"],
        "miss": _stats["miss"],
        "hit_rate": (hit / total) if total else 0.0,
    }


def reset_stats() -> None:
    for k in _stats:
        _stats[k] = 0
