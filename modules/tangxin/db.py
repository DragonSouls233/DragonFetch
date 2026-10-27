# -*- coding: utf-8 -*-
"""糖心数据库（SQLite，单文件 ``data/tangxin.db``，与主平台完全隔离）。

## 表清单
1. ``downloads``     —— 每次下载任务的状态/结果（历史记录）
2. ``existing_files``—— 已下载文件索引（跨根目录去重，见 existing.py）
3. ``actors``        —— 演员云缓存（★ 启动秒开的关键，见 cache.py）
4. ``videos``        —— 作品列表缓存（★ 启动秒开的关键，见 cache.py）
5. ``meta``          —— 杂项键值（如上次全量同步时间）

所有写操作都加锁（多线程下载/后台同步安全），且由调用方保证 best-effort
（DB 异常不影响下载）。连接为**进程级单例**（WAL 模式，支持读写并发）。
"""
import os
import sqlite3
import threading
from datetime import datetime

from . import config

_lock = threading.Lock()
_conn = None

_COLS = ["id", "vid", "actor", "title", "file_path", "poster_path", "nfo_path",
         "status", "size_bytes", "duration_sec", "started_at", "finished_at", "error"]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db_path():
    """数据库文件路径（默认 ``{RUNTIME_ROOT}/data/tangxin.db``）。

    首次调用会把老版本遗留在根目录的 DB 自动挪进 ``data/``（见 layout.py）。
    """
    env = os.environ.get("TX_DB")
    if env:
        return env
    try:
        from . import layout
        return layout.data_file("tangxin.db")
    except Exception:
        return getattr(config, "DOWNLOAD_DB", None) or os.path.join(
            config.RUNTIME_ROOT, "tangxin.db")


def get_conn():
    """取得进程级数据库连接（公开给 cache.py 复用，避免开第二个连接）。"""
    return _connect()


def _connect():
    global _conn
    if _conn is None:
        path = get_db_path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        c = sqlite3.connect(path, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")          # 并发读写更稳
        c.execute("PRAGMA synchronous=NORMAL")         # 写更快（缓存场景够用）
        c.execute("""CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vid TEXT UNIQUE,
            actor TEXT,
            title TEXT,
            file_path TEXT,
            poster_path TEXT,
            nfo_path TEXT,
            status TEXT,
            size_bytes INTEGER,
            duration_sec REAL,
            started_at TEXT,
            finished_at TEXT,
            error TEXT
        )""")
        # 已下载文件索引（跨根目录去重，见 existing.py）
        c.execute("""CREATE TABLE IF NOT EXISTS existing_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root TEXT,
            vid TEXT,
            actor TEXT,
            title TEXT,
            mp4_path TEXT UNIQUE,
            size_bytes INTEGER,
            mtime REAL,
            scanned_at TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_existing_vid ON existing_files(vid)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_existing_key ON existing_files(actor, title)")
        # ---- 演员云缓存（启动秒开）----
        c.execute("""CREATE TABLE IF NOT EXISTS actors (
            slug TEXT PRIMARY KEY,
            name TEXT,
            count INTEGER DEFAULT 0,
            first_seen TEXT,
            last_seen TEXT,
            synced_at TEXT
        )""")
        # ---- 作品列表缓存（启动秒开）----
        c.execute("""CREATE TABLE IF NOT EXISTS videos (
            vid TEXT PRIMARY KEY,
            actor_slug TEXT,
            actor_name TEXT,
            title TEXT,
            cover_url TEXT,
            duration TEXT,
            first_seen TEXT,
            last_seen TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_videos_actor ON videos(actor_slug)")
        c.execute("""CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY,
            v TEXT
        )""")
        c.commit()
        _conn = c
    return _conn


def record_start(vid, actor, title, file_path):
    """下载开始：写入 downloading 状态（同一 vid 幂等更新，不重复插行）。"""
    if not vid:
        return
    with _lock:
        c = _connect()
        now = _now()
        c.execute("""INSERT INTO downloads (vid, actor, title, file_path, status, started_at)
            VALUES (?,?,?,?, 'downloading', ?)
            ON CONFLICT(vid) DO UPDATE SET
                actor=excluded.actor, title=excluded.title, file_path=excluded.file_path,
                status='downloading', started_at=excluded.started_at, finished_at=NULL, error=NULL""",
            (vid, actor, title, file_path, now))
        c.commit()


def record_done(vid, file_path, poster_path, nfo_path, size_bytes, duration_sec):
    """下载成功：标记 done 并补充分片大小/NFO/封面路径。"""
    if not vid:
        return
    with _lock:
        c = _connect()
        c.execute("""UPDATE downloads SET status='done', file_path=?, poster_path=?, nfo_path=?,
            size_bytes=?, duration_sec=?, finished_at=? WHERE vid=?""",
            (file_path, poster_path, nfo_path, size_bytes, duration_sec, _now(), vid))
        c.commit()


def record_failed(vid, error):
    """下载失败：标记 failed 并记录错误（截断到 1000 字符）。"""
    if not vid:
        return
    with _lock:
        c = _connect()
        c.execute("UPDATE downloads SET status='failed', error=?, finished_at=? WHERE vid=?",
            (str(error)[:1000], _now(), vid))
        c.commit()


def record_skipped(vid):
    """已存在被跳过：标记 skipped。"""
    if not vid:
        return
    with _lock:
        c = _connect()
        c.execute("UPDATE downloads SET status='skipped', finished_at=? WHERE vid=?",
            (_now(), vid))
        c.commit()


def get_records(limit=500, status=None):
    """查询下载记录（按 id 倒序）。status 可选过滤。返回 list[dict]。"""
    with _lock:
        c = _connect()
        cols = ",".join(_COLS)
        if status:
            rows = c.execute("SELECT %s FROM downloads WHERE status=? ORDER BY id DESC LIMIT ?"
                             % cols, (status, limit)).fetchall()
        else:
            rows = c.execute("SELECT %s FROM downloads ORDER BY id DESC LIMIT ?"
                             % cols, (limit,)).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]


def count_by_status():
    """统计各状态数量，用于 CLI 收尾汇总。"""
    with _lock:
        c = _connect()
        rows = c.execute("SELECT status, COUNT(*) FROM downloads GROUP BY status").fetchall()
        return {s: n for s, n in rows}


def close():
    global _conn
    with _lock:
        if _conn:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None


# ---------- 已下载文件索引（跨根目录去重）----------
_EXISTING_COLS = ["root", "vid", "actor", "title", "mp4_path",
                  "size_bytes", "mtime", "scanned_at"]


def replace_existing(rows):
    """整表替换已下载索引（扫描结果一次性写入，避免逐行开事务）。"""
    with _lock:
        c = _connect()
        c.execute("DELETE FROM existing_files")
        c.executemany(
            """INSERT OR REPLACE INTO existing_files
               (root, vid, actor, title, mp4_path, size_bytes, mtime, scanned_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(r.get("root") or "", str(r.get("vid") or ""), r.get("actor") or "",
              r.get("title") or "", r.get("mp4_path") or "",
              int(r.get("size_bytes") or 0), float(r.get("mtime") or 0), _now())
             for r in rows])
        c.commit()


def upsert_existing(row):
    """往已下载索引里**插/更一行**（刚下载完立即生效，不必等全盘扫描）。"""
    if not row or not row.get("mp4_path"):
        return False
    with _lock:
        c = _connect()
        c.execute(
            """INSERT OR REPLACE INTO existing_files
               (root, vid, actor, title, mp4_path, size_bytes, mtime, scanned_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (row.get("root") or "", str(row.get("vid") or ""), row.get("actor") or "",
             row.get("title") or "", row.get("mp4_path") or "",
             int(row.get("size_bytes") or 0), float(row.get("mtime") or 0), _now()))
        c.commit()
    return True


def get_existing_all():
    """读取全部索引行。"""
    with _lock:
        c = _connect()
        cols = ",".join(_EXISTING_COLS)
        rows = c.execute("SELECT %s FROM existing_files" % cols).fetchall()
        return [dict(zip(_EXISTING_COLS, r)) for r in rows]


def get_existing_by_vid(vid):
    with _lock:
        c = _connect()
        cols = ",".join(_EXISTING_COLS)
        rows = c.execute("SELECT %s FROM existing_files WHERE vid=? LIMIT 1"
                         % cols, (str(vid),)).fetchall()
        return dict(zip(_EXISTING_COLS, rows[0])) if rows else None


def count_existing():
    with _lock:
        c = _connect()
        return c.execute("SELECT COUNT(*) FROM existing_files").fetchone()[0]


def downloaded_actor_stats():
    """按演员聚合**已下载成品** → ``list[dict]``（数量多的排前面）。

    返回项：
      - ``actor``       展示名（该组里文件数最多的那个原始目录名）
      - ``key``         归并键（``utils.norm_key``）
      - ``names``       该组包含的所有原始目录名（查明细时用，能捞全变体）
      - ``n``           已下载部数
      - ``total_bytes`` 总大小
      - ``last_mtime`` 最近一次文件修改时间（= 最近下载时间）
      - ``site_total``  站上该演员的作品总数（actors 表缓存，匹配不到为 0）

    ⚠️ 用 ``norm_key`` 归并"同一个演员的不同写法"（全角/半角、大小写、空格差异），
       与 ``count_existing_by_actor`` 口径一致 —— 否则列表里会出现
       「林予曦」和「林予曦 」两条，点进去各自只有一半片子（实测会踩）。
    """
    from .utils import norm_key as _nk

    with _lock:
        c = _connect()
        rows = c.execute(
            "SELECT actor, COUNT(*), COALESCE(SUM(size_bytes),0), "
            "COALESCE(MAX(mtime),0) FROM existing_files GROUP BY actor").fetchall()
        try:
            site = c.execute("SELECT name, count FROM actors").fetchall()
        except Exception:
            site = []

    site_map = {}
    for name, cnt in site:
        k = _nk(name)
        if k:
            site_map[k] = max(int(cnt or 0), site_map.get(k, 0))

    out = {}
    for actor, n, total, last in rows:
        k = _nk(actor) or ""
        it = out.get(k)
        if it is None:
            it = out[k] = {"actor": actor or "(未分类)", "key": k, "names": set(),
                           "n": 0, "total_bytes": 0, "last_mtime": 0.0,
                           "site_total": site_map.get(k, 0), "_best": -1}
        it["n"] += int(n or 0)
        it["total_bytes"] += int(total or 0)
        it["last_mtime"] = max(it["last_mtime"], float(last or 0))
        it["names"].add(actor or "")
        # 展示名取"文件数最多"的那个写法（并列时取更长的，视觉信息更全）
        if int(n or 0) > it["_best"] or (
                int(n or 0) == it["_best"] and len(actor or "") > len(it["actor"])):
            it["_best"] = int(n or 0)
            it["actor"] = actor or "(未分类)"
    for it in out.values():
        it.pop("_best", None)
        it["names"] = sorted(it["names"])
    return sorted(out.values(), key=lambda d: (-d["n"], d["actor"]))


def existing_detail_by_actor_names(names, limit=3000):
    """给定一组**原始演员目录名**，返回这些演员的已下载明细（按文件时间倒序）。

    顺带 LEFT JOIN 出作品时长（videos）与完成时间（downloads）—— 有了它们，
    「已下载」TAB 才能显示"这片子多长、什么时候下的"，而不只是文件名。

    ⚠️ ``e.vid <> ''`` 是必须的：无 NFO 的裸 mp4 的 vid 是空串，
       不加这个条件会与所有空 vid 行交叉相乘（行数爆炸）。
    """
    names = [x for x in (names or []) if x is not None]
    if not names:
        return []
    qs = ",".join("?" * len(names))
    sql = (
        "SELECT e.title, e.vid, e.actor, e.mp4_path, e.size_bytes, e.mtime, e.root, "
        "  COALESCE(v.duration,'') AS duration, "
        "  COALESCE(d.finished_at,'') AS finished_at "
        "FROM existing_files e "
        "LEFT JOIN videos v ON v.vid = e.vid AND e.vid <> '' "
        "LEFT JOIN downloads d ON d.vid = e.vid AND e.vid <> '' "
        "WHERE e.actor IN (%s) "
        "ORDER BY e.mtime DESC, e.id DESC LIMIT ?" % qs)
    cols = ["title", "vid", "actor", "mp4_path", "size_bytes", "mtime", "root",
            "duration", "finished_at"]
    with _lock:
        c = _connect()
        rows = c.execute(sql, names + [int(limit)]).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def count_existing_by_actor():
    """按演员统计已下载文件数 → ``{actor_key: n}``。

    ``actor_key`` 用 NFKC+casefold+去空白 的规范化键（见 existing.norm_key），
    磁盘目录名与站点演员名的细微差异也能对上。
    """
    import unicodedata
    import re as _re

    def _k(text):
        if not text:
            return ""
        s = unicodedata.normalize("NFKC", str(text)).casefold()
        return _re.sub(r"\s+", "", s).strip()

    with _lock:
        c = _connect()
        rows = c.execute("SELECT actor, COUNT(*) FROM existing_files "
                         "GROUP BY actor").fetchall()
    out = {}
    for actor, n in rows:
        out[_k(actor)] = out.get(_k(actor), 0) + int(n or 0)
    return out


def upsert_downloads_from_scan(rows):
    """把扫描到的成品写进**下载记录**表（status='done'）。

    用户需求：「识别到下载目录已经下好的，需要记录到数据库里」——
    这样「下载记录」对话框能查到这些片子，统计口径也统一。

    ``rows`` 元素需含：vid/actor/title/mp4_path/nfo_path/poster_path/size_bytes/duration_sec。
    有 vid 的按 vid 幂等更新；没有 vid 的（无 NFO 的裸 mp4）用
    ``actor+title`` 兜底去重。返回写入条数。
    """
    n = 0
    with _lock:
        c = _connect()
        now = _now()
        for r in rows or []:
            vid = str(r.get("vid") or "").strip()
            actor = r.get("actor") or ""
            title = r.get("title") or ""
            mp4 = r.get("mp4_path") or ""
            nfo = r.get("nfo_path") or ""
            poster = r.get("poster_path") or ""
            size = int(r.get("size_bytes") or 0)
            dur = r.get("duration_sec")
            if vid:
                c.execute(
                    """INSERT INTO downloads (vid, actor, title, file_path, poster_path,
                        nfo_path, status, size_bytes, duration_sec, started_at, finished_at, error)
                       VALUES (?,?,?,?,?,?, 'done', ?,?,?,?,NULL)
                       ON CONFLICT(vid) DO UPDATE SET
                         actor=excluded.actor, title=excluded.title,
                         file_path=excluded.file_path, poster_path=excluded.poster_path,
                         nfo_path=excluded.nfo_path, status='done',
                         size_bytes=excluded.size_bytes, duration_sec=excluded.duration_sec,
                         finished_at=excluded.finished_at""",
                    (vid, actor, title, mp4, poster, nfo, size, dur, now, now))
                n += 1
            else:
                # 无 vid：用 (actor,title) 找已有行更新，否则新增
                row = c.execute(
                    "SELECT id FROM downloads WHERE (vid IS NULL OR vid='') "
                    "AND actor=? AND title=? LIMIT 1", (actor, title)).fetchone()
                if row:
                    c.execute("UPDATE downloads SET file_path=?, poster_path=?, nfo_path=?, "
                              "status='done', size_bytes=?, duration_sec=?, finished_at=? "
                              "WHERE id=?",
                              (mp4, poster, nfo, size, dur, now, row[0]))
                else:
                    c.execute(
                        """INSERT INTO downloads (vid, actor, title, file_path, poster_path,
                            nfo_path, status, size_bytes, duration_sec, started_at, finished_at, error)
                           VALUES ('',?,?,?,?,?, 'done', ?,?,?,?,NULL)""",
                        (actor, title, mp4, poster, nfo, size, dur, now, now))
                n += 1
        c.commit()
    return n
