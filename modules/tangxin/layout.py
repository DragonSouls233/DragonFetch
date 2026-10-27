# -*- coding: utf-8 -*-
"""运行目录布局：**DB 放 DATA，二进制放 BIN**。

## 目标布局（打包后 = exe 同级目录）
    {RUNTIME_ROOT}/
        TangXinVlog.exe
        bin/                 ← 外部可执行文件（ffmpeg.exe / yt-dlp.exe / xray/）
        data/                ← 数据库与设置（tangxin.db / tangxin_settings.json）
        config/              ← 可改的节点配置（nodes.json）
        logs/                ← 运行日志
        downloads/           ← 成片输出（可在 GUI 里改）
        temp/tangxin/        ← 下载临时目录

## 为什么要有这个模块
以前 DB 与设置散在根目录、外部程序也散在根目录，部署到服务器时要手工
搬一堆文件，漏一个就"能启动但没缓存 / 找不到 ffmpeg"。集中到 data/ 与
bin/ 后：**整目录同步即可**，且目录职责一眼可见。

## 迁移（老版本兼容）
老版本把 ``tangxin_downloads.db`` / ``tangxin_settings.json`` 直接扔在
根目录（或主平台的 data/ 里）。本模块在首次访问 DB / 设置时自动把它们
**迁移**进 ``data/``（老 DB 直接改名成新的 ``tangxin.db``，表结构是超集，
下载历史不会丢）。已存在目标文件则跳过（不覆盖新数据）。
"""
import os
import shutil

from . import config

# 需要从老位置迁进 data/ 的文件
_LEGACY_DB = "tangxin_downloads.db"      # 老版本的下载记录库（现在并入 tangxin.db）
_LEGACY_SETTINGS = "tangxin_settings.json"

_migrated = False


def ensure_dirs():
    """创建运行所需目录（幂等）。"""
    for d in (config.BIN_DIR, config.DATA_DIR, config.LOG_DIR,
              config.TEMP_DIR, config.OUTPUT_BASE):
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


def _legacy_dirs():
    """老位置候选（去重、排除 data/ 自身）。"""
    out = []
    for d in (config.RUNTIME_ROOT,
              os.path.join(config.RUNTIME_ROOT, "data"),
              config.APP_ROOT):
        if not d or not os.path.isdir(d):
            continue
        if os.path.normpath(d) == os.path.normpath(config.DATA_DIR):
            continue
        if os.path.normpath(d) not in [os.path.normpath(x) for x in out]:
            out.append(d)
    return out


def _move_into_data(src: str, dst: str) -> bool:
    """把文件挪进 data/（先改名，失败再复制）。"""
    try:
        os.replace(src, dst)
        return True
    except OSError:
        try:
            shutil.copy2(src, dst)
            return True
        except Exception:
            return False


def migrate_legacy():
    """把老 DB / 设置挪进 ``data/``。返回迁移清单（列表[str]）。

    - 老 ``tangxin_downloads.db`` → 直接改名成 ``tangxin.db``（**表结构是超集**，
      老的 downloads / existing_files 数据原样带过来，不会丢下载历史）。
    - 老 ``tangxin_settings.json`` → 挪进 ``data/``。

    目标已存在时**不覆盖**（新数据优先）。
    """
    global _migrated
    if _migrated:
        return []
    _migrated = True
    moved = []
    data_dir = config.DATA_DIR
    if not data_dir:
        return moved
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        return moved

    new_db = os.path.join(data_dir, "tangxin.db")
    if not os.path.exists(new_db):
        # 两种老名字都要认：tangxin_downloads.db（最早）与 tangxin.db（中间版
        # 落在主平台 data/ 里的那个）
        for legacy_name in (_LEGACY_DB, "tangxin.db"):
            if os.path.exists(new_db):
                break
            for d in _legacy_dirs():
                old_db = os.path.join(d, legacy_name)
                if os.path.isfile(old_db) and _move_into_data(old_db, new_db):
                    moved.append(new_db)
                    break

    new_set = os.path.join(data_dir, _LEGACY_SETTINGS)
    if not os.path.exists(new_set):
        for d in _legacy_dirs():
            old_set = os.path.join(d, _LEGACY_SETTINGS)
            if os.path.isfile(old_set) and _move_into_data(old_set, new_set):
                moved.append(new_set)
                break
    return moved


def data_file(name: str) -> str:
    """``data/`` 下的文件路径（自动确保目录存在 + 触发老文件迁移）。"""
    ensure_dirs()
    migrate_legacy()
    return os.path.join(config.DATA_DIR, name)


def bin_file(*parts) -> str:
    """``bin/`` 下的文件路径。"""
    return os.path.join(config.BIN_DIR, *parts)
