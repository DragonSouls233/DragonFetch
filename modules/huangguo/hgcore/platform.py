# -*- coding: utf-8 -*-
"""短剧平台聚合层：多站统一入库 + 跨站去重。

设计目标（用户需求）：
  * 把多个短剧平台（黄果系 / 黄豆 / 91短剧 / …）的内容**扫进同一个库**；
  * **以黄果为主**，其它平台只补充黄果没有的（独有内容）；
  * 同一个剧在多个站都出现时只保留一条记录（记来源），避免重复下载；
  * **新增平台要快**：加一条站点定义 + 一个解析器即可，UI 与本文件不用改。

去重口径：以**归一化标题**为跨站主键。
  - 归一化 = NFKC（全角→半角）+ 去空白/标点 + 小写。
  - 不做「去第N季」这类激进合并：「母子同欢第二季」和「母子同欢」是不同剧，
    合并会导致漏下或错下（已踩坑思路）。
  - 同一站内的重复 id 天然被 (site_id, did) 主键合并。

主站（primary）集合 = 黄果系三个站（huangguoai / huangguome / huangguoac）。
主站先占位 title_key，其它站遇到同 key → 标记 dup，不再进入「独有」列表。
非主站之间同名 → 先扫到的占位，后来的标 dup（记录 dup_of）。

数据表 platform_dramas 与剧库 episodes 在同一个 SQLite 文件里，
所以「是否已下载」可以直接 JOIN 出来给界面用。
"""

import re
import sqlite3
import time
import unicodedata
from typing import Dict, Iterable, List, Optional, Tuple

from . import config, sites
from .library_db import _tx  # 复用剧库的连接与事务（同一把锁、同一文件）

# 主站：内容以它们为准，其它站只做补充
PRIMARY_SITE_IDS = ("huangguoai", "huangguome", "huangguoac")

# 归一化时要剔除的标点/空白（含全角空格）
_PUNCT = re.compile(
    r"[\s　·・•\-—–_~～,，。!！?？:：;；\"'“”‘’`·()（）\[\]【】《》/\\|]+"
)

_TABLE = """
CREATE TABLE IF NOT EXISTS platform_dramas (
    site_id     TEXT NOT NULL,
    did         TEXT NOT NULL,
    title       TEXT NOT NULL,
    title_key   TEXT NOT NULL,
    ep_count    INTEGER DEFAULT 0,
    score       TEXT DEFAULT '',
    cover_url   TEXT DEFAULT '',
    detail_url  TEXT DEFAULT '',
    source      TEXT DEFAULT '',     -- 列表页分类（AI短剧 / 成人漫剧 …）
    is_unique   INTEGER DEFAULT 0,    -- 相对主站是否独有（1=独有）
    dup_of      TEXT DEFAULT '',      -- 与之重复的站点 id
    first_seen  INTEGER DEFAULT 0,
    updated_at  INTEGER DEFAULT 0,
    PRIMARY KEY (site_id, did)
);
CREATE INDEX IF NOT EXISTS idx_plat_key ON platform_dramas(title_key);
CREATE INDEX IF NOT EXISTS idx_plat_unique ON platform_dramas(is_unique);
CREATE INDEX IF NOT EXISTS idx_plat_site ON platform_dramas(site_id);
"""


# ---------------------------------------------------------------- 基础工具

def normalize_title(title: str) -> str:
    """跨站比对用的标题键。"""
    t = unicodedata.normalize("NFKC", str(title or "")).strip().lower()
    return _PUNCT.sub("", t)


def init_db() -> None:
    """建表（幂等）。

    聚合表与剧库表在同一个 SQLite 文件里，所以先让剧库把 episodes/dramas 建好
    （否则 list_catalog 的 JOIN 会报 no such table）。两个 init 各自拿同一把锁，
    但**顺序调用不嵌套**，不会死锁。
    """
    try:
        from .library_db import init_db as _lib_init
        _lib_init()
    except Exception:                            # noqa: BLE001
        pass
    with _tx() as c:
        c.executescript(_TABLE)


def is_primary(site_id: str) -> bool:
    return site_id in PRIMARY_SITE_IDS


def platform_sites(include_disabled: bool = False) -> List[Dict]:
    """参与聚合的短剧站列表（默认只给 enabled 的）。"""
    out = []
    for s in sites.all_sites():
        if not include_disabled and not s.get("enabled", True):
            continue
        out.append(s)
    return out


def site_label(site_id: str) -> str:
    s = sites.get_site(site_id) or {}
    return s.get("name") or site_id


# ---------------------------------------------------------------- 写入

def upsert_drama(site_id: str, drama, detail_url: str = "") -> None:
    """把一条剧目写进聚合库（同站同 id 覆盖更新）。"""
    key = normalize_title(getattr(drama, "title", ""))
    now = int(time.time())
    ep_count = int(getattr(drama, "episode_count", 0) or 0)
    with _tx() as c:
        c.execute(
            """
            INSERT INTO platform_dramas
                (site_id, did, title, title_key, ep_count, score, cover_url,
                 detail_url, source, first_seen, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(site_id, did) DO UPDATE SET
                title=excluded.title,
                title_key=excluded.title_key,
                ep_count=MAX(platform_dramas.ep_count, excluded.ep_count),
                score=excluded.score OR platform_dramas.score,
                cover_url=excluded.cover_url OR platform_dramas.cover_url,
                detail_url=excluded.detail_url OR platform_dramas.detail_url,
                source=excluded.source OR platform_dramas.source,
                updated_at=excluded.updated_at
            """,
            (site_id, str(getattr(drama, "did", "")), getattr(drama, "title", "") or "",
             key, ep_count, getattr(drama, "score", "") or "",
             getattr(drama, "cover", "") or "", detail_url,
             getattr(drama, "category", "") or "", now, now),
        )


def refresh_uniqueness() -> Tuple[int, int]:
    """按主站优先重算 is_unique / dup_of。

    口径：**一个标题只在一个站出现**才算「独有」（is_unique=1）；
    只要在两个及以上站出现，**所有**相关记录都标 is_unique=0，并记下
    与自己重复的那个站（dup_of）。

    为什么不把主站那条留成独有：主站的剧本来就在黄果剧库里，
    「只提取独有的」指的是**别的平台有、黄果没有**的那些片；
    若主站那条也算独有，筛选出来的就是黄果的老片，违背需求。

    返回 (独有条数, 重复条数)。
    """
    with _tx() as c:
        rows = c.execute(
            "SELECT site_id, did, title_key FROM platform_dramas").fetchall()

        groups: Dict[str, List[Tuple[str, str]]] = {}
        for r in rows:
            groups.setdefault(r["title_key"], []).append((r["site_id"], r["did"]))

        dup_pairs: List[Tuple[str, str, str]] = []
        for _key, members in groups.items():
            # 按站分组：同一个站里出现多个同名（不同剧 id 撞了同名）**不算**
            # 跨站重复，跨站才判重。
            by_site: Dict[str, List[str]] = {}
            for site_id, did in members:
                by_site.setdefault(site_id, []).append(did)
            if len(by_site) <= 1:
                continue
            # 代表站：主站优先（黄果系），否则取第一个
            rep_site = sorted(by_site.keys(),
                              key=lambda s: (0 if is_primary(s) else 1))[0]
            others = [s for s in by_site if s != rep_site]
            for site_id, dids in by_site.items():
                other = rep_site if site_id != rep_site else others[0]
                for did in dids:
                    dup_pairs.append((site_id, did, other))

        c.execute("UPDATE platform_dramas SET is_unique=1, dup_of=''")
        for site_id, did, other in dup_pairs:
            c.execute(
                "UPDATE platform_dramas SET is_unique=0, dup_of=? "
                "WHERE site_id=? AND did=?", (other, site_id, did))

        total = c.execute("SELECT COUNT(*) FROM platform_dramas").fetchone()[0]
    return max(0, total - len(dup_pairs)), len(dup_pairs)


# ---------------------------------------------------------------- 扫描

def scan_site(site_id: str, max_pages: int = 0, on_progress=None) -> int:
    """扫描一个站的所有分类列表页，把剧目写进聚合库。

    返回新增/更新的条数。单个站失败不影响其它站（异常向上抛给调用方处理）。
    """
    from . import site_api

    site = sites.get_site(site_id)
    if site is None:
        raise KeyError(f"未知站点：{site_id}")
    cats = site.get("categories") or []
    if not cats:
        return 0

    mod = site_api._backend_for(site_id)      # noqa: SLF001 - 同包内部调用
    count = 0
    total_jobs = len(cats)
    for idx, (_name, path) in enumerate(cats, 1):
        url = (site.get("base_url") or "").rstrip("/") + path
        try:
            items = mod.list_category_all(url, max_pages=max_pages)
        except TypeError:
            items = mod.list_category(url)
        except Exception as exc:                # noqa: BLE001 - 单个分类失败跳过
            if on_progress:
                on_progress(idx, total_jobs, f"{_name} 读取失败：{exc}"[:80])
            continue
        for d in items:
            detail_url = _detail_url_of(site, d)
            upsert_drama(site_id, d, detail_url)
            count += 1
        if on_progress:
            on_progress(idx, total_jobs, f"{_name} {len(items)} 部")
    return count


def _detail_url_of(site: Dict, drama) -> str:
    base = (site.get("base_url") or "").rstrip("/")
    tpl = config.url_tpl(site, "detail")
    try:
        return base + tpl.format(did=getattr(drama, "did", ""))
    except Exception:                            # noqa: BLE001
        return base


def scan_all(max_pages: int = 0, on_progress=None,
             include_primary: bool = True, workers: int = 6) -> Dict[str, int]:
    """扫描所有短剧站（**并发**）。

    include_primary=True（默认）连主站一起扫 —— 判重必须知道主库**全量**
    有什么，只靠剧库里「用户浏览过的那些」会把重合率算低（主站有 28 页，
    剧库通常只有几十部）。全扫之后重合率才准。

    并发说明：http_client 的 Session 是线程本地的（每线程一个连接池），
    所以多线程抓是安全的；sqlite 写入走同一把锁，串行落库。

    返回 {site_id: 条数}（-1 表示该站失败）。
    """
    from concurrent.futures import ThreadPoolExecutor

    sites_to_scan = [s for s in platform_sites()
                     if include_primary or not is_primary(s["id"])]

    def _one(site: Dict) -> Tuple[str, int]:
        try:
            return site["id"], scan_site(site["id"], max_pages=max_pages,
                                         on_progress=on_progress)
        except Exception as exc:                # noqa: BLE001 - 单站失败继续
            if on_progress:
                on_progress(0, 0, f"{site.get('name')} 扫描失败：{exc}"[:100])
            return site["id"], -1

    out: Dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(sites_to_scan)))) as ex:
        for sid, n in ex.map(_one, sites_to_scan):
            out[sid] = n
    refresh_uniqueness()
    return out


def ensure_primary_index() -> int:
    """把主站（黄果系）剧库里已有的剧写进聚合表。

    主站的剧库由黄果模块自己维护，这里只做一次「索引」，用于跨站判重。
    """
    from . import library_db

    n = 0
    for sid in PRIMARY_SITE_IDS:
        try:
            rows = library_db.list_dramas(sid, limit=100000)
        except Exception:                        # noqa: BLE001
            continue
        for r in rows:
            did = r.get("did")
            title = r.get("title") or ""
            if not title:
                continue
            upsert_drama(sid, _SimpleDrama(did, title, r), r.get("detail_url") or "")
            n += 1
    refresh_uniqueness()
    return n


class _SimpleDrama:
    """把剧库里的 dict 包成 Drama 形状（聚合层只用到这几个字段）。"""

    def __init__(self, did, title, row: Dict):
        self.did = did
        self.title = title
        self.cover = row.get("cover_url") or ""
        self.score = ""
        self.episode_count = int(row.get("episode_count") or 0)
        self.category = row.get("category") or ""


# ---------------------------------------------------------------- 查询

def list_catalog(site_id: str = "", only_unique: bool = False,
                 keyword: str = "") -> List[Dict]:
    """聚合库列表（带已下载集数）。

    site_id 为空表示全部站；only_unique=True 只看「主站没有的独有剧」。
    """
    init_db()
    sql = (
        "SELECT p.*, "
        "  (SELECT COUNT(*) FROM episodes e "
        "    WHERE e.site_id=p.site_id AND e.did=p.did AND e.status='done') AS done_eps "
        "FROM platform_dramas p WHERE 1=1"
    )
    params: List = []
    if site_id:
        sql += " AND p.site_id=?"
        params.append(site_id)
    if only_unique:
        sql += " AND p.is_unique=1"
    if keyword:
        sql += " AND p.title LIKE ?"
        params.append(f"%{keyword}%")
    sql += " ORDER BY p.updated_at DESC LIMIT 20000"
    with _tx() as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def lookup_by_title(title: str) -> List[Dict]:
    """按标题查各站记录（详情页/下载前做对照用）。"""
    init_db()
    key = normalize_title(title)
    with _tx() as c:
        rows = c.execute(
            "SELECT p.*, "
            "  (SELECT COUNT(*) FROM episodes e "
            "    WHERE e.site_id=p.site_id AND e.did=p.did AND e.status='done') AS done_eps "
            "FROM platform_dramas p WHERE p.title_key=? ORDER BY p.site_id",
            (key,)).fetchall()
    return [dict(r) for r in rows]


def stats() -> Dict[str, int]:
    """各站条目数与独有数。"""
    init_db()
    with _tx() as c:
        rows = c.execute(
            "SELECT site_id, COUNT(*) n, SUM(is_unique) u FROM platform_dramas "
            "GROUP BY site_id").fetchall()
    return {r["site_id"]: {"total": r["n"], "unique": r["u"] or 0} for r in rows}


# ---------------------------------------------------------------- 重合度

# 重合率超过这个比例就认为「和主库基本重复」，不建议纳入下载范围。
# 依据用户口径：「一致的太多就不加入，如果不多还是需要加入进来」。
OVERLAP_SKIP_THRESHOLD = 0.80


def overlap_report() -> List[Dict]:
    """各补充站与**主库（黄果系）**的重合度报告。

    重合率 = 该站剧目中，标题键也出现在主库里的比例。
    返回按重合率从高到低排序的列表：
        {site_id, name, total, dup, overlap_rate, unique, recommend}
    recommend=False 表示重合过多，建议不纳入。
    """
    init_db()
    with _tx() as c:
        rows = c.execute(
            "SELECT site_id, title_key, is_unique FROM platform_dramas").fetchall()

    primary_keys = set()
    per_site: Dict[str, List[str]] = {}
    for r in rows:
        sid = r["site_id"]
        per_site.setdefault(sid, []).append(r["title_key"])
        if is_primary(sid):
            primary_keys.add(r["title_key"])

    out: List[Dict] = []
    for sid, keys in per_site.items():
        if is_primary(sid):
            continue
        uniq_keys = set(keys)
        dup = len(uniq_keys & primary_keys)
        total = len(uniq_keys)
        rate = (dup / total) if total else 0.0
        out.append({
            "site_id": sid,
            "name": site_label(sid),
            "total": total,
            "dup": dup,
            "overlap_rate": rate,
            "unique": total - dup,
            "recommend": rate < OVERLAP_SKIP_THRESHOLD,
        })
    out.sort(key=lambda x: -x["overlap_rate"])
    return out


def refresh_all() -> Dict[str, int]:
    """扫描全部站 + 重算去重/重合度。

    返回 {site_id: 收录条数}。
    """
    res = scan_all(max_pages=0)
    ensure_primary_index()
    refresh_uniqueness()
    return res
