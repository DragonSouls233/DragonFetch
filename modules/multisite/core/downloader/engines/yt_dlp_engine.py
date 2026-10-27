# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
yt-dlp下载引擎
"""
import asyncio
import concurrent.futures
import threading
import time
import os
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional, Callable
import logging

# 添加clf2支持
try:
    import clf2
except ImportError:
    clf2 = None
    logging.warning("clf2未安装，将无法处理Cloudflare保护")

# 添加M3U8处理支持
try:
    from m3u8_tools import playlist2stream, M3u8_stream
except ImportError:
    playlist2stream = None
    M3u8_stream = None
    logging.warning("m3u8_tools未安装，M3U8流处理功能将受限")

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

from ..base import BaseDownloader

logger = logging.getLogger(__name__)


class DownloadCancelledError(Exception):
    """下载取消异常"""
    pass


class YtDlpEngine(BaseDownloader):
    """yt-dlp下载引擎"""

    # 常量定义
    _DEFAULT_MAX_WORKERS: int = 20  # 默认最大工作线程数（增加到20以支持更多并发）
    _MIN_WORKERS: int = 1  # 最小工作线程数
    _MAX_WORKERS: int = 50  # 最大工作线程数（增加到50以支持高并发）
    _EXECUTOR_IDLE_TIMEOUT: int = 300  # 空闲超时时间（秒），空闲300秒后关闭线程池

    # 类级别的共享线程池执行器（锁必须先于被保护的资源初始化）
    _executor_lock: threading.Lock = threading.Lock()
    _shared_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
    _executor_ref_count: int = 0
    _configured_max_workers: Optional[int] = None  # 全局配置的线程池大小
    _executor_shutting_down: bool = False  # 标记executor正在关闭中，防止竞态条件
    _last_activity_time: float = 0.0  # 最后活动时间戳
    _executor_close_scheduled: bool = False  # 是否已安排延迟关闭
    _shutdown_timer: Optional[threading.Timer] = None  # 延迟关闭定时器
    
    @classmethod
    def set_global_max_workers(cls, max_workers: int) -> None:
        """设置全局线程池大小
        
        此方法应在创建任何下载器实例之前调用。
        如果线程池已创建，将记录警告并忽略此设置。
        
        Args:
            max_workers: 最大工作线程数
        """
        max_workers = cls._validate_max_workers(max_workers)
        with cls._executor_lock:
            if cls._shared_executor is not None:
                current_workers = cls._configured_max_workers or cls._DEFAULT_MAX_WORKERS
                logger.warning(
                    f"YtDlpEngine: Cannot change max_workers after executor created. "
                    f"Current workers: {current_workers}, requested: {max_workers}"
                )
                return
            cls._configured_max_workers = max_workers
            logger.info(f"YtDlpEngine: Global max_workers set to {max_workers}")
    
    @classmethod
    def get_current_max_workers(cls) -> int:
        """获取当前线程池大小
        
        Returns:
            int: 当前线程池大小，如果未创建则返回配置值或默认值
        """
        with cls._executor_lock:
            return cls._configured_max_workers or cls._DEFAULT_MAX_WORKERS
    
    @classmethod
    def shutdown_shared_executor(cls, wait: bool = True) -> None:
        """关闭共享线程池
        
        此方法会强制关闭共享线程池，无论引用计数是否为0。
        应在应用程序退出时调用。
        
        Args:
            wait: 是否等待所有任务完成
        """
        with cls._executor_lock:
            if cls._shared_executor is not None:
                cls._executor_shutting_down = True  # 标记正在关闭
                logger.info("YtDlpEngine: Force shutting down shared ThreadPoolExecutor...")
                try:
                    cls._shared_executor.shutdown(wait=wait)
                except Exception as e:
                    logger.warning(f"YtDlpEngine: Error during forced shutdown: {e}")
                finally:
                    cls._shared_executor = None
                    cls._executor_ref_count = 0
                    cls._configured_max_workers = None
                    cls._executor_shutting_down = False  # 重置标志
    
    def __init__(self, config: Dict[str, Any], progress_callback: Optional[Callable] = None):
        """
        初始化yt-dlp引擎
        
        Args:
            config: 配置字典,包含以下可选参数:
                - format: 视频格式选择器(默认: bestvideo+bestaudio)
                - timeout: 超时时间(默认: 30秒)
                - retry_times: 重试次数(默认: 3)
                - proxy: 代理地址(如: http://127.0.0.1:7890)
                - cookie_file: Cookie文件路径
                - speed_limit: 速度限制(字节/秒)
            progress_callback: 进度回调函数,接收一个字典参数:
                {
                    'status': 'downloading' | 'finished',
                    'progress': float (0-100),
                    'speed': str (如: "1.50 MB/s"),
                    'eta': str (如: "00:30" 或 "01:00:30"),
                    'downloaded_bytes': int,
                    'total_bytes': int
                }
        """
        super().__init__(config)
        self.set_progress_callback(progress_callback)

        # 取消任何待执行的延迟关闭，确保线程池被复用
        with YtDlpEngine._executor_lock:
            if YtDlpEngine._shutdown_timer is not None:
                YtDlpEngine._shutdown_timer.cancel()
                YtDlpEngine._shutdown_timer = None
                logger.debug("YtDlpEngine: Cancelled pending executor shutdown on new instance creation")

            if YtDlpEngine._executor_close_scheduled and YtDlpEngine._shared_executor is not None:
                YtDlpEngine._executor_close_scheduled = False
                logger.debug("YtDlpEngine: Reusing existing shared executor (shutdown cancelled)")

        # 进度日志记录相关属性
        self._last_logged_progress: int = -10  # 初始值，确保第一次一定记录
        
        # 使用共享线程池执行器，避免多个实例创建多个线程池
        # 优先级：全局配置 > 实例配置 > 默认值
        if YtDlpEngine._configured_max_workers is not None:
            self._max_workers = YtDlpEngine._configured_max_workers
        else:
            self._max_workers = self._validate_max_workers(
                self.config.get('max_workers', YtDlpEngine._DEFAULT_MAX_WORKERS)
            )
        self._executor = None
        self._using_shared_executor = False
        
        # 先检查 yt_dlp 是否可用（在获取锁之前，避免长时间持有锁）
        if yt_dlp is None:
            raise ImportError("yt_dlp is not installed. Please install it with: pip install yt-dlp")
        
        # 获取或创建共享线程池（所有操作在同一个锁保护块内）
        ref_count_incremented = False
        with YtDlpEngine._executor_lock:
            # 如果executor正在关闭中，等待关闭完成后再创建新的
            if YtDlpEngine._executor_shutting_down:
                logger.info("YtDlpEngine: Shared executor is shutting down, will create standalone executor")
                self._using_shared_executor = False
                self._executor = None
            else:
                try:
                    if YtDlpEngine._shared_executor is None:
                        try:
                            YtDlpEngine._shared_executor = concurrent.futures.ThreadPoolExecutor(
                                max_workers=self._max_workers
                            )
                            logger.debug(f"YtDlpEngine: Created shared ThreadPoolExecutor with {self._max_workers} workers")
                        except Exception as e:
                            logger.error(f"YtDlpEngine: Failed to create shared ThreadPoolExecutor with {self._max_workers} workers: {e}")
                            raise
                    else:
                        logger.debug(f"YtDlpEngine: Reusing existing shared ThreadPoolExecutor")
                    
                    # 先增加引用计数，确保在设置标志前增加
                    YtDlpEngine._executor_ref_count += 1
                    ref_count_incremented = True
                    self._executor = YtDlpEngine._shared_executor
                    self._using_shared_executor = True
                    logger.debug(f"YtDlpEngine: Using shared executor, ref_count={YtDlpEngine._executor_ref_count}")
                except Exception as e:
                    # 如果共享线程池初始化失败，回滚引用计数并降级到独立线程池
                    if ref_count_incremented:
                        YtDlpEngine._executor_ref_count = max(0, YtDlpEngine._executor_ref_count - 1)
                        logger.debug(f"YtDlpEngine: Rolled back ref_count due to exception, ref_count={YtDlpEngine._executor_ref_count}")
                    logger.warning(f"YtDlpEngine: Falling back to standalone executor due to shared executor failure: {e}")
                    self._using_shared_executor = False
                    self._executor = None
        
        # 如果共享线程池失败，创建独立线程池
        if self._executor is None:
            try:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self._max_workers
                )
                logger.info(f"YtDlpEngine: Created standalone ThreadPoolExecutor with {self._max_workers} workers (fallback)")
            except Exception as e:
                logger.error(f"YtDlpEngine: Failed to create standalone ThreadPoolExecutor with {self._max_workers} workers: {e}")
                raise
    
    @staticmethod
    def _validate_max_workers(value) -> int:
        """验证并返回有效的max_workers值
        
        Args:
            value: 配置值，可以是int、float或str类型
            
        Returns:
            int: 有效的max_workers值，范围_MIN_WORKERS到_MAX_WORKERS
        """
        try:
            workers = int(value)
        except (ValueError, TypeError):
            logger.warning(f"Invalid max_workers value '{value}', using default: {YtDlpEngine._DEFAULT_MAX_WORKERS}")
            return YtDlpEngine._DEFAULT_MAX_WORKERS
        
        if workers < YtDlpEngine._MIN_WORKERS or workers > YtDlpEngine._MAX_WORKERS:
            logger.warning(f"max_workers {workers} out of range [{YtDlpEngine._MIN_WORKERS}, {YtDlpEngine._MAX_WORKERS}], clamping to valid range")
        
        result = max(YtDlpEngine._MIN_WORKERS, min(workers, YtDlpEngine._MAX_WORKERS))
        logger.debug(f"max_workers validated: config={value}, result={result}")
        return result
    
    def close(self):
        """释放资源

        减少共享线程池的引用计数。当引用计数为0时，安排延迟关闭线程池。
        如果使用独立的执行器，直接关闭该执行器。
        调用此方法后，下载器实例不应再被使用。

        注意：
        - 必须在不再使用下载器时显式调用此方法
        - 使用引用计数管理共享线程池，避免频繁创建销毁线程池
        - 方法可以安全地多次调用
        """
        if self._using_shared_executor:
            with YtDlpEngine._executor_lock:
                if YtDlpEngine._executor_ref_count > 0:
                    YtDlpEngine._executor_ref_count -= 1
                    logger.debug(f"YtDlpEngine: Released shared executor, ref_count={YtDlpEngine._executor_ref_count}")

                if YtDlpEngine._executor_ref_count == 0 and YtDlpEngine._shared_executor is not None:
                    if YtDlpEngine._shutdown_timer is not None:
                        YtDlpEngine._shutdown_timer.cancel()
                        YtDlpEngine._shutdown_timer = None
                        logger.debug("YtDlpEngine: Cancelled pending executor shutdown")

                    YtDlpEngine._executor_close_scheduled = True
                    YtDlpEngine._last_activity_time = time.time()

                    def delayed_shutdown():
                        with YtDlpEngine._executor_lock:
                            if YtDlpEngine._executor_ref_count == 0 and YtDlpEngine._shared_executor is not None:
                                logger.info("YtDlpEngine: Delayed shutdown timer expired, closing shared ThreadPoolExecutor...")
                                executor = YtDlpEngine._shared_executor
                                YtDlpEngine._shared_executor = None
                                YtDlpEngine._executor_shutting_down = False
                                YtDlpEngine._executor_close_scheduled = False
                                try:
                                    executor.shutdown(wait=False)
                                    logger.info("YtDlpEngine: Shared ThreadPoolExecutor closed (delayed)")
                                except Exception as e:
                                    logger.warning(f"YtDlpEngine: Error during delayed executor shutdown: {e}")

                    YtDlpEngine._shutdown_timer = threading.Timer(
                        YtDlpEngine._EXECUTOR_IDLE_TIMEOUT,
                        delayed_shutdown
                    )
                    YtDlpEngine._shutdown_timer.daemon = True
                    YtDlpEngine._shutdown_timer.start()
                    logger.info(f"YtDlpEngine: Scheduled delayed executor shutdown in {YtDlpEngine._EXECUTOR_IDLE_TIMEOUT} seconds")

            self._executor = None
            self._using_shared_executor = False
        elif self._executor is not None:
            executor_to_shutdown = self._executor
            self._executor = None
            logger.info("YtDlpEngine: Closing standalone ThreadPoolExecutor...")
            try:
                executor_to_shutdown.shutdown(wait=True)
                logger.info("YtDlpEngine: Standalone ThreadPoolExecutor closed")
            except RuntimeError:
                logger.debug("YtDlpEngine: Executor already shut down")
            except Exception as e:
                logger.warning(f"YtDlpEngine: Error during executor shutdown: {e}")
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口，确保资源释放
        
        Args:
            exc_type: 异常类型
            exc_val: 异常值
            exc_tb: 异常追踪信息
        
        Returns:
            bool: 总是返回False，不抑制任何异常
        """
        self.close()
        return False
    
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
            'quiet': False,
            'no_warnings': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,  # 不忽略错误，让异常正确抛出以便重试
            'extract_flat': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
            'writethumbnail': False,
            'writeinfojson': False,
            'progress_hooks': [self._progress_hook],
            'socket_timeout': self.config.get('timeout', 30),
            'retries': self.config.get('retry_times', 5),
            'fragment_retries': self.config.get('retry_times', 10),  # 增加片段重试次数
            'extractor_retries': self.config.get('retry_times', 5),
            # 处理410错误的额外配置
            'skip_unavailable_fragments': True,  # 跳过不可用的片段
            'allow_unplayable_formats': True,  # 允许不可播放的格式
            'keep_fragments': True,  # 保留已下载的片段
        }
        
        # 尝试使用 curl_cffi 模拟浏览器 - 这是绕过 PornHub 410 错误的关键
        # 根据用户测试，--impersonate chrome --no-cookies 可以成功绕过 410 错误
        cookie_file = self.config.get('cookie_file')
        use_impersonate = False
        
        try:
            import curl_cffi
            
            # 测试 impersonate 是否可用（避免与新版 yt-dlp 的兼容性问题）
            test_opts = {'impersonate': 'chrome', 'quiet': True, 'no_warnings': True}
            test_ydl = yt_dlp.YoutubeDL(test_opts)
            del test_ydl  # 清理测试对象
            
            opts['impersonate'] = 'chrome'  # 使用 curl_cffi 模拟 Chrome 浏览器
            
            # 添加 extractor-args 来确保所有 extractor 请求都使用 impersonate
            # 这是解决 JSON metadata 下载卡住问题的关键
            opts['extractor_args'] = {
                'pornhub': ['impersonate=chrome'],
                'pornhubpremium': ['impersonate=chrome'],
            }
            
            use_impersonate = True
            logger.info("YT-DLP Engine: 使用 curl_cffi 模拟 Chrome 浏览器访问 (绕过 410 错误)")
            logger.info("YT-DLP Engine: 已添加 extractor_args 确保所有请求都使用 impersonate")
        except (ImportError, AssertionError, Exception) as e:
            logger.warning(f"curl_cffi 不可用或存在兼容性问题 ({e})，回退到 HTTP 头模拟")
            # 回退到基本的 HTTP 头配置
            opts['http_headers'] = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Upgrade-Insecure-Requests': '1',
            }
            
            # 添加 clf2 (curl_cffi) 作为备用方案 - 类似 Hitomi-Downloader 的做法
            # 当 yt-dlp 遇到 410 错误时，可以使用 clf2 来绕过
            try:
                import clf2
                opts['_clf2_fallback'] = True  # 标记使用 clf2 备用方案
                logger.info("YT-DLP Engine: 已启用 clf2 备用方案 (当遇到 410 错误时使用)")
            except ImportError:
                logger.warning("clf2 未安装，将无法使用备用方案")
        
        # Cookie支持 - 如果使用 impersonate 则不使用 cookies（--no-cookies）
        if use_impersonate:
            logger.info("使用 impersonate 模式，不加载 cookies 文件")
        elif cookie_file and Path(cookie_file).exists():
            opts['cookiefile'] = cookie_file
            logger.info(f"使用 cookie 文件: {cookie_file}")
        
        # 代理支持
        proxy = self.config.get('proxy')
        if proxy:
            opts['proxy'] = proxy
            logger.info(f"Using proxy: {proxy}")
        else:
            logger.info("No proxy configured. If you encounter connection errors, please configure proxy in config/download_config.json")
        
        # 输出路径
        if output_path:
            output_dir = Path(output_path).parent
            output_filename = Path(output_path).name
            self.ensure_dir(str(output_dir))
            # 使用完整路径作为输出模板（yt-dlp会直接使用这个路径）
            opts['outtmpl'] = str(output_path)
        
        # 速度限制
        speed_limit = self.config.get('speed_limit', 0)
        if speed_limit > 0:
            opts['ratelimit'] = speed_limit
        
        return opts
    
    def _progress_hook(self, d: Dict[str, Any]) -> None:
        """
        进度回调钩子 - yt-dlp内部调用
        
        此方法由yt-dlp在下载过程中自动调用,处理进度信息并触发progress_callback。
        
        Args:
            d: yt-dlp进度信息字典,包含:
                - status: 'downloading' | 'finished' | 'error'
                - downloaded_bytes: 已下载字节数
                - total_bytes: 总字节数(可能为None)
                - total_bytes_estimate: 估计总字节数
                - speed: 下载速度(字节/秒)
                - eta: 预计剩余时间(秒)
                - filename: 文件名(finished状态)
        
        Note:
            - 进度回调异常会被捕获并记录,不会中断下载
            - 速度和剩余时间会自动格式化为易读格式
            - 支持下载进度从0%到100%的完整跟踪
        """
        if self._is_cancelled:
            raise DownloadCancelledError("Download cancelled by user")

        if d['status'] == 'downloading':
            # 获取关键进度信息（不记录完整字典，避免性能问题）
            downloaded_bytes = d.get('downloaded_bytes', 0) or 0
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0) or 0
            speed = d.get('speed', 0) or 0
            eta = d.get('eta', 0) or 0
            
            # 计算进度百分比
            if total_bytes > 0:
                progress = (downloaded_bytes / total_bytes) * 100
            else:
                # 尝试从yt-dlp的格式化字符串中提取百分比
                percent_str = d.get('_percent_str', d.get('percent_str', '0%'))
                try:
                    progress = float(percent_str.replace('%', '').replace('\x1b[0;94m', '').replace('\x1b[0m', ''))
                except Exception:
                    progress = 0
            
            # 只在进度变化显著时才记录日志（避免日志过多）
            progress_diff = abs(progress - self._last_logged_progress)
            if progress_diff >= 5:  # 每5%记录一次
                # 从文件名或URL生成简短的任务标识
                filename = d.get('filename', '')
                if filename:
                    task_ident = os.path.basename(filename)[:8]
                else:
                    task_ident = hashlib.md5(d.get('url', '').encode()).hexdigest()[:8]
                logger.info(f"[Task {task_ident}] 下载进度: {progress:.1f}%, 速度: {speed/1024/1024:.2f}MB/s, 剩余: {int(eta)}秒")
                self._last_logged_progress = progress

            # 格式化速度
            speed_bytes = speed if speed else d.get('speed', 0) or 0
            if speed_bytes > 0:
                if speed_bytes >= 1024 * 1024:
                    speed_str = f"{speed_bytes / (1024 * 1024):.2f} MB/s"
                elif speed_bytes >= 1024:
                    speed_str = f"{speed_bytes / 1024:.2f} KB/s"
                else:
                    speed_str = f"{speed_bytes:.2f} B/s"
            else:
                speed_str = '未知'

            # 格式化ETA
            eta_seconds = eta if eta else d.get('eta', 0) or 0
            eta_seconds = int(eta_seconds) if isinstance(eta_seconds, (int, float)) else 0
            if eta_seconds > 0:
                hours = eta_seconds // 3600
                minutes = (eta_seconds % 3600) // 60
                seconds = eta_seconds % 60
                if hours > 0:
                    eta_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                else:
                    eta_str = f"{minutes:02d}:{seconds:02d}"
            else:
                eta_str = '未知'

            progress_info = {
                'status': 'downloading',
                'progress': int(round(progress)),
                'speed': speed_str,
                'eta': eta_str,
                'downloaded_bytes': int(downloaded_bytes),
                'total_bytes': int(total_bytes) if total_bytes else 0,
            }
            self._report_progress(progress_info)

        elif d['status'] == 'finished':
            progress_info = {
                'status': 'finished',
                'progress': 100,
                'speed': '0 B/s',
                'eta': '00:00',
                'filename': d.get('filename', ''),
                'total_bytes': d.get('total_bytes', 0),
                'elapsed': d.get('elapsed', 0),
            }
            self._report_progress(progress_info)

            logger.info(f"Download finished: {d.get('filename')}")

        elif d['status'] == 'error':
            logger.error(f"Download error: {d.get('error')}")
    
    async def download(self, url: str, output_path: str) -> bool:
        """
        下载视频

        Args:
            url: 视频URL
            output_path: 输出路径

        Returns:
            是否成功
        """

        max_retries = 5  # 最大重试次数
        retry_delay = 3  # 重试延迟（秒）
        
        # 生成任务ID
        task_id = hashlib.md5(url.encode()).hexdigest()[:8]
        
        for attempt in range(max_retries):
            try:
                logger.info(f"[Task {task_id}] Starting download (attempt {attempt + 1}/{max_retries}): {url}")
                
                # 预处理URL，处理特殊情况
                url = self._preprocess_url(url)
                
                # 处理Cloudflare保护
                if clf2 and 'pornhub' in url.lower():
                    try:
                        logger.info(f"[Task {task_id}] Checking Cloudflare protection...")
                        session = self._get_session()
                        res = clf2.solve(url, session=session)
                        logger.info(f"[Task {task_id}] Cloudflare protection bypassed successfully")
                        # 更新会话cookie
                        self._update_session_cookies(session, res.get('cookies', {}))
                    except Exception as e:
                        logger.warning(f"[Task {task_id}] Cloudflare bypass failed: {e}")
                
                # 获取配置
                opts = self._get_ydl_options(output_path)
                
                # 添加额外的配置来处理连接问题
                opts['socket_timeout'] = 60  # 增加超时时间
                opts['retries'] = 5  # 增加重试次数
                opts['fragment_retries'] = 5
                opts['extractor_retries'] = 5
                
                # 在线程池中运行yt-dlp（因为它是同步的）
                # 使用实例的executor，避免每次下载都创建新的线程池
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    self._executor,
                    self._download_sync,
                    url,
                    opts
                )
                
                # 验证下载的文件是否存在且有效
                if os.path.exists(output_path):
                    file_size = os.path.getsize(output_path)
                    if file_size > 0:
                        logger.info(f"[Task {task_id}] Download completed: {output_path} ({file_size / 1024 / 1024:.2f} MB)")
                        return True
                    else:
                        logger.warning(f"[Task {task_id}] Downloaded file is empty (0 bytes): {output_path}")
                        # 删除空文件
                        try:
                            os.remove(output_path)
                            logger.info(f"[Task {task_id}] Removed empty file: {output_path}")
                        except Exception as e:
                            logger.warning(f"[Task {task_id}] Failed to remove empty file: {e}")
                        # 如果文件为空，继续重试
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (attempt + 1)
                            logger.info(f"[Task {task_id}] Retrying download (empty file)...")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            logger.error(f"[Task {task_id}] Max retries reached (empty file): {url}")
                            return False
                else:
                    logger.warning(f"[Task {task_id}] Downloaded file not found: {output_path}")
                    # 如果文件不存在，继续重试
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (attempt + 1)
                        logger.info(f"[Task {task_id}] Retrying download (file not found)...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"[Task {task_id}] Max retries reached (file not found): {url}")
                        return False
            
            except DownloadCancelledError:
                logger.warning(f"[Task {task_id}] Download cancelled: {url}")
                return False
            
            except Exception as e:
                error_msg = str(e)
                logger.error(f"[Task {task_id}] Download failed (attempt {attempt + 1}/{max_retries}): {url}, error: {error_msg}")
                
                # 检查是否是连接重置错误或超时错误
                if ('ConnectionResetError' in error_msg or 'Connection aborted' in error_msg or 
                    '10054' in error_msg or 'Read timed out' in error_msg or 
                    'timed out' in error_msg.lower()):
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (attempt + 1)  # 递增延迟
                        logger.info(f"[Task {task_id}] Connection issue detected, waiting {wait_time} seconds before retry...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"[Task {task_id}] Max retries reached for: {url}")
                        logger.error("=" * 60)
                        logger.error(f"[Task {task_id}] 连接或超时错误，可能的原因：")
                        logger.error(f"[Task {task_id}] 1. 未配置代理 - 请在 config/download_config.json 中配置代理")
                        logger.error(f"[Task {task_id}]    示例: \"proxy\": \"http://127.0.0.1:7890\"")
                        logger.error(f"[Task {task_id}] 2. 代理未启动 - 请确保代理软件正在运行")
                        logger.error(f"[Task {task_id}] 3. 网络问题 - 请检查网络连接")
                        logger.error(f"[Task {task_id}] 4. 服务器响应慢 - 请稍后重试")
                        logger.error("=" * 60)
                        return False
                else:
                    # 其他错误，不重试
                    return False
        
        return False
    
    def _download_sync(self, url: str, opts: Dict[str, Any]) -> None:
        """
        同步下载（在线程池中运行）

        Args:
            url: 视频URL
            opts: yt-dlp配置
        """
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    
    def _preprocess_url(self, url: str) -> str:
        """
        预处理URL，处理特殊情况

        Args:
            url: 原始URL

        Returns:
            处理后的URL
        """
        # 处理PornHub特殊URL格式
        if 'pornhub' in url.lower():
            # 处理pornhub_gif_格式
            if 'pornhub_gif_' in url:
                url = 'https://www.pornhub.com/gif/{}'.format(
                    url.replace('pornhub_gif_', ''))
            # 处理pornhub_album_格式
            elif 'pornhub_album_' in url:
                url = 'https://www.pornhub.com/album/{}'.format(
                    url.replace('pornhub_album_', ''))
            # 处理pornhub_格式
            elif 'pornhub_' in url:
                url = 'https://www.pornhub.com/view_video.php?viewkey={}'.format(
                    url.replace('pornhub_', ''))
            # 处理认证跳转URL
            if '/authenticate/goToLoggedIn' in url:
                import urllib.parse
                parsed = urllib.parse.urlparse(url)
                query_params = urllib.parse.parse_qs(parsed.query)
                if 'url' in query_params:
                    url = query_params['url'][0]
            # 处理onion域名
            url = url.replace('pornhubthbh7ap3u.onion', 'pornhub.com')
        return url
    
    def _get_session(self):
        """
        获取requests会话

        Returns:
            requests.Session实例
        """
        import requests
        session = requests.Session()
        # 设置请求头
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        })
        # 禁用SSL验证
        session.verify = False
        # 添加代理
        proxy = self.config.get('proxy')
        if proxy:
            session.proxies = {
                'http': proxy,
                'https': proxy,
            }
        return session
    
    def _update_session_cookies(self, session, cookies):
        """
        更新会话cookie

        Args:
            session: requests.Session实例
            cookies: cookie字典
        """
        for key, value in cookies.items():
            session.cookies.set(key, value)
    
    async def get_video_info(self, url: str) -> Dict[str, Any]:
        """
        获取视频信息
        
        Args:
            url: 视频URL
        
        Returns:
            视频信息字典
        """
        try:
            logger.info(f"Getting video info: {url}")
            
            opts = self._get_ydl_options()
            opts['quiet'] = True
            opts['no_warnings'] = True
            
            # 在线程池中运行
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(
                None,
                self._get_info_sync,
                url,
                opts
            )
            
            # 提取关键信息
            video_info = {
                'id': info.get('id', ''),
                'title': info.get('title', ''),
                'description': info.get('description', ''),
                'duration': info.get('duration', 0),
                'view_count': info.get('view_count', 0),
                'like_count': info.get('like_count', 0),
                'upload_date': info.get('upload_date', ''),
                'uploader': info.get('uploader', ''),
                'uploader_id': info.get('uploader_id', ''),
                'thumbnail': info.get('thumbnail', ''),
                'formats': [],
                'url': url,
            }
            
            # 提取可用格式
            if 'formats' in info:
                for fmt in info['formats']:
                    if fmt.get('vcodec') != 'none' or fmt.get('acodec') != 'none':
                        video_info['formats'].append({
                            'format_id': fmt.get('format_id', ''),
                            'ext': fmt.get('ext', ''),
                            'resolution': f"{fmt.get('width', 0)}x{fmt.get('height', 0)}",
                            'fps': fmt.get('fps', 0),
                            'vcodec': fmt.get('vcodec', ''),
                            'acodec': fmt.get('acodec', ''),
                            'filesize': fmt.get('filesize', 0),
                        })
            
            logger.info(f"Video info retrieved: {video_info['title']}")
            return video_info
        
        except Exception as e:
            logger.error(f"Failed to get video info: {url}, error: {str(e)}")
            return {'error': str(e), 'url': url}
    
    def _get_info_sync(self, url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
        """
        同步获取信息（在线程池中运行）
        
        Args:
            url: 视频URL
            opts: yt-dlp配置
        
        Returns:
            视频信息字典
        """
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    
    async def get_playlist_info(self, url: str) -> Dict[str, Any]:
        """
        获取播放列表信息
        
        Args:
            url: 播放列表URL
        
        Returns:
            播放列表信息
        """
        try:
            logger.info(f"Getting playlist info: {url}")
            
            opts = self._get_ydl_options()
            opts['quiet'] = True
            opts['extract_flat'] = True
            
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(
                None,
                self._get_info_sync,
                url,
                opts
            )
            
            playlist_info = {
                'id': info.get('id', ''),
                'title': info.get('title', ''),
                'description': info.get('description', ''),
                'uploader': info.get('uploader', ''),
                'video_count': info.get('playlist_count', 0),
                'videos': [],
            }
            
            # 提取视频列表
            if 'entries' in info:
                for entry in info['entries']:
                    if entry:
                        playlist_info['videos'].append({
                            'id': entry.get('id', ''),
                            'title': entry.get('title', ''),
                            'url': entry.get('url', ''),
                            'duration': entry.get('duration', 0),
                        })
            
            logger.info(f"Playlist info retrieved: {playlist_info['title']}, {len(playlist_info['videos'])} videos")
            return playlist_info
        
        except Exception as e:
            logger.error(f"Failed to get playlist info: {url}, error: {str(e)}")
            return {'error': str(e), 'url': url}
    
    def cleanup(self):
        """
        清理资源
        """
        # 可以在这里添加清理逻辑
        # 例如关闭会话、释放资源等
        pass
