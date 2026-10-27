# -*- coding: utf-8 -*-
"""站点内容缓存层（SQLite，落在 ``data/tangxin.db``）。

## 要解决的问题
以前每次打开软件都要重新联网抓：演员云（800+ 位）+ 点开某位演员再抓她的作品
（还要翻页）。网络一慢就干等，而且**每次打开都重复劳动**。

## 方案（缓存优先 + 后台增量）
1. **打开即读缓存**：演员云、作品列表都先从 DB 读，**毫秒级**填满界面。
2. **后台增量同步**：后台线程再联网抓一遍，只把**新增的**写进 DB、插进界面；
   已有的不动（不重复、不闪烁、不重新加载）。
3. **全量预热**：首次运行时后台把**所有**演员的作品都抓一遍入库，
   之后每次打开都是"全量缓存秒开"。

## 增量语义（关键）
``upsert_actors`` / ``upsert_videos`` 都是**只增不改**为主：
- 不存在的行 → INSERT（计数 added）
- 已存在的行 → 只有**内容真的变了**才 UPDATE（计数 updated）
返回 ``{"added": n, "updated": m, "total": t}``，GUI 据此提示
"新增 N 位 / 更新 M 位"。
"""
import threading
from datetime import datetime

from . import db
from .models import Actor, Video

_lock = threading.Lock()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 演员云
def upsert_actors(actors) -> dict:
    """写入/更新演员云，返回 ``{added, updated, total}``。

    ⚠️ 名字变化时**保留 slug 作为主键**（slug 是 URL 标识，名字只是展示名）。
    """
    rows = []
    for a in actors or []:
        slug = (getattr(a, "slug", "") or "").strip()
        if not slug:
            continue
        rows.append((slug, getattr(a, "name", "") or "",
                     int(getattr(a, "count", 0) or 0)))
    if not rows:
        return {"added": 0, "updated": 0, "total": 0}

    added = updated = 0
    with _lock:
        c = db.get_conn()
        now = _now()
        for slug, name, cnt in rows:
            old = c.execute("SELECT name, count FROM actors WHERE slug=?",
                            (slug,)).fetchone()
            if old is None:
                # ⚠️ synced_at 留空：演员刚进缓存，**作品还没抓过**，
                #    预热队列正是靠这个字段挑出"还没同步作品"的演员。
                c.execute(
                    "INSERT INTO actors (slug, name, count, first_seen, last_seen, synced_at)"
                    " VALUES (?,?,?,?,?,NULL)",
                    (slug, name, cnt, now, now))
                added += 1
            else:
                if old[0] != name or int(old[1] or 0) != cnt:
                    # ⚠️ 不动 synced_at：作品是否同步过是另一回事
                    c.execute("UPDATE actors SET name=?, count=?, last_seen=? "
                              "WHERE slug=?", (name, cnt, now, slug))
                    updated += 1
                else:
                    c.execute("UPDATE actors SET last_seen=? WHERE slug=?",
                              (now, slug))
        c.commit()
        total = c.execute("SELECT COUNT(*) FROM actors").fetchone()[0]
    return {"added": added, "updated": updated, "total": total}


def get_actors() -> list:
    """读取全部演员（按作品数倒序，再按名字），返回 ``List[Actor]``。"""
    with _lock:
        c = db.get_conn()
        rows = c.execute(
            "SELECT slug, name, count FROM actors "
            "ORDER BY count DESC, name COLLATE NOCASE ASC").fetchall()
    return [Actor(slug=s, name=n or "", count=int(cn or 0)) for s, n, cn in rows]


def get_actor(slug) -> Actor:
    with _lock:
        c = db.get_conn()
        row = c.execute("SELECT slug, name, count FROM actors WHERE slug=?",
                        (str(slug),)).fetchone()
    if not row:
        return None
    return Actor(slug=row[0], name=row[1] or "", count=int(row[2] or 0))


def actor_count() -> int:
    with _lock:
        c = db.get_conn()
        return c.execute("SELECT COUNT(*) FROM actors").fetchone()[0]


# ---------------------------------------------------------------- 作品列表
def upsert_videos(actor_slug, actor_name, videos) -> dict:
    """写入/更新某演员的作品，返回 ``{added, updated, total, total_actor}``。

    ``total_actor`` = 该演员缓存里的作品总数（用于对比网站标注的作品数）。
    """
    slug = str(actor_slug or "").strip()
    if not slug:
        return {"added": 0, "updated": 0, "total": 0, "total_actor": 0}
    rows = []
    for v in videos or []:
        vid = str(getattr(v, "vid", "") or "").strip()
        if not vid:
            continue
        rows.append((vid, slug, actor_name or getattr(v, "actor", "") or "",
                     getattr(v, "title", "") or "",
                     getattr(v, "cover_url", "") or "",
                     getattr(v, "duration", "") or ""))
    added = updated = 0
    with _lock:
        c = db.get_conn()
        now = _now()
        for vid, aslug, aname, title, cover, dur in rows:
            old = c.execute("SELECT title, duration, cover_url FROM videos WHERE vid=?",
                            (vid,)).fetchone()
            if old is None:
                c.execute(
                    "INSERT INTO videos (vid, actor_slug, actor_name, title, cover_url,"
                    " duration, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                    (vid, aslug, aname, title, cover, dur, now, now))
                added += 1
            else:
                if (old[0] or "") != title or (old[1] or "") != dur:
                    c.execute("UPDATE videos SET title=?, duration=?, actor_slug=?, "
                              "actor_name=?, last_seen=? WHERE vid=?",
                              (title, dur, aslug, aname, now, vid))
                    updated += 1
                else:
                    c.execute("UPDATE videos SET last_seen=? WHERE vid=?", (now, vid))
        c.execute("UPDATE actors SET synced_at=? WHERE slug=?", (now, slug))
        c.commit()
        total_actor = c.execute(
            "SELECT COUNT(*) FROM videos WHERE actor_slug=?", (slug,)).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    return {"added": added, "updated": updated, "total": total,
            "total_actor": total_actor}


def get_videos(actor_slug) -> list:
    """读取某演员的缓存作品（按入库顺序），返回 ``List[Video]``。"""
    with _lock:
        c = db.get_conn()
        rows = c.execute(
            "SELECT vid, actor_slug, actor_name, title, cover_url, duration "
            "FROM videos WHERE actor_slug=? ORDER BY rowid ASC",
            (str(actor_slug),)).fetchall()
    return [Video(vid=r[0], title=r[3] or "", cover_url=r[4] or "",
                  duration=r[5] or "", actor=r[2] or "") for r in rows]


def get_video_vids(actor_slug) -> set:
    """某演员缓存里已有的 vid 集合（增量比对用，避免取整表）。"""
    with _lock:
        c = db.get_conn()
        rows = c.execute("SELECT vid FROM videos WHERE actor_slug=?",
                         (str(actor_slug),)).fetchall()
    return set(r[0] for r in rows)


def video_count() -> int:
    with _lock:
        c = db.get_conn()
        return c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


# ---------------------------------------------------------------- 统计 / meta
def stats() -> dict:
    """缓存总览（供 GUI 状态条与「预热」按钮显示）。"""
    with _lock:
        c = db.get_conn()
        actors = c.execute("SELECT COUNT(*) FROM actors").fetchone()[0]
        videos = c.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        synced = c.execute(
            "SELECT COUNT(*) FROM actors WHERE synced_at IS NOT NULL "
            "AND synced_at <> ''").fetchone()[0]
        last = c.execute("SELECT v FROM meta WHERE k='last_cloud_sync'").fetchone()
    return {"actors": actors, "videos": videos, "synced_actors": synced,
            "last_cloud_sync": last[0] if last else ""}


def pending_actor_slugs(limit=0) -> list:
    """还没同步过作品的演员 slug 列表（预热时按这个顺序跑）。"""
    with _lock:
        c = db.get_conn()
        sql = ("SELECT slug FROM actors WHERE synced_at IS NULL OR synced_at='' "
               "ORDER BY count DESC, name COLLATE NOCASE ASC")
        if limit:
            sql += " LIMIT %d" % int(limit)
        rows = c.execute(sql).fetchall()
    return [r[0] for r in rows]


def set_meta(key, value):
    with _lock:
        c = db.get_conn()
        c.execute("INSERT INTO meta (k, v) VALUES (?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))
        c.commit()


def get_meta(key, default=""):
    with _lock:
        c = db.get_conn()
        row = c.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def clear_cache():
    """清空演员云/作品缓存（保留下载记录与已下载索引）。"""
    with _lock:
        c = db.get_conn()
        c.execute("DELETE FROM videos")
        c.execute("DELETE FROM actors")
        c.commit()
