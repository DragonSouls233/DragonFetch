# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
视频下载引擎
支持多种下载方式：
1. yt-dlp (主要)
2. M3U8 直接下载
3. 直接 URL 下载
4. 自动分流策略
"""
import os
import asyncio
import logging
from typing import Any, Dict, Optional, Callable
from pathlib import Path
import concurrent.futures

logger = logging.getLogger(__name__)

# 尝试导入多引擎下载器
try:
    from .multi_engine import (
        MultiEngineDownloader, 
        DownloadEngine,
        YTDLPDownloadEngine,
        M3U8DownloadEngine,
        DirectDownloadEngine,
        create_multi_engine_downloader
    )
    MULTI_ENGINE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"多引擎下载器不可用: {e}")
    MULTI_ENGINE_AVAILABLE = False


class VideoDownloader:
    """
    视频下载器
    
    功能：
    1. 支持多引擎下载 (yt-dlp, M3U8, 直接URL)
    2. 支持分流策略 (自动回退)
    3. 支持代理
    4. 支持进度回调
    5. 支持取消下载
    6. 优化的线程池管理
    """
    
    # 共享线程池
    _shared_executor = None
    _executor_ref_count = 0
    _executor_max_workers = 3  # 最大工作线程数，减少到3以避免系统资源耗尽
    
    def __init__(self, config: Dict[str, Any] = None):
        """
        初始化下载器
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        self._is_cancelled = False
        self._progress_callback = None
        self._multi_engine = None
        
        # 根据配置决定是否使用多引擎
        engine_type = self.config.get('engine', 'auto')
        self._use_multi_engine = (engine_type == 'auto' or engine_type == 'multi') and MULTI_ENGINE_AVAILABLE
        
        if self._use_multi_engine:
            self._multi_engine = create_multi_engine_downloader(self.config)
            logger.info("使用多引擎下载器")
        else:
            logger.info(f"使用传统下载器 (engine={engine_type})")
        
        # 获取或创建共享线程池
        self._init_executor()
    
    def _init_executor(self):
        """
        初始化线程池
        """
        if VideoDownloader._shared_executor is None:
            VideoDownloader._shared_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=VideoDownloader._executor_max_workers,
                thread_name_prefix="VideoDownloader-"
            )
            logger.info(f"创建共享线程池，最大工作线程数: {VideoDownloader._executor_max_workers}")
        
        VideoDownloader._executor_ref_count += 1
        logger.debug(f"线程池引用计数: {VideoDownloader._executor_ref_count}")
    
    def __del__(self):
        """
        析构函数，清理线程池
        """
        try:
            self._cleanup_executor()
        except Exception as e:
            logger.error(f"析构函数清理线程池失败: {e}")
    
    def _cleanup_executor(self):
        """
        清理线程池
        """
        try:
            if VideoDownloader._executor_ref_count > 0:
                VideoDownloader._executor_ref_count -= 1
                logger.debug(f"线程池引用计数: {VideoDownloader._executor_ref_count}")
            
            # 如果没有引用了，关闭线程池
            if VideoDownloader._executor_ref_count <= 0 and VideoDownloader._shared_executor:
                try:
                    # 使用 wait=True 确保所有任务完成
                    VideoDownloader._shared_executor.shutdown(wait=True, cancel_futures=False)
                    VideoDownloader._shared_executor = None
                    logger.info("关闭共享线程池")
                except Exception as e:
                    logger.error(f"关闭线程池失败: {e}")
        except Exception as e:
            logger.error(f"清理线程池失败: {e}")
    
    def _get_ydl_options(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """
        获取yt-dlp配置选项
        
        Args:
            output_path: 输出路径
        
        Returns:
            配置字典
        """
        opts = {
            'format': self.config.get('format', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'),
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'progress_hooks': [self._progress_hook],
            'socket_timeout': self.config.get('timeout', 60),  # 增加超时时间
            'retries': self.config.get('retry_times', 5),  # 增加重试次数
            'fragment_retries': self.config.get('retry_times', 5),
            'extractor_retries': self.config.get('retry_times', 5),
            'concurrent_fragment_downloads': 5,  # 并发片段下载
        }
        
        # 尝试使用 curl_cffi 模拟浏览器 - 这是绕过 PornHub 410 错误的关键
        # 根据用户测试，--impersonate chrome --no-cookies 可以成功绕过 410 错误
        cookie_file = self.config.get('cookie_file')
        use_impersonate = False
        
        try:
            import curl_cffi
            opts['impersonate'] = 'chrome'  # 使用 curl_cffi 模拟 Chrome 浏览器
            use_impersonate = True
            logger.info("VideoDownloader: 使用 curl_cffi 模拟 Chrome 浏览器访问 (绕过 410 错误)")
        except ImportError:
            logger.warning("curl_cffi 未安装，无法模拟浏览器。可能无法绕过 PornHub 410 错误")
            # 回退到基本的 HTTP 头配置
            opts['http_headers'] = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
            }
        
        # 代理支持
        proxy = self.config.get('proxy')
        if proxy:
            opts['proxy'] = proxy
            logger.info(f"使用代理: {proxy}")
        
        # Cookie支持 - 如果使用 impersonate 则不使用 cookies（--no-cookies）
        if use_impersonate:
            logger.info("使用 impersonate 模式，不加载 cookies 文件")
        elif cookie_file and os.path.exists(cookie_file):
            opts['cookiefile'] = cookie_file
            logger.info(f"使用 cookie 文件: {cookie_file}")
        
        # 输出路径
        if output_path:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            opts['outtmpl'] = output_path
        
        # 速度限制
        speed_limit = self.config.get('speed_limit', 0)
        if speed_limit > 0:
            opts['ratelimit'] = speed_limit
        
        return opts
    
    def _progress_hook(self, d: Dict[str, Any]) -> None:
        """
        进度回调钩子
        
        Args:
            d: 进度信息字典
        """
        if self._is_cancelled:
            raise DownloadCancelledError("下载已取消")
        
        if d['status'] == 'downloading':
            downloaded_bytes = d.get('downloaded_bytes', 0)
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            
            if total_bytes > 0:
                progress = (downloaded_bytes / total_bytes) * 100
            else:
                progress = 0
            
            # 格式化速度
            speed_bytes = d.get('speed', 0) or 0
            if speed_bytes >= 1024 * 1024:
                speed = f"{speed_bytes / (1024 * 1024):.2f} MB/s"
            elif speed_bytes >= 1024:
                speed = f"{speed_bytes / 1024:.2f} KB/s"
            else:
                speed = f"{speed_bytes:.0f} B/s"
            
            # 格式化ETA
            eta_seconds = d.get('eta', 0) or 0
            # 确保是整数
            eta_seconds = int(eta_seconds) if isinstance(eta_seconds, (int, float)) else 0
            if eta_seconds > 0:
                hours = eta_seconds // 3600
                minutes = (eta_seconds % 3600) // 60
                seconds = eta_seconds % 60
                if hours > 0:
                    eta = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                else:
                    eta = f"{minutes:02d}:{seconds:02d}"
            else:
                eta = "--:--"
            
            progress_info = {
                'status': 'downloading',
                'progress': round(progress, 1),
                'speed': speed,
                'eta': eta,
                'downloaded_bytes': downloaded_bytes,
                'total_bytes': total_bytes,
            }
            
            if self._progress_callback:
                self._progress_callback(progress_info)
        
        elif d['status'] == 'finished':
            progress_info = {
                'status': 'finished',
                'progress': 100,
                'filename': d.get('filename', ''),
            }
            
            if self._progress_callback:
                self._progress_callback(progress_info)
            
            logger.info(f"下载完成: {d.get('filename')}")
    
    def set_progress_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """设置进度回调"""
        self._progress_callback = callback
    
    def cancel(self):
        """取消下载"""
        self._is_cancelled = True
    
    def download(self, url: str, output_path: str) -> bool:
        """
        同步下载视频
        
        Args:
            url: 视频URL
            output_path: 输出路径
        
        Returns:
            是否成功
        """
        # 使用多引擎下载器
        if self._multi_engine:
            try:
                logger.info(f"[多引擎] 开始下载: {url}")
                fallback = self.config.get('fallback', True)
                
                result = self._multi_engine.download(
                    url, 
                    output_path, 
                    progress_callback=self._progress_callback,
                    fallback=fallback
                )
                
                logger.info(f"[多引擎] 下载完成: {output_path}")
                return result
            
            except Exception as e:
                logger.error(f"[多引擎] 下载失败: {url}, 错误: {str(e)}")
                raise
        
        # 回退到传统 yt-dlp 下载
        return self._download_with_ytdlp(url, output_path)
    
    def _download_with_ytdlp(self, url: str, output_path: str) -> bool:
        """
        使用传统 yt-dlp 下载
        
        Args:
            url: 视频URL
            output_path: 输出路径
        
        Returns:
            是否成功
        """
        try:
            import yt_dlp
            
            logger.info(f"[YT-DLP] 开始下载: {url}")
            
            opts = self._get_ydl_options(output_path)
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            
            logger.info(f"[YT-DLP] 下载完成: {output_path}")
            return True
        
        except DownloadCancelledError:
            logger.warning(f"下载已取消: {url}")
            return False
        
        except Exception as e:
            logger.error(f"[YT-DLP] 下载失败: {url}, 错误: {str(e)}")
            raise
    
    async def download_async(self, url: str, output_path: str) -> bool:
        """
        异步下载视频
        
        Args:
            url: 视频URL
            output_path: 输出路径
        
        Returns:
            是否成功
        """
        # ===== URL 规范化：处理 PornHub 中国域名问题 =====
        if 'cn.pornhub.com' in url:
            logger.info(f"VideoDownloader: 检测到中国域名，转换为 www.pornhub.com")
            url = url.replace('cn.pornhub.com', 'www.pornhub.com')
        # ===== URL 规范化结束 =====
        
        # 多引擎下载器已经支持异步
        if self._multi_engine:
            return await self._multi_engine.download_async(
                url, 
                output_path, 
                self._progress_callback
            )
        
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            VideoDownloader._shared_executor,  # 使用共享线程池
            self.download,
            url,
            output_path
        )
    
    def get_video_info(self, url: str) -> Dict[str, Any]:
        """
        获取视频信息
        
        Args:
            url: 视频URL
        
        Returns:
            视频信息字典
        """
        # 使用多引擎下载器获取信息
        if self._multi_engine:
            try:
                info = self._multi_engine.extract_info(url)
                if info:
                    return {
                        'id': info.get('id', ''),
                        'title': info.get('title', ''),
                        'duration': info.get('duration', 0),
                        'thumbnail': info.get('thumbnail', ''),
                        'uploader': info.get('uploader', ''),
                        'view_count': info.get('view_count', 0),
                        'url': url,
                        'formats': info.get('formats', []),
                    }
            except Exception as e:
                logger.warning(f"[多引擎] 获取视频信息失败，回退到YT-DLP: {e}")
        
        # 回退到传统 yt-dlp
        try:
            import yt_dlp
            
            opts = self._get_ydl_options()
            opts['quiet'] = True
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            
            return {
                'id': info.get('id', ''),
                'title': info.get('title', ''),
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', ''),
                'view_count': info.get('view_count', 0),
                'url': url,
            }
        
        except Exception as e:
            logger.error(f"获取视频信息失败: {url}, 错误: {str(e)}")
            raise


class DownloadCancelledError(Exception):
    """下载取消异常"""
    pass


def create_downloader(config: Dict[str, Any] = None) -> VideoDownloader:
    """
    创建下载器
    
    Args:
        config: 配置字典
    
    Returns:
        下载器实例
    """
    from ..utils.config import config as global_config
    
    # 合并配置
    download_config = global_config.get_download_config()
    
    # 添加代理
    proxy_url = global_config.get_proxy_url()
    if proxy_url:
        download_config['proxy'] = proxy_url
    
    if config:
        download_config.update(config)
    
    return VideoDownloader(download_config)
