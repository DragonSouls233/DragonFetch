# -*- coding: utf-8 -*-
"""外盘同步：把下载目录里「近期更新」的剧集增量镜像到其它盘符（下载盘 / 服务器盘）。

增量口径（2026-10-21 改版）：以**项目内 DB 的「已同步文件清单」**为准，
而不是「文件 mtime > 上次同步时间」。理由与语义：

- 「上一轮同步之后**下载**的」= 清单里没有的文件 → 必拷。
  旧口径用 mtime 比较，而 mtime 会因为「重命名/改写/复制」而变动，
  也无法区分「这轮同步进行中才下载完成的文件」——那种文件极易被永久漏拷。
- 清单里有、但目标端已丢失或大小变了 → 补拷（自愈）。
- 清单里有且目标端一致 → 跳过。所以**外盘被清空/转走后，下次仍只补新增**，
  不会退化成全量重来（这正是移动硬盘的使用场景）。
- 目标路径变了（换盘符/换目录）→ 视为新盘 → 该盘无清单 → 全量（不会漏拷）。

同步记录（会话 + 文件明细）全部写在**项目内**的 DATA/hg.db：
- sync_sessions        —— 每轮会话摘要
- sync_session_files   —— 每轮拷贝的文件明细（清单本体）
- sync_files           —— 每个目标盘「已同步文件」的当前状态（增量基准）
不再往外盘根目录写「同步清单_*.txt」（避免污染移动硬盘、避免依赖盘上的文件）。

设计要点：
- 源是 config.DOWNLOAD_DIR（J:\\AI剧），内含各站点子目录与 换脸/魔改 等分类目录；
  镜像时整棵树按相对路径原样复制，自动覆盖所有站点/分类，无需逐个处理。
- 不同步临时文件（.part/.ytdl/.tmp）与隐藏文件；目标已存在且大小一致则跳过（幂等）。
- 若某目标盘路径发生变化（用户在设置里改了盘符），自动退化为「全量」拷贝该盘，
  并把基准重置为当前路径，避免漏拷。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from hgcore import applog, config, library_db

_KV_PREFIX = "sync_last:"   # 后接角色名: download / server


# ---------------------------------------------------------------- 移动硬盘识别
# 下载盘常是「移动硬盘」，盘符不固定；这里在 Windows 上自动识别（优先 H:），
# 用户插上盘即可直接同步，无需每次手动填路径。

def _enum_drives() -> list:
    """返回 [(盘符如 'H:', 类型码), ...]。非 Windows 返回 []。"""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(256)
        n = k32.GetLogicalDriveStringsW(255, buf)
        if n == 0:
            return []
        # 注意：buf.value 会在第一个 \0 处截断（多盘符字符串是 "C:\\\0D:\\\0..."），
        # 且 c_wchar 数组没有 .raw 属性；必须用 wstring_at 按 n 取完整内容再切分，
        # 否则永远只看得到第一个盘符 → H: 识别失效。
        text = ctypes.wstring_at(buf, n)
        drives = [d for d in text.split("\x00") if d and len(d) >= 2]
        out = []
        for d in drives:
            try:
                typ = k32.GetDriveTypeW(d)
            except Exception:                      # noqa: BLE001
                typ = -1
            out.append((d[:2], typ))
        return out
    except Exception:                             # noqa: BLE001
        return []


def _pick_target(drives, prefer: str = "H",
                 subfolder: str | list = "AI剧") -> str:
    """纯逻辑：从盘符列表中选出同步目标根路径。

    1) 优先偏好盘符（如 H:），只要该盘可访问即用；
       盘下若已有 subfolder 候选目录（如 AI剧 / AI短剧）则优先返回已存在的那个；
    2) 否则在所有「可移动」盘里找一个已存在候选子目录的（即曾经同步过）；
    3) 都找不到返回 ""。
    注意：固定磁盘（如系统 C:）不会被误选，避免拷到系统盘。
    """
    cands = [subfolder] if isinstance(subfolder, str) else list(subfolder)
    pref = prefer.upper() + ":"
    for letter, _typ in drives:
        if letter.upper() == pref and os.path.isdir(letter + "\\"):
            for s in cands:
                if os.path.isdir(f"{letter}\\{s}"):
                    return f"{letter}\\{s}"
            return f"{letter}\\{cands[0]}"
    for letter, typ in drives:
        if typ == 2:            # DRIVE_REMOVABLE
            for s in cands:
                cand = f"{letter}\\{s}"
                if os.path.isdir(cand):
                    return cand
    return ""


def detect_removable_target(prefer: str = "H",
                            subfolder: str | list = ("AI剧", "AI短剧")) -> str:
    """自动识别移动硬盘作为同步目标根路径（如 'H:\\AI短剧'），识别不到返回 ''。"""
    return _pick_target(_enum_drives(), prefer=prefer, subfolder=subfolder)


# 不同步的文件（yt-dlp 临时文件、系统隐藏文件等）
_EXCLUDE_SUFFIXES = (".part", ".ytdl", ".tmp", ".crdownload")
_EXCLUDE_NAMES = (".DS_Store", "Thumbs.db")


@dataclass
class TargetResult:
    """单个目标盘同步结果。"""

    role: str                                 # "download" / "server"
    root: str
    full: bool = False
    copied: int = 0
    copied_bytes: int = 0
    skipped: int = 0
    errors: int = 0
    error_samples: list = field(default_factory=list)
    tag: str | None = None                    # 范围说明，如「自 2026-09-15 起」
    copied_files: list = field(default_factory=list)   # [(rel_path, size_bytes), ...]
    manifest_path: str = ""                   # 清单位置（现在记录在项目 DB 内，为空）
    session_id: int = 0                       # 项目 DB 里的同步会话 id
    new_files: int = 0                        # 「上一轮同步后新下载」的个数
    repaired: int = 0                        # 目标端丢失/损坏、被补拷的个数
    synced_rows: list = field(default_factory=list)   # [(rel_posix, size, mtime)]

    @property
    def label(self) -> str:
        return {"download": "下载盘", "server": "服务器盘"}.get(self.role, self.role)

    def summary_line(self) -> str:
        mb = self.copied_bytes / (1024 * 1024)
        mode = self.tag or ("全量" if self.full else "增量")
        extra = ""
        if self.new_files:
            extra = f"，其中新增 {self.new_files}"
        if self.repaired:
            extra += f"，补拷 {self.repaired}"
        return (f"[{self.label}] {self.root}（{mode}）："
                f"拷贝 {self.copied} 个文件 ({mb:.1f} MB){extra}，"
                f"跳过 {self.skipped}，失败 {self.errors}")


def _last_sync(role: str, path: str):
    """返回 (since_ts, baseline_path)。baseline_path 与当前 path 不一致则视为全量。"""
    raw = library_db.get_kv(_KV_PREFIX + role)
    if not raw:
        return 0.0, path
    try:
        data = json.loads(raw)
    except Exception:                       # noqa: BLE001
        return 0.0, path
    if data.get("path") != path:
        return 0.0, path                   # 路径变了 → 全量
    return float(data.get("ts", 0) or 0), path


def _save_last_sync(role: str, path: str, ts: float) -> None:
    library_db.set_kv(_KV_PREFIX + role, json.dumps(
        {"path": path, "ts": ts}, ensure_ascii=False))


def reset_baseline(role: str | None = None,
                   target_path: str | None = None) -> None:
    """重置同步基准（下次同步视为首次 → 全量）。

    role 为 None 时重置全部角色。target_path 给定时只重置该盘；
    否则重置该角色下所有盘（通过项目 DB 的已同步文件清单实现）。
    """
    roles = [role] if role else ["download", "server"]
    for r in roles:
        library_db.set_kv(_KV_PREFIX + r, "")
        try:
            if target_path:
                library_db.forget_sync_files(r, target_path)
            else:
                # 清掉该角色下所有目标盘的清单
                with library_db._tx() as c:      # noqa: SLF001 - 内部事务封装
                    c.execute("DELETE FROM sync_files WHERE role=?", (r,))
        except Exception as exc:                # noqa: BLE001
            applog.warning("重置同步基准失败 %s: %s", r, exc)


def paths_conflict(src: str, dst: str) -> bool:
    """判断两个路径是否相同，或 dst 落在 src 内部（互为父子/同一目录）。

    用于拦截「把下载源同步到它自己」这种错误配置——否则全部文件都会被判为
    「已存在且大小一致」而被跳过，最终拷贝 0 个文件，且无任何报错，极难排查。
    """
    try:
        ps = Path(src).resolve()
        pd = Path(dst).resolve()
    except Exception:                      # noqa: BLE001
        return False
    if ps == pd:
        return True
    try:
        pd.relative_to(ps)
        return True
    except ValueError:
        return False


def _excluded(p: Path) -> bool:
    name = p.name
    if name in _EXCLUDE_NAMES:
        return True
    if name.startswith("."):
        return True
    return p.suffix.lower() in _EXCLUDE_SUFFIXES


def _list_drama_dirs(src: Path) -> list:
    """列出下载根下的**剧目录**（第二层），不递归进剧集文件。

    实测 2473 个剧目录只要 0.01 秒（对比递归到底 78 秒）。
    结构固定为 <下载根>/<站点或分类>/<剧目录>/...。
    """
    out = []
    for path, _mtime in _list_drama_dirs_mtime(src):
        out.append(path)
    return out


def _list_drama_dirs_mtime(src: Path) -> list:
    """列出剧目录 + 它们的 mtime，**一次 scandir 就拿到**（不走 os.stat）。

    ⚠ 性能关键：2453 个目录若逐个 ``os.stat`` 要 **5.5 秒**（J 盘是机械盘/
    网络盘，每次 stat 都要落盘寻道）；而 ``os.scandir`` 返回的 ``DirEntry``
    自带目录项缓存，``entry.stat()`` 几乎零成本，实测 **0.01 秒**——快 500 倍。
    """
    out = []
    try:
        with os.scandir(str(src)) as it:
            for site in it:
                try:
                    if not site.is_dir(follow_symlinks=False):
                        continue
                    if site.name.startswith("_"):
                        continue
                except OSError:
                    continue
                try:
                    with os.scandir(site.path) as it2:
                        for drama in it2:
                            try:
                                if not drama.is_dir(follow_symlinks=False):
                                    continue
                                mt = drama.stat(follow_symlinks=False).st_mtime
                            except OSError:
                                continue
                            out.append((drama.path, mt))
                except OSError:
                    continue
    except OSError:
        return []
    return out


def _iter_source_files(src: Path, candidates: list | None):
    """产出待检查的源文件。

    用 ``os.scandir`` 递归遍历（比 ``Path.rglob`` 快 12 倍：
    实测 19552 个文件，rglob 78 秒 vs scandir 6.2 秒——rglob 对每个文件
    都做两次 stat，scandir 走目录缓存）。

    candidates 非空时，只遍历这些**剧目录**（调用方给的候选），
    进一步缩小范围；candidates 为 None 则全量遍历（不会漏）。
    """
    if not candidates:
        stack = [str(src)]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                yield Path(e.path)
                        except OSError:
                            continue
            except OSError:
                continue
        return
    seen = set()
    for d in candidates:
        dp = Path(d)
        if not dp.is_dir():
            continue
        for p in dp.rglob("*"):
            if not p.is_file():
                continue
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            yield p


def sync_target(role: str, target_root: str, since_ts: float, full: bool,
                src_root: str | None = None,
                on_log=None, on_scan=None, scope_desc: str | None = None,
                known_files: dict | None = None,
                cutoff_ts: float | None = None,
                candidate_dirs: list | None = None) -> TargetResult:
    """把 src_root 下「待同步」的文件镜像到 target_root（保留相对结构）。

    待同步判定（2026-10-21 起）：
    - known_files 传入时（增量模式）：以「项目 DB 的已同步文件清单」为准。
        * 不在清单里 → **新下载**，必拷；
        * 在清单里但目标端缺失/大小不符 → 补拷（自愈）；
        * 在清单里且目标端一致 → 跳过。
      这样「上一轮同步之后下载的」一定会被拷到，且不依赖 mtime，天然避免漏拷。
    - known_files 为 None 且给了 cutoff_ts（按日期范围同步）：mtime > cutoff 才拷。
    - full=True：全部拷贝。

    candidate_dirs：DB 索引给出的候选剧目录（性能优化）。非空时只扫这些目录，
    避免全量 rglob（2 万文件要 175 秒）。为 None 时全量扫描。

    target_root 不存在会自动创建；已存在且大小一致的文件跳过（幂等）。
    on_log 用于回调单条日志（相对路径），便于 UI 实时展示。
    on_scan(examined, copied) 周期回调扫描/拷贝进度，供 UI 显示「正在干活」。
    """
    src = Path(src_root or config.DOWNLOAD_DIR)
    dst = Path(target_root)
    res = TargetResult(role=role, root=str(dst), full=full)

    if not src.is_dir():
        res.errors = 1
        res.error_samples.append(f"源目录不存在: {src}")
        return res
    # 拦截「目标盘 == 下载源」这类错误配置（会把全部文件判为已存在而跳过）
    if paths_conflict(str(src), str(dst)):
        res.errors = 1
        res.error_samples.append(
            f"目标盘与下载源相同（{dst}），会导致自我覆盖，已跳过。"
            f"请在「设置 → 外盘同步」把下载备份盘改为其它盘符（如 H:\\AI剧）。")
        return res
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        res.errors = 1
        res.error_samples.append(f"无法创建目标目录 {dst}: {exc}")
        return res

    use_manifest = known_files is not None
    cutoff = 0.0 if full else (cutoff_ts if cutoff_ts is not None else since_ts)
    examined = 0
    synced_rows: list = []      # [(rel_posix, size, mtime)] 本轮成功同步的

    for src_file in _iter_source_files(src, candidate_dirs):
        if not src_file.is_file():
            continue
        if _excluded(src_file):
            res.skipped += 1
            continue
        examined += 1
        if on_scan and examined % 200 == 0:
            on_scan(examined, res.copied)
        try:
            st = src_file.stat()
        except OSError:
            res.skipped += 1
            continue

        rel = src_file.relative_to(src)
        rel_posix = rel.as_posix()
        dst_file = dst / rel

        if use_manifest:
            # 增量：集合差集口径
            known = known_files.get(rel_posix)
            if known is not None and not full:
                # 已同步过：目标端还在且大小一致 -> 跳过
                if dst_file.exists():
                    try:
                        if dst_file.stat().st_size == st.st_size:
                            res.skipped += 1
                            continue
                    except OSError:
                        pass
                # 目标端没了 / 大小变了 -> 补拷（自愈）
                res.repaired += 1
            else:
                # 清单里没有 = 上一轮同步之后新下载的 -> 必拷
                res.new_files += 1
        else:
            # 按日期范围 / 全量：mtime 过滤
            if not full and st.st_mtime <= cutoff:
                res.skipped += 1
                continue
            if dst_file.exists():
                try:
                    if dst_file.stat().st_size == st.st_size:
                        res.skipped += 1
                        continue
                except OSError:
                    pass

        try:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            res.copied += 1
            res.copied_bytes += st.st_size
            res.copied_files.append((rel_posix, st.st_size))
            if use_manifest:
                synced_rows.append((rel_posix, st.st_size, st.st_mtime))
            if on_log and (res.copied <= 3 or res.copied % 25 == 0):
                on_log(f"已拷贝 {res.copied}: {rel}")
        except OSError as exc:
            res.errors += 1
            if len(res.error_samples) < 8:
                res.error_samples.append(f"{rel}: {exc}")
    res.synced_rows = synced_rows
    return res


def start_of_day_epoch(y: int, m: int, d: int) -> float:
    """返回本地时区下该日 00:00 的 epoch 秒，用于与文件 st_mtime 比较。

    文件 mtime 是本地 epoch；用本地 0 点构造并 .timestamp()（naive 视为本地），
    两者口径一致，避免因时区差异把「今天」的文件漏掉或误判。
    """
    import datetime as _dt
    return _dt.datetime(y, m, d).timestamp()


def _persist_session(role: str, path: str, res, scope_desc: str, mode: str) -> int:
    """把一次同步会话落库（**项目内 DATA/hg.db**，不往外盘写任何文件）。

    - sync_sessions      : 会话摘要（时间/范围/模式/文件数/字节数）
    - sync_session_files : 本轮拷贝的**文件明细**（清单本体）
    - sync_files         : 该目标盘「已同步文件」的当前状态（下一轮增量的依据）

    仅 res.copied>0 时才记会话，避免「无新文件」刷屏历史；
    但 sync_files 的基准**总是**要更新的（哪怕这轮 0 拷贝），
    否则会反复把同一批文件当成「新增」。
    返回 session_id（0 表示未记录）。
    """
    import datetime as _dt
    ts = time.time()
    session_id = 0
    if res.copied > 0:
        try:
            session_id = library_db.add_sync_session(
                int(ts), role, path, scope_desc, mode,
                res.copied, res.copied_bytes, "")
        except Exception as exc:                # noqa: BLE001
            applog.warning("写同步记录失败: %s", exc)
            session_id = 0
        if session_id:
            try:
                library_db.add_sync_session_files(
                    session_id, role, res.copied_files)
            except Exception as exc:            # noqa: BLE001
                applog.warning("写同步明细失败: %s", exc)
    # 基准：登记本轮同步成功的文件（增量模式）
    rows = getattr(res, "synced_rows", None) or []
    if rows:
        try:
            library_db.record_sync_files(role, path, rows,
                                         session_id=session_id, synced_at=int(ts))
        except Exception as exc:                # noqa: BLE001
            applog.warning("更新同步基准失败: %s", exc)
    res.session_id = session_id
    return session_id


def sync_all(targets: list, full: bool = False,
             src_root: str | None = None,
             on_log=None, since_override: float | None = None,
             on_scan=None, scope_desc: str | None = None,
             use_index: bool = True) -> list:
    """对多个目标盘执行同步。

    targets: [{"role": "download"/"server", "path": "..."}, ...]
    - full=True：全量拷贝，并把基准刷新为当前已同步文件集合。
    - since_override 给定时：以该时刻为起点拷贝（日期范围同步），不更新增量基准
      （保留「自上次同步以来」的语义，便于后续用增量模式补齐）。
    - 二者都不给：**增量模式**——以项目 DB 的「已同步文件清单」做集合差集，
      只拷「上一轮同步之后新下载的」文件；该盘还没有任何清单记录时视为
      「首次同步」→ 全量（建立基准）。

    use_index=True（默认）：用项目 DB 的下载索引圈定候选剧目录，
    只扫「最近有下载活动」的目录，而不是全盘 rglob。
    实测：全量 rglob 2 万文件要 175 秒，索引命中后只要几秒。
    索引不可用时自动回退全量扫描（保证不漏）。

    关键护栏：只要目标盘「路径」没变（即仍是同一块盘/同一目录），即使该盘被清空、
    下次同步也**不会退化成全量**，只会按清单补拷新增/丢失的文件——这正是
    「移动硬盘拿去服务器清空、下次只拷增量」的使用场景。只有用户主动改了盘符
    或选「全部（全量）」才会全量。
    """
    results: list = []
    for t in targets:
        role = t.get("role")
        path = (t.get("path") or "").strip()
        if not role or not path:
            continue

        known: dict | None = None
        use_manifest = False
        cdirs: list = []
        mark_ts = 0.0
        if full:
            since_ts, cutoff = 0.0, None
            scope_eff, mode = "全量", "full"
            # 全量也要登记 sync_files，否则全量之后增量会把全部文件当「新增」。
            # 用空集合走「集合差集」分支：本轮所有文件都算新增并登记为基准。
            known = {}
            use_manifest = True
        elif since_override is not None:
            since_ts, cutoff = since_override, since_override
            scope_eff, mode = scope_desc or "自指定日期起", "date"
        else:
            since_ts, cutoff = 0.0, None
            known = library_db.get_sync_files(role, path)
            use_manifest = True
            if not known:
                # 该盘没有任何同步记录 -> 首次同步，全量建立基准
                scope_eff, mode = "全量（首次同步，建立基准）", "full"
            else:
                scope_eff, mode = "增量（自上次同步以来新增的）", "incremental"
                # 性能：只扫「有新增」的剧目录（剧目录 mtime + DB 索引两级过滤）
                mark_ts = float(library_db.get_sync_mark(role, path) or 0)
                cdirs = _candidate_dirs(src_root, mark_ts, use_index,
                                         known_files=known)

        res = sync_target(role, path, since_ts, full,
                          src_root=src_root, on_log=on_log, on_scan=on_scan,
                          scope_desc=scope_eff,
                          known_files=(known if use_manifest else None),
                          cutoff_ts=cutoff, candidate_dirs=cdirs)
        _persist_session(role, path, res, scope_eff, mode)
        # 记录本盘「最近同步时刻」：下一轮用它做剧目录 mtime 过滤（快速定位候选）。
        # 只有本轮无错误才推进，否则下次仍会全查该盘（不漏）。
        if res.errors == 0 and mode != "date":
            try:
                library_db.set_sync_mark(role, path, int(time.time()))
            except Exception as exc:            # noqa: BLE001
                applog.warning("写同步时刻失败: %s", exc)
        if res.errors == 0:
            applog.info("外盘同步完成 %s -> %s（%s）: 拷贝 %d 文件"
                        "（新增 %d，补拷 %d）",
                        role, path, mode, res.copied, res.new_files, res.repaired)
        else:
            applog.warning("外盘同步有失败 %s -> %s: %d 个错误",
                           role, path, res.errors)
        results.append(res)
    return results


def _count_pending(role: str, target_root: str, since_ts: float, full: bool,
                  src_root: str | None = None, on_scan=None,
                  known_files: dict | None = None,
                  candidate_dirs: list | None = None) -> tuple:
    """统计某目标盘「待同步」的文件数与字节数（不实际拷贝），供 UI 预览。

    口径与 sync_target 完全一致：
    - known_files 传入（增量模式）：不在清单=新增；清单有但目标端缺失/大小不符=需补拷；
    - 否则（按日期范围）：mtime>cutoff 才算待同步。
    candidate_dirs 非空时只扫这些目录（DB 索引优化）。
    on_scan(n) 在遍历过程中周期回调（n=已检查的文件数），供 UI 显示进度不至静默。
    """
    src = Path(src_root or config.DOWNLOAD_DIR)
    if not src.is_dir():
        return 0, 0
    cutoff = 0.0 if full else since_ts
    cnt = 0
    total = 0
    examined = 0
    for sf in _iter_source_files(src, candidate_dirs):
        if _excluded(sf):
            continue
        examined += 1
        if on_scan and examined % 200 == 0:
            on_scan(examined)
        try:
            st = sf.stat()
        except OSError:
            continue
        rel = sf.relative_to(src)
        dst_file = Path(target_root) / rel
        if known_files is not None:
            key = rel.as_posix()
            if key in known_files and not full:
                # 已同步过：只有目标端缺失/大小不符才需补拷
                if dst_file.exists():
                    try:
                        if dst_file.stat().st_size == st.st_size:
                            continue
                    except OSError:
                        pass
            # 清单里没有 = 新下载 -> 计入
        else:
            if not full and st.st_mtime <= cutoff:
                continue
            if dst_file.exists():
                try:
                    if dst_file.stat().st_size == st.st_size:
                        continue
                except OSError:
                    pass
        cnt += 1
        total += st.st_size
    return cnt, total


def _candidate_dirs(src_root: str | None, since_ts: float,
                    use_index: bool = True,
                    known_files: dict | None = None) -> list:
    """算出「候选剧目录」列表（性能优化）。

    两级过滤，**既快又不漏**：

    1) **剧目录层扫描**（0.04 秒）：列出下载根下全部剧目录（2473 个）。
       这一步不碰任何文件，所以很快。
    2) **目录 mtime 过滤**：只有「mtime 晚于 since_ts」的剧目录才需细查。
       手工放进剧目录的文件同样会更新该目录的 mtime -> **不会漏**。
    3) 若上层拿不到 since_ts（首次同步/无基准），则全部剧目录都要查——
       此时用 DB 下载索引（episodes.file_path）把「已同步过」的剧目录排除掉，
       只留真正有新增的。

    use_index=False -> 返回 []（调用方回退全量扫描）。
    """
    if not use_index:
        return []
    src = Path(src_root or config.DOWNLOAD_DIR)
    try:
        all_dirs = _list_drama_dirs_mtime(src)
    except Exception:                       # noqa: BLE001
        return []
    if not all_dirs:
        return []

    # 第 2 级：目录 mtime 过滤（mtime 由 scandir 一次拿到，几乎零成本）
    fresh: list = []
    for d, mt in all_dirs:
        if since_ts <= 0 or mt > since_ts:
            fresh.append(d)
    if fresh:
        return fresh

    # 第 3 级：mtime 全旧 -> 用 DB 下载索引找「有新增的剧」
    # （覆盖「下载程序改了目录内文件但目录 mtime 没变」的极端情况）
    try:
        exclude = set(known_files.keys()) if known_files else None
        dirs = library_db.get_drama_dirs_for_sync(str(src), since_ts,
                                                  exclude_paths=exclude)
        return dirs or []
    except Exception:                       # noqa: BLE001
        return []

def preview_sync(targets: list, full: bool = False,
                 src_root: str | None = None,
                 since_override: float | None = None,
                 on_scan=None, use_index: bool = True) -> dict:
    """返回每个目标盘的待同步统计 {role: {path, full, count, bytes}}，不拷贝文件。

    用于在点击「开始同步」前给用户一个明确的数字——「将拷贝 N 个新增/更新的文件」。
    日期范围同步（since_override 给定）口径与 sync_all 一致；
    增量模式用项目 DB 的已同步清单做差集，口径也与 sync_all 一致。
    use_index=True 时用下载索引圈定候选目录，避免全盘扫描（2 万文件 175 秒）。
    on_scan(n) 遍历过程中周期回调，供 UI 显示统计进度。
    """
    out: dict = {}
    for t in targets:
        role = t.get("role")
        path = (t.get("path") or "").strip()
        if not role or not path:
            continue
        known = None
        cdirs: list = []
        if full:
            since_ts = 0.0
            known = {}          # 全量：所有文件都算待同步
        elif since_override is not None:
            since_ts = since_override
        else:
            known = library_db.get_sync_files(role, path)
            since_ts = 0.0
            # 性能：与 sync_all 口径一致——用剧目录 mtime + DB 索引定位候选
            mark_ts = float(library_db.get_sync_mark(role, path) or 0)
            cdirs = _candidate_dirs(src_root, mark_ts, use_index,
                                     known_files=known)
        cnt, total = _count_pending(role, path, since_ts, full,
                                    src_root=src_root, on_scan=on_scan,
                                    known_files=known, candidate_dirs=cdirs)
        out[role] = {"path": path, "full": full, "count": cnt, "bytes": total,
                     "indexed": bool(cdirs)}
    return out
