# -*- coding: utf-8 -*-
"""单站点模块基座：一个域名 = 一个模块 = 一个页面 = 一个内容库。

设计约束
--------
* **每个站点完全独立**：站点定义（域名/主页/内容类型/列表页特征）写在各自的
  ``modules/sites/<key>/__init__.py`` 里，互不引用；删掉一个站点不影响其他站点。
* **共享的只有解析引擎**（``modules/multisite``）：它等价于「yt-dlp 那样的公共
  适配层」，负责 "把页面变成直链" 与 "把列表页展开成条目"，不知道有哪些站点。
* **数据各归各**：每个站点一个 SQLite（``DATA/sites/<key>/sites.db``），
  存本站的采集记录与下载记录，不与其他站点混在一起。

⚠️ 这一层不能 import ``launcher``（后端必须能脱离 GUI 运行），因此数据根目录
从 ``DF_DATA_ROOT`` 环境变量取；取不到就按 ``<项目根>/DATA`` 推断。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------- #
# 路径：后端自解析，不依赖前端
# ---------------------------------------------------------------------- #
def _data_root() -> Path:
    env = (os.environ.get("DF_DATA_ROOT") or "").strip()
    if env:
        return Path(env).expanduser()
    # modules/sites/base.py -> modules/sites -> modules -> <root>
    return Path(__file__).resolve().parents[2] / "DATA"


def _engine():
    """惰性导入共享解析引擎（避免模块导入期就拉起 requests / curl_cffi）。"""
    from multisite.engine import registry, service  # noqa: WPS433

    return registry, service


def _service(proxy: Optional[str] = None):
    _registry, service = _engine()
    return service.get_site_service(proxy=proxy or None)


def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------------- #
# 站点定义
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class SiteModule:
    """一个站点的全部静态定义 + 本站数据/解析入口。"""

    key: str
    label: str
    domains: Tuple[str, ...]
    homepage: str
    content: str                       # 内容类型说明（综合片库 / 需站点包解析 …）
    list_hints: Tuple[str, ...] = ()   # 形如列表页的路径片段
    needs_site_api: bool = False       # yt-dlp 无 extractor，必须走站点包
    note: str = ""

    # ------------------------------------------------------------ 元信息
    @property
    def domain(self) -> str:
        return self.domains[0] if self.domains else ""

    def matches(self, url: str) -> bool:
        """该 URL 是否属于本模块的域名（后缀匹配，避免子串误判）。"""
        try:
            from urllib.parse import urlparse

            host = (urlparse(url or "").hostname or "").lower()
        except Exception:
            return False
        if not host:
            return False
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def spec(self):
        """对应的引擎 SiteSpec（同一 key 从 registry 取，保证行为一致）。"""
        registry, _service_mod = _engine()
        spec = registry.get_site(self.key)
        if spec is not None:
            return spec
        return registry.SiteSpec(
            key=self.key, label=self.label, domains=tuple(self.domains),
            ytdlp=not self.needs_site_api, eaf_module=None,
            list_paths=tuple(self.list_hints), homepage_url=self.homepage,
        )

    @property
    def data_dir(self) -> Path:
        p = _data_root() / "sites" / self.key
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def download_dir(self) -> Path:
        p = self.data_dir / "downloads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ------------------------------------------------------------ 本站库
    def db(self) -> "SiteDB":
        return SiteDB(self.key, self.data_dir)

    # ------------------------------------------------------------ 解析能力
    def list_videos(self, url: str, pages: int = 2, proxy: Optional[str] = None,
                    limit: int = 0) -> List[Dict[str, Any]]:
        """展开本站的列表页（模特/频道/分类/搜索页）。"""
        svc = _service(proxy)
        items = asyncio.run(svc.list_videos(url, pages=pages, limit=limit,
                                            proxy=proxy))
        return [i.to_dict() for i in items]

    def search(self, query: str, pages: int = 1, proxy: Optional[str] = None,
               limit: int = 0) -> List[Dict[str, Any]]:
        """在本站内按关键词搜索。"""
        svc = _service(proxy)
        items = asyncio.run(svc.search(self.key, query, pages=pages, limit=limit,
                                       proxy=proxy))
        return [i.to_dict() for i in items]

    def resolve(self, url: str, proxy: Optional[str] = None) -> Dict[str, Any]:
        """解析单个视频页：拿到直链 + 能拿到的元信息（站点包链路）。"""
        svc = _service(proxy)
        rv = asyncio.run(svc.resolve(url, proxy=proxy))
        return rv.to_dict() if rv is not None else {}


# ---------------------------------------------------------------------- #
# 单站点内容库
# ---------------------------------------------------------------------- #
_SCHEMA = """
create table if not exists records (
    url text primary key,
    title text, duration text, author text, views text,
    publish_date text, categories text, direct text,
    source text, created_at real
);
create index if not exists idx_records_created on records(created_at);

create table if not exists downloads (
    id integer primary key autoincrement,
    url text, title text, path text, size integer,
    status text, error text, started_at real, finished_at real
);
"""


@dataclass
class SiteDB:
    """本站采集记录 + 下载记录（独立 SQLite，一个站点一个库文件）。"""

    key: str
    db_dir: Path
    _path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.db_dir = Path(self.db_dir)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.db_dir / "sites.db"
        self.init()

    # ------------------------------------------------------------ 基础
    @property
    def path(self) -> Path:
        return self._path

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self._path), timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def init(self) -> None:
        try:
            with self._con() as con:
                con.executescript(_SCHEMA)
        except Exception:
            pass

    # ------------------------------------------------------------ 采集记录
    def save_records(self, items: List[Dict[str, Any]], source: str = "") -> int:
        """把列表/搜索结果写进本站库（按 url 去重，保留最新字段）。"""
        now = time.time()
        n = 0
        try:
            with self._con() as con:
                for it in items or []:
                    url = _clean(it.get("url"))
                    if not url:
                        continue
                    cats = it.get("categories") or []
                    if isinstance(cats, (list, tuple)):
                        cats = ", ".join(str(c) for c in cats if c)
                    direct = _clean(it.get("direct"))
                    if not direct:
                        urls = it.get("direct_urls") or []
                        direct = _clean(urls[-1]) if urls else ""
                    con.execute(
                        "insert into records(url,title,duration,author,views,"
                        "publish_date,categories,direct,source,created_at) "
                        "values(?,?,?,?,?,?,?,?,?,?) "
                        "on conflict(url) do update set "
                        "title=coalesce(nullif(excluded.title,''),title),"
                        "duration=coalesce(nullif(excluded.duration,''),duration),"
                        "author=coalesce(nullif(excluded.author,''),author),"
                        "views=coalesce(nullif(excluded.views,''),views),"
                        "publish_date=coalesce(nullif(excluded.publish_date,''),publish_date),"
                        "categories=coalesce(nullif(excluded.categories,''),categories),"
                        "direct=coalesce(nullif(excluded.direct,''),direct),"
                        "source=coalesce(nullif(excluded.source,''),source),"
                        "created_at=excluded.created_at",
                        (url, _clean(it.get("title")), _clean(it.get("duration")),
                         _clean(it.get("author")), _clean(it.get("views")),
                         _clean(it.get("publish_date") or it.get("date")),
                         str(cats), direct,
                         source or _clean(it.get("source")) or self.key, now))
                    n += 1
        except Exception:
            return 0
        return n

    def records(self, limit: int = 300, search: str = "") -> List[Dict[str, Any]]:
        kw = (search or "").strip()
        try:
            with self._con() as con:
                if kw:
                    cur = con.execute(
                        "select * from records where title like ? or url like ? "
                        "or author like ? order by created_at desc limit ?",
                        (f"%{kw}%", f"%{kw}%", f"%{kw}%", int(limit)))
                else:
                    cur = con.execute(
                        "select * from records order by created_at desc limit ?",
                        (int(limit),))
                return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def count_records(self) -> int:
        try:
            with self._con() as con:
                return int(con.execute("select count(*) from records").fetchone()[0])
        except Exception:
            return 0

    def clear_records(self) -> int:
        try:
            with self._con() as con:
                cur = con.execute("delete from records")
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except Exception:
            return 0

    # ------------------------------------------------------------ 下载记录
    def start_download(self, url: str, title: str = "") -> int:
        try:
            with self._con() as con:
                cur = con.execute(
                    "insert into downloads(url,title,status,started_at) "
                    "values(?,?,?,?)", (url, title, "running", time.time()))
                return int(cur.lastrowid or 0)
        except Exception:
            return 0

    def finish_download(self, dl_id: int, ok: bool, path: str = "", size: int = 0,
                        error: str = "") -> None:
        if not dl_id:
            return
        try:
            with self._con() as con:
                con.execute(
                    "update downloads set status=?, path=?, size=?, error=?, "
                    "finished_at=? where id=?",
                    ("done" if ok else "failed", path, int(size or 0),
                     error or "", time.time(), int(dl_id)))
        except Exception:
            pass

    def recent_downloads(self, limit: int = 200) -> List[Dict[str, Any]]:
        try:
            with self._con() as con:
                cur = con.execute(
                    "select * from downloads order by started_at desc limit ?",
                    (int(limit),))
                return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def stats(self) -> Dict[str, int]:
        out = {"records": 0, "downloads": 0, "done": 0, "failed": 0}
        try:
            with self._con() as con:
                out["records"] = int(con.execute(
                    "select count(*) from records").fetchone()[0])
                out["downloads"] = int(con.execute(
                    "select count(*) from downloads").fetchone()[0])
                out["done"] = int(con.execute(
                    "select count(*) from downloads where status='done'").fetchone()[0])
                out["failed"] = int(con.execute(
                    "select count(*) from downloads where status='failed'").fetchone()[0])
        except Exception:
            pass
        return out
