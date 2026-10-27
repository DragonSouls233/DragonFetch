# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
下载管理器
支持高并发下载到本地，串行移动到NAS
"""
import os
import asyncio
import logging
import time
from typing import List, Dict, Any
from pathlib import Path

from multisite.core.utils.temp_download_manager import TempDownloadManager

class SimpleDebugger:
    """完全空的调试器实现，避免任何外部引用"""
    def __getattr__(self, name):
        def no_op(*args, **kwargs):
            pass
        return no_op

def get_debugger():
    return SimpleDebugger()

logger = logging.getLogger(__name__)


class SmartDownloadManager:
    """智能下载管理器
    
    特点：
    1. 高并发下载到本地临时目录（SSD）
    2. 串行移动到目标目录（NAS）
    3. 充分利用本地存储速度
    """
    
    def __init__(self, download_concurrency: int = 10, move_concurrency: int = 1):
        """
        初始化下载管理器
        
        Args:
            download_concurrency: 下载并发数（默认10）
            move_concurrency: 移动并发数（默认1，即串行）
        """
        self.download_concurrency = download_concurrency
        self.move_concurrency = move_concurrency
        self.temp_manager = TempDownloadManager()
        self.download_semaphore = asyncio.Semaphore(download_concurrency)
        self.move_semaphore = asyncio.Semaphore(move_concurrency)
        
        # 初始化调试器
        self.debugger = get_debugger()
        self.debugger.info('SMART_DOWNLOAD', '智能下载管理器初始化')
        self.debugger.checkpoint('初始化完成', f'并发下载数: {download_concurrency}, 并发移动数: {move_concurrency}')
        
    async def download_and_move(self, video_url: str, video_title: str, 
                               model_id: int, model_name: str, 
                               target_dir: str, download_config: dict,
                               progress_callback=None) -> Dict[str, Any]:
        """
        下载并移动文件
        
        Args:
            video_url: 视频URL
            video_title: 视频标题
            model_id: 模特ID
            model_name: 模特名
            target_dir: 目标目录
            download_config: 下载配置
            progress_callback: 进度回调函数
            
        Returns:
            结果字典
        """
        # 第一步：高并发下载到本地临时目录
        temp_file = await self._download_to_temp(
            video_url=video_url,
            video_title=video_title,
            model_id=model_id,
            model_name=model_name,
            target_dir=target_dir,
            download_config=download_config,
            progress_callback=progress_callback
        )
        
        if not temp_file:
            return {
                'success': False,
                'error': '下载失败'
            }
        
        # 第二步：串行移动到目标目录
        result = await self._move_to_target(video_url)
        
        # 如果移动失败，清理临时文件
        if not result.get('success', False):
            if temp_file:
                logger.warning(f"移动失败，清理临时文件: {temp_file}")
                self.temp_manager.fail_download(video_url, result.get('error', '移动失败'))
            else:
                logger.warning(f"移动失败，但临时文件路径无效: {video_url}")
        
        return result
    
    async def _download_to_temp(self, video_url: str, video_title: str, 
                              model_id: int, model_name: str, 
                              target_dir: str, download_config: dict,
                              progress_callback=None) -> str:
        """
        下载到临时目录
        
        Args:
            video_url: 视频URL
            video_title: 视频标题
            model_id: 模特ID
            model_name: 模特名
            target_dir: 目标目录
            download_config: 下载配置
            progress_callback: 进度回调函数
            
        Returns:
            临时文件路径，失败返回None
        """
        async with self.download_semaphore:
            try:
                # 记录开始下载
                self.debugger.checkpoint('开始下载', f'URL: {video_url[:50]}... | 标题: {video_title[:30]}...')
                
                # 获取临时文件路径
                temp_file = self.temp_manager.start_download(
                    video_url=video_url,
                    video_title=video_title,
                    model_id=model_id,
                    model_name=model_name,
                    target_dir=target_dir
                )
                
                self.debugger.info('DOWNLOAD', f"开始下载: {video_title} -> TEMP: {temp_file}")
                
                # 创建下载器
                from multisite.core.downloader import DownloaderFactory
                downloader = DownloaderFactory.create_downloader('yt-dlp', download_config, progress_callback)
                
                # 下载
                success = await downloader.download(video_url, temp_file)
                
                if downloader:
                    try:
                        downloader.close()
                    except Exception as e:
                        logger.warning(f"关闭下载器时出错: {e}")
                        self.debugger.warning('DOWNLOAD', f"关闭下载器时出错: {e}")
                
                if success:
                    logger.info(f"下载完成: {video_title[:30]} -> {temp_file}")
                    self.debugger.info('DOWNLOAD', f"下载完成: {video_title} -> {temp_file}")
                    return temp_file
                else:
                    logger.error(f"下载失败: {video_title[:30]}")
                    self.debugger.error('DOWNLOAD', f"下载失败: {video_title}")
                    self.temp_manager.fail_download(video_url, "下载失败")
                    return None
                    
            except Exception as e:
                logger.error(f"下载异常: {video_title[:30]} - {e}")
                self.debugger.error('DOWNLOAD', f"下载异常: {video_title} - {e}", exc_info=True)
                self.temp_manager.fail_download(video_url, str(e))
                return None
    
    async def _move_to_target(self, video_url: str) -> Dict[str, Any]:
        """
        移动到目标目录
        
        Args:
            video_url: 视频URL
            
        Returns:
            结果字典
        """
        async with self.move_semaphore:
            try:
                logger.info(f"开始移动文件: {video_url}")
                result = self.temp_manager.complete_download(video_url)
                
                if result['success']:
                    logger.info(f"移动完成: {result.get('target_file', '未知文件')}")
                else:
                    logger.error(f"移动失败: {result.get('error', '未知错误')}")
                
                return result
                
            except Exception as e:
                logger.error(f"移动异常: {e}")
                return {
                    'success': False,
                    'error': str(e)
                }
    
    async def download_batch(self, videos: List[Dict[str, Any]], 
                           model_name: str, target_dir: str, 
                           model_id: int = None, download_config: dict = None,
                           progress_callback=None) -> List[Dict[str, Any]]:
        """
        批量下载
        
        Args:
            videos: 视频列表
            model_name: 模特名
            target_dir: 目标目录
            model_id: 模特ID
            download_config: 下载配置
            progress_callback: 进度回调函数
            
        Returns:
            结果列表
        """
        if not download_config:
            download_config = {}
        
        tasks = []
        for video in videos:
            task = self.download_and_move(
                video_url=video.get('url', ''),
                video_title=video.get('title', 'Unknown'),
                model_id=model_id or 0,
                model_name=model_name,
                target_dir=target_dir,
                download_config=download_config,
                progress_callback=progress_callback
            )
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理结果
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                video = videos[i]
                processed_results.append({
                    'status': 'failed',
                    'url': video.get('url', ''),
                    'title': video.get('title', 'Unknown'),
                    'error': str(result)
                })
            else:
                video = videos[i]
                processed_results.append({
                    'status': 'completed' if result.get('success') else 'failed',
                    'url': video.get('url', ''),
                    'title': video.get('title', 'Unknown'),
                    'file_path': result.get('target_file'),
                    'error': result.get('error')
                })
        
        # 批量下载完成后清理过期临时文件
        self._cleanup_temp_files()
        
        return processed_results
    
    def _cleanup_temp_files(self):
        """
        智能清理临时文件
        
        策略：
        1. 清理孤儿文件（无数据库记录的残留）
        2. 超过10分钟的文件直接删除
        3. TEMP超过2GB时，从最旧的已完成记录开始清理
        """
        try:
            result = self.temp_manager.smart_cleanup(
                max_size_mb=2048,      # 2GB阈值
                max_age_minutes=10     # 10分钟超时
            )
            
            if result['orphan_cleaned'] > 0 or result['size_cleaned'] > 0:
                logger.info(
                    f"批量下载完成，TEMP清理: "
                    f"孤儿{result['orphan_cleaned']}个 + "
                    f"超时/强制{result['size_cleaned']}个 = "
                    f"释放 {result['freed_mb']} MB"
                )
                                
        except Exception as e:
            logger.error(f"清理临时文件时出错: {e}")