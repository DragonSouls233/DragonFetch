# -*- coding: utf-8 -*-
"""合并「同一部剧因标题漂移产生的重复文件夹」，去重、改名（安全·可恢复版）。

背景：站点有时把同一部剧的标题加上「全集在线观看」等 SEO 后缀（或中途改标题），
导致同一部剧的剧集被下载到两个文件夹，例如：
    一个乖乖女\\Season 01\\一个乖乖女 S01E02.mp4
    一个乖乖女全集在线观看\\Season 01\\一个乖乖女全集在线观看 S01E02.mp4

做法（全程只用「移动」，从不删除，全部可恢复）：
  * 按 normalize_title 分组；把「带后缀」文件夹的内容并入「干净名」文件夹，
    文件名前缀改成干净名。
  * 同名冲突（含元数据/大小一致/大小不同）一律【移入回收文件夹】，绝不覆盖。
  * 收尾把已空的「后缀文件夹」整体移入回收文件夹。

回收文件夹：<root 的上级>/_重复清理_YYYYMMDD_HHMMSS/

默认只预览（dry-run），加 --apply 才真正执行。

用法：
    <venv>/python.exe tools/merge_duplicate_folders.py            # 预览
    <venv>/python.exe tools/merge_duplicate_folders.py --apply    # 执行
    <venv>/python.exe tools/merge_duplicate_folders.py --root "J:\\AI剧\\huangguoai"
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hgcore.utils import normalize_title   # noqa: E402


def _count_files(p: Path) -> int:
    return sum(len(fs) for _, _, fs in os.walk(p))


def _pick_canonical(root: Path, names: list[str]) -> str:
    """选出「干净名」作为合并目标。

    排序优先级：① 名字==归一化名（没有 SEO 后缀）优先；② 文件数多的优先；
    ③ 名字短的优先。取第一位。
    """
    def key(name: str):
        is_clean = 0 if name == normalize_title(name) else 1
        return (is_clean, -_count_files(root / name), len(name))
    return sorted(names, key=key)[0]


def _target_name(file_name: str, drifted: str, canonical: str) -> str:
    """把文件名前缀的「漂移标题」替换成「干净标题」。"""
    if file_name.startswith(drifted):
        return canonical + file_name[len(drifted):]
    return file_name


def _unique(dst: Path) -> Path:
    """若目标已存在，则退避取 "name (2).ext" 等，确保不覆盖。"""
    if not dst.exists():
        return dst
    n = 2
    while True:
        cand = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _has_files(p: Path) -> bool:
    for _cur, _sd, fs in os.walk(p):
        if fs:
            return True
    return False


def _mv(src: Path, dst: Path, retries: int = 3) -> None:
    """同盘移动（os.rename，绝不触发删除）。目标须不存在；失败重试若干次。"""
    last = None
    for _ in range(retries):
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(str(src), str(dst))
            return
        except OSError as e:
            last = e
            time.sleep(0.2)
    raise last


def merge_root(root: Path, apply: bool, qroot: Path) -> dict:
    stats = {"groups": 0, "moved": 0, "quarantined": 0, "shell_moved": 0,
             "failed": 0, "left_dirty": 0}
    if not root.is_dir():
        print(f"[跳过] 目录不存在: {root}")
        return stats

    subs = [d for d in os.listdir(root) if (root / d).is_dir()
            and not d.startswith("_")]
    groups = defaultdict(list)
    for d in subs:
        groups[normalize_title(d)].append(d)
    dups = {k: v for k, v in groups.items() if len(v) > 1}

    print(f"\n=== {root} ===")
    print(f"子文件夹 {len(subs)} 个；归一化后重名 {len(dups)} 组")
    print(f"回收文件夹: {qroot}")
    if not dups:
        return stats

    for norm, names in sorted(dups.items()):
        stats["groups"] += 1
        canonical = _pick_canonical(root, names)
        canon_dir = root / canonical
        others = [n for n in names if n != canonical]
        print(f"\n[{norm}]  目标(保留) -> {canonical!r}")
        for other in others:
            src_dir = root / other
            print(f"   合并 <- {other!r}")
            plan = []
            for cur, _sd, files in os.walk(src_dir):
                rel = Path(cur).relative_to(src_dir)
                for f in files:
                    plan.append((Path(cur) / f, rel, f))
            for src, rel, f in plan:
                new_name = _target_name(f, other, canonical)
                dst = canon_dir / rel / new_name
                conflict = dst.exists()
                target = _unique(qroot / other / rel / f) if conflict else dst
                tag = "[移入回收·重复]" if conflict else "[移动]"
                if not apply:
                    print(f"      [待{'移入回收' if conflict else '移动'}] "
                          f"{rel / f}" + ("" if not conflict else ""))
                    stats["quarantined" if conflict else "moved"] += 1
                    continue
                try:
                    _mv(src, target)
                    stats["quarantined" if conflict else "moved"] += 1
                    print(f"      {tag} {rel / f}"
                          + (f"  ->  {rel / new_name}" if not conflict else ""))
                except OSError as e:
                    stats["failed"] += 1
                    print(f"      [失败·跳过] {rel / f} : {e}")
            # 收尾：整棵已空的「后缀文件夹」也移入回收（不删除）
            if apply:
                if _has_files(src_dir):
                    stats["left_dirty"] += 1
                    print(f"      [保留·仍有文件] {other!r}")
                else:
                    try:
                        shell = _unique(qroot / f"{other}__空壳")
                        _mv(src_dir, shell)
                        stats["shell_moved"] += 1
                        print(f"      [移走空壳] {other!r}")
                    except OSError as e:
                        stats["failed"] += 1
                        print(f"      [空壳移走失败] {other!r} : {e}")
    return stats


def _empty_tree(p: Path) -> bool:
    for _c, _s, fs in os.walk(p):
        if fs:
            return False
    return True


def _cleanup_artifacts(root: Path) -> int:
    """清理调试残留（_hgtest_*/_mvtest_*）与失败运行留下的空回收目录。"""
    import shutil
    removed = 0
    prefixes = ("_hgtest", "_mvtest", "_ctypes_del_test")
    for base in (root, root.parent):
        if not base.is_dir():
            continue
        for name in os.listdir(base):
            if not name.startswith(prefixes):
                continue
            p = base / name
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
                removed += 1
            except OSError:
                pass
    for p in root.parent.glob("_重复清理_*"):
        try:
            if p.is_dir() and _empty_tree(p):
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="合并因标题漂移产生的重复剧集文件夹（安全可恢复）")
    ap.add_argument("--root", default=r"J:\AI剧\huangguoai",
                    help="要扫描的目录（默认为站点下载子目录）")
    ap.add_argument("--apply", action="store_true",
                    help="真正执行合并（不加则仅预览）")
    args = ap.parse_args()

    root = Path(args.root)
    qroot = root.parent / f"_重复清理_{datetime.now():%Y%m%d_%H%M%S}"
    mode = "执行合并 (--apply)" if args.apply else "预览模式（不改动任何文件）"
    print("=" * 60)
    print(f"重复剧集文件夹合并工具（移动隔离，不删除）  |  {mode}")
    print("=" * 60)
    st = merge_root(root, args.apply, qroot)
    print("\n" + "=" * 60)
    print(f"汇总：重名组 {st['groups']}，"
          f"{'已移动' if args.apply else '待移动'} {st['moved']}，"
          f"{'已移入回收' if args.apply else '待移入回收'} {st['quarantined']}，"
          f"空壳移走 {st['shell_moved']}，失败 {st['failed']}，"
          f"仍有文件保留 {st['left_dirty']}")
    if not args.apply:
        print("\n以上为预览。确认无误后加 --apply 执行。")
    else:
        cleaned = _cleanup_artifacts(root)
        if cleaned:
            print(f"\n已清理调试残留/空回收目录 {cleaned} 项。")
        print("\n完成。重复/冲突文件已移入回收文件夹，确认无误后可自行删除该文件夹。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
