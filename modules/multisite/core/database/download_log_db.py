# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
下载日志数据库
记录每次下载的详细信息，按日期和模特查询下载历史
"""
import os
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
import sqlite3

from multisite.core.database.db_connection_pool import DatabaseConnectionPool
from multisite.core.utils.path_utils import get_data_path

logger = logging.getLogger(__name__)


class DownloadLogDB(DatabaseConnectionPool):
    """
    下载日志数据库
    
    功能：
    1. 记录每次下载的详细信息
    2. 按日期查询下载历史
    3. 按模特查询下载记录
    4. 支持时间范围筛选
    """
    
    def _init_db(self):
        """初始化数据库表"""
        super()._init_db()
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 创建下载日志表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER,
                model_name TEXT NOT NULL,
                video_title TEXT NOT NULL,
                video_url TEXT NOT NULL,
                file_path TEXT,
                file_size INTEGER DEFAULT 0,
                download_date TEXT NOT NULL,
                download_time TEXT NOT NULL,
                status TEXT DEFAULT 'success',
                error_msg TEXT,
                module TEXT DEFAULT 'PORN',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 创建索引提高查询性能
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_download_date ON download_logs(download_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_model_name ON download_logs(model_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_model_id ON download_logs(model_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_status ON download_logs(status)')
        
        conn.commit()
    
    def __init__(self, db_path: str = None):
        """
        初始化数据库
        
        Args:
            db_path: 数据库文件路径
        """
        db_path = db_path or get_data_path('db', 'download_logs.db')
        super().__init__(db_path)
    
    def add_download_log(
        self,
        model_id: int,
        model_name: str,
        video_title: str,
        video_url: str,
        file_path: str = None,
        file_size: int = 0,
        status: str = 'success',
        error_msg: str = None,
        module: str = 'PORN'
    ) -> bool:
        """
        添加下载日志记录
        
        Args:
            model_id: 模特ID
            model_name: 模特名
            video_title: 视频标题
            video_url: 视频URL
            file_path: 文件路径
            file_size: 文件大小
            status: 下载状态
            error_msg: 错误信息
            module: 模块名
        
        Returns:
            是否成功
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            now = datetime.now()
            download_date = now.strftime('%Y-%m-%d')
            download_time = now.strftime('%H:%M:%S')
            
            cursor.execute('''
                INSERT INTO download_logs 
                (model_id, model_name, video_title, video_url, file_path, file_size, 
                 download_date, download_time, status, error_msg, module)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                model_id, model_name, video_title, video_url, 
                file_path, file_size, download_date, download_time,
                status, error_msg, module
            ))
            
            conn.commit()
            logger.info(f"记录下载日志: {model_name} - {video_title[:30]}...")
            return True
            
        except Exception as e:
            logger.error(f"添加下载日志失败: {e}")
            return False
        finally:
            self._close_connection()
    
    def get_recent_logs(
        self,
        limit: int = 50,
        module: str = 'PORN'
    ) -> List[Dict[str, Any]]:
        """
        最近 N 条下载记录（最新在前）

        Args:
            limit: 返回条数上限
            module: 模块名

        Returns:
            下载记录列表
        """
        limit = max(1, min(int(limit or 50), 500))
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT id, model_id, model_name, video_title, video_url, 
                       file_path, file_size, download_date, download_time, 
                       status, error_msg, module, created_at
                FROM download_logs 
                WHERE module = ?
                ORDER BY download_date DESC, download_time DESC, id DESC
                LIMIT ?
            ''', (module, limit))
            
            rows = cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
            
        finally:
            self._close_connection()

    def get_logs_by_date(
        self,
        date: str,
        module: str = 'PORN'
    ) -> List[Dict[str, Any]]:
        """
        查询指定日期的下载记录
        
        Args:
            date: 日期字符串 (YYYY-MM-DD)
            module: 模块名
        
        Returns:
            下载记录列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT id, model_id, model_name, video_title, video_url, 
                       file_path, file_size, download_date, download_time, 
                       status, error_msg, module, created_at
                FROM download_logs 
                WHERE download_date = ? AND module = ?
                ORDER BY download_time ASC
            ''', (date, module))
            
            rows = cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
            
        finally:
            self._close_connection()
    
    def get_logs_by_date_range(
        self,
        start_date: str,
        end_date: str,
        module: str = 'PORN'
    ) -> List[Dict[str, Any]]:
        """
        查询日期范围内的下载记录
        
        Args:
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            module: 模块名
        
        Returns:
            下载记录列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT id, model_id, model_name, video_title, video_url, 
                       file_path, file_size, download_date, download_time, 
                       status, error_msg, module, created_at
                FROM download_logs 
                WHERE download_date BETWEEN ? AND ? AND module = ?
                ORDER BY download_date DESC, download_time DESC
            ''', (start_date, end_date, module))
            
            rows = cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
            
        finally:
            self._close_connection()
    
    def get_logs_by_model(
        self,
        model_name: str = None,
        model_id: int = None,
        start_date: str = None,
        end_date: str = None,
        module: str = 'PORN'
    ) -> List[Dict[str, Any]]:
        """
        查询指定模特的下载记录
        
        Args:
            model_name: 模特名
            model_id: 模特ID
            start_date: 开始日期
            end_date: 结束日期
            module: 模块名
        
        Returns:
            下载记录列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            query = '''
                SELECT id, model_id, model_name, video_title, video_url, 
                       file_path, file_size, download_date, download_time, 
                       status, error_msg, module, created_at
                FROM download_logs 
                WHERE module = ?
            '''
            params = [module]
            
            if model_name:
                query += ' AND model_name = ?'
                params.append(model_name)
            
            if model_id:
                query += ' AND model_id = ?'
                params.append(model_id)
            
            if start_date:
                query += ' AND download_date >= ?'
                params.append(start_date)
            
            if end_date:
                query += ' AND download_date <= ?'
                params.append(end_date)
            
            query += ' ORDER BY download_date DESC, download_time DESC'
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
            
        finally:
            self._close_connection()
    
    def get_all_models(self, module: str = 'PORN') -> List[str]:
        """
        获取所有有下载记录的模特名
        
        Args:
            module: 模块名
        
        Returns:
            模特名列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT DISTINCT model_name 
                FROM download_logs 
                WHERE module = ?
                ORDER BY model_name ASC
            ''', (module,))
            
            return [row[0] for row in cursor.fetchall()]
            
        finally:
            self._close_connection()
    
    def get_all_dates(self, module: str = 'PORN') -> List[str]:
        """
        获取所有有下载记录的日期
        
        Args:
            module: 模块名
        
        Returns:
            日期列表 (YYYY-MM-DD)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT DISTINCT download_date 
                FROM download_logs 
                WHERE module = ?
                ORDER BY download_date DESC
            ''', (module,))
            
            return [row[0] for row in cursor.fetchall()]
            
        finally:
            self._close_connection()
    
    def get_stats_by_date(
        self,
        start_date: str = None,
        end_date: str = None,
        module: str = 'PORN'
    ) -> List[Dict[str, Any]]:
        """
        获取按日期分组的统计信息
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            module: 模块名
        
        Returns:
            统计列表 [{date, model_name, count, total_size}, ...]
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            query = '''
                SELECT download_date, model_name, COUNT(*) as count, 
                       SUM(file_size) as total_size
                FROM download_logs 
                WHERE module = ?
            '''
            params = [module]
            
            if start_date:
                query += ' AND download_date >= ?'
                params.append(start_date)
            
            if end_date:
                query += ' AND download_date <= ?'
                params.append(end_date)
            
            query += ' GROUP BY download_date, model_name ORDER BY download_date DESC, model_name ASC'
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            return [
                {
                    'date': row[0],
                    'model_name': row[1],
                    'count': row[2],
                    'total_size': row[3] or 0,
                    'total_size_mb': round((row[3] or 0) / (1024 * 1024), 2)
                }
                for row in rows
            ]
            
        finally:
            self._close_connection()
    
    def get_total_stats(self, module: str = 'PORN') -> Dict[str, Any]:
        """
        获取总体统计信息
        
        Args:
            module: 模块名
        
        Returns:
            统计信息
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT COUNT(*), SUM(file_size), COUNT(DISTINCT model_name), 
                       MIN(download_date), MAX(download_date)
                FROM download_logs 
                WHERE module = ?
            ''', (module,))
            
            row = cursor.fetchone()
            
            return {
                'total_downloads': row[0] or 0,
                'total_size': row[1] or 0,
                'total_size_mb': round((row[1] or 0) / (1024 * 1024), 2),
                'total_size_gb': round((row[1] or 0) / (1024 * 1024 * 1024), 2),
                'total_models': row[2] or 0,
                'first_download': row[3],
                'last_download': row[4]
            }
            
        finally:
            self._close_connection()
    
    def delete_log(self, log_id: int) -> bool:
        """
        删除指定日志记录
        
        Args:
            log_id: 日志ID
        
        Returns:
            是否成功
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('DELETE FROM download_logs WHERE id = ?', (log_id,))
            conn.commit()
            return True
            
        except Exception as e:
            logger.error(f"删除日志失败: {e}")
            return False
        finally:
            self._close_connection()
    
    def clear_logs(self, module: str = 'PORN') -> bool:
        """
        清空所有日志记录
        
        Args:
            module: 模块名
        
        Returns:
            是否成功
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('DELETE FROM download_logs WHERE module = ?', (module,))
            conn.commit()
            return True
            
        except Exception as e:
            logger.error(f"清空日志失败: {e}")
            return False
        finally:
            self._close_connection()
    
    def _row_to_dict(self, row) -> Dict[str, Any]:
        """将数据库行转换为字典"""
        return {
            'id': row[0],
            'model_id': row[1],
            'model_name': row[2],
            'video_title': row[3],
            'video_url': row[4],
            'file_path': row[5],
            'file_size': row[6],
            'download_date': row[7],
            'download_time': row[8],
            'status': row[9],
            'error_msg': row[10],
            'module': row[11],
            'created_at': row[12]
        }
    
    def close(self):
        """关闭数据库连接
        
        提供与其他数据库类一致的接口
        """
        self.cleanup()
