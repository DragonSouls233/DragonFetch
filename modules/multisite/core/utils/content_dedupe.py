# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""内容级去重 —— 同一视频跨站点重复落盘的检测

为什么需要
==========
本项目已注册 **13 个站点**（``modules/sites/registry.py`` 的 ``SITES``），
同一部视频从 PornHub / XVideos / XNXX / Eporner 等站各下一份是**结构性必然**。

现有两层去重都盖不住这种情况：

=================  ==========================================  ==================
层级               位置                                        局限
=================  ==========================================  ==================
URL 级             ``download_manager.py:583``                 只认 URL **完全相同**
文件名级           ``download_manager.py:1254``                只认**文件名**相同
内容级             ← **本模块**                                —
=================  ==========================================  ==================

跨站点时 URL 不同、各站自己的命名也不同 → **同一个视频双双落盘**。

算法
====
指纹 = **文件大小** + **首 64 KB 的 sha1** + **尾 64 KB 的 sha1**

* 为什么不是"只比大小"：KVS 系镜像站会重新封装容器，大小可能只差几字节。
* 为什么不做全文件哈希：1 GB 文件全量哈希要几分钟；首尾各 64 KB **<50 ms/文件**，
  可覆盖 >99% 的真实重复 —— 重编码/换封装改的是中间帧与头部，首尾通常逐字节一致。

> 算法移植自 ``G:\\MDCX\\.references\\GitHub\\harvestr-main\\dedupe.py``（179 行，**MIT**）。
> 依本项目 GPL-3.0-or-later 与 MIT 的兼容关系，可自由取用。

⚠️ 本模块**只读**
================
当前**唯一支持**的模式是 **dry-run**（只出报告，不碰任何文件）。

原因：
1. 删除涉及回收站 / 网络盘（``O:`` ``N:`` ``M:`` 都是映射盘，回收站机制在上面**不可用**）
2. 宿主有「批量删除守卫」：一次性删除超过约 50 个文件会**直接终止进程**（不是抛异常）
3. 删除策略必须与「视频完整性校验」（``video_repair`` 路线）以及磁盘保留策略
   **统一设计**，否则三者会互相打架

用法
====
.. code-block:: bash

    # 扫全部已配置盘符，出文本报告
    python -m modules.core.utils.content_dedupe --all-roots

    # 只扫某个盘 / 某个模特
    python -m modules.core.utils.content_dedupe --root "O:\\" --root "N:\\"

    # 输出 JSON（便于后续处理）
    python -m modules.core.utils.content_dedupe --all-roots --json > dup.json

    # 调阈值
    python -m modules.core.utils.content_dedupe --all-roots --min-size-mb 50
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .download_artifacts import (
    INCOMPLETE_SUFFIXES as _INCOMPLETE_SUFFIXES,
    SKIP_DIR_NAMES as _SKIP_DIR_NAMES,
    VIDEO_EXTENSIONS as _VIDEO_EXTENSIONS,
    is_incomplete_download,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MIN_SIZE_BYTES",
    "HEAD_BYTES",
    "TAIL_BYTES",
    "VIDEO_EXTENSIONS",
    "SKIP_DIR_NAMES",
    "file_fingerprint",
    "pick_keeper",
    "scan_roots",
    "find_duplicate_groups",
    "build_report",
    "format_report_text",
    "get_configured_roots",
    "DuplicateGroup",
]

#: 小于此大小的文件直接跳过（预告片、花絮、误下的碎片 → 噪声）
MIN_SIZE_BYTES: int = 1_000_000
#: 取头部多少字节参与指纹
HEAD_BYTES: int = 64 * 1024
#: 取尾部多少字节参与指纹
TAIL_BYTES: int = 64 * 1024

#: 参与去重的视频扩展名（其余一律不看）—— 与完整性校验共用同一份定义
VIDEO_EXTENSIONS = _VIDEO_EXTENSIONS

#: 扫描时跳过的目录名
SKIP_DIR_NAMES = _SKIP_DIR_NAMES

#: 进行中的下载产物后缀（保留导出，便于外部引用）
INCOMPLETE_SUFFIXES = _INCOMPLETE_SUFFIXES


@dataclass
class DuplicateGroup:
    """一组内容相同的文件。"""

    fingerprint: str
    size_bytes: int
    files: List[Path] = field(default_factory=list)
    keeper: Optional[Path] = None
    removers: List[Path] = field(default_factory=list)
    #: 这些文件是否跨多个模特目录（跨目录重复通常更值得关注）
    model_dirs: List[str] = field(default_factory=list)

    @property
    def wasted_bytes(self) -> int:
        """若删掉 removers 可回收的字节数。"""
        return self.size_bytes * len(self.removers)

    @property
    def is_cross_model(self) -> bool:
        """是否跨模特目录（同一模特目录内的重复多半是重名下载）。"""
        return len(self.model_dirs) > 1

    def to_dict(self) -> Dict:
        return {
            "fingerprint": self.fingerprint,
            "size_bytes": self.size_bytes,
            "size_mb": round(self.size_bytes / 1024 / 1024, 2),
            "copies": len(self.files),
            "cross_model": self.is_cross_model,
            "model_dirs": self.model_dirs,
            "keeper": str(self.keeper) if self.keeper else None,
            "removers": [str(p) for p in self.removers],
            "wasted_bytes": self.wasted_bytes,
            "wasted_mb": round(self.wasted_bytes / 1024 / 1024, 2),
        }


#: 「进行中产物」判定 —— 与 video_integrity 共用同一份实现（见 download_artifacts）
_is_incomplete = is_incomplete_download


def file_fingerprint(path: Path,
                     min_size_bytes: int = MIN_SIZE_BYTES) -> Optional[str]:
    """计算内容指纹 ``大小:首64K哈希:尾64K哈希``。

    返回 ``None`` 表示该文件不适合参与去重
    （读不到、太小、非视频扩展名）。

    Args:
        path: 目标文件
        min_size_bytes: 小于此大小直接返回 None。
            必须由调用方把 ``--min-size-mb`` 传进来，否则命令行调小阈值会失效。

    Returns:
        str | None: 指纹字符串；None = 跳过
    """
    try:
        st = path.stat()
    except OSError:
        logger.debug("取文件状态失败，跳过: %s", path, exc_info=True)
        return None

    size = st.st_size
    if size < min_size_bytes:
        return None

    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_BYTES)
            if size > HEAD_BYTES + TAIL_BYTES:
                f.seek(-TAIL_BYTES, os.SEEK_END)
                tail = f.read(TAIL_BYTES)
            else:
                # 小文件：head 已覆盖全文，尾块置空（避免重复计入同一段）
                tail = b""
    except OSError:
        logger.debug("读取文件内容失败，跳过: %s", path, exc_info=True)
        return None

    h1 = hashlib.sha1(head).hexdigest()[:16]
    h2 = hashlib.sha1(tail).hexdigest()[:16] if tail else "0"
    return f"{size}:{h1}:{h2}"


def pick_keeper(files: Sequence[Path]) -> Path:
    """从一组重复文件中挑一个保留。

    策略（与 harvestr/dedupe.py 一致）：
    1. **文件名最长**的（通常标题最完整、最具描述性）
    2. 长度相同则保留 **mtime 最早**的（大概是最先下到的）

    为什么不是"保留新的"：文件名短的多半是被截断/简化的版本；
    而"先下的那个"更可能是原始文件。
    """
    def _key(p: Path) -> Tuple[int, float]:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = float("inf")
        return (-len(p.name), mtime)

    return sorted(files, key=_key)[0]


def _iter_candidate_files(root: Path, recursive: bool = True) -> Iterable[Path]:
    """遍历 root 下所有**可能**参与去重的文件（已排除跳过目录/进行中产物）。"""
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            # 原地过滤，避免 os.walk 白挖这些目录
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES
                           and not d.startswith("_")]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                if _is_incomplete(p):
                    continue
                yield p
    else:
        try:
            entries = list(root.iterdir())
        except OSError:
            logger.debug("列目录失败: %s", root, exc_info=True)
            return
        for p in entries:
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and not _is_incomplete(p):
                yield p


def scan_roots(
    roots: Sequence[str | Path],
    recursive: bool = True,
    min_size_bytes: int = MIN_SIZE_BYTES,
    progress_every: int = 2000,
) -> Tuple[Dict[str, List[Path]], Dict[str, int]]:
    """扫描若干根目录，按指纹分组。

    Args:
        roots: 根目录列表（形如 ``O:\\`` 或 ``O:\\某模特``）
        recursive: 是否递归
        min_size_bytes: 小于此大小跳过
        progress_every: 每处理多少个文件打一条 INFO 进度日志

    Returns:
        (指纹 -> 文件列表, 统计字典)
    """
    groups: Dict[str, List[Path]] = defaultdict(list)
    stats = {
        "scanned_files": 0,
        "skipped_small": 0,
        "skipped_unreadable": 0,
        "roots_missing": 0,
        "total_bytes": 0,
    }

    for raw_root in roots:
        root = Path(raw_root)
        if not root.exists():
            stats["roots_missing"] += 1
            logger.warning("去重扫描：根目录不存在，跳过: %s", root)
            continue
        logger.info("去重扫描：开始 %s", root)

        for p in _iter_candidate_files(root, recursive=recursive):
            stats["scanned_files"] += 1
            if stats["scanned_files"] % progress_every == 0:
                logger.info("去重扫描进度：已处理 %d 个文件（唯一指纹 %d 个）",
                            stats["scanned_files"], len(groups))

            try:
                size = p.stat().st_size
            except OSError:
                stats["skipped_unreadable"] += 1
                continue
            if size < min_size_bytes:
                stats["skipped_small"] += 1
                continue

            fp = file_fingerprint(p, min_size_bytes=min_size_bytes)
            if fp is None:
                stats["skipped_unreadable"] += 1
                continue

            groups[fp].append(p)
            stats["total_bytes"] += size

    return dict(groups), stats


def _model_dir_of(path: Path) -> str:
    """取文件所属的"模特目录"名（= 上一级目录名）。"""
    try:
        return path.parent.name
    except Exception:
        return ""


def find_duplicate_groups(
    roots: Sequence[str | Path],
    recursive: bool = True,
    min_size_bytes: int = MIN_SIZE_BYTES,
    include_same_dir: bool = True,
) -> Tuple[List[DuplicateGroup], Dict[str, int]]:
    """找出所有内容重复的组（**只读**）。

    Args:
        roots: 根目录列表
        recursive: 是否递归
        min_size_bytes: 大小阈值
        include_same_dir: 是否包含"同一目录内的重复"。
            设 False 时只返回**跨目录**重复（通常更值得处理 —— 同目录内重名
            多半是同一模特被重复下载，跨目录才是"同一视频挂了两个模特/两个站"）。

    Returns:
        (重复组列表（按浪费空间降序）, 统计字典)
    """
    raw_groups, stats = scan_roots(roots, recursive=recursive,
                                   min_size_bytes=min_size_bytes)

    out: List[DuplicateGroup] = []
    for fp, files in raw_groups.items():
        if len(files) < 2:
            continue
        model_dirs = sorted({_model_dir_of(p) for p in files})
        if len(model_dirs) < 2 and not include_same_dir:
            continue

        # 目录名也参与挑选：优先保留名字最长的文件（跨目录时同样适用）
        keeper = pick_keeper(files)
        removers = [p for p in files if p != keeper]
        try:
            size = keeper.stat().st_size
        except OSError:
            size = 0

        out.append(DuplicateGroup(
            fingerprint=fp,
            size_bytes=size,
            files=sorted(files, key=lambda x: str(x)),
            keeper=keeper,
            removers=sorted(removers, key=lambda x: str(x)),
            model_dirs=model_dirs,
        ))

    out.sort(key=lambda g: g.wasted_bytes, reverse=True)
    stats["duplicate_groups"] = len(out)
    stats["duplicate_files"] = sum(len(g.removers) for g in out)
    stats["wasted_bytes"] = sum(g.wasted_bytes for g in out)
    stats["cross_model_groups"] = sum(1 for g in out if g.is_cross_model)
    return out, stats


def build_report(
    roots: Sequence[str | Path],
    recursive: bool = True,
    min_size_bytes: int = MIN_SIZE_BYTES,
    include_same_dir: bool = True,
    max_groups: int = 0,
) -> Dict:
    """生成可序列化的去重报告（**dry-run，不删任何文件**）。

    Args:
        roots: 根目录列表
        recursive: 是否递归
        min_size_bytes: 大小阈值
        include_same_dir: 是否含同目录重复
        max_groups: >0 时只保留浪费空间最大的前 N 组

    Returns:
        dict: 报告（可直接 json.dumps）
    """
    started = datetime.now()
    groups, stats = find_duplicate_groups(
        roots, recursive=recursive, min_size_bytes=min_size_bytes,
        include_same_dir=include_same_dir)

    if max_groups and len(groups) > max_groups:
        groups = groups[:max_groups]

    return {
        "generated_at": started.isoformat(timespec="seconds"),
        "elapsed_seconds": round((datetime.now() - started).total_seconds(), 2),
        "mode": "dry-run",
        "roots": [str(r) for r in roots],
        "recursive": recursive,
        "min_size_bytes": min_size_bytes,
        "include_same_dir": include_same_dir,
        "stats": stats,
        "groups": [g.to_dict() for g in groups],
        "note": ("仅检测，未删除任何文件。删除需另行决策 —— 涉及回收站/网络盘可用性"
                 "与宿主批量删除守卫，且要与磁盘保留策略统一设计。"),
    }


def format_report_text(report: Dict, max_groups: int = 50) -> str:
    """把报告渲染成人类可读文本。"""
    s = report.get("stats", {})
    lines: List[str] = []
    add = lines.append

    add("=" * 78)
    add("内容级去重报告（dry-run —— 未删除任何文件）")
    add("=" * 78)
    add(f"生成时间      : {report.get('generated_at')}")
    add(f"耗时          : {report.get('elapsed_seconds')}s")
    add(f"扫描根目录    : {', '.join(report.get('roots', []))}")
    add(f"递归          : {report.get('recursive')}")
    add(f"大小阈值      : {report.get('min_size_bytes', 0) / 1024 / 1024:.1f} MB")
    add("-" * 78)
    add(f"扫描文件数    : {s.get('scanned_files', 0)}")
    add(f"跳过(过小)    : {s.get('skipped_small', 0)}")
    add(f"跳过(读不到)  : {s.get('skipped_unreadable', 0)}")
    add(f"根目录不存在  : {s.get('roots_missing', 0)}")
    add(f"★ 重复组数    : {s.get('duplicate_groups', 0)}"
        f"（其中跨模特目录 {s.get('cross_model_groups', 0)} 组）")
    add(f"★ 多余文件数  : {s.get('duplicate_files', 0)}")
    add(f"★ 可回收空间  : {s.get('wasted_bytes', 0) / 1024 / 1024 / 1024:.2f} GB")
    add("-" * 78)

    groups = report.get("groups", [])
    if not groups:
        add("没有发现重复文件。")
    else:
        add(f"重复组（按可回收空间降序，最多显示 {max_groups} 组）：")
        for i, g in enumerate(groups[:max_groups], 1):
            tag = "跨模特" if g.get("cross_model") else "同目录"
            add("")
            add(f"[{i}] {g.get('size_mb')} MB x {g.get('copies')} 份"
                f"  可回收 {g.get('wasted_mb')} MB  ({tag})")
            add(f"    保留: {g.get('keeper')}")
            for r in g.get("removers", []):
                add(f"    多余: {r}")
        if len(groups) > max_groups:
            add("")
            add(f"... 还有 {len(groups) - max_groups} 组未显示（用 --json 拿全量）")

    add("=" * 78)
    add("⚠️ 本次为 dry-run。删除需另行决策：网络盘回收站不可用、宿主有批量删除守卫、")
    add("   且删除策略必须与「视频完整性校验」「磁盘保留策略」统一设计。")
    add("=" * 78)
    return "\n".join(lines)


def get_configured_roots() -> List[str]:
    """取本项目**已配置的落盘根目录**去重后的列表。

    来源：
    1. ``disk_region_config`` 里各地区绑定的盘符（形如 ``N:\\`` ``O:\\`` ``M:\\``）
    2. 兜底：``download_dir`` 配置项

    Returns:
        list[str]: 去重后的根目录（不存在于磁盘的也返回，交给扫描阶段报告）
    """
    roots: List[str] = []
    seen = set()

    def _add(v: Optional[str]) -> None:
        if not v:
            return
        try:
            norm = os.path.normcase(os.path.abspath(str(v)))
        except Exception:
            norm = str(v)
        if norm in seen:
            return
        seen.add(norm)
        roots.append(str(v))

    try:
        from .disk_region_manager import get_disk_region_manager
        mgr = get_disk_region_manager()
        for region in mgr.get_all_regions():
            _add(mgr.get_region_disk(region))
    except Exception:
        logger.debug("读取地区-盘符配置失败", exc_info=True)

    try:
        from .app_config import get_app_config
        cfg = get_app_config()
        for key in ("download_dir", "output_dir"):
            try:
                _add(cfg.get(key))
            except Exception:
                logger.debug("读取配置项 %s 失败", key, exc_info=True)
    except Exception:
        logger.debug("读取应用配置失败", exc_info=True)

    return roots


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口（**永远只做 dry-run**）。"""
    parser = argparse.ArgumentParser(
        description="内容级去重检测（只读，不删除任何文件）")
    parser.add_argument("--root", action="append", default=[],
                        help="扫描根目录，可重复；不传则配合 --all-roots 或自动取配置")
    parser.add_argument("--all-roots", action="store_true",
                        help="扫描本项目已配置的全部盘符根目录")
    parser.add_argument("--min-size-mb", type=float, default=MIN_SIZE_BYTES / 1024 / 1024,
                        help="小于此大小的文件跳过（默认 1 MB）")
    parser.add_argument("--no-recursive", action="store_true", help="不递归")
    parser.add_argument("--exclude-same-dir", action="store_true",
                        help="只报跨目录重复（同一模特目录内的重复不算）")
    parser.add_argument("--max-groups", type=int, default=0,
                        help="报告里最多保留多少组（>0 生效，按可回收空间降序）")
    parser.add_argument("--show", type=int, default=50, help="文本报告最多打印多少组")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本")
    parser.add_argument("--verbose", action="store_true", help="打开 DEBUG 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    roots: List[str] = list(args.root)
    if args.all_roots or not roots:
        configured = get_configured_roots()
        if not roots:
            roots = configured
        if not roots:
            print("没有可扫描的根目录：请用 --root 指定，或检查 disk_region_config。")
            return 2
        logger.info("自动解析到 %d 个根目录: %s", len(roots), roots)

    report = build_report(
        roots,
        recursive=not args.no_recursive,
        min_size_bytes=int(args.min_size_mb * 1024 * 1024),
        include_same_dir=not args.exclude_same_dir,
        max_groups=args.max_groups,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report_text(report, max_groups=args.show))
    return 0


if __name__ == "__main__":
    sys.exit(main())
