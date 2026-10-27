# -*- coding: utf-8 -*-
"""站点 URL 数据库：记录候选地址、健康检查历史、当前生效站点。

表结构:
  - candidates: 人工种子 + 自动发现的候选主页（按站点归类）
  - checks:    每次健康检查的结果（url, ok, http_code, latency_ms, checked_at, site）
  - kv:        键值配置（active_site, last_discovery, ...）
"""

import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from . import sites as _sites
from . import config as _config


# 站点 DB 路径：优先 DATA/site.db；旧版（ROOT/.hg_site.db）由 config._migrate_legacy_data 复制过来
DB_PATH = _config.SITE_DB_PATH


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """初始化表并插入默认种子（按站点归类）。"""
    conn = _connect()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS candidates (
            url TEXT PRIMARY KEY,
            note TEXT,
            site TEXT,
            added_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            site TEXT,
            ok INTEGER,
            http_code INTEGER,
            latency_ms INTEGER,
            checked_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_checks_url ON checks(url);
        CREATE INDEX IF NOT EXISTS idx_checks_time ON checks(checked_at);
    """)
    # 用注册表中的候选地址作为种子
    for site in _sites.all_sites():
        for url in site.get("candidates", []):
            cur.execute(
                "INSERT OR IGNORE INTO candidates(url, note, site) VALUES (?, ?, ?)",
                (url, site.get("name", ""), site["id"]),
            )
    conn.commit()
    conn.close()


def get_candidates(site_id: str = None) -> List[Tuple[str, str, str]]:
    """获取候选 URL（含备注与站点）。可指定站点过滤。"""
    conn = _connect()
    cur = conn.cursor()
    if site_id:
        cur.execute(
            "SELECT url, note, site FROM candidates WHERE site = ? ORDER BY added_at DESC",
            (site_id,),
        )
    else:
        cur.execute("SELECT url, note, site FROM candidates ORDER BY added_at DESC")
    rows = cur.fetchall()
    conn.close()
    return [(r["url"], r["note"], r["site"]) for r in rows]


def add_candidate(url: str, note: str = "", site: str = "") -> None:
    """新增候选 URL。"""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO candidates(url, note, site) VALUES (?, ?, ?)",
        (url, note, site),
    )
    conn.commit()
    conn.close()


def log_check(url: str, ok: bool, http_code: int, latency_ms: int, site: str = "") -> None:
    """记录一次健康检查。"""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO checks(url, site, ok, http_code, latency_ms, checked_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (url, site, int(ok), http_code, latency_ms, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_latest_checks(limit: int = 20) -> List[Dict]:
    """获取最近的健康检查记录。"""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT url, site, ok, http_code, latency_ms, checked_at FROM checks "
        "ORDER BY checked_at DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_kv(key: str, value: str) -> None:
    """设置键值。"""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO kv(key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_kv(key: str, default: str = "") -> str:
    """读取键值。"""
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT value FROM kv WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


# 便捷键名
def set_active_site(site_id: str) -> None:
    set_kv("active_site", site_id)


def get_active_site() -> str:
    return get_kv("active_site", _sites.SITE_HUANGUOAI["id"])
