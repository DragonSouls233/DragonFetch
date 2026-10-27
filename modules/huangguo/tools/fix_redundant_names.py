# -*- coding: utf-8 -*-
"""把落盘目录/文件里带 SEO 冗余词的名字改回干净名（安全·可恢复版）。

背景：站点会给同一部剧套上「全集在线观看」「在线看」等 SEO 后缀。程序新下载时
已经用 core.utils.normalize_title 归一化，但**历史已落盘**的目录名/文件名里
仍留着这些后缀，例如：
    换脸\\AI换脸 刘亦菲 高清 性爱视频\\        <- 目录名带尾巴
    huangguoai\\疯狂小杨哥\\Season 01\\
        疯狂小杨哥全集在线观看 S01E02.mp4       <- 文件名带尾巴
    huangguoai\\阳光合租屋 暧昧同居后宫在线看\\  <- 目录名带尾巴

做法（全程只用「移动」，从不删除，全部可恢复）：
  * **目录改名**：<root>/<带尾巴目录> -> <root>/<干净目录>。
      目标已存在 -> 把源目录里的内容**合并**进目标（同名冲突移入回收），再把空壳移入回收。
  * **文件改名**：只对文件名里的**剧名部分**做归一化，保留 "S01E02" 之类的集数后缀
      与扩展名。目标已存在 -> 移入回收（若大小相同算重复件；不同则保留副本，绝不覆盖）。
  * 回收文件夹：<root 的上级>/_命名清理_YYYYMMDD_HHMMSS/

⚠ 只改「确定是 SEO 冗余」的名字。core.utils.normalize_title 保守，
  「视频」「高清」「在线」等在描述词位置是**实义**（如 "AI换脸 …性爱视频"），
  不会被改动，因此实义剧名绝对安全。

默认只预览（dry-run），加 --apply 才真正执行。

用法：
    <venv>/python.exe tools/fix_redundant_names.py                    # 预览全部
    <venv>/python.exe tools/fix_redundant_names.py --apply            # 执行
    <venv>/python.exe tools/fix_redundant_names.py --root "J:\\AI剧"  # 指定根
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hgcore.utils import normalize_title   # noqa: E402

# 集数后缀：S01E02 / S01E02-E03 / 之类，出现在剧名后面
_EP_SUFFIX = re.compile(r"^(.*?)(\s+S\d{1,2}E\d{1,3}(?:-E?\d{1,3})?)$")
# 这些扩展名是元数据/附件，剧名在其 stem 里
_META_EXT = {".nfo", ".jpg", ".jpeg", ".png", ".json", ".srt", ".ass", ".txt"}
# 明确不参与改名的文件
_SKIP_NAMES = {"Thumbs.db", "library.json"}


def _norm_dir_name(name: str) -> str:
    """目录名归一化。"""
    return normalize_title(name)


def _norm_file_name(stem: str, ext: str) -> str:
    """文件名归一化：只处理「剧名部分」，保留 " S01E02" 集数后缀。

    "疯狂小杨哥全集在线观看 S01E02" -> "疯狂小杨哥 S01E02"
    "AI换脸 刘亦菲 高清 性爱视频"     -> 不变（实义）
    """
    if not stem or stem in _SKIP_NAMES:
        return stem
    # 先拆出集数后缀
    m = _EP_SUFFIX.match(stem)
    if m:
        head, tail = m.group(1), m.group(2)
        new_head = normalize_title(head)
        return f"{new_head}{tail}" if new_head else stem
    # 无集数后缀：整体归一化（单集类作品、nfo、jpg 等）
    new_stem = normalize_title(stem)
    if not new_stem:
        return stem
    # 元数据文件（如 S01E01.nfo）不含剧名，跳过
    if ext.lower() in _META_EXT and re.fullmatch(r"S\d{1,2}E\d{1,3}", stem):
        return stem
    return new_stem


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return -1


def _unique(dst: Path) -> Path:
    """目标已存在时退避取 "name (2).ext"，确保不覆盖。"""
    if not dst.exists():
        return dst
    n = 2
    while True:
        cand = dst.with_name(f"{dst.stem} ({n}){dst.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _mv(src: Path, dst: Path, retries: int = 3) -> None:
    """同盘移动（os.rename）。绝不触发删除；失败重试若干次。"""
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


def _move_into(src: Path, qroot: Path, rel_hint: str) -> bool:
    """把文件移入回收目录（保留原名，必要时加序号）。"""
    try:
        target = qroot / rel_hint
        target = _unique(target)
        _mv(src, target)
        return True
    except OSError as e:
        print(f"      [移入回收失败] {src.name}: {e}")
        return False


def _merge_dir_into(src: Path, dst: Path, qroot: Path, apply: bool,
                    stats: dict) -> None:
    """把 src 目录的内容合并进 dst（dst 已存在），再把空壳 src 移入回收。"""
    for cur, _subs, files in os.walk(src):
        for f in files:
            s = Path(cur) / f
            rel = s.relative_to(src)
            d = dst / rel
            if not d.exists():
                if apply:
                    try:
                        _mv(s, d)
                    except OSError as e:
                        print(f"      [移动失败] {rel}: {e}")
                        stats["failed"] += 1
                        continue
                stats["moved"] += 1
                print(f"      [待移动] {rel}")
            else:
                # 同名冲突 -> 移入回收
                if apply:
                    if _move_into(s, qroot, f"{src.name}/{rel}"):
                        stats["quarantined"] += 1
                    else:
                        stats["failed"] += 1
                else:
                    stats["quarantined"] += 1
                print(f"      [待移入回收·同名] {rel}")
    # 收尾：把（已空的）源目录移入回收
    if apply:
        try:
            q = _unique(qroot / f"_空目录_{src.name}")
            _mv(src, q)
            stats["shell_moved"] += 1
        except OSError as e:
            print(f"    [空目录移入回收失败] {src}: {e}")
            stats["failed"] += 1
    else:
        stats["shell_moved"] += 1
    print(f"    [待移入回收·空壳] {src.name}")


def scan_and_fix(root: Path, apply: bool, qroot: Path, stats: dict) -> None:
    """扫描 root 下的**一级子目录**（站点目录/分类目录），处理其中的剧目录。"""
    if not root.is_dir():
        print(f"[跳过] 目录不存在: {root}")
        return

    # 站点目录 / 分类目录（下面才是剧目录）
    containers = [d for d in root.iterdir()
                  if d.is_dir() and not d.name.startswith("_")]
    for cont in sorted(containers, key=lambda p: p.name):
        try:
            dramas = [d for d in cont.iterdir() if d.is_dir()]
        except OSError:
            continue
        for drama in sorted(dramas, key=lambda p: p.name):
            new_name = _norm_dir_name(drama.name)
            if new_name and new_name != drama.name:
                target = cont / new_name
                print(f"\n  [目录改名] {drama.name!r} -> {new_name!r}")
                stats["dir_renamed"] += 1
                if target.exists():
                    print(f"      目标已存在 -> 合并内容")
                    if apply:
                        _merge_dir_into(drama, target, qroot, apply, stats)
                    else:
                        # 预览：统计会移动多少
                        for cur, _s, files in os.walk(drama):
                            for f in files:
                                rel = (Path(cur) / f).relative_to(drama)
                                t = target / rel
                                if t.exists():
                                    stats["quarantined"] += 1
                                else:
                                    stats["moved"] += 1
                        stats["shell_moved"] += 1
                else:
                    if apply:
                        try:
                            _mv(drama, target)
                            stats["moved"] += 1
                        except OSError as e:
                            print(f"      [改名失败] {e}")
                            stats["failed"] += 1
                    else:
                        stats["moved"] += 1

            # 处理该剧目录内的文件名
            for cur, _subs, files in os.walk(drama):
                for f in files:
                    if f in _SKIP_NAMES:
                        continue
                    fp = Path(cur) / f
                    stem, ext = os.path.splitext(f)
                    new_stem = _norm_file_name(stem, ext)
                    if not new_stem or new_stem == stem:
                        continue
                    dst = fp.with_name(new_stem + ext)
                    print(f"    [文件改名] {f!r} -> {new_stem + ext!r}")
                    stats["file_renamed"] += 1
                    if dst.exists():
                        same = _size(dst) == _size(fp)
                        tag = "同名·内容相同" if same else "同名·内容不同"
                        if apply:
                            if _move_into(fp, qroot, f"{drama.name}/{f}"):
                                stats["quarantined"] += 1
                            else:
                                stats["failed"] += 1
                        else:
                            stats["quarantined"] += 1
                        print(f"      [待移入回收·{tag}]")
                    else:
                        if apply:
                            try:
                                _mv(fp, dst)
                                stats["moved"] += 1
                            except OSError as e:
                                print(f"      [改名失败] {e}")
                                stats["failed"] += 1
                        else:
                            stats["moved"] += 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把落盘目录/文件里带 SEO 冗余词的名字改回干净名（可恢复）")
    ap.add_argument("--root", default="", help="下载根目录，默认取 config.DOWNLOAD_DIR")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    ap.add_argument("--quarantine", default="", help="回收目录（默认在 root 上级）")
    args = ap.parse_args()

    if args.root:
        root = Path(args.root)
    else:
        from hgcore import config
        root = Path(config.DOWNLOAD_DIR)

    qroot = Path(args.quarantine) if args.quarantine else \
        root.parent / f"_命名清理_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    stats = {"dir_renamed": 0, "file_renamed": 0, "moved": 0,
             "quarantined": 0, "shell_moved": 0, "failed": 0}

    print("=" * 74)
    print("  清理落盘命名里的 SEO 冗余词（如「全集在线观看」「在线看」）")
    print("=" * 74)
    print(f"下载根目录：{root}")
    print(f"回收目录  ：{qroot}")
    print(f"模式      ：{'执行' if args.apply else '预览（加 --apply 才动手）'}")
    print("-" * 74)

    scan_and_fix(root, args.apply, qroot, stats)

    print("-" * 74)
    print("汇总：")
    print(f"  目录改名        ：{stats['dir_renamed']}")
    print(f"  文件改名        ：{stats['file_renamed']}")
    print(f"  实际移动        ：{stats['moved']}")
    print(f"  移入回收        ：{stats['quarantined']}")
    print(f"  空目录壳移入回收：{stats['shell_moved']}")
    print(f"  失败            ：{stats['failed']}")
    if not args.apply:
        print("\n以上为预览。确认无误后加 --apply 执行。")
    else:
        print(f"\n完成。移入回收的文件在：{qroot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
