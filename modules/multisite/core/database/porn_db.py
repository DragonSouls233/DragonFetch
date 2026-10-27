# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
PORN模块数据库
模特订阅、视频记录、下载状态
"""
import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path
import sqlite3
import threading

from multisite.core.database.db_connection_pool import DatabaseConnectionPool
from multisite.core.utils.path_utils import get_data_path, ensure_data_dir

logger = logging.getLogger(__name__)


class PornDatabase(DatabaseConnectionPool):
    """
    PORN模块数据库

    功能：
    1. 模特订阅管理
    2. 视频记录
    3. 下载状态跟踪
    """

    def _init_db(self):
        """初始化数据库表"""
        super()._init_db()
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                avatar TEXT,
                country TEXT DEFAULT '',
                local_directory TEXT,
                online_count INTEGER DEFAULT 0,
                local_count INTEGER DEFAULT 0,
                missing_count INTEGER DEFAULT 0,
                downloaded_count INTEGER DEFAULT 0,
                shorty_count INTEGER DEFAULT 0,
                last_checked TEXT,
                auto_download INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                mode TEXT DEFAULT 'model'
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER,
                title TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                duration TEXT,
                thumbnail TEXT,
                status TEXT DEFAULT 'missing',
                local_path TEXT,
                file_size INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (model_id) REFERENCES models(id)
            )
        ''')

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_model_name ON models(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_model_url ON models(url)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_video_model ON videos(model_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_video_status ON videos(status)')

        conn.commit()

    def __init__(self, db_path: str = None):
        """
        初始化数据库

        Args:
            db_path: 数据库文件路径
        """
        # 默认使用 models.db（统一数据库），兼容现有数据
        db_path = db_path or get_data_path('db', 'models.db')
        super().__init__(db_path)

    def add_model(
        self,
        name: str,
        url: str,
        local_directory: str = None,
        avatar: str = None,
        auto_download: bool = False,
        country: str = '',
        mode: str = 'model'
    ) -> int:
        """
        添加模特订阅

        Args:
            name: 模特名
            url: 模特URL
            local_directory: 本地目录
            avatar: 头像URL
            auto_download: 是否自动下载
            country: 国家代码

        Returns:
            模特ID
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('''
                INSERT INTO models (name, url, local_directory, avatar, auto_download, country, mode)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (name, url, local_directory, avatar, 1 if auto_download else 0, country, mode))

            model_id = cursor.lastrowid
            conn.commit()

            logger.info(f"添加模特订阅: {name}")
            return model_id
        except sqlite3.IntegrityError:
            logger.warning(f"模特已存在: {url}")
            cursor.execute('SELECT id FROM models WHERE url = ?', (url,))
            return cursor.fetchone()[0]
        finally:
            self._close_connection()

    def get_model(self, model_id: int = None, url: str = None, name: str = None) -> Optional[Dict]:
        """获取模特信息"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            if model_id:
                cursor.execute('SELECT * FROM models WHERE id = ?', (model_id,))
            elif url:
                cursor.execute('SELECT * FROM models WHERE url = ?', (url,))
            elif name:
                cursor.execute('SELECT * FROM models WHERE name = ?', (name,))
            else:
                return None

            row = cursor.fetchone()

            if row:
                columns = ['id', 'name', 'url', 'avatar', 'country', 'local_directory', 'online_count',
                           'local_count', 'missing_count', 'downloaded_count', 'shorty_count', 'last_checked',
                           'auto_download', 'created_at', 'updated_at', 'mode']
                return dict(zip(columns, row))
            return None
        finally:
            self._close_connection()

    def get_model_by_name(self, name: str) -> Optional[Dict]:
        """根据名称获取模特"""
        return self.get_model(name=name)

    def get_all_models(self) -> List[Dict]:
        """获取所有模特"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('SELECT * FROM models ORDER BY updated_at DESC')
            rows = cursor.fetchall()

            columns = ['id', 'name', 'url', 'avatar', 'country', 'local_directory', 'online_count',
                       'local_count', 'missing_count', 'downloaded_count', 'shorty_count', 'last_checked',
                       'auto_download', 'created_at', 'updated_at', 'mode']

            return [dict(zip(columns, row)) for row in rows]
        finally:
            self._close_connection()

    def update_model_stats(
        self,
        model_id: int,
        online_count: int = None,
        local_count: int = None,
        missing_count: int = None,
        downloaded_count: int = None,
        shorty_count: int = None
    ):
        """更新模特统计"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            updates = []
            params = []

            if online_count is not None:
                updates.append('online_count = ?')
                params.append(online_count)
            if local_count is not None:
                updates.append('local_count = ?')
                params.append(local_count)
            if missing_count is not None:
                updates.append('missing_count = ?')
                params.append(missing_count)
            if downloaded_count is not None:
                updates.append('downloaded_count = ?')
                params.append(downloaded_count)
            if shorty_count is not None:
                updates.append('shorty_count = ?')
                params.append(shorty_count)

            updates.append('last_checked = ?')
            params.append(datetime.now().isoformat())
            updates.append('updated_at = ?')
            params.append(datetime.now().isoformat())

            params.append(model_id)

            cursor.execute(f'''
                UPDATE models SET {', '.join(updates)} WHERE id = ?
            ''', params)

            conn.commit()
        finally:
            self._close_connection()

    def delete_model(self, model_id: int):
        """删除模特"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('DELETE FROM videos WHERE model_id = ?', (model_id,))
            cursor.execute('DELETE FROM models WHERE id = ?', (model_id,))

            conn.commit()
        finally:
            self._close_connection()

    def add_video(
        self,
        model_id: int,
        title: str,
        url: str,
        duration: str = None,
        thumbnail: str = None,
        status: str = 'missing'
    ) -> int:
        """添加视频"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('''
                INSERT INTO videos (model_id, title, url, duration, thumbnail, status)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (model_id, title, url, duration, thumbnail, status))

            video_id = cursor.lastrowid
            conn.commit()
            return video_id
        except sqlite3.IntegrityError:
            # 视频已存在，更新其状态
            cursor.execute('UPDATE videos SET status = ?, title = ?, duration = ?, thumbnail = ? WHERE url = ?',
                           (status, title, duration, thumbnail, url))
            cursor.execute('SELECT id FROM videos WHERE url = ?', (url,))
            video_id = cursor.fetchone()[0]
            conn.commit()
            return video_id
        finally:
            self._close_connection()

    def add_videos(self, model_id: int, videos: List[Dict]):
        """批量添加视频"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            for video in videos:
                try:
                    cursor.execute('''
                        INSERT OR REPLACE INTO videos (model_id, title, url, duration, thumbnail, status)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (
                        model_id,
                        video.get('title', ''),
                        video.get('url', ''),
                        video.get('duration', ''),
                        video.get('thumbnail', ''),
                        video.get('status', 'missing')
                    ))
                except Exception as e:
                    logger.warning(f"添加视频失败: {e}")
                    pass

            conn.commit()
        finally:
            self._close_connection()

    def get_videos_by_model(self, model_id: int, status: str = None) -> List[Dict]:
        """获取模特的视频"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            if status:
                cursor.execute('SELECT * FROM videos WHERE model_id = ? AND status = ?', (model_id, status))
            else:
                cursor.execute('SELECT * FROM videos WHERE model_id = ?', (model_id,))

            rows = cursor.fetchall()

            columns = ['id', 'model_id', 'title', 'url', 'duration', 'thumbnail',
                       'status', 'local_path', 'file_size', 'created_at']

            return [dict(zip(columns, row)) for row in rows]
        finally:
            self._close_connection()

    def update_video_status(self, video_id: int, status: str, local_path: str = None, file_size: int = 0):
        """更新视频状态"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            if local_path:
                cursor.execute('''
                    UPDATE videos SET status = ?, local_path = ?, file_size = ? WHERE id = ?
                ''', (status, local_path, file_size, video_id))
            else:
                cursor.execute('UPDATE videos SET status = ? WHERE id = ?', (status, video_id))

            conn.commit()
        finally:
            self._close_connection()

    def get_missing_videos(self, model_id: int) -> List[Dict]:
        """获取缺失视频"""
        return self.get_videos_by_model(model_id, status='missing')

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('SELECT COUNT(*) FROM models')
            model_count = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM videos')
            video_count = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM videos WHERE status = "missing"')
            missing_count = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM videos WHERE status = "downloaded"')
            downloaded_count = cursor.fetchone()[0]

            return {
                'model_count': model_count,
                'video_count': video_count,
                'missing_count': missing_count,
                'downloaded_count': downloaded_count,
            }
        finally:
            self._close_connection()
