# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
数据库连接池基类
提供线程安全的数据库连接池管理
"""
import sqlite3
import threading
from typing import Optional
from multisite.core.utils.path_utils import get_data_path, ensure_data_dir
import logging

logger = logging.getLogger(__name__)


class DatabaseConnectionPool:
    """
    数据库连接池基类
    
    功能：
    1. 线程安全的连接池管理
    2. 自动连接有效性检查
    3. 连接复用和清理
    """
    
    def __init__(self, db_path: str):
        """
        初始化连接池
        
        Args:
            db_path: 数据库文件路径
        """
        self.db_path = db_path
        ensure_data_dir('db')
        
        # 连接池管理
        self._connection_pool = {}
        self._pool_lock = threading.Lock()
        
        # 初始化数据库表
        self._init_db()
    
    def _get_connection(self) -> sqlite3.Connection:
        """
        从连接池获取数据库连接

        Returns:
            sqlite3.Connection: 数据库连接
        """
        thread_id = threading.get_ident()
        
        try:
            with self._pool_lock:
                if thread_id not in self._connection_pool:
                    # 创建新连接
                    conn = sqlite3.connect(
                        self.db_path, 
                        timeout=10,  # 减少超时时间，避免长时间阻塞
                        check_same_thread=False  # 允许多线程访问
                    )
                    
                    # 启用WAL模式提高并发性能
                    conn.execute('PRAGMA journal_mode=WAL')
                    conn.execute('PRAGMA synchronous=NORMAL')  # 平衡性能和数据安全
                    conn.execute('PRAGMA cache_size=-2000')  # 2MB缓存
                    conn.execute('PRAGMA busy_timeout=10000')  # 减少到10秒超时
                    conn.execute('PRAGMA foreign_keys=ON')  # 启用外键约束
                    
                    conn.row_factory = sqlite3.Row
                    self._connection_pool[thread_id] = conn
                else:
                    conn = self._connection_pool[thread_id]
                    # 检查连接是否有效
                    try:
                        conn.execute('SELECT 1')
                    except sqlite3.OperationalError:
                        # 连接无效，重新创建
                        conn = sqlite3.connect(
                            self.db_path, 
                            timeout=10,
                            check_same_thread=False
                        )
                        
                        # 启用WAL模式
                        conn.execute('PRAGMA journal_mode=WAL')
                        conn.execute('PRAGMA synchronous=NORMAL')
                        conn.execute('PRAGMA cache_size=-2000')
                        conn.execute('PRAGMA busy_timeout=10000')
                        conn.execute('PRAGMA foreign_keys=ON')
                        
                        conn.row_factory = sqlite3.Row
                        self._connection_pool[thread_id] = conn
            
            return conn
        except Exception as e:
            # 记录错误并返回新连接
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"获取数据库连接失败: {e}")
            # 创建临时连接作为备用
            conn = sqlite3.connect(
                self.db_path, 
                timeout=5,
                check_same_thread=False
            )
            conn.execute('PRAGMA journal_mode=WAL')
            conn.row_factory = sqlite3.Row
            return conn
    
    def _close_connection(self):
        """
        关闭当前线程的数据库连接
        """
        thread_id = threading.get_ident()
        
        with self._pool_lock:
            if thread_id in self._connection_pool:
                try:
                    self._connection_pool[thread_id].close()
                except Exception:
                    logger.debug("_close_connection 忽略异常", exc_info=True)
                del self._connection_pool[thread_id]
    
    @classmethod
    def cleanup_connections(cls, connection_pool: dict, pool_lock: threading.Lock):
        """
        清理所有数据库连接

        Args:
            connection_pool: 连接池字典
            pool_lock: 连接池锁
        """
        with pool_lock:
            for thread_id, conn in list(connection_pool.items()):
                try:
                    conn.close()
                except Exception:
                    logger.debug("cleanup_connections 忽略异常", exc_info=True)
            connection_pool.clear()

    def cleanup(self):
        """
        清理当前实例的数据库连接池
        """
        self.cleanup_connections(self._connection_pool, self._pool_lock)

    def __del__(self):
        """
        析构函数，确保连接池被清理
        """
        try:
            self.cleanup()
        except Exception:
            logger.debug("__del__ 忽略异常", exc_info=True)

    def _init_db(self):
        """
        初始化数据库表（子类实现）
        """
        pass
