# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
全局下载队列管理器
解决并发下载、排队下载、进度显示问题
"""
import logging
import threading
import queue
from typing import Dict, List, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, Future
try:  # 优先 PyQt6（新前端统一在 PyQt6 进程内运行，避免同进程加载两套 Qt）
    from PyQt6.QtCore import QObject, pyqtSignal
except ImportError:  # 兼容仍在 PyQt5 环境下的独立运行
    from PyQt5.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)


class DownloadTask:
    """下载任务"""
    
    def __init__(self, task_id: str, url: str, title: str, model_name: str, 
                 model_id: int, output_dir: str, callback: Callable = None):
        self.task_id = task_id
        self.url = url
        self.title = title
        self.model_name = model_name
        self.model_id = model_id
        self.output_dir = output_dir
        self.callback = callback
        self.status = 'pending'  # pending, downloading, completed, failed
        self.progress = 0
        self.speed = ''
        self.remaining_time = ''


class DownloadQueueManager(QObject):
    """全局下载队列管理器"""
    
    # 信号
    task_added = pyqtSignal(str, dict)      # 任务添加 (task_id, task_data)
    task_updated = pyqtSignal(str, dict)    # 任务更新 (task_id, update_data)
    task_completed = pyqtSignal(str, dict)  # 任务完成 (task_id, result)
    all_completed = pyqtSignal()            # 所有任务完成
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not hasattr(self, '_initialized'):
            super().__init__()
            self._initialized = True
            
            # 任务队列
            self.task_queue = queue.Queue()
            self.tasks: Dict[str, DownloadTask] = {}
            self.futures: Dict[str, Future] = {}
            
            # 线程池
            self.executor: Optional[ThreadPoolExecutor] = None
            self.max_workers = 3
            
            # 状态
            self.is_running = False
            self.active_count = 0
            self.completed_count = 0
            self.total_count = 0
            
            # 锁
            self._task_lock = threading.Lock()
    
    def set_concurrency(self, count: int):
        """设置并发数"""
        self.max_workers = max(1, min(10, count))
        
        # 重新创建线程池
        if self.executor:
            self.executor.shutdown(wait=False)
        
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        logger.info(f"并发数设置为: {self.max_workers}")
    
    def add_task(self, task: DownloadTask):
        """添加任务到队列"""
        with self._task_lock:
            self.tasks[task.task_id] = task
            self.total_count += 1
        
        # 添加到队列
        self.task_queue.put(task)
        
        # 发送信号
        self.task_added.emit(task.task_id, {
            'name': task.title[:50],
            'model_name': task.model_name,
            'status': task.status,
            'progress': 0
        })
        
        logger.info(f"任务添加: {task.title[:30]} (队列: {self.task_queue.qsize()})")
        
        # 启动处理
        if not self.is_running:
            self._start_processing()
    
    def add_tasks(self, tasks: List[DownloadTask]):
        """批量添加任务"""
        for task in tasks:
            self.add_task(task)
    
    def _start_processing(self):
        """启动队列处理"""
        if self.is_running:
            return
        
        self.is_running = True
        
        # 确保线程池存在
        if not self.executor:
            self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        
        # 启动调度线程
        threading.Thread(target=self._process_queue, daemon=True).start()
    
    def _process_queue(self):
        """处理队列（在独立线程中运行）"""
        logger.info("下载队列处理启动")
        
        while self.is_running:
            try:
                # 从队列获取任务
                task = self.task_queue.get(timeout=1)
                
                # 提交到线程池
                future = self.executor.submit(self._download_task, task)
                
                with self._task_lock:
                    self.futures[task.task_id] = future
                    self.active_count += 1
                
                logger.info(f"任务开始: {task.title[:30]} (活跃: {self.active_count})")
                
            except queue.Empty:
                # 队列为空，检查是否所有任务都完成
                with self._task_lock:
                    if self.completed_count >= self.total_count and self.total_count > 0:
                        logger.info("所有任务完成")
                        self.all_completed.emit()
                        self.is_running = False
                        break
            except Exception as e:
                logger.error(f"队列处理错误: {e}")
        
        logger.info("下载队列处理结束")
    
    def _download_task(self, task: DownloadTask):
        """下载单个任务（在线程池中执行）"""
        try:
            # 更新状态
            task.status = 'downloading'
            self.task_updated.emit(task.task_id, {
                'status': 'downloading',
                'progress': 0
            })
            
            # 调用下载回调
            if task.callback:
                result = task.callback(task)
            else:
                # 默认下载逻辑
                result = self._default_download(task)
            
            # 更新完成状态
            task.status = 'completed' if result.get('success') else 'failed'
            
            with self._task_lock:
                self.completed_count += 1
                self.active_count -= 1
            
            # 发送完成信号
            self.task_completed.emit(task.task_id, result)
            self.task_updated.emit(task.task_id, {
                'status': task.status,
                'progress': 100
            })
            
            logger.info(f"任务完成: {task.title[:30]} ({task.status})")
            
        except Exception as e:
            logger.error(f"任务失败: {task.title[:30]} - {e}")
            
            task.status = 'failed'
            with self._task_lock:
                self.completed_count += 1
                self.active_count -= 1
            
            self.task_completed.emit(task.task_id, {
                'success': False,
                'error': str(e)
            })
    
    def _default_download(self, task: DownloadTask) -> dict:
        """默认下载逻辑"""
        import asyncio
        import os
        
        try:
            # 导入下载器
            from multisite.core.downloader.factory import DownloaderFactory
            from multisite.core.utils.temp_download_manager import TempDownloadManager
            from multisite.core.utils.proxy_config import get_global_proxy_config as _get_global_proxy_config
            
            # 创建事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # 获取代理配置
            proxy_config = _get_global_proxy_config()
            download_config = {
                'proxy': proxy_config,
                'timeout': 60,
                'retry_times': 5,
            }
            
            # 创建下载器
            downloader = DownloaderFactory.create_downloader('yt-dlp', download_config)
            temp_manager = TempDownloadManager()
            
            # 确保输出目录存在
            if task.output_dir:
                os.makedirs(task.output_dir, exist_ok=True)
            
            # 获取临时文件路径
            temp_file = temp_manager.start_download(
                video_url=task.url,
                video_title=task.title,
                model_id=task.model_id,
                model_name=task.model_name,
                target_dir=task.output_dir
            )
            
            # 更新进度
            self.task_updated.emit(task.task_id, {
                'progress': 10,
                'speed': '准备中'
            })
            
            # 下载
            success = loop.run_until_complete(downloader.download(task.url, temp_file))
            loop.close()
            
            if success:
                # 移动到目标目录
                result = temp_manager.complete_download(task.url)
                
                if result['success']:
                    return {
                        'success': True,
                        'file_path': result['target_file']
                    }
                else:
                    return {
                        'success': False,
                        'error': result.get('error', '移动失败')
                    }
            else:
                temp_manager.fail_download(task.url, '下载失败')
                return {
                    'success': False,
                    'error': '下载失败'
                }
                
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def update_task_progress(self, task_id: str, progress: int, speed: str = '', remaining_time: str = ''):
        """更新任务进度"""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.progress = progress
            task.speed = speed
            task.remaining_time = remaining_time
            
            self.task_updated.emit(task_id, {
                'progress': progress,
                'speed': speed,
                'remaining_time': remaining_time
            })
    
    def get_queue_size(self) -> int:
        """获取队列大小"""
        return self.task_queue.qsize()
    
    def get_active_count(self) -> int:
        """获取活跃任务数"""
        return self.active_count
    
    def get_total_count(self) -> int:
        """获取总任务数"""
        return self.total_count
    
    def get_completed_count(self) -> int:
        """获取已完成任务数"""
        return self.completed_count
    
    def stop(self):
        """停止队列处理"""
        self.is_running = False
        
        if self.executor:
            self.executor.shutdown(wait=False)
            self.executor = None


# 全局实例
download_queue = DownloadQueueManager()
