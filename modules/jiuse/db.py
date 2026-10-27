# -*- coding: utf-8 -*-
"""九色模块的独立 SQLite 库：线路检测历史 + 下载记录。

与其他模块一样**一模块一库**（``DATA/jiuse/jiuse.db``），不与别的模块共享写锁。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import List

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    nav_url   TEXT,
    via       TEXT,
    n_ok      INTEGER,
    n_total   INTEGER,
    detail    TEXT,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS downloads (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    url       TEXT,
    title     TEXT,
    status    TEXT,
    file_path TEXT,
    size      INTEGER DEFAULT 0,
    error     TEXT,
    created_at INTEGER,
    finished_at INTEGER
);
CREATE TABLE IF NOT EXISTS addresses (
    url        TEXT PRIMARY KEY,
    host       TEXT,
    ok         INTEGER DEFAULT 0,
    elapsed_ms INTEGER DEFAULT 0,
    last_seen  INTEGER,
    last_check INTEGER
);
"""


def _data_dir() -> Path:
    root = (os.environ.get("DF_DATA_ROOT") or "").strip()
    p = (Path(root) / "jiuse") if root else \
        (Path(__file__).resolve().parent.parent / "DATA")
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_db_path() -> Path:
    return _data_dir() / "jiuse.db"


def _connect() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect(str(get_db_path()), timeout=10,
                                check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.executescript(_SCHEMA)
        _CONN.commit()
    return _CONN


def init_db() -> None:
    with _LOCK:
        _connect()


def log_check(nav_url: str, via: str, items: List[dict]) -> None:
    """记一条检测历史（同时更新 addresses 表的最新状态）。"""
    if not items:
        return
    try:
        with _LOCK:
            c = _connect()
            n_ok = sum(1 for i in items if i.get("ok"))
            c.execute(
                "insert into checks(nav_url,via,n_ok,n_total,detail,created_at) "
                "values(?,?,?,?,?,?)",
                (nav_url, via, n_ok, len(items),
                 json.dumps(items, ensure_ascii=False)[:4000], int(time.time())))
            now = int(time.time())
            for it in items:
                url = (it.get("url") or "").rstrip("/")
                if not url:
                    continue
                c.execute(
                    "insert into addresses(url,host,ok,elapsed_ms,last_seen,last_check) "
                    "values(?,?,?,?,?,?) on conflict(url) do update set "
                    "host=excluded.host, ok=excluded.ok, "
                    "elapsed_ms=excluded.elapsed_ms, last_check=excluded.last_check",
                    (url, it.get("host", ""), 1 if it.get("ok") else 0,
                     int(it.get("elapsed_ms") or 0), now, now))
            c.commit()
    except Exception:
        pass


def recent_checks(limit: int = 20) -> List[dict]:
    try:
        with _LOCK:
            c = _connect()
            rows = c.execute(
                "select * from checks order by id desc limit ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def start_download(url: str, title: str) -> int:
    try:
        with _LOCK:
            c = _connect()
            cur = c.execute(
                "insert into downloads(url,title,status,created_at) values(?,?,?,?)",
                (url, title, "started", int(time.time())))
            c.commit()
            return int(cur.lastrowid)
    except Exception:
        return 0


def finish_download(dl_id: int, ok: bool, file_path: str = "", size: int = 0,
                    error: str = "") -> None:
    if not dl_id:
        return
    try:
        with _LOCK:
            c = _connect()
            c.execute(
                "update downloads set status=?, file_path=?, size=?, error=?, "
                "finished_at=? where id=?",
                ("done" if ok else "failed", file_path, int(size or 0),
                 (error or "")[:400], int(time.time()), dl_id))
            c.commit()
    except Exception:
        pass


def recent_downloads(limit: int = 100) -> List[dict]:
    try:
        with _LOCK:
            c = _connect()
            rows = c.execute(
                "select * from downloads order by id desc limit ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []
