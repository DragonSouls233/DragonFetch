# -*- coding: utf-8 -*-
"""统一路径解析：源码运行与打包运行（PyInstaller）行为一致。

约定（与 README 一致）：
  <ROOT>/DATA/<module>/  运行期数据（DB、缓存、下载）
  <ROOT>/TEMP/<module>/  临时文件
  <ROOT>/bin/            共享二进制（yt-dlp / ffmpeg / ffprobe / ffplay）
  <ROOT>/logs/           前端日志
  <ROOT>/modules/        后端源码（打包后也放在 exe 旁边，保持可读可修）

``ROOT`` 的判定：
  * 源码运行      → 仓库根目录（launcher/core/paths.py 上两级）
  * 打包运行      → exe 所在目录（onedir 时 exe 与 modules/bin/DATA 同级）
  * 环境变量      → ``DF_ROOT`` 优先（便于绿色版放到任意位置、或做数据分离）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _detect_root() -> Path:
    env = (os.environ.get("DF_ROOT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        # onedir：sys.executable 在发布目录根部；onefile：同样取 exe 所在目录，
        # 真正的运行期数据因此始终落在用户可见的位置，而不是临时解包目录。
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


ROOT = _detect_root()
DATA = ROOT / "DATA"
TEMP = ROOT / "TEMP"
BIN = ROOT / "bin"
LOGS = ROOT / "logs"
MODULES = ROOT / "modules"

APP_DATA = DATA / "app"

FROZEN = bool(getattr(sys, "frozen", False))


def ensure_dirs() -> None:
    for p in (DATA, TEMP, BIN, LOGS, APP_DATA):
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def data_dir(module: str, *parts: str, create: bool = True) -> Path:
    p = DATA / module
    for extra in parts:
        p = p / extra
    if create:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return p


def temp_dir(module: str, *parts: str, create: bool = True) -> Path:
    p = TEMP / module
    for extra in parts:
        p = p / extra
    if create:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return p


def bin_path(name: str) -> str:
    """返回 bin 下可执行文件的绝对路径；不存在时回退到 PATH。"""
    exe = BIN / name
    if exe.exists():
        return str(exe)
    if not name.lower().endswith(".exe") and os.name == "nt":
        exe2 = BIN / f"{name}.exe"
        if exe2.exists():
            return str(exe2)
    import shutil
    return shutil.which(name) or name


def app_file(name: str) -> Path:
    try:
        APP_DATA.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return APP_DATA / name


def rel(path: str | os.PathLike) -> str:
    """把绝对路径转成相对 ROOT 的展示形式，便于 UI 里显示得短一些。"""
    try:
        p = Path(path).resolve()
        return str(p.relative_to(ROOT))
    except Exception:
        return str(path)


def describe() -> dict:
    """打包/诊断页展示用。"""
    return {
        "root": str(ROOT),
        "frozen": FROZEN,
        "executable": sys.executable,
        "data": str(DATA),
        "bin": str(BIN),
        "modules": str(MODULES),
        "python": sys.version.split()[0],
    }
