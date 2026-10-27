# -*- coding: utf-8 -*-
"""统一库访问层：把四个模块的本地数据（SQLite / JSON）收敛成同一套读写接口。

用途：库管理页要能浏览、搜索、改一格、删一行、导出 CSV、备份、清空，
而不关心底层是 sqlite 还是 json。**只做数据层，不碰 UI。**

数据位置（遵守「DATA 全在根目录」的约束）：
  DATA/tangxin/tangxin.db          SQLite   糖心演员 / 下载记录
  DATA/huangguo/hg.db              SQLite   黄果剧库 / 集 / 下载记录 / 同步清单
  DATA/jiuse/jiuse.db              SQLite   九色线路检测 / 下载记录
  DATA/madouqu/{models,videos}.json           麻豆采集结果
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from . import paths


# ------------------------------------------------------------------ 描述
@dataclass
class Store:
    sid: str
    label: str
    kind: str                      # "sqlite" | "json"
    path: Path
    hint: str = ""
    tables: List[str] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def size_text(self) -> str:
        try:
            n = self.path.stat().st_size
        except OSError:
            return "-"
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit != "GB" else f"{n / 1024:.1f} GB"
            n /= 1024
        return "-"


def _sqlite_tables(p: Path) -> List[str]:
    if not p.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=3)
        try:
            return [r[0] for r in con.execute(
                "select name from sqlite_master where type='table' "
                "and name not like 'sqlite_%' order by name")]
        finally:
            con.close()
    except Exception:
        return []


def _json_keys(p: Path, limit: int = 4000) -> List[str]:
    """JSON 库：返回『集合』名（文件里是 dict 的话取其键，否则整体视为一条）。"""
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, dict):
        keys = [k for k in raw.keys() if isinstance(raw[k], dict)]
        return keys[:limit]
    return []


def stores() -> List[Store]:
    out: List[Store] = []

    tx = paths.DATA / "tangxin" / "tangxin.db"
    out.append(Store("tangxin", "糖心 · 演员/记录", "sqlite", tx,
                     "演员云缓存、下载记录与本地已有索引", _sqlite_tables(tx)))

    hg = paths.DATA / "huangguo" / "hg.db"
    out.append(Store("huangguo", "黄果 · 剧库", "sqlite", hg,
                     "剧集、集状态、下载记录、同步清单", _sqlite_tables(hg)))

    ji = paths.DATA / "jiuse" / "jiuse.db"
    if ji.exists():
        out.append(Store("jiuse", "九色 · 线路/下载", "sqlite", ji,
                         "线路检测历史与下载记录", _sqlite_tables(ji)))

    mq_mod = paths.DATA / "madouqu" / "models.json"
    if mq_mod.exists():
        out.append(Store("madouqu", "麻豆 · 采集库", "json", mq_mod,
                         "模特与磁力结果", ["models", "videos"]))
    return out


def store(sid: str) -> Store | None:
    for s in stores():
        if s.sid == sid:
            return s
    return None


# ------------------------------------------------------------------ JSON 适配
def _json_file(sid: str, table: str) -> Path:
    """JSON 库的文件：``table`` 决定读哪个文件。"""
    base = paths.DATA / "madouqu"
    mapping = {
        ("madouqu", "models"): "models.json",
        ("madouqu", "videos"): "videos.json",
    }
    return base / mapping.get((sid, table), f"{table}.json")


def _json_load(sid: str, table: str) -> Dict[str, dict]:
    p = _json_file(sid, table)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _json_save(sid: str, table: str, data: Dict[str, dict]) -> bool:
    p = _json_file(sid, table)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


# ------------------------------------------------------------------ 统一查询
def columns(sid: str, table: str) -> List[str]:
    s = store(sid)
    if not s:
        return []
    if s.kind == "sqlite":
        try:
            con = sqlite3.connect(f"file:{s.path}?mode=ro", uri=True, timeout=3)
            try:
                return [r[1] for r in con.execute(f'pragma table_info("{table}")')]
            finally:
                con.close()
        except Exception:
            return []
    data = _json_load(sid, table)
    keys: List[str] = []
    for v in list(data.values())[:200]:
        if isinstance(v, dict):
            for k in v:
                if k not in keys:
                    keys.append(k)
    return keys


def rows(sid: str, table: str, search: str = "", limit: int = 300,
         offset: int = 0, order: str = "") -> Tuple[List[List[Any]], int]:
    """返回 (行数据[含首列 rowkey], 总行数)。"""
    s = store(sid)
    if not s:
        return [], 0
    kw = (search or "").strip()

    if s.kind == "sqlite":
        cols = columns(sid, table)
        if not cols:
            return [], 0
        try:
            con = sqlite3.connect(f"file:{s.path}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                where, args = "", []
                if kw:
                    where = " where " + " or ".join(
                        f'cast("{c}" as text) like ?' for c in cols)
                    args = [f"%{kw}%"] * len(cols)
                total = con.execute(
                    f'select count(*) from "{table}"{where}', args).fetchone()[0]
                order_sql = f' order by "{order}" desc' if order in cols else ""
                cur = con.execute(
                    f'select rowid as __rowid__, * from "{table}"{where}'
                    f'{order_sql} limit ? offset ?', (*args, int(limit), int(offset)))
                out = [[r["__rowid__"]] + [r[c] for c in cols] for r in cur.fetchall()]
                return out, int(total)
            finally:
                con.close()
        except Exception:
            return [], 0

    data = _json_load(sid, table)
    items = list(data.items())
    if kw:
        items = [(k, v) for k, v in items
                 if kw.lower() in k.lower()
                 or kw.lower() in json.dumps(v, ensure_ascii=False).lower()]
    items.sort(key=lambda kv: kv[0], reverse=(order == "__desc__"))
    cols = columns(sid, table)
    page = items[int(offset): int(offset) + int(limit)]
    out = [[k] + [v.get(c, "") if isinstance(v, dict) else "" for c in cols]
           for k, v in page]
    return out, len(items)


def update_cell(sid: str, table: str, rowkey: Any, column: str, value: Any) -> bool:
    s = store(sid)
    if not s:
        return False
    if s.kind == "sqlite":
        try:
            con = sqlite3.connect(str(s.path), timeout=5)
            try:
                con.execute(f'update "{table}" set "{column}"=? where rowid=?',
                            (value, rowkey))
                con.commit()
                return True
            finally:
                con.close()
        except Exception:
            return False
    data = _json_load(sid, table)
    if str(rowkey) not in data:
        return False
    data[str(rowkey)][column] = value
    return _json_save(sid, table, data)


def delete_row(sid: str, table: str, rowkey: Any) -> bool:
    s = store(sid)
    if not s:
        return False
    if s.kind == "sqlite":
        try:
            con = sqlite3.connect(str(s.path), timeout=5)
            try:
                con.execute(f'delete from "{table}" where rowid=?', (rowkey,))
                con.commit()
                return True
            finally:
                con.close()
        except Exception:
            return False
    data = _json_load(sid, table)
    if str(rowkey) not in data:
        return False
    data.pop(str(rowkey), None)
    return _json_save(sid, table, data)


def export_csv(sid: str, table: str, out_path: str) -> int:
    cols = columns(sid, table)
    all_rows, total = rows(sid, table, limit=200000)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["__rowid__"] + cols)
        w.writerows(all_rows)
    return total


def backup(sid: str) -> str:
    s = store(sid)
    if not s or not s.path.exists():
        return ""
    bdir = paths.DATA / "app" / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    if s.kind == "sqlite":
        dst = bdir / f"{s.sid.replace(':', '_')}_{stamp}.db"
        try:
            src = sqlite3.connect(f"file:{s.path}?mode=ro", uri=True, timeout=5)
            out = sqlite3.connect(str(dst))
            with out:
                src.backup(out)
            out.close()
            src.close()
            return str(dst)
        except Exception:
            return ""
    dst = bdir / f"{s.sid.replace(':', '_')}_{stamp}.json"
    try:
        shutil.copyfile(s.path, dst)
        return str(dst)
    except Exception:
        return ""


def clear_store(sid: str) -> int:
    """清空某个库的全部数据（保留表结构）。返回删除行数。"""
    s = store(sid)
    if not s:
        return 0
    n = 0
    if s.kind == "sqlite":
        try:
            con = sqlite3.connect(str(s.path), timeout=8)
            try:
                for t in _sqlite_tables(s.path):
                    cur = con.execute(f'delete from "{t}"')
                    n += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                con.commit()
            finally:
                con.close()
        except Exception:
            return 0
        return n
    for table in s.tables:
        data = _json_load(sid, table)
        n += len(data)
        _json_save(sid, table, {})
    return n
