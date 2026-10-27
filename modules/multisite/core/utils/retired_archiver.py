# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
已注销模特归档器
================

把「已注销」模特的视频目录从各国文件夹**移动**到统一的 ``已注销/`` 文件夹，
并在 ``已注销/注销台账.md`` 中记录每位的**原国籍**（为何会进这里）。

背景
----
``models.country == '已注销'`` 的模特其视频仍散落在 ``T:\\日本\\``、``V:\\俄罗斯\\``
等各国目录中，持续污染国家级文件夹；且这些目录后续不会再更新。

判定口径
--------
* **谁是已注销**：``models.country == '已注销'``（项目既有约定，多处代码已依赖）
* **原国籍从哪来**：``models`` 表**没有** ``original_country`` 列，国籍被覆盖后
  已丢失；按以下来源反查（越靠前越可靠）：

  #. ``retired_origin`` 表——首次反查成功后**永久固化**，人工修正优先；
  #. **当前 ``local_directory`` 所在地区文件夹**（目录尚未搬入「已注销」时，
     盘符后的第一级目录即原国籍，如 ``T:\\素人2\\[Channel] Foo`` → ``素人2``，
     权重最高；已搬入「已注销」者跳过，不会误写成「已注销」）；
  #. 历史下载路径反查（``download_status.target_dir`` / ``downloads.file_path``，取众数）；
  #. 以上皆无 → 记「未知」（从未下载过、且目录已被搬走、旧路径不可溯）。

安全设计
--------
* **默认 dry-run**，必须显式 ``apply=True`` 才真正移动
* 跨盘移动（``T:\\`` → ``U:\\``）自动退化为 **复制+校验+删除**，不假设 rename 原子
* 目标同名冲突时自动加 ``(2)`` 后缀，**绝不覆盖**
* 每步写日志；DB 路径仅在文件确认到位后才更新
* 幂等：已归档的模特再跑一次会被识别并跳过
"""
from __future__ import annotations

import os
import re
import json
import shutil
import sqlite3
import logging
import time
import string
import threading
import subprocess
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 判定「已注销」的 country 值（项目既有约定）
RETIRED_COUNTRY = '已注销'

#: 归档目标文件夹名（挂在「其他」地区盘符下，与 disk_region_manager 的
#: ``'已注销': '其他'`` 映射一致）
RETIRED_DIRNAME = '已注销'

#: 台账文件名
LEDGER_NAME = '注销台账.md'

#: 视频扩展名（用于统计与完整性校验）
VIDEO_EXTS = ('.mp4', '.wmv', '.avi', '.mov', '.mkv', '.flv', '.ts', '.m4v', '.rmvb')

_DRIVE_RE = re.compile(r'^[A-Za-z]:$')


@dataclass
class ArchiveItem:
    """单个待归档模特的处理结果"""
    model_id: int
    name: str
    original_country: Optional[str]      # 反查到的原国籍；None = 未知
    country_evidence: int = 0            # 反查票数（可信度）
    source_dir: Optional[str] = None     # 磁盘上实际找到的目录
    target_dir: str = ''                 # 归档目标目录
    video_count: int = 0
    total_bytes: int = 0
    status: str = 'pending'              # moved|skipped|notfound|conflict|failed
    message: str = ''

    def as_row(self) -> str:
        c = self.original_country or '未知'
        return f"| {self.name} | {c} | {self.country_evidence} | {self.video_count} | {self.status} |"


@dataclass
class ArchiveReport:
    """整体归档报告"""
    dry_run: bool = True
    started_at: str = ''
    finished_at: str = ''
    items: List[ArchiveItem] = field(default_factory=list)
    ledger_path: str = ''

    def summary(self) -> Dict[str, int]:
        c = Counter(i.status for i in self.items)
        return {
            'total': len(self.items),
            'moved': c.get('moved', 0),
            'skipped': c.get('skipped', 0),
            'notfound': c.get('notfound', 0),
            'failed': c.get('failed', 0),
        }


def _base_dir() -> Path:
    """程序根目录（兼容 PyInstaller onefile）

    ⚠ 2026-10-02：脚本被直接放在桌面双击运行时，``__file__`` 在桌面，
    找不到 ``data/db``。这里按优先级探测多个候选根目录。
    """
    import sys
    cands = []
    if getattr(sys, 'frozen', False):
        cands.append(Path(sys.executable).parent)
    else:
        cands.append(Path(__file__).resolve().parents[3])   # 项目根
        cands.append(Path(__file__).resolve().parent)         # 脚本所在目录（桌面/工具目录）
    # 常见部署位置兜底
    cands.append(Path(r"E:\PornSimilarityPlatform"))
    cands.append(Path(r"D:\PornSimilarityPlatform"))
    cands.append(Path.cwd())

    for c in cands:
        if (c / 'data' / 'db' / 'models.db').exists():
            return c
    # 都没找到 → 返回最可能的（让后续报错信息指向正确位置）
    return cands[0]


def _db_paths() -> Tuple[Path, Optional[Path]]:
    """返回 (models.db, download_cache.db)"""
    root = _base_dir() / 'data' / 'db'
    return root / 'models.db', root / 'download_cache.db'


def normalize_name(name: str) -> str:
    """模特名归一化：去空白 + 小写，用于跨表匹配"""
    return re.sub(r'\s+', ' ', (name or '').strip()).lower()


def country_from_path(path: Optional[str]) -> Optional[str]:
    """从 ``T:\\德国\\[Channel] viberider`` 提取 ``德国``

    兼容 ``/`` 与 ``\\`` 两种分隔符（历史库两种都有）。
    """
    if not path:
        return None
    s = str(path).replace('\\', '/')
    parts = [p for p in s.split('/') if p]
    for i, p in enumerate(parts):
        if _DRIVE_RE.match(p) and i + 1 < len(parts):
            cand = parts[i + 1].strip()
            if cand and not cand.startswith('['):
                return cand
    return None


def dir_display_name(dirname: str) -> str:
    """``[Channel] Foo`` → ``Foo``；无前缀则原样返回"""
    m = re.match(r'^\[Channel\]\s*(.+)$', dirname or '', re.I)
    return (m.group(1) if m else (dirname or '')).strip()


def scan_dir_stats(path: str) -> Tuple[int, int]:
    """统计目录下视频数与总字节数（不递归子目录）"""
    cnt = 0
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(VIDEO_EXTS):
                    cnt += 1
                    try:
                        total += e.stat().st_size
                    except OSError:
                        pass
    except OSError as e:
        logger.warning(f"统计目录失败 {path}: {e}")
    return cnt, total


def _unique_path(target: str) -> str:
    """目标已存在时加 ``(2)`` ``(3)`` 后缀，绝不覆盖"""
    if not os.path.exists(target):
        return target
    base, ext = os.path.splitext(target)
    for i in range(2, 1000):
        cand = f"{base}({i}){ext}"
        if not os.path.exists(cand):
            return cand
    raise RuntimeError(f"无法为 {target} 找到可用名")


#: 跨盘复制时的分块大小。1 MB —— 用分块流式复制代替 shutil.copy2 的默认实现，
#: 避免大文件整块进内存（8 GB 机器上并发复制会把可用内存打到 400 MB 以下，
#: 触发 Qt 侧 qFatal → abort 闪退，见 2026-10-08 15:08:38 事故）。
_COPY_CHUNK = 1024 * 1024

#: 跨盘复制前若可用内存低于此值(MB)则放弃本次移动（内存熔断）。
#: ⚠ 2026-10-13 修正：旧值 1200 在「常驻下载」的服务端几乎永远触发——
#: 服务端可用内存长期在 1100~1300MB 波动，于是归档遇到一次低内存就把整批
#: 几十个待归档模特全部放弃（全部变 pending），表现就是「点归档没效果」。
#: 而 2026-10-08 的闪退事故发生在可用内存 **460MB（94% 负载）**，1200 留了
#: 740MB 的过大余量。这里降到 700：仍比崩溃点高 240MB（安全），又能让归档
#: 在服务器常态内存下真正推进。配合下方「单项目级重试」逻辑，即使偶发低于阈值
#: 也会 sleep 等下载侧内存监控回收后重试，而不是整批放弃。
_MIN_AVAIL_MB = 700


class LowMemoryError(RuntimeError):
    """可用内存低于安全阈值，跨盘复制被熔断"""


def _avail_mb() -> float:
    """当前可用物理内存(MB)；取不到时返回一个大数（不阻断）"""
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:
        return 99999.0


def _stream_copy(src: str, dst: str) -> None:
    """分块流式复制单个文件（替代 shutil.copy2，降低峰值内存）"""
    with open(src, 'rb', buffering=0) as fsrc:
        # 预分配目标大小但**不一次性申请内存**：Windows 上 truncate 只是改元数据
        try:
            total = os.path.getsize(src)
        except OSError:
            total = 0
        with open(dst, 'wb', buffering=0) as fdst:
            if total:
                try:
                    fdst.truncate(total)
                except OSError:
                    pass
            while True:
                chunk = fsrc.read(_COPY_CHUNK)
                if not chunk:
                    break
                fdst.write(chunk)
    # 尽量保留时间戳（失败不影响主流程）
    try:
        st = os.stat(src)
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def _dir_size(path: str) -> int:
    """递归统计目录字节数（用于拷贝进度/卡死检测）。"""
    total = 0
    try:
        for dp, _, fns in os.walk(path):
            for fn in fns:
                try:
                    total += os.path.getsize(os.path.join(dp, fn))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _listdir_timeout(path: str, timeout: int = 8) -> List[str]:
    """带超时的 ``os.listdir``。SMB 不可达时本地 I/O 会阻塞数分钟，
    用线程 + join 超时兜底，超时返回空列表（视为无内容）。"""
    res: List[Optional[list]] = []
    th = threading.Thread(target=lambda: res.append(_safe_listdir(path)), daemon=True)
    th.start()
    th.join(timeout)
    if not res:
        return []
    return res[0] or []


def _safe_listdir(path: str) -> Optional[list]:
    try:
        return os.listdir(path)
    except Exception:
        return None


def _isdir_timeout(path: str, timeout: int = 8) -> bool:
    """带超时的 ``os.path.isdir``（同上，避免 SMB 挂死拖垮归档线程）。"""
    res: List[bool] = []
    th = threading.Thread(target=lambda: res.append(_safe_isdir(path)), daemon=True)
    th.start()
    th.join(timeout)
    return bool(res and res[0])


def _safe_isdir(path: str) -> bool:
    try:
        return os.path.isdir(path)
    except Exception:
        return False


def _candidate_drives(retired_root: str) -> List[str]:
    """归档/源可能所在的全部盘符（去重排序）。

    合并三类来源，确保**源文件所在的真实盘符**被扫到：
      * 归档根所在盘符
      * ``disk_region_config.json`` 的 ``region_disk_map`` 各盘符（地区盘）
      * ``nas_drives.json`` 的全部挂载盘（T:/U:/V:...——源文件夹真正所在）
    """
    drives: set = set()
    root_drive = os.path.splitdrive(retired_root or '')[0]
    if root_drive:
        drives.add(root_drive)
    try:
        cfg = _base_dir() / 'data' / 'config' / 'disk_region_config.json'
        if cfg.exists():
            data = json.loads(cfg.read_text(encoding='utf-8'))
            for d in (data.get('region_disk_map') or {}).values():
                if d:
                    drives.add(d if d.endswith(':') else d + ':')
    except Exception:
        pass
    try:
        nd = _base_dir() / 'data' / 'config' / 'nas_drives.json'
        if nd.exists():
            data = json.loads(nd.read_text(encoding='utf-8'))
            for m in data.get('mappings', []):
                d = m.get('drive')
                if d:
                    drives.add(d if d.endswith(':') else d + ':')
    except Exception:
        pass
    return sorted(drives)



_MOVE_TIMEOUT = 1200       # 硬上限：单次移动总耗时超过此值直接放弃（仅兜底，极罕见）
_MOVE_STUCK_LIMIT = 300    # 连续 N 秒零进展 => 判定 NAS 卡死，杀掉重来


def _move_cross_drive_robocopy(src: str, dst: str) -> None:
    """跨盘移动：用 robocopy 子进程（可真正取消）+ 心跳日志 + 卡死检测。

    2026-10-13 发现：``shutil.copytree`` 跨 NAS 拷贝时，若连接在传输中途卡死，
    Python 的 I/O 调用会无限阻塞（SMB 无 socket 超时），且**无法 kill 阻塞在 I/O
    上的线程**——旧实现要么整批卡死、要么超时后留孤儿线程。改用 robocopy 子进程：
    * 子进程可被 ``proc.kill()`` 真正终止，真卡死能干净取消；
    * 轮询目标目录体积，每 30s 打一条心跳日志（用户能看到进度，不再「没动静」）；
    * 连续 ``_MOVE_STUCK_LIMIT`` 秒体积零增长才判定卡死——**健康的慢拷贝不会被误杀**。
    """
    os.makedirs(dst, exist_ok=True)
    robocopy = r"C:\Windows\System32\robocopy.exe"
    if not os.path.exists(robocopy):
        # 退化到 copytree（无取消能力，但保证不崩溃）
        logger.warning("未找到 robocopy，退化为 copytree（不可取消）")
        shutil.copytree(src, dst, copy_function=_stream_copy)
        shutil.rmtree(src)
        return
    cmd = [robocopy, src, dst, "/E", "/MOVE", "/R:3", "/W:5",
           "/NP", "/NFL", "/NDL", "/COPY:DAT"]
    logger.info(f"  启动 robocopy 跨盘移动: {src} -> {dst}")
    start = time.time()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace")
    last_growth = time.time()
    last_log = time.time()
    prev_size = _dir_size(dst)
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        now = time.time()
        size = _dir_size(dst)
        if size > prev_size:
            prev_size = size
            last_growth = now
        if now - last_log >= 30:
            logger.info(f"  归档进度: 已复制 {size/1024/1024:.0f} MB（已 {int(now - start)}s）")
            last_log = now
        if now - last_growth >= _MOVE_STUCK_LIMIT:
            logger.error(f"  跨盘复制 {_MOVE_STUCK_LIMIT}s 无进展，判定 NAS 卡死，终止: {src}")
            proc.kill()
            raise TimeoutError(
                f"跨盘复制 {_MOVE_STUCK_LIMIT}s 无进展（疑似 NAS 卡死）: {src} -> {dst}")
        if now - start >= _MOVE_TIMEOUT:
            logger.error(f"  跨盘复制超过 {_MOVE_TIMEOUT}s 硬上限，终止: {src}")
            proc.kill()
            raise TimeoutError(f"跨盘复制超时（>{_MOVE_TIMEOUT}s）: {src} -> {dst}")
        time.sleep(3)
    # robocopy 退出码 <8 视为成功（0~7 含「全部复制 / 有跳过 / 有同名」）
    if proc.returncode is not None and proc.returncode >= 8:
        tail = ""
        try:
            tail = (proc.stdout.read() or "")[-600:] if proc.stdout else ""
        except Exception:
            pass
        raise RuntimeError(f"robocopy 失败(rc={proc.returncode}): {tail}")
    # /MOVE 应已删除源文件；保险起见清掉残留空目录
    if os.path.isdir(src):
        try:
            shutil.rmtree(src, ignore_errors=True)
        except OSError:
            pass


def _move_dir(src: str, dst: str) -> None:
    """移动目录；跨盘走 robocopy（可取消+心跳+卡死检测），同盘直接 move。"""
    src_drive = os.path.splitdrive(src)[0].upper()
    dst_drive = os.path.splitdrive(dst)[0].upper()
    if src_drive and dst_drive and src_drive != dst_drive:
        avail = _avail_mb()
        if avail < _MIN_AVAIL_MB:
            raise LowMemoryError(
                f"可用内存仅 {avail:.0f} MB（低于安全阈值 {_MIN_AVAIL_MB} MB），"
                f"已跳过跨盘复制以避免进程崩溃：{src}")
        logger.info(f"跨盘移动 {src_drive}->{dst_drive}: {src}（可用内存 {avail:.0f} MB）")
        _move_cross_drive_robocopy(src, dst)
    else:
        shutil.move(src, dst)


def _move_with_timeout(src: str, dst: str, timeout: int = _MOVE_TIMEOUT) -> None:
    """兼容旧调用点：直接调用 ``_move_dir``（robocopy 内部已处理超时/卡死）。"""
    _move_dir(src, dst)


class RetiredModelArchiver:
    """已注销模特归档器"""

    def __init__(self, models_db: Optional[str] = None,
                 cache_db: Optional[str] = None,
                 db_adapter=None):
        m, c = _db_paths()
        self.models_db = str(models_db or m)
        self.cache_db = str(cache_db or c)
        #: 可选：外部注入的 ModuleDatabaseManager（有 update_model_local_directory）
        self._adapter = db_adapter

    # ---------- 数据读取 ----------

    def _connect_ro(self, path: str) -> sqlite3.Connection:
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"数据库不存在: {path}\n"
                f"（探测到的程序根目录: {_base_dir()}）\n"
                f"若脚本被放在桌面运行，请改用程序目录下的 data\\db\\，"
                f"或把本脚本移到程序根目录再运行。")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        return con

    # ---------- 原国籍固化（retired_origin 表） ----------
    #
    # 为什么要固化：`models.local_directory` 在归档时被改写成 `已注销\...`，
    # 原国籍路径就此消失。若只靠运行时反查 `download_status.target_dir`，
    # 目录一旦搬完就再也查不到 → 台账永久记「未知」。
    # 故首次反查成功即写入本表，此后以本表为准（可人工修正）。

    def _ensure_origin_table(self) -> bool:
        """确保 retired_origin 表存在（只建不改已有数据）"""
        try:
            con = sqlite3.connect(self.models_db, timeout=30)
            cur = con.cursor()
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS retired_origin ("
                " model_id INTEGER PRIMARY KEY,"
                " original_country TEXT NOT NULL,"
                " evidence INTEGER DEFAULT 0,"
                " source TEXT DEFAULT '',"
                " recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
            # 兼容早期版本：补列
            cols = {r[1] for r in cur.execute("PRAGMA table_info(retired_origin)")}
            for col, ddl in (('evidence', 'INTEGER DEFAULT 0'),
                              ('source', "TEXT DEFAULT ''"),
                              ('recorded_at', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP')):
                if col not in cols:
                    cur.execute(f"ALTER TABLE retired_origin ADD COLUMN {col} {ddl}")
            con.commit()
            con.close()
            return True
        except Exception as e:
            logger.error(f"创建 retired_origin 表失败: {e}")
            return False

    def load_origin_map(self) -> Dict[int, Tuple[str, int]]:
        """读取已固化的原国籍 → ``{model_id: (country, evidence)}``"""
        if not os.path.exists(self.models_db):
            return {}
        try:
            con = sqlite3.connect(f"file:{self.models_db}?mode=ro", uri=True, timeout=30)
            cur = con.cursor()
            tabs = {r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if 'retired_origin' not in tabs:
                con.close()
                return {}
            rows = cur.execute(
                "SELECT model_id, original_country, evidence FROM retired_origin").fetchall()
            con.close()
            return {r[0]: (r[1], r[2] or 0) for r in rows if r[1]}
        except Exception as e:
            logger.error(f"读取 retired_origin 失败: {e}")
            return {}

    def save_origin(self, entries: Dict[int, Tuple[str, int]], source: str = 'lookup') -> int:
        """把反查到的原国籍固化进表（不覆盖已有记录，人工修正优先）"""
        if not entries:
            return 0
        if not self._ensure_origin_table():
            return 0
        try:
            con = sqlite3.connect(self.models_db, timeout=30)
            cur = con.cursor()
            cur.execute("PRAGMA busy_timeout=30000")
            n = 0
            for mid, (country, evidence) in entries.items():
                if not country:
                    continue
                cur.execute(
                    "INSERT OR IGNORE INTO retired_origin"
                    " (model_id, original_country, evidence, source)"
                    " VALUES (?,?,?,?)",
                    (mid, country, evidence, source))
                n += cur.rowcount > 0
            con.commit()
            con.close()
            if n:
                logger.info(f"已固化 {n} 条原国籍到 retired_origin")
            return n
        except Exception as e:
            logger.error(f"写入 retired_origin 失败: {e}")
            return 0

    def overwrite_origin(self, model_id: int, country: str,
                         evidence: int = 9999, source: str = 'manual') -> bool:
        """人工修正原国籍（覆盖已有记录）"""
        if not country:
            return False
        if not self._ensure_origin_table():
            return False
        try:
            con = sqlite3.connect(self.models_db, timeout=30)
            cur = con.cursor()
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute(
                "INSERT INTO retired_origin"
                " (model_id, original_country, evidence, source, recorded_at)"
                " VALUES (?,?,?,?,CURRENT_TIMESTAMP)"
                " ON CONFLICT(model_id) DO UPDATE SET"
                " original_country=excluded.original_country,"
                " evidence=excluded.evidence,"
                " source=excluded.source,"
                " recorded_at=CURRENT_TIMESTAMP",
                (model_id, country, evidence, source))
            con.commit()
            con.close()
            logger.info(f"人工修正原国籍: id={model_id} -> {country}")
            return True
        except Exception as e:
            logger.error(f"人工修正原国籍失败 id={model_id}: {e}")
            return False

    def get_retired_models(self) -> List[Dict]:
        """取所有已注销模特"""
        con = self._connect_ro(self.models_db)
        try:
            cur = con.cursor()
            cols = [r[1] for r in cur.execute("PRAGMA table_info(models)")]
            pick = [c for c in ('id', 'name', 'url', 'module', 'country',
                                'local_directory', 'status') if c in cols]
            q = f"SELECT {','.join(pick)} FROM models WHERE country=?"
            rows = cur.execute(q, (RETIRED_COUNTRY,)).fetchall()
            return [dict(zip(pick, r)) for r in rows]
        finally:
            con.close()

    def lookup_original_countries(self) -> Dict[int, Tuple[Optional[str], int]]:
        """反查每个已注销模特的原国籍 → ``{model_id: (country, votes)}``

        数据源：``download_status.target_dir`` 与 ``downloads.file_path``
        （``downloads`` 表无 ``model_id``，只有 ``model_name``，按名字匹配）。
        """
        result: Dict[int, Tuple[Optional[str], int]] = {}
        models = self.get_retired_models()
        if not models:
            return result

        by_id = {m['id']: m for m in models}
        # 名字归一化 -> id（用于 downloads 表按名字匹配）
        name_map: Dict[str, List[int]] = {}
        for m in models:
            name_map.setdefault(normalize_name(m['name']), []).append(m['id'])

        if not os.path.exists(self.cache_db):
            logger.warning(f"下载库不存在，跳过原国籍反查: {self.cache_db}")
            return {mid: (None, 0) for mid in by_id}

        con = self._connect_ro(self.cache_db)
        try:
            cur = con.cursor()
            tabs = {r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}

            votes: Dict[int, Counter] = {mid: Counter() for mid in by_id}

            # (a) download_status: 有 model_id
            if 'download_status' in tabs:
                cols = [r[1] for r in cur.execute("PRAGMA table_info(download_status)")]
                if 'model_id' in cols and 'target_dir' in cols:
                    for mid, tdir in cur.execute(
                            "SELECT model_id, target_dir FROM download_status "
                            "WHERE target_dir IS NOT NULL AND target_dir<>''"):
                        c = country_from_path(tdir)
                        if c and mid in votes:
                            votes[mid][c] += 1

            # (b) downloads: 无 model_id，按 model_name 匹配
            if 'downloads' in tabs:
                cols = [r[1] for r in cur.execute("PRAGMA table_info(downloads)")]
                if 'model_name' in cols and 'file_path' in cols:
                    for mname, fpath in cur.execute(
                            "SELECT model_name, file_path FROM downloads "
                            "WHERE file_path IS NOT NULL AND file_path<>''"):
                        c = country_from_path(fpath)
                        if not c:
                            continue
                        for mid in name_map.get(normalize_name(mname), ()):
                            votes[mid][c] += 1

            # (c) 目录当前所在地区文件夹（最直接证据）：
            #     目录尚未被搬入「已注销」前，盘符后的第一级目录即原国籍
            #     （如 ``T:\素人2\[Channel] Amelia Amour`` → ``素人2``）。
            #     给高权重——当历史下载反查缺失时这是唯一可靠来源；
            #     即使存在历史下载反查票，磁盘当前所在地区目录通常更准。
            #     ⚠️ 已搬入「已注销」的模特其 local_directory 已是 ``...\已注销\...``，
            #     提取到的是「已注销」本身，必须跳过，否则会把国籍错写成「已注销」。
            for mid, m in by_id.items():
                if mid not in votes:
                    continue
                ld = (m.get('local_directory') or '').strip()
                fc = country_from_path(ld)
                if fc and fc != RETIRED_DIRNAME:
                    votes[mid][fc] += 10000

            for mid, cnt in votes.items():
                if cnt:
                    c, n = cnt.most_common(1)[0]
                    result[mid] = (c, n)
                else:
                    result[mid] = (None, 0)
            return result
        except Exception as e:
            logger.error(f"反查原国籍失败: {e}")
            return {mid: (None, 0) for mid in by_id}
        finally:
            con.close()

    def resolve_retired_root(self) -> Optional[str]:
        """确定 ``已注销/`` 目标根目录。

        ⚠️ 2026-10-13 修正：旧实现只信 ``disk_region_config.json`` 的「其他」盘符，
        但该配置写的是 ``O:\\``，而 **真实归档数据在 ``T:\\已注销``（原始共享）**，
        且 ``nas_drives.json`` 里根本没有 ``O:`` 的挂载。于是旧逻辑要么解析到不可见的
        ``O:\\已注销`` 报错，要么回退到 ``os.path.dirname(os.path.dirname(ld))`` 把根
        错解成 ``T:\\``（盘符根）而误判所有模特「已归档」。

        新逻辑：收集所有候选位置（配置「其他」盘符 / nas_drives.json 全部挂载盘 /
        DB 指针里已有的 ``已注销`` 父目录），**优先返回「已存在且内容最多」的那一个**，
        从而保证归档落进真实有数据的位置、不与现有 32 位已归档模特分裂。
        """
        cfg = _base_dir() / 'data' / 'config' / 'disk_region_config.json'
        candidates: List[str] = []
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding='utf-8'))
                disk = (data.get('region_disk_map') or {}).get('其他')
                if disk:
                    candidates.append(os.path.join(disk, RETIRED_DIRNAME))
            except Exception as e:
                logger.error(f"读取地区盘符配置失败: {e}")
        # nas_drives.json 的全部挂载盘（源/归档真实所在）
        try:
            nd = _base_dir() / 'data' / 'config' / 'nas_drives.json'
            if nd.exists():
                data = json.loads(nd.read_text(encoding='utf-8'))
                for m in data.get('mappings', []):
                    d = m.get('drive')
                    if d:
                        candidates.append(
                            f"{d if d.endswith(':') else d + ':'}\\{RETIRED_DIRNAME}")
        except Exception:
            pass
        # DB 指针里已存在的 已注销 父目录（多数模特已指向真实归档位置）
        for m in self.get_retired_models():
            ld = m.get('local_directory') or ''
            if RETIRED_DIRNAME in ld:
                p = os.path.dirname(ld)        # T:\已注销
                if p:
                    candidates.append(p)

        # 去重保序
        seen = set()
        uniq: List[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                uniq.append(c)

        # 优先：已存在且子项最多的（= 真实归档位置，避免分裂）
        best: Optional[str] = None
        best_n = -1
        for c in uniq:
            try:
                if os.path.isdir(c):
                    n = len(_listdir_timeout(c, timeout=8))
                    if n > best_n:
                        best_n = n
                        best = c
            except Exception:
                pass
        if best is not None:
            return best
        # 都不存在 → 取第一个有效候选作为新建位置
        for c in uniq:
            if c:
                return c
        return None

    def find_source_dir(self, retired_root: str, item: Dict,
                        src_index: Optional[Dict[str, str]] = None) -> Optional[str]:
        """在各国文件夹里定位该模特的实际目录（用于「DB 指针是幽灵」时找回源）。

        判定顺序：
          1) DB ``local_directory`` 真实存在且不在归档根内 → 直接作为源；
          2) DB 指针在归档根内但目录**非空** → 视为已归档，返回 None（跳过）；
          3) DB 指针在归档根内但**空/不存在**（幽灵指针）→ 不假判为已归档，继续找源；
          4) 用 ``src_index``（一次扫描全部 NAS 挂载盘建立的 ``[Channel]名→目录`` 索引）
             精确找回源目录。旧实现只扫 ``region_disk_map`` 盘符（N:/O:/M:），而源实际在
             T:/U:/V:，导致幽灵指针永远找不到源 → 永久 notfound。
        """
        # 1) 数据库记录的路径
        ld = (item.get('local_directory') or '').strip()
        if ld and _isdir_timeout(ld):
            if os.path.normcase(os.path.abspath(ld)).startswith(
                    os.path.normcase(os.path.abspath(retired_root))):
                # 已在归档根内：仅「有内容」才算真正归档
                if _listdir_timeout(ld):
                    return None
                # 空目录 = 幽灵指针，fall through 到索引找回源
            else:
                return ld      # 真实源在原国家文件夹

        # 4) 索引查源
        if src_index:
            want = normalize_name(dir_display_name(item['name']))
            if want and want in src_index:
                return src_index[want]
        return None

    def _build_source_index(self) -> Dict[str, str]:
        """一次性扫描全部候选盘符，建立 ``{归一化[Channel]名: 源目录}`` 索引。

        ⚠️ 2026-10-13 修复：旧实现 ``find_source_dir`` 逐模特重复扫盘，且只扫
        ``region_disk_map`` 盘符（N:/O:/M:），漏掉了源真正所在的 T:/U:/V:。现改为
        扫 ``nas_drives.json`` 全部挂载盘 + region 盘 + 归档根盘，建索引一次，
        每个模特直接查表，既找得全、又不拖慢。所有列举带超时，避免 SMB 挂死冻线程。
        """
        index: Dict[str, str] = {}
        drives = _candidate_drives(self.resolve_retired_root() or '')
        for disk in drives:
            try:
                countries = _listdir_timeout(disk, timeout=8)
            except Exception:
                continue
            for country in countries:
                if country == RETIRED_DIRNAME:
                    continue        # 跳过归档根本身，避免把已归档目录当源
                cpath = os.path.join(disk, country)
                try:
                    subs = _listdir_timeout(cpath, timeout=8)
                except Exception:
                    continue
                for sub in subs:
                    spath = os.path.join(cpath, sub)
                    if not _isdir_timeout(spath, timeout=8):
                        continue
                    key = normalize_name(dir_display_name(sub))
                    if key and key not in index:
                        index[key] = spath
        logger.info(f"源索引建立完成：扫描 {len(drives)} 个盘符，命中 {len(index)} 个目录")
        return index


    # ---------- 台账 ----------

    @staticmethod
    def render_ledger(report: ArchiveReport, extra_note: str = '') -> str:
        """生成注销台账 Markdown（幂等：按 原国籍 分组汇总 + 明细表）"""
        s = report.summary()
        lines = [
            f"# 已注销模特归档台账",
            "",
            f"> 本目录由程序自动维护，收录**已注销**（PornHub 账号被关闭、后续不再更新）"
            f"模特的视频。原属各国文件夹的内容已统一移入此处，以避免污染国家级目录。",
            "",
            f"- 生成时间：{report.finished_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 模式：{'预演（未实际移动）' if report.dry_run else '已执行'}",
            f"- 本次处理：共 {s['total']} 位｜移动 {s['moved']}｜跳过 {s['skipped']}"
            f"｜未找到目录 {s['notfound']}｜失败 {s['failed']}",
            "",
            "> **原国籍**一列的来源（按可靠性排序）：",
            ">",
            "> 1. `retired_origin` 表——首次反查成功后**永久固化**，人工修正优先；",
            "> 1.5 当前 `local_directory` 所在**地区文件夹**（目录尚未搬入「已注销」时，盘符后第一级即原国籍，权重最高；已搬入者自动跳过）；",
            "> 2. 历史下载路径反查（`download_status.target_dir` / `downloads.file_path`，取众数）；",
            "> 3. 标为「未知」者——从未下载过视频、目录已被搬走、且无旧路径可溯。",
            ">",
            "> 如需修正某位的原国籍，直接改 `models.db` 的 `retired_origin` 表"
            "（`UPDATE retired_origin SET original_country='德国' WHERE model_id=123`），"
            "重新生成台账即生效。",
            "",
            "## 按原国籍汇总",
            "",
            "| 原国籍 | 人数 | 视频数 |",
            "|---|---:|---:|",
        ]

        by_country: Dict[str, List[ArchiveItem]] = {}
        for it in report.items:
            by_country.setdefault(it.original_country or '未知', []).append(it)
        for c in sorted(by_country, key=lambda x: (x == '未知', x)):
            grp = by_country[c]
            lines.append(f"| {c} | {len(grp)} | {sum(g.video_count for g in grp)} |")

        lines += [
            "",
            "## 明细",
            "",
            "| 模特 | 原国籍 | 反查票数 | 视频数 | 状态 |",
            "|---|---|---:|---:|---|",
        ]
        for it in sorted(report.items, key=lambda x: ((x.original_country or '未知'), x.name.lower())):
            lines.append(it.as_row())

        lines += [
            "",
            "## 原始目录对照",
            "",
            "| 模特 | 原国籍 | 迁出目录 | 迁入目录 |",
            "|---|---|---|---|",
        ]
        for it in report.items:
            if it.source_dir:
                lines.append(f"| {it.name} | {it.original_country or '未知'} | "
                             f"`{it.source_dir}` | `{it.target_dir}` |")

        if extra_note:
            lines += ["", "## 备注", "", extra_note]

        lines.append("")
        return "\n".join(lines)

    def write_ledger(self, retired_root: str, report: ArchiveReport,
                     extra_note: str = '') -> str:
        """把台账写入 ``已注销/注销台账.md``"""
        os.makedirs(retired_root, exist_ok=True)
        path = os.path.join(retired_root, LEDGER_NAME)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(self.render_ledger(report, extra_note))
        logger.info(f"台账已写入: {path}")
        return path

    # ---------- DB 更新 ----------

    def _update_db_dir(self, model_id: int, new_dir: str) -> bool:
        """更新 DB 里的 local_directory（优先走注入的 adapter）"""
        if self._adapter is not None:
            try:
                return bool(self._adapter.update_model_local_directory(model_id, new_dir))
            except Exception as e:
                logger.error(f"adapter 更新目录失败 id={model_id}: {e}")
                return False
        try:
            con = sqlite3.connect(self.models_db, timeout=30)
            cur = con.cursor()
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute(
                "UPDATE models SET local_directory=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_dir, model_id))
            con.commit()
            n = cur.rowcount
            con.close()
            return n > 0
        except Exception as e:
            logger.error(f"更新 local_directory 失败 id={model_id}: {e}")
            return False

    # ---------- 主流程 ----------

    def scan(self) -> ArchiveReport:
        """只扫描不移动，产出归档计划"""
        rep = ArchiveReport(dry_run=True, started_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        retired_root = self.resolve_retired_root()
        if not retired_root:
            logger.error("无法确定「已注销」归档根目录（地区盘符不可用）")
            return rep
        logger.info(f"归档根目录: {retired_root}")

        # 一次性建立源目录索引（扫描全部 NAS 挂载盘，带超时，避免逐模特重复扫盘）
        src_index = self._build_source_index()

        countries = self.lookup_original_countries()
        # 固化：首次反查成功即写入 retired_origin，此后以固化值为准
        self.save_origin({k: v for k, v in countries.items() if v[0]})
        frozen = self.load_origin_map()
        for m in self.get_retired_models():
            mid = m['id']
            c, votes = countries.get(mid, (None, 0))
            # 固化值无条件优先：固化表可能来自人工修正（可信度高于自动反查），
            # 且目录搬走后运行时反查本就失效。
            if mid in frozen:
                c, votes = frozen[mid]
            it = ArchiveItem(
                model_id=mid, name=m['name'],
                original_country=c, country_evidence=votes,
                target_dir=os.path.join(retired_root, f"[Channel] {m['name']}"),
            )
            src = self.find_source_dir(retired_root, m, src_index)
            if src is None:
                # 已在已注销目录 或 磁盘上确实没有
                if os.path.isdir(it.target_dir):
                    it.status = 'skipped'
                    it.message = '已位于已注销目录'
                    n, b = scan_dir_stats(it.target_dir)
                    it.video_count, it.total_bytes = n, b
                    it.source_dir = it.target_dir
                else:
                    it.status = 'notfound'
                    it.message = '磁盘上未找到目录（可能从未下载过）'
            else:
                it.source_dir = src
                n, b = scan_dir_stats(src)
                it.video_count, it.total_bytes = n, b
            rep.items.append(it)
        return rep

    def run(self, apply: bool = False, write_ledger_file: bool = True) -> ArchiveReport:
        """执行归档

        Args:
            apply: ``False`` = 预演（默认，只报告不改动）
            write_ledger_file: 是否写台账文件
        """
        rep = self.scan()
        rep.dry_run = not apply
        retired_root = self.resolve_retired_root()

        if apply and retired_root:
            os.makedirs(retired_root, exist_ok=True)
            # ⚠ 2026-10-13 修正：旧逻辑遇到一次低内存就 `aborted_lowmem=True`
            # 把整批剩余项全变 pending 并中止——在服务端常驻下载（可用内存长期
            # 1100~1300MB）下等于「几乎永远只搬 1 个就停」，用户观感即「没效果」。
            # 新逻辑改为**单项目级重试**：某项因内存不足时 sleep 让下载侧内存监控
            # 回收后再重试若干次；仍不行才标记 pending 并跳到下一项，整批尽量推进，
            # 最后汇总「因内存不足未处理」的数量，提示用户内存充足后重跑（幂等）。
            mem_skipped = 0
            _MAX_RETRY = 5
            _todo = [it for it in rep.items if it.status not in ('skipped', 'notfound')]
            total_pending = len(_todo)
            done = 0
            for it in rep.items:
                if it.status in ('skipped', 'notfound'):
                    continue
                done += 1
                if not it.source_dir or not os.path.isdir(it.source_dir):
                    it.status = 'notfound'
                    it.message = '源目录已消失'
                    continue
                # 目标已存在且非空：
                #  - 源已消失 → 视为已归档，跳过（避免重复劳动）；
                #  - 源还在   → 上次大概率只搬了一半，清理目标重来（绝不生成 (2) 副本）。
                if os.path.isdir(it.target_dir) and os.listdir(it.target_dir):
                    if not os.path.isdir(it.source_dir):
                        it.status = 'skipped'
                        it.message = '目标已存在且源已消失，视为已归档'
                        continue
                    try:
                        shutil.rmtree(it.target_dir, ignore_errors=True)
                    except OSError:
                        pass
                dst = it.target_dir
                moved = False
                last_err = ''
                for attempt in range(_MAX_RETRY):
                    try:
                        logger.info(
                            f"归档进度 {done}/{total_pending}: 开始移动 {it.name} "
                            f"（{it.source_dir}）")
                        _move_with_timeout(it.source_dir, dst, timeout=_MOVE_TIMEOUT)
                        if self._update_db_dir(it.model_id, dst):
                            it.status = 'moved'
                            it.message = '已归档'
                        else:
                            it.status = 'failed'
                            it.message = '文件已移动，但 DB 路径更新失败（需手工修正）'
                        logger.info(f"归档进度 {done}/{total_pending}: 完成 {it.name}")
                        moved = True
                        break
                    except LowMemoryError as e:
                        # 内存不足：等几秒让下载侧内存监控回收后再试本项
                        last_err = str(e)
                        logger.warning(
                            f"归档 {it.name} 因内存不足暂停（第 {attempt + 1}/{_MAX_RETRY} 次），"
                            f"5s 后重试本项")
                        time.sleep(5)
                        continue
                    except TimeoutError as e:
                        # 单模特移动超时（疑似 NAS 连接卡死）→ 标记失败并跳到下一位，
                        # 不让一个卡死的模特阻塞整批归档；尽力清理半成品目标。
                        try:
                            if os.path.isdir(dst):
                                shutil.rmtree(dst, ignore_errors=True)
                        except OSError:
                            pass
                        it.status = 'failed'
                        it.message = (f'移动超时（>{_MOVE_TIMEOUT}s，疑似 NAS 连接卡死），已跳过；'
                                      f'其余模特继续。可稍后单独重试本模特')
                        logger.error(f"归档超时跳过 {it.name}: {e}")
                        moved = True
                        break
                    except Exception as e:
                        it.status = 'failed'
                        it.message = f"移动失败: {e}"
                        logger.error(f"归档失败 {it.name}: {e}")
                        moved = True  # 非内存/超时错误：不再重试，继续下一项
                        break
                if not moved:
                    mem_skipped += 1
                    it.status = 'pending'
                    it.message = (f'暂因内存不足未处理（{last_err}）；'
                                  f'内存充足后重跑本功能即可继续（幂等）')
            if mem_skipped:
                logger.warning(
                    f"归档完成但有 {mem_skipped} 位因内存不足暂未处理，"
                    f"内存充足后重跑即可继续（幂等）")

        rep.finished_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if write_ledger_file and retired_root:
            note = ('本次由「已注销归档」功能生成。dry-run 模式下表格为预演结果，'
                    '文件未被移动。') if not apply else ''
            rep.ledger_path = self.write_ledger(retired_root, rep, note)

        logger.info(f"归档{'预演' if not apply else '执行'}完成: {rep.summary()}")
        return rep


def main() -> int:
    """命令行入口：``python -m modules.core.utils.retired_archiver [--apply]``"""
    import argparse
    ap = argparse.ArgumentParser(description='已注销模特归档器')
    ap.add_argument('--apply', action='store_true', help='真正执行（默认仅预演）')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    arch = RetiredModelArchiver()

    root = arch.resolve_retired_root()
    if root is None:
        print("\n" + "=" * 60)
        print("无法确定「已注销」归档根目录。")
        print("最常见原因：当前会话看不到 NAS 映射盘（T: U: V: ...）。")
        print("Windows 的网络盘映射是**每个登录会话各一份** —— 程序所在的桌面会话")
        print("能看到，但 SSH / 计划任务 / 服务方式启动的会话看不到。")
        print("")
        print("解决办法：")
        print("  1) 在**桌面**上双击「已注销归档.bat」运行本工具；")
        print("  2) 若桌面也提示盘符不可用，先运行「重建NAS盘符映射.bat」；")
        print("  3) 或等下次打包部署后，直接用程序内的「已注销归档」Tab。")
        print("=" * 60)
        return 2

    rep = arch.run(apply=args.apply)

    s = rep.summary()
    print("\n===== 归档结果 =====")
    print(f"模式: {'预演' if rep.dry_run else '已执行'}")
    print(f"共 {s['total']} 位 | 移动 {s['moved']} | 跳过 {s['skipped']} "
          f"| 未找到 {s['notfound']} | 失败 {s['failed']}")
    if rep.ledger_path:
        print(f"台账: {rep.ledger_path}")
    print("\n--- 明细（前 30）---")
    for it in rep.items[:30]:
        print(f"  {it.status:<9} {it.name[:26]:<28} 原国籍={it.original_country or '未知':<8} "
              f"视频={it.video_count:<5} {it.message}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
