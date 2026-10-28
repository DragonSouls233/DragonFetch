# -*- coding: utf-8 -*-
"""发布目录组装：把 exe 旁边的运行期文件准备好（pyinstaller 之后执行）。

产物结构（dist/DragonFetch/）::

    DragonFetch.exe
    _internal/           PyQt6 等依赖（PyInstaller 生成）
    modules/             后端源码（可直接改，改完不用重新打包）
    bin/                 yt-dlp.exe / ffmpeg.exe / ffprobe.exe / ffplay.exe
    assets/icon.ico
    DATA/                运行期数据（空骨架，首启动自动补齐）
    TEMP/  logs/
    使用说明.txt

关键点：**不复制 modules 内的联接/数据目录**（modules/huangguo/DATA
在开发树里是指向根 DATA 的 junction，直接拷贝会把几百 MB 运行期数据带进
发布包）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "DragonFetch"

SKIP_DIR_NAMES = {"__pycache__", ".git", ".idea", ".vscode", "DATA", "data",
                  "build", "dist"}
SKIP_FILE_SUFFIX = {".pyc", ".pyo", ".log", ".tmp", ".bak"}

BIN_FILES = ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "ffplay.exe")

DATA_SKELETON = [
    "DATA/app",
    "DATA/app/backups",
    "DATA/tangxin/downloads",
    "DATA/huangguo/downloads",
    "DATA/jiuse/downloads",
    "DATA/madouqu/downloads",
    "DATA/multisite/downloads",
    "DATA/sites",
    "DATA/yttiktok/downloads",
    "TEMP/tangxin",
    "TEMP/huangguo",
    "TEMP/jiuse",
    "TEMP/multisite",
    "TEMP/madouqu",
    "TEMP/yttiktok",
    "logs",
]

# 发布包里必须出现的后端包（少了界面会缺页或报「未找到后端」）
REQUIRED_MODULES = (
    "tangxin", "huangguo", "multisite", "sites", "madouqu", "jiuse", "yttiktok",
)


def _is_link(p: Path) -> bool:
    try:
        if p.is_symlink():
            return True
        is_junction = getattr(p, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return False


def _copy_tree(src: Path, dst: Path, verbose: bool = False) -> tuple[int, int]:
    """复制目录，跳过联接、缓存与数据目录。返回 (文件数, 字节数)。"""
    files = size = 0
    for base, dirs, names in os.walk(src):
        base_p = Path(base)
        keep = []
        for d in dirs:
            dp = base_p / d
            if d in SKIP_DIR_NAMES or _is_link(dp):
                continue
            keep.append(d)
        dirs[:] = keep

        rel = base_p.relative_to(src)
        target = dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            if Path(name).suffix.lower() in SKIP_FILE_SUFFIX:
                continue
            sp = base_p / name
            if _is_link(sp) or not sp.is_file():
                continue
            tp = target / name
            try:
                shutil.copy2(sp, tp)
                files += 1
                size += sp.stat().st_size
                if verbose:
                    print(f"  + {tp.relative_to(DIST)}")
            except OSError as exc:
                print(f"  ! 跳过 {sp}: {exc}")
    return files, size


def _copy_bins() -> tuple[int, int]:
    src_dir = ROOT / "bin"
    dst_dir = DIST / "bin"
    dst_dir.mkdir(parents=True, exist_ok=True)
    files = size = 0
    if not src_dir.is_dir():
        print("  ! 未找到 bin/，跳过分发二进制（yt-dlp 需用户自备）")
        return 0, 0
    for name in BIN_FILES:
        src = src_dir / name
        if not src.exists():
            continue
        dst = dst_dir / name
        try:
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dst)
            files += 1
            size += src.stat().st_size
        except OSError as exc:
            print(f"  ! 复制 {name} 失败：{exc}")
    return files, size


def _write_readme() -> None:
    from launcher.core.version import APP_VERSION

    text = f"""DragonFetch 统一下载工作台  v{APP_VERSION}
============================================

启动：双击 DragonFetch.exe

目录说明
--------
modules\\   后端代码（糖心 / 黄果 / 九色 / 多站点 / 麻豆 / 映取）。可直接修改，重启生效。
bin\\       共享二进制：yt-dlp.exe、ffmpeg.exe、ffprobe.exe、ffplay.exe
DATA\\      全部运行期数据（数据库、下载、缓存、设置）。**备份这个目录即可。**
TEMP\\      临时文件，可随时清空。
logs\\      前端运行日志。

常见问题
--------
1) 提示「未找到后端代码目录」
   请保持 exe 与 modules、bin、DATA 在同一级，不要只拷 exe。

2) 提示缺少 VCRUNTIME140.dll / api-ms-win-*
   安装「Microsoft Visual C++ 2015-2022 运行库(x64)」。

3) 杀软报毒
   本程序用 PyInstaller 打包，未签名，部分杀软会误报。可加入信任目录。

4) 想放到别的盘 / 让数据与程序分离
   设环境变量 DF_ROOT 指向程序目录即可（数据仍在其下 DATA）。

5) 升级后端
   直接替换 modules 文件夹，不必重新打包 exe。
"""
    (DIST / "使用说明.txt").write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    # --dist 必须真正生效：_copy_bins / _write_readme 直接读模块级 DIST，
    # 以前只有 _copy_tree 用了参数，导致自定义产物目录时 bin 与说明文件
    # 仍写进默认的 dist\DragonFetch（产物里 ffmpeg / yt-dlp 全部缺失）。
    global DIST

    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(DIST))
    ap.add_argument("--no-bin", action="store_true", help="不复制 bin/（小体积分发）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    dist = Path(args.dist)
    DIST = dist
    if not (dist / "DragonFetch.exe").exists():
        print(f"[!] 未找到 {dist / 'DragonFetch.exe'}，请先运行 PyInstaller")
        return 1

    print(f"[1/4] 复制后端源码 modules/ -> {dist / 'modules'}")
    n, size = _copy_tree(ROOT / "modules", dist / "modules", args.verbose)
    print(f"      {n} 个文件，{size / 1024 / 1024:.1f} MB")
    missing = [m for m in REQUIRED_MODULES if not (dist / "modules" / m).is_dir()]
    if missing:
        print(f"      [x] 缺少后端包：{', '.join(missing)}")
        return 1

    print("[2/4] 复制二进制 bin/")
    if args.no_bin:
        print("      已跳过（--no-bin）")
    else:
        n2, size2 = _copy_bins()
        print(f"      {n2} 个文件，{size2 / 1024 / 1024:.1f} MB")
        if n2 == 0:
            print("      [x] 一个二进制都没复制成功，请检查根目录 bin/")
            return 1
        miss_bin = [b for b in BIN_FILES if not (dist / "bin" / b).exists()]
        if miss_bin:
            print(f"      [!] 缺少：{', '.join(miss_bin)}（对应功能会不可用）")

    print("[3/4] 复制图标与文档")
    assets = dist / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for name in ("icon.ico", "icon.png"):
        src = ROOT / "assets" / name
        if src.exists():
            shutil.copy2(src, assets / name)
    _write_readme()

    print("[4/4] 建立 DATA / TEMP / logs 骨架")
    for rel in DATA_SKELETON:
        (dist / rel).mkdir(parents=True, exist_ok=True)

    total = sum(f.stat().st_size for f in dist.rglob("*") if f.is_file())
    print(f"\n完成：{dist}")
    print(f"总大小：{total / 1024 / 1024:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
