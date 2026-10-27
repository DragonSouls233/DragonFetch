# -*- coding: utf-8 -*-
"""剧库主数据库：记录各站点的剧目目录、每集下载状态、下载历史。

存储位置：<APP_DIR>/DATA/hg.db（由 config.DB_PATH 指向）。

表结构:
  - dramas    平台有哪些剧 + 链接（首页/分类/搜索/专题加载时入库）
  - episodes  各集在本地的状态（done/failed/pending）与文件路径
  - downloads 下载流水（开始/结束/耗时/字节数/错误）
  - meta      通用键值（最后启动时间、版本号等）

并发：sqlite3 连接本身可多线程使用但写需串行；本模块用线程锁保证原子性。
查询/写都很快（每条 < 1ms），足以在 UI 线程直接调用。
"""

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import config


# 状态枚举（与 core.models.TaskStatus 保持一致）
EP_PENDING = "pending"
EP_RUNNING = "running"
EP_DONE = "done"
EP_FAILED = "failed"
EP_CANCELLED = "cancelled"

_EP_STATUSES = {EP_PENDING, EP_RUNNING, EP_DONE, EP_FAILED, EP_CANCELLED}


_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _connect() -> sqlite3.Connection:
    """打开（或复用）一个连接。"""
    global _conn
    if _conn is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(config.DB_PATH), timeout=10, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")      # 允许并发读
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


@contextmanager
def _tx():
    """拿锁 + 事务。"""
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db() -> None:
    """创建表（若已存在则跳过）。"""
    with _tx() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS dramas (
            site_id        TEXT NOT NULL,
            did            TEXT NOT NULL,
            title          TEXT,
            cover_url      TEXT,
            detail_url     TEXT,
            score          TEXT,
            episode_count  INTEGER DEFAULT 0,
            tags           TEXT,            -- JSON 数组
            description    TEXT,
            first_seen     INTEGER NOT NULL,
            last_seen      INTEGER NOT NULL,
            PRIMARY KEY (site_id, did)
        );
        CREATE INDEX IF NOT EXISTS idx_dramas_last_seen ON dramas(last_seen DESC);
        CREATE INDEX IF NOT EXISTS idx_dramas_title    ON dramas(title);

        CREATE TABLE IF NOT EXISTS episodes (
            site_id        TEXT NOT NULL,
            did            TEXT NOT NULL,
            ep             INTEGER NOT NULL,
            status         TEXT NOT NULL DEFAULT 'pending',
            file_path      TEXT,
            size_bytes     INTEGER DEFAULT 0,
            duration       REAL DEFAULT 0,
            error          TEXT,
            updated_at     INTEGER NOT NULL,
            PRIMARY KEY (site_id, did, ep)
        );
        CREATE INDEX IF NOT EXISTS idx_eps_status ON episodes(status);

        CREATE TABLE IF NOT EXISTS downloads (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id       TEXT NOT NULL,
            did           TEXT NOT NULL,
            ep            INTEGER NOT NULL,
            title         TEXT,
            status        TEXT,
            bytes         INTEGER DEFAULT 0,
            started_at    INTEGER NOT NULL,
            finished_at   INTEGER,
            error         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dl_site ON downloads(site_id, did, ep);
        CREATE INDEX IF NOT EXISTS idx_dl_time ON downloads(finished_at DESC);

        CREATE TABLE IF NOT EXISTS meta (
            key           TEXT PRIMARY KEY,
            value         TEXT,
            updated_at    INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sync_sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            INTEGER NOT NULL,
            role          TEXT NOT NULL,        -- 'download' / 'server'
            target_path   TEXT NOT NULL,
            scope_desc    TEXT,                -- '全量' / '自 2026-09-15 起' / '增量'
            mode          TEXT,                -- 'full' / 'incremental' / 'date'
            file_count    INTEGER DEFAULT 0,
            total_bytes   INTEGER DEFAULT 0,
            manifest_path TEXT                 -- 清单落盘位置（现在记录在项目 DB 内，为空）
        );
        CREATE INDEX IF NOT EXISTS idx_synss_time ON sync_sessions(ts DESC);

        -- 每个目标盘「已同步文件」的当前状态（增量口径的权威依据）。
        -- 用「集合差集」代替「mtime > 上次同步时间」：
        --   * 集合里没有的文件 = 上一轮同步之后新下载的 -> 必拷
        --   * 集合里有、但目标端已不存在/大小变了 -> 补拷（修掉坏文件）
        --   * 集合里有且目标端一致 -> 跳过（外盘被清空也只补新增，不全量重来）
        -- rel_path 是相对下载根的路径，天然随内容改名而变化。
        CREATE TABLE IF NOT EXISTS sync_files (
            role        TEXT NOT NULL,
            target_path TEXT NOT NULL,
            rel_path    TEXT NOT NULL,
            size        INTEGER DEFAULT 0,
            mtime       REAL DEFAULT 0,
            synced_at   INTEGER NOT NULL,
            session_id  INTEGER DEFAULT 0,
            PRIMARY KEY (role, target_path, rel_path)
        );
        CREATE INDEX IF NOT EXISTS idx_syncfiles_target
            ON sync_files(role, target_path);

        -- 每个目标盘「最近一次同步完成时刻」——用于**快速定位候选剧目录**
        -- （剧目录 mtime 晚于它才需细查），避免每次遍历整个下载盘。
        CREATE TABLE IF NOT EXISTS sync_marks (
            role        TEXT NOT NULL,
            target_path TEXT NOT NULL,
            last_sync_at INTEGER NOT NULL,
            PRIMARY KEY (role, target_path)
        );

        -- 每轮同步的**文件明细**（清单本体，留在项目 DB 里，不再写到外盘）。
        -- 这样移动硬盘拿走后，清单依然在项目里可查，且不污染外盘根目录。
        CREATE TABLE IF NOT EXISTS sync_session_files (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role       TEXT NOT NULL,
            rel_path   TEXT NOT NULL,
            size       INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ssf_session
            ON sync_session_files(session_id);
        """)

        # 迁移：dramas 表增加 category 列（换脸/魔改 等单集类分类的权威来源）。
        # 升级旧库时补列，缺失才加，幂等；新建库由上面的 CREATE 已含该列则不触发。
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(dramas)")]
            if "category" not in cols:
                c.execute("ALTER TABLE dramas ADD COLUMN category TEXT")
        except Exception:                  # noqa: BLE001 - 表异常等
            pass


# ---------------------------------------------------------------- 剧库

_DRAMA_UPSERT = """
INSERT INTO dramas(site_id,did,title,cover_url,detail_url,
                  score,episode_count,tags,description,category,first_seen,last_seen)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(site_id,did) DO UPDATE SET
    title=excluded.title,
    cover_url=excluded.cover_url,
    detail_url=excluded.detail_url,
    score=excluded.score,
    episode_count=COALESCE(NULLIF(excluded.episode_count,0), dramas.episode_count),
    tags=excluded.tags,
    description=COALESCE(NULLIF(excluded.description,''), dramas.description),
    category=COALESCE(NULLIF(excluded.category,''), dramas.category),
    last_seen=excluded.last_seen
"""


def _drama_params(drama, site_id: str, now: int) -> tuple:
    tags = json.dumps(list(getattr(drama, "tags", None) or []), ensure_ascii=False)
    # 只持久化单集类分类（换脸/魔改）；普通分类不需要独立目录，留空避免误导。
    raw_cat = getattr(drama, "category", "") or ""
    cat = ""
    if raw_cat:
        try:
            from . import category_rules
            cat = raw_cat if category_rules.is_single_ep(raw_cat) else ""
        except Exception:                  # noqa: BLE001
            cat = ""
    # 入库兜底：剧名本身就带「换脸 / 魔改」字样（搜索 / 上新 / 跨 TAB 下载等入口
    # 拿不到分类），直接按标题关键词归类，保证剧库成为分类的权威来源，
    # 之后任意入口下载都能据此归到正确目录，而不是漏进 huangguoai 根目录。
    if not cat:
        try:
            from . import category_rules
            cat = category_rules.infer_category_from_title(getattr(drama, "title", "") or "")
        except Exception:                  # noqa: BLE001
            cat = ""
    return (site_id, str(drama.did), getattr(drama, "title", "") or "",
            getattr(drama, "cover", "") or "",
            getattr(drama, "url", "") or "",
            getattr(drama, "score", "") or "",
            int(getattr(drama, "episode_count", 0) or 0),
            tags,
            getattr(drama, "desc", "") or "",
            cat,
            now, now)


def record_drama(drama, site_id: str, now: Optional[int] = None) -> None:
    """把单个 Drama 写入/更新到 dramas 表。"""
    if drama is None:
        return
    now = now or int(time.time())
    with _tx() as c:
        c.execute(_DRAMA_UPSERT, _drama_params(drama, site_id, now))


def record_dramas(dramas: Iterable, site_id: str) -> int:
    """批量入库（单事务，几十行开销 < 20ms），返回成功条数。"""
    items = list(dramas)
    if not items:
        return 0
    now = int(time.time())
    n = 0
    with _tx() as c:
        for d in items:
            try:
                c.execute(_DRAMA_UPSERT, _drama_params(d, site_id, now))
                n += 1
            except Exception:                # noqa: BLE001
                pass
    return n


def set_episode_count(site_id: str, did, count: int) -> None:
    """更新某剧的「平台总集数」基线（更新监测确认后调用）。"""
    count = int(count or 0)
    if count <= 0:
        return
    with _tx() as c:
        c.execute(
            "UPDATE dramas SET episode_count=?, last_seen=? "
            "WHERE site_id=? AND did=?",
            (count, int(time.time()), site_id, str(did))
        )


def get_drama(site_id: str, did) -> Optional[dict]:
    """取一条剧目（None 表示库内未见）。"""
    with _lock:
        c = _connect()
        row = c.execute(
            "SELECT * FROM dramas WHERE site_id=? AND did=?",
            (site_id, str(did))
        ).fetchone()
    return dict(row) if row else None


def get_category(site_id: str, did) -> str:
    """取某剧的归属分类（换脸/魔改 等单集类）；库内无记录返回空串。

    这是分类的「权威来源」：浏览分类 TAB 或打开详情时会随剧入库，
    之后搜索 / 更新检测 / 有更新 TAB / 一键补下 等任意入口下载都能据此
    正确归类到 J:\\AI剧\\换脸 等独立目录，而不必依赖内存映射。
    """
    with _lock:
        c = _connect()
        row = c.execute(
            "SELECT category FROM dramas WHERE site_id=? AND did=?",
            (site_id, str(did))
        ).fetchone()
    return (row["category"] or "") if row else ""


def set_category(site_id: str, did, category: str) -> None:
    """写入某剧的归属分类（仅单集类有意义；空串忽略）。"""
    if not category:
        return
    with _tx() as c:
        c.execute(
            "UPDATE dramas SET category=? WHERE site_id=? AND did=?",
            (category, site_id, str(did))
        )


def list_dramas(site_id: str, limit: int = 200) -> List[dict]:
    with _lock:
        c = _connect()
        rows = c.execute(
            "SELECT * FROM dramas WHERE site_id=? ORDER BY last_seen DESC LIMIT ?",
            (site_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def count_dramas(site_id: Optional[str] = None) -> int:
    with _lock:
        c = _connect()
        if site_id:
            return c.execute(
                "SELECT COUNT(*) FROM dramas WHERE site_id=?", (site_id,)
            ).fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM dramas").fetchone()[0]


# ---------------------------------------------------------------- 集状态

def set_episode_status(site_id: str, did, ep: int, status: str,
                       file_path: str = "", size_bytes: int = 0,
                       duration: float = 0.0, error: str = "") -> None:
    """写入/更新一集状态。"""
    if status not in _EP_STATUSES:
        status = EP_FAILED
    now = int(time.time())
    with _tx() as c:
        c.execute("""
        INSERT INTO episodes(site_id,did,ep,status,file_path,size_bytes,duration,error,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(site_id,did,ep) DO UPDATE SET
            status=excluded.status,
            file_path=COALESCE(NULLIF(excluded.file_path,''), episodes.file_path),
            size_bytes=CASE WHEN excluded.size_bytes>0 THEN excluded.size_bytes ELSE episodes.size_bytes END,
            duration=CASE WHEN excluded.duration>0 THEN excluded.duration ELSE episodes.duration END,
            error=excluded.error,
            updated_at=excluded.updated_at
        """, (site_id, str(did), int(ep), status, file_path,
              int(size_bytes), float(duration), error, now))


def known_done_eps(site_id: str, did) -> Dict[int, str]:
    """返回已记为 done 的集号 -> 文件路径（仅查库，零 ffprobe）。

    用途：EpisodePanel 选中剧时先按 DB 瞬时标记，文件丢失的会留待后台复核。
    """
    with _lock:
        c = _connect()
        rows = c.execute(
            "SELECT ep, file_path FROM episodes "
            "WHERE site_id=? AND did=? AND status='done' ORDER BY ep",
            (site_id, str(did))
        ).fetchall()
    return {int(r["ep"]): (r["file_path"] or "") for r in rows}


def downloaded_status_map(site_id: str) -> Dict[str, int]:
    """返回当前站点「每部已下载(done)集数」映射：{did_str: done_count}。

    用于网格卡片即时标注「已下载 / 部分下载」。一次聚合查询覆盖全站，
    避免逐剧查库的 N 次往返。未下载的剧不会出现在结果里（视为 0）。
    """
    with _lock:
        c = _connect()
        rows = c.execute(
            "SELECT did, COUNT(*) AS n FROM episodes "
            "WHERE site_id=? AND status='done' GROUP BY did",
            (site_id,),
        ).fetchall()
    return {str(r["did"]): int(r["n"]) for r in rows}


def episode_status_map(site_id: str, did, eps: Iterable[int]) -> Dict[int, str]:
    """批量取若干集的状态（pending/running/done/failed/cancelled 或未知）。"""
    eps = [int(e) for e in eps]
    if not eps:
        return {}
    placeholders = ",".join("?" * len(eps))
    with _lock:
        c = _connect()
        rows = c.execute(
            f"SELECT ep, status FROM episodes "
            f"WHERE site_id=? AND did=? AND ep IN ({placeholders})",
            (site_id, str(did), *eps)
        ).fetchall()
    return {int(r["ep"]): r["status"] for r in rows}


# ---------------------------------------------------------------- 下载流水

def log_download(site_id: str, did, ep: int, title: str,
                 status: str, bytes_done: int = 0,
                 started_at: Optional[int] = None,
                 finished_at: Optional[int] = None,
                 error: str = "") -> int:
    """写一条下载流水，返回自增 id。"""
    started_at = started_at or int(time.time())
    with _tx() as c:
        cur = c.execute("""
        INSERT INTO downloads(site_id,did,ep,title,status,bytes,started_at,finished_at,error)
        VALUES(?,?,?,?,?,?,?,?,?)
        """, (site_id, str(did), int(ep), title or "", status,
              int(bytes_done), int(started_at),
              int(finished_at) if finished_at else None, error))
        return int(cur.lastrowid)


def update_download(download_id: int, status: str,
                    bytes_done: Optional[int] = None,
                    finished_at: Optional[int] = None,
                    error: str = "") -> None:
    if status not in _EP_STATUSES:
        status = EP_FAILED
    finished_at = int(finished_at) if finished_at else int(time.time())
    with _tx() as c:
        if bytes_done is None:
            c.execute("""UPDATE downloads SET status=?, finished_at=?, error=? WHERE id=?""",
                      (status, finished_at, error, download_id))
        else:
            c.execute("""UPDATE downloads SET status=?, bytes=?, finished_at=?, error=? WHERE id=?""",
                      (status, int(bytes_done), finished_at, error, download_id))


def recent_downloads(limit: int = 50) -> List[dict]:
    with _lock:
        c = _connect()
        rows = c.execute(
            "SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 统计

def stats(site_id: Optional[str] = None) -> dict:
    """汇总：剧数、集状态、累计字节、最后活跃。"""
    with _lock:
        c = _connect()
        params: Tuple = ()
        where = ""
        if site_id:
            where = " WHERE site_id=?"
            params = (site_id,)
        d_count = c.execute(
            f"SELECT COUNT(*) FROM dramas{where}", params
        ).fetchone()[0]
        ep_rows = c.execute(
            f"SELECT status, COUNT(*) c FROM episodes{where} GROUP BY status", params
        ).fetchall()
        total_bytes = c.execute(
            f"SELECT COALESCE(SUM(size_bytes),0) FROM episodes WHERE status='done'{(' AND site_id=?' if site_id else '')}",
            params
        ).fetchone()[0]
        last = c.execute(
            f"SELECT MAX(updated_at) FROM episodes{where}", params
        ).fetchone()[0]
    return {
        "dramas": d_count,
        "episodes": {r["status"]: r["c"] for r in ep_rows},
        "total_bytes": total_bytes,
        "last_episode_at": last,
    }


# ---------------------------------------------------------------- 通用键值

def set_kv(key: str, value: str) -> None:
    now = int(time.time())
    with _tx() as c:
        c.execute("""INSERT INTO meta(key,value,updated_at) VALUES(?,?,?)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                  (key, value, now))


def get_kv(key: str, default: Optional[str] = None) -> Optional[str]:
    with _lock:
        c = _connect()
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return (row["value"] if row else default)


# ---------------------------------------------------------------- 同步记录
# 把每次「同步到外盘」的会话持久化，便于追溯「今天全量拷了什么到移动硬盘」、
# 「上次增量拷了哪些」，也方便把移动硬盘拿到服务器后对照清单转移。

def add_sync_session(ts: int, role: str, target_path: str,
                     scope_desc: str, mode: str,
                     file_count: int, total_bytes: int,
                     manifest_path: str = "") -> int:
    """写一条同步会话记录，返回自增 id。仅 file_count>0 时调用才有意义。"""
    with _tx() as c:
        cur = c.execute("""
        INSERT INTO sync_sessions(ts, role, target_path, scope_desc, mode,
                                  file_count, total_bytes, manifest_path)
        VALUES(?,?,?,?,?,?,?,?)
        """, (int(ts), role, target_path, scope_desc or "", mode or "",
              int(file_count), int(total_bytes), manifest_path or ""))
        return int(cur.lastrowid)


def get_sync_sessions(limit: int = 100) -> List[dict]:
    """返回最近 limit 条同步会话（时间倒序）。"""
    with _lock:
        c = _connect()
        rows = c.execute(
            "SELECT * FROM sync_sessions ORDER BY ts DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 同步文件清单
# 增量同步的权威依据：每个目标盘「已经成功同步过去」的文件集合。
# 全部保存在**项目内**的 DATA/hg.db，不写到外盘、不依赖外盘上的任何文件。

def get_sync_files(role: str, target_path: str) -> Dict[str, dict]:
    """返回某目标盘已同步文件的映射 {rel_path: {size, mtime, synced_at}}。

    rel_path 统一用正斜杠（Windows 上也如此），便于跨平台比较。
    """
    out: Dict[str, dict] = {}
    try:
        with _lock:
            c = _connect()
            rows = c.execute(
                "SELECT rel_path, size, mtime, synced_at FROM sync_files "
                "WHERE role=? AND target_path=?",
                (role, _norm_target(target_path)),
            ).fetchall()
    except Exception:                       # noqa: BLE001
        return out
    for r in rows:
        out[str(r["rel_path"]).replace("\\", "/")] = {
            "size": int(r["size"] or 0),
            "mtime": float(r["mtime"] or 0.0),
            "synced_at": int(r["synced_at"] or 0),
        }
    return out


def _norm_target(target_path: str) -> str:
    """目标盘路径归一化：去尾斜杠、统一大写盘符，避免 H:\\AI剧 与 H:/AI剧/ 视为两个盘。"""
    p = (target_path or "").strip().replace("/", "\\").rstrip("\\")
    if len(p) >= 2 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def record_sync_files(role: str, target_path: str,
                      files: Iterable[Tuple[str, int, float]],
                      session_id: int = 0, synced_at: int = 0) -> int:
    """把本轮成功同步的文件登记进 sync_files（增量基准）。

    files: [(rel_path, size, mtime), ...]
    返回登记条数。同一 rel_path 重复登记时覆盖旧记录。
    """
    rows = []
    ts = int(synced_at or time.time())
    for rel, size, mtime in files:
        r = str(rel).replace("\\", "/")
        rows.append((role, _norm_target(target_path), r,
                     int(size or 0), float(mtime or 0.0), ts, int(session_id or 0)))
    if not rows:
        return 0
    with _tx() as c:
        c.executemany("""
        INSERT INTO sync_files(role, target_path, rel_path, size, mtime,
                                synced_at, session_id)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(role, target_path, rel_path) DO UPDATE SET
            size=excluded.size, mtime=excluded.mtime,
            synced_at=excluded.synced_at, session_id=excluded.session_id
        """, rows)
    return len(rows)


def forget_sync_files(role: str, target_path: str) -> int:
    """清空某目标盘的同步基准（下次同步视为首次 → 全量）。"""
    with _tx() as c:
        cur = c.execute("DELETE FROM sync_files WHERE role=? AND target_path=?",
                         (role, _norm_target(target_path)))
        return int(cur.rowcount or 0)


def get_sync_mark(role: str, target_path: str) -> int:
    """取某目标盘「最近一次同步完成时刻」（epoch 秒），没有则返回 0。"""
    try:
        with _lock:
            c = _connect()
            row = c.execute(
                "SELECT last_sync_at FROM sync_marks WHERE role=? AND target_path=?",
                (role, _norm_target(target_path))).fetchone()
    except Exception:                       # noqa: BLE001
        return 0
    return int(row["last_sync_at"] or 0) if row else 0


def set_sync_mark(role: str, target_path: str, ts: int) -> None:
    """记录某目标盘「最近一次同步完成时刻」。"""
    with _tx() as c:
        c.execute("""
        INSERT INTO sync_marks(role, target_path, last_sync_at) VALUES(?,?,?)
        ON CONFLICT(role, target_path) DO UPDATE SET last_sync_at=excluded.last_sync_at
        """, (role, _norm_target(target_path), int(ts)))


def add_sync_session_files(session_id: int, role: str,
                            files: Iterable[Tuple[str, int]]) -> int:
    """记录某轮同步的**文件明细**（清单本体，留在项目 DB 内）。"""
    rows = [(int(session_id), role, str(rel).replace("\\", "/"), int(size or 0))
            for rel, size in files]
    if not rows:
        return 0
    with _tx() as c:
        c.executemany(
            "INSERT INTO sync_session_files(session_id, role, rel_path, size) "
            "VALUES(?,?,?,?)", rows)
    return len(rows)


def get_sync_session_files(session_id: int) -> List[dict]:
    """取某轮同步的文件明细。"""
    with _lock:
        c = _connect()
        rows = c.execute(
            "SELECT rel_path, size FROM sync_session_files "
            "WHERE session_id=? ORDER BY id", (int(session_id),)).fetchall()
    return [{"rel_path": str(r["rel_path"]), "size": int(r["size"] or 0)}
            for r in rows]


# ---------------------------------------------------------------- 增量文件索引
# 性能关键：全量 rglob 遍历下载盘 2 万个文件要 **175 秒**（用户反馈"检查很久"）。
# 而 episodes 表里已存着每个已完成集的**精确 file_path**，查询只要 0.02 秒。
# 增量同步时先用 DB 圈出「候选文件」，只有 DB 不可用时才回退全量扫描。

def get_downloaded_files_since(src_root: str, since_ts: float = 0.0,
                               limit: int = 0) -> List[dict]:
    """查出「下载根目录内、更新时间晚于 since_ts」的已完成剧集文件。

    返回 [{path, size, mtime}, ...]，path 为绝对路径。

    ⚠ 只覆盖**通过程序下载**的文件（episodes.file_path）。手工放进下载盘、
    或早于本表建立时间的文件不会出现在结果里——所以调用方必须保留
    「全量扫描」作为兜底/校验手段，不能只信这个索引。
    """
    out: List[dict] = []
    root = str(src_root or "").rstrip("\\/")
    if not root:
        return out
    prefix = root + "\\"
    sql = ("SELECT file_path, size_bytes, updated_at FROM episodes "
           "WHERE status='done' AND file_path IS NOT NULL AND file_path<>'' "
           "AND file_path LIKE ?")
    args: list = [prefix + "%"]
    if since_ts > 0:
        sql += " AND updated_at > ?"
        args.append(int(since_ts))
    sql += " ORDER BY updated_at DESC"
    if limit > 0:
        sql += " LIMIT ?"
        args.append(int(limit))
    try:
        with _lock:
            c = _connect()
            rows = c.execute(sql, args).fetchall()
    except Exception:                       # noqa: BLE001
        return out
    for r in rows:
        p = str(r["file_path"])
        out.append({"path": p, "size": int(r["size_bytes"] or 0),
                    "mtime": float(r["updated_at"] or 0)})
    return out


def get_drama_dirs_for_sync(src_root: str, since_ts: float = 0.0,
                            exclude_paths: Optional[set] = None) -> List[str]:
    """列出「下载根目录下、最近有下载活动的剧集目录」（绝对路径）。

    用于增量同步时**只扫相关剧目录**，而不是整个下载盘。
    做法：从 episodes.file_path 反推剧目录（去掉 "Season xx" 与文件名两层）。

    exclude_paths: 已同步文件的相对路径集合（posix 风格）。命中这些的
    剧目录不再返回——它们没有新东西要拷，扫了纯浪费时间。
    这是把「增量」从「全量 2400+ 个目录」缩到「真正有新增的几个目录」的关键。
    """
    files = get_downloaded_files_since(src_root, since_ts)
    dirs = set()
    root = str(src_root or "").rstrip("\\/")
    for f in files:
        p = Path(f["path"])
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if exclude_paths:
            if rel.as_posix() in exclude_paths:
                continue        # 这一集已同步过，没有新增
        parts = rel.parts
        if len(parts) >= 3 and re.match(r"^Season\s+\d+$", parts[-2], re.I):
            dirs.add(str(p.parent.parent))      # <剧目录>/Season 01/xxx.mp4
        elif len(parts) >= 2:
            dirs.add(str(p.parent))             # <剧目录>/xxx.mp4
    return sorted(dirs)


def get_download_dir_size() -> int:
    """DATA 目录的尺寸估算（含封面与 DB）。"""
    total = 0
    root = config.DATA_DIR
    if not root.is_dir():
        return 0
    for p in root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------- 下载记录（聚合视图）

def list_downloaded_dramas(site_id: Optional[str] = None,
                           include_zero: bool = False) -> List[dict]:
    """列出「有下载记录」的剧（剧列表视图）。

    返回每部剧一行：标题、站点、已下载集数、平台总集数、占用字节、最近更新时间、封面。
    include_zero=True 时连「一部未下」的剧也返回（用于浏览全部剧库）。

    说明：总集数以 dramas.episode_count（平台集数）为准；若为 0 则退化为
    episodes 表里的最大集号，避免显示 "0 集"。
    """
    where_site = " AND e.site_id = ?" if site_id else ""
    having = "" if include_zero else " HAVING COUNT(CASE WHEN e.status='done' THEN 1 END) > 0"
    params: Tuple = (site_id,) if site_id else ()
    sql = f"""
    SELECT
        e.site_id                                   AS site_id,
        e.did                                       AS did,
        COALESCE(d.title, e.did)                    AS title,
        COALESCE(d.cover_url, '')                   AS cover_url,
        COALESCE(d.detail_url, '')                  AS detail_url,
        COALESCE(d.episode_count, 0)                AS platform_eps,
        CASE WHEN d.did IS NULL THEN 0 ELSE 1 END    AS has_meta,
        COUNT(CASE WHEN e.status='done' THEN 1 END) AS done_eps,
        COUNT(CASE WHEN e.status='failed' THEN 1 END) AS failed_eps,
        COUNT(e.ep)                                 AS tracked_eps,
        COALESCE(SUM(CASE WHEN e.status='done' THEN e.size_bytes ELSE 0 END), 0) AS total_bytes,
        MAX(CASE WHEN e.status='done' THEN e.updated_at ELSE 0 END) AS last_done_at,
        MAX(e.updated_at)                           AS last_at
    FROM episodes e
    LEFT JOIN dramas d
           ON d.site_id = e.site_id AND d.did = e.did
    WHERE 1=1{where_site}
    GROUP BY e.site_id, e.did
    {having}
    ORDER BY last_at DESC
    """
    with _lock:
        c = _connect()
        rows = c.execute(sql, params).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        total = max(int(d.get("platform_eps") or 0),
                    int(d.get("tracked_eps") or 0),
                    int(d.get("done_eps") or 0))
        d["total_eps"] = total
        out.append(d)
    return out


def episodes_of(site_id: str, did, limit: int = 1000) -> List[dict]:
    """某部剧的每集明细（集号、状态、大小、路径、更新时间）。"""
    with _lock:
        c = _connect()
        rows = c.execute(
            "SELECT ep, status, file_path, size_bytes, duration, error, updated_at "
            "FROM episodes WHERE site_id=? AND did=? ORDER BY ep LIMIT ?",
            (site_id, str(did), limit)
        ).fetchall()
    return [dict(r) for r in rows]


def overall_stats() -> dict:
    """全局总览：剧数、完成集数、总字节、失败集数、最近下载时间。"""
    with _lock:
        c = _connect()
        dramas_cnt = c.execute("SELECT COUNT(*) FROM dramas").fetchone()[0]
        done = c.execute(
            "SELECT COUNT(*) FROM episodes WHERE status='done'").fetchone()[0]
        failed = c.execute(
            "SELECT COUNT(*) FROM episodes WHERE status='failed'").fetchone()[0]
        total_bytes = c.execute(
            "SELECT COALESCE(SUM(size_bytes),0) FROM episodes WHERE status='done'"
        ).fetchone()[0]
        last = c.execute(
            "SELECT MAX(updated_at) FROM episodes WHERE status='done'").fetchone()[0]
        dls = c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
    return {
        "dramas": dramas_cnt,
        "done_episodes": done,
        "failed_episodes": failed,
        "total_bytes": total_bytes,
        "last_done_at": last,
        "download_records": dls,
    }


# ---------------------------------------------------------------- 目录迁移

def rewrite_file_paths(old_prefix: str, new_prefix: str) -> int:
    """下载目录转移后，把 episodes.file_path 的旧前缀改为新前缀。

    返回被改写的行数。仅做字符串前缀替换（大小写与分隔符规范化由调用方保证）。
    """
    old_prefix = (old_prefix or "").rstrip("\\/")
    new_prefix = (new_prefix or "").rstrip("\\/")
    if not old_prefix or not new_prefix or old_prefix == new_prefix:
        return 0
    n = 0
    with _tx() as c:
        rows = c.execute(
            "SELECT rowid, file_path FROM episodes WHERE file_path LIKE ?",
            (old_prefix + "%",)
        ).fetchall()
        for row in rows:
            fp = row["file_path"] or ""
            new_fp = new_prefix + fp[len(old_prefix):]
            c.execute("UPDATE episodes SET file_path=? WHERE rowid=?",
                      (new_fp, row["rowid"]))
            n += 1
    return n

