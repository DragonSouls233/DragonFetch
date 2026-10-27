# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
URL查重同步日志模块
专门记录URL查重和标题同步的日志
"""
import os
import logging
from datetime import datetime
from pathlib import Path


def get_sync_logger():
    """
    获取URL查重同步专用日志器
    日志文件保存在 data/logs/url_sync_YYYYMMDD.log
    """
    logger = logging.getLogger('url_sync')
    
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.INFO)
    logger.propagate = False
    
    # 确保日志目录存在
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'logs')
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
    except Exception:
        # 如果获取不到目录，使用当前目录
        log_dir = '.'
    
    # 每天一个日志文件
    today = datetime.now().strftime('%Y%m%d')
    log_file = os.path.join(log_dir, f'url_sync_{today}.log')
    
    # 文件处理器
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    
    # 格式化
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        '%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    
    return logger


def log_url_check(model_name: str, url: str, title: str, is_downloaded: bool, action: str = "CHECK"):
    """
    记录URL查重检查
    
    Args:
        model_name: 模特名称
        url: 视频URL
        title: 视频标题
        is_downloaded: 是否已下载
        action: 操作类型 (CHECK/MATCH/SKIP)
    """
    logger = get_sync_logger()
    status = "已下载" if is_downloaded else "未下载"
    logger.info(f"[{action}] {model_name} | {title[:30]}... | {status} | {url[:50]}...")


def log_title_sync(model_name: str, old_title: str, new_title: str, url: str):
    """
    记录标题同步
    
    Args:
        model_name: 模特名称
        old_title: 旧标题
        new_title: 新标题
        url: 视频URL
    """
    logger = get_sync_logger()
    logger.info(f"[SYNC] {model_name} | {old_title[:30]}... -> {new_title[:30]}... | {url[:50]}...")


def log_download_skip(model_name: str, title: str, url: str, reason: str = "already_downloaded"):
    """
    记录下载跳过
    
    Args:
        model_name: 模特名称
        title: 视频标题
        url: 视频URL
        reason: 跳过原因
    """
    logger = get_sync_logger()
    logger.info(f"[SKIP] {model_name} | {title[:30]}... | 原因: {reason} | {url[:50]}...")


def log_summary(model_name: str, total_checked: int, found_downloaded: int, synced_count: int):
    """
    记录处理摘要
    
    Args:
        model_name: 模特名称
        total_checked: 总检查数
        found_downloaded: 发现已下载数
        synced_count: 同步标题数
    """
    logger = get_sync_logger()
    logger.info(f"[SUMMARY] {model_name} | 检查: {total_checked} | 已下载: {found_downloaded} | 标题同步: {synced_count}")
    logger.info("=" * 80)