# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
路径工具模块
处理开发环境和打包环境的路径差异
"""
import logging
import os
import shutil
import sys

logger = logging.getLogger(__name__)


def get_app_path() -> str:
    """
    获取应用程序根目录
    
    开发环境：返回项目根目录
    打包环境：返回exe所在目录
    """
    if getattr(sys, 'frozen', False):
        # PyInstaller打包后运行
        return os.path.dirname(sys.executable)
    else:
        # 开发环境运行
        current = os.path.dirname(os.path.abspath(__file__))
        return os.path.dirname(os.path.dirname(os.path.dirname(current)))


def _resolve_user_path(kind: str, *paths) -> str:
    """
    打包环境下解析「用户数据 / 配置」路径（kind 为 'data' 或 'config'）。

    ⚠️ 检查顺序至关重要，不要随意调换：
        `sys._MEIPASS` 是 PyInstaller 的**临时解包目录**，进程退出即被删除。
        若把它的检查放在「可写位置」之前，那么只要 exe 同级目录里缺了那个
        文件（打包漏拷 / 用户误删），程序就会静默地把数据库与配置读写到
        临时目录里的那一份 —— 表面上一切正常，**退出后所有改动全部丢失**。
        而构建时 `datas` 会把整个 `data/`、`config/` 内嵌进 exe，
        所以 `_MEIPASS` 里**必然**存在同名文件，这条路径一定会被命中。

    因此策略固定为：
        1. exe 同级已存在该文件 -> 直接返回（可写，用户既有数据优先）
        2. 否则优先在 exe 同级建立**可写**位置；
           若内置资源里有同名文件，先「种子拷贝」一份过去，
           避免首次运行读到空库（例如 models.db 里的演员目录）
        3. 只有当可写位置**确实建不出来**（只读安装目录等极端情况）时，
           才退回 `_MEIPASS`（只读），保证程序仍能启动而不是直接崩
    """
    exe_dir = os.path.dirname(sys.executable)
    user_path = os.path.join(exe_dir, kind, *paths)

    # 1) 可写位置已有该文件：直接使用
    if os.path.exists(user_path):
        return user_path

    # 记录内置资源（若有），但**不在这里返回**
    internal_path = None
    if hasattr(sys, '_MEIPASS'):
        _candidate = os.path.join(sys._MEIPASS, kind, *paths)
        if os.path.exists(_candidate):
            internal_path = _candidate

    # 2) 建立可写位置
    parent_dir = os.path.dirname(user_path)
    try:
        os.makedirs(parent_dir, exist_ok=True)
        # 无扩展名 -> 调用方是把它当目录用的，一并建出来
        if not os.path.splitext(user_path)[1]:
            os.makedirs(user_path, exist_ok=True)
    except OSError as exc:
        # 3) 可写位置建不出来（只读安装目录等）-> 退回只读内置资源
        if internal_path:
            logger.warning(
                "无法创建可写路径 %s (%s)，本次运行退回只读内置资源 %s；"
                "此模式下产生的改动不会持久化",
                user_path, exc, internal_path,
            )
            return internal_path
        return user_path

    # 种子拷贝：仅当目标是「文件」且内置资源里有同名文件时
    if internal_path and os.path.isfile(internal_path):
        try:
            shutil.copy2(internal_path, user_path)
            logger.info("已从内置资源初始化: %s", user_path)
        except OSError as exc:
            # 拷贝失败不致命：让调用方按「文件尚不存在」自行创建
            logger.warning("从内置资源初始化 %s 失败: %s", user_path, exc)

    return user_path


def get_data_path(*paths) -> str:
    """
    获取数据文件路径
    
    开发环境：项目根目录/data/...
    打包环境：
        - 用户数据在 exe 目录下的 data 目录（可写，**优先**）
        - 内置资源在 _MEIPASS 中（只读，仅作最后兜底）
    
    Args:
        *paths: 相对于data目录的路径组件
        
    Returns:
        完整的数据文件路径
    """
    if getattr(sys, 'frozen', False):
        return _resolve_user_path('data', *paths)

    # ========== 开发环境 ==========
    app_path = get_app_path()
    data_path = os.path.join(app_path, 'data', *paths)

    # 确保目录存在
    try:
        os.makedirs(os.path.dirname(data_path), exist_ok=True)
    except OSError as exc:
        logger.warning("创建数据目录失败: %s", exc)

    return data_path


def get_config_path(*paths) -> str:
    """
    获取配置文件路径
    
    Args:
        *paths: 相对于config目录的路径组件
        
    Returns:
        完整的配置文件路径
    """
    if getattr(sys, 'frozen', False):
        return _resolve_user_path('config', *paths)

    app_path = get_app_path()
    return os.path.join(app_path, 'config', *paths)


def ensure_data_dir(*paths) -> str:
    """
    确保数据目录存在
    
    Args:
        *paths: 相对于data目录的路径组件
        
    Returns:
        完整的数据目录路径
    """
    dir_path = get_data_path(*paths)
    
    # 如果是文件路径，取父目录
    if os.path.splitext(dir_path)[1]:
        dir_path = os.path.dirname(dir_path)
    
    os.makedirs(dir_path, exist_ok=True)
    return dir_path
