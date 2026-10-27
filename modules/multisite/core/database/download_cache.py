# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
下载缓存数据库
记录已下载的视频，避免重复下载
"""
import os
import re
import json
import hashlib
import logging
from datetime import datetime
from typing import Dict, List, Set, Any, Optional
from pathlib import Path
import sqlite3
import threading

from multisite.core.database.db_connection_pool import DatabaseConnectionPool
from multisite.core.utils.path_utils import get_data_path, ensure_data_dir

logger = logging.getLogger(__name__)


class DownloadCacheDB(DatabaseConnectionPool):
    """
    下载缓存数据库

    功能：
    1. 记录已下载的视频URL
    2. 检查视频是否已下载
    3. 支持按模块分类（PORN/JAVDB/Madouqu）
    """

    def _init_db(self):
        """初始化数据库表"""
        super()._init_db()
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                url_hash TEXT NOT NULL,
                title TEXT,
                module TEXT DEFAULT 'porn',
                model_name TEXT,
                file_path TEXT,
                file_size INTEGER DEFAULT 0,
                status TEXT DEFAULT 'completed',
                downloaded_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 数据库迁移：添加新列
        self._migrate_database(conn, cursor)

        cursor.execute('CREATE INDEX IF NOT EXISTS idx_url_hash ON downloads(url_hash)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_module ON downloads(module)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_model_name ON downloads(model_name)')

        conn.commit()

    def _migrate_database(self, conn, cursor):
        """数据库迁移：添加新列"""
        try:
            # 检查是否需要迁移
            cursor.execute("PRAGMA table_info(downloads)")
            columns = [col[1] for col in cursor.fetchall()]
            
            # 添加缺失的列
            if 'error_code' not in columns:
                cursor.execute('ALTER TABLE downloads ADD COLUMN error_code TEXT')
                logger.info("添加列: error_code")
            
            if 'error_message' not in columns:
                cursor.execute('ALTER TABLE downloads ADD COLUMN error_message TEXT')
                logger.info("添加列: error_message")
            
            if 'retry_count' not in columns:
                cursor.execute('ALTER TABLE downloads ADD COLUMN retry_count INTEGER DEFAULT 0')
                logger.info("添加列: retry_count")
            
            if 'last_attempt_at' not in columns:
                cursor.execute('ALTER TABLE downloads ADD COLUMN last_attempt_at TEXT')
                logger.info("添加列: last_attempt_at")
            
        except Exception as e:
            logger.warning(f"数据库迁移失败: {e}")

    def __init__(self, db_path: str = None):
        """
        初始化数据库

        Args:
            db_path: 数据库文件路径
        """
        db_path = db_path or get_data_path('db', 'download_cache.db')
        super().__init__(db_path)

    def _normalize_url(self, url: str) -> str:
        """
        标准化URL，使其能够正确匹配相同视频的不同域名格式
        
        例如：
        - https://cn.pornhub.com/view_video.php?viewkey=xxx
        - https://www.pornhub.com/view_video.php?viewkey=xxx
        应该被视为同一个URL
        """
        if not url:
            return url
            
        # 移除协议
        url = url.replace('https://', '').replace('http://', '')
        
        # 移除 www. 和 cn. 等子域名
        url = re.sub(r'^(www\.|cn\.|jp\.|de\.|fr\.|es\.)', '', url, flags=re.IGNORECASE)
        
        # 转换为小写
        return url.lower()

    def _get_url_hash(self, url: str) -> str:
        """计算URL哈希（使用标准化后的URL）"""
        normalized_url = self._normalize_url(url)
        return hashlib.md5(normalized_url.encode('utf-8')).hexdigest()

    # ---- 公开接口：供外部（如批量下载去重）复用归一化/哈希逻辑 ----
    def normalize_url(self, url: str) -> str:
        """公开的标准化URL接口（剥离协议/子域/大小写）。供去重使用。"""
        return self._normalize_url(url)

    def get_url_hash(self, url: str) -> str:
        """公开的URL哈希接口（与 is_downloaded 内部一致）。"""
        return self._get_url_hash(url)

    def get_downloaded_url_hash_set(self, module: str = None) -> Set[str]:
        """返回所有“已成功下载”记录的 URL 哈希集合。

        用于批量去重：一次查询拿到全部哈希，避免逐条 is_downloaded 的大量往返。
        仅统计 status != 'failed' 的记录（failed 视为未成功，需要重新下载）。
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            if module:
                cursor.execute(
                    'SELECT url_hash FROM downloads WHERE module = ? AND status != ?',
                    (module, 'failed'),
                )
            else:
                cursor.execute('SELECT url_hash FROM downloads WHERE status != ?', ('failed',))
            return {row[0] for row in cursor.fetchall() if row[0]}
        finally:
            self._close_connection()

    def is_downloaded(self, url: str, verify_file_exists: bool = True) -> bool:
        """
        检查URL是否已下载（支持不同域名格式）

        Args:
            url: 视频URL
            verify_file_exists: 是否验证实际文件存在

        Returns:
            是否已下载
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            # 方法1: 使用标准化URL哈希（排除失败的记录）
            url_hash = self._get_url_hash(url)
            cursor.execute('SELECT file_path FROM downloads WHERE url_hash = ? AND status != ?', (url_hash, 'failed'))
            result = cursor.fetchone()
            
            if result is not None:
                if verify_file_exists:
                    file_path = result[0]
                    if file_path and os.path.exists(file_path):
                        return True
                    return False
                return True
            
            # 方法2: 通过viewkey匹配（处理数据库中旧记录）
            import re
            viewkey_match = re.search(r'viewkey=([a-zA-Z0-9]+)', url)
            if viewkey_match:
                viewkey = viewkey_match.group(1)
                # 只查询成功的下载记录，排除失败的
                cursor.execute('SELECT file_path FROM downloads WHERE url LIKE ? AND status != ?', (f'%viewkey={viewkey}%', 'failed'))
                result = cursor.fetchone()
                
                if result is not None:
                    if verify_file_exists:
                        file_path = result[0]
                        if file_path and os.path.exists(file_path):
                            return True
                        return False
                    return True
            
            return False
        finally:
            self._close_connection()

    def get_title_by_url(self, url: str) -> str:
        """
        通过URL获取已下载视频的标题（支持不同域名格式）

        Args:
            url: 视频URL

        Returns:
            视频标题，如果没有则返回空字符串
        """
        import re
        url_hash = self._get_url_hash(url)

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            # 方法1: 使用标准化URL哈希
            cursor.execute('SELECT title FROM downloads WHERE url_hash = ?', (url_hash,))
            result = cursor.fetchone()
            
            if result and result[0]:
                return result[0]
            
            # 方法2: 通过viewkey匹配
            viewkey_match = re.search(r'viewkey=([a-zA-Z0-9]+)', url)
            if viewkey_match:
                viewkey = viewkey_match.group(1)
                cursor.execute('SELECT title FROM downloads WHERE url LIKE ?', (f'%viewkey={viewkey}%',))
                result = cursor.fetchone()
                
                if result and result[0]:
                    return result[0]
            
            return ""
        finally:
            self._close_connection()

    def add_download(
        self,
        url: str,
        title: str = None,
        module: str = 'porn',
        model_name: str = None,
        file_path: str = None,
        file_size: int = 0,
        status: str = 'completed'
    ) -> bool:
        """
        添加下载记录

        Args:
            url: 视频URL
            title: 视频标题
            module: 模块名（porn/javdb/madouqu）
            model_name: 模特名
            file_path: 文件路径
            file_size: 文件大小
            status: 状态（completed/failed/skipped）

        Returns:
            是否成功
        """
        url_hash = self._get_url_hash(url)

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('''
                INSERT OR REPLACE INTO downloads
                (url, url_hash, title, module, model_name, file_path, file_size, status, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                url, url_hash, title, module, model_name, file_path, file_size, status,
                datetime.now().isoformat()
            ))

            conn.commit()
            return True
        except Exception as e:
            logger.error(f"添加下载记录失败: {e}")
            return False
        finally:
            self._close_connection()

    def add_failed_download(
        self,
        url: str,
        title: str = None,
        module: str = 'porn',
        model_name: str = None,
        error_code: str = None,
        error_message: str = None
    ) -> bool:
        """
        添加下载失败记录

        Args:
            url: 视频URL
            title: 视频标题
            module: 模块名（porn/javdb/madouqu）
            model_name: 模特名
            error_code: 错误码
            error_message: 错误信息

        Returns:
            是否成功
        """
        url_hash = self._get_url_hash(url)

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            # 获取当前重试次数
            cursor.execute('SELECT retry_count FROM downloads WHERE url_hash = ?', (url_hash,))
            result = cursor.fetchone()
            retry_count = result[0] + 1 if result else 1

            cursor.execute('''
                INSERT OR REPLACE INTO downloads
                (url, url_hash, title, module, model_name, status, error_code, error_message, retry_count, last_attempt_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                url, url_hash, title, module, model_name, 'failed', error_code, error_message,
                retry_count, datetime.now().isoformat()
            ))

            conn.commit()
            return True
        except Exception as e:
            logger.error(f"添加下载失败记录失败: {e}")
            return False
        finally:
            self._close_connection()

    def is_permanently_failed(self, url: str, max_retries: int = 3) -> bool:
        """
        检查URL是否已永久失败（超过最大重试次数或特定错误码）

        Args:
            url: 视频URL
            max_retries: 最大重试次数

        Returns:
            是否已永久失败
        """
        # 先用标准化URL查找
        url_hash = self._get_url_hash(url)

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            # 尝试用标准化URL哈希查找
            cursor.execute('SELECT retry_count, error_code FROM downloads WHERE url_hash = ? AND status = ?', (url_hash, 'failed'))
            result = cursor.fetchone()

            # 如果没找到，尝试用viewkey匹配
            if result is None:
                import re
                viewkey_match = re.search(r'viewkey=([a-zA-Z0-9]+)', url)
                if viewkey_match:
                    viewkey = viewkey_match.group(1)
                    cursor.execute('SELECT retry_count, error_code FROM downloads WHERE url LIKE ? AND status = ?', (f'%viewkey={viewkey}%', 'failed'))
                    result = cursor.fetchone()

            if result is None:
                return False

            retry_count, error_code = result

            # 如果是410 Gone错误，直接标记为永久失败
            if error_code == '410':
                return True

            # 如果是视频已被禁用，直接标记为永久失败
            if error_code == 'DISABLED':
                return True

            # 若是 error_classifier 判定为永久不可用（视频已删除/下架），
            # 同样直接标记永久失败，避免跨会话反复重试。
            if error_code == 'VIDEO_UNAVAILABLE':
                return True

            # 如果重试次数超过最大值，标记为永久失败
            if retry_count >= max_retries:
                return True

            return False
        finally:
            self._close_connection()

    def reset_failed_download(self, url: str) -> bool:
        """
        重置失败的下载记录，允许重新尝试下载

        Args:
            url: 视频URL

        Returns:
            是否成功
        """
        url_hash = self._get_url_hash(url)

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('DELETE FROM downloads WHERE url_hash = ? AND status = ?', (url_hash, 'failed'))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"重置失败记录失败: {e}")
            return False
        finally:
            self._close_connection()

    def clear_all_failed_downloads(self, module: str = None) -> int:
        """
        清除所有失败的下载记录

        Args:
            module: 模块名（可选，不指定则清除全部）

        Returns:
            清除的记录数
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            total = 0
            # 清除 download_status 表的失败记录
            try:
                if module:
                    cursor.execute('DELETE FROM download_status WHERE status = ?', ('failed',))
                else:
                    cursor.execute('DELETE FROM download_status WHERE status = ?', ('failed',))
                total += cursor.rowcount
            except Exception:
                logger.debug("clear_all_failed_downloads 忽略异常", exc_info=True)
            # 清除 downloads 表的失败记录
            if module:
                cursor.execute('DELETE FROM downloads WHERE status = ? AND module = ?', ('failed', module))
            else:
                cursor.execute('DELETE FROM downloads WHERE status = ?', ('failed',))
            total += cursor.rowcount
            
            conn.commit()
            logger.info(f"清除了 {total} 条失败下载记录")
            return total
        except Exception as e:
            logger.error(f"清除失败记录失败: {e}")
            return 0
        finally:
            self._close_connection()

    def get_failed_downloads(self, module: str = None) -> List[Dict]:
        """
        获取所有失败的下载记录

        Args:
            module: 模块名（可选）

        Returns:
            失败记录列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            # 先从 download_status 表获取（实时下载失败记录）
            try:
                if module:
                    cursor.execute('''
                        SELECT * FROM download_status
                        WHERE status = ?
                        ORDER BY created_at DESC
                    ''', ('failed',))
                else:
                    cursor.execute('''
                        SELECT * FROM download_status
                        WHERE status = ?
                        ORDER BY created_at DESC
                    ''', ('failed',))

                status_columns = ['id', 'video_url', 'video_title', 'model_id', 'model_name',
                                  'target_dir', 'temp_file', 'status', 'progress', 'file_size',
                                  'error_msg', 'started_at', 'completed_at', 'created_at']
                status_rows = cursor.fetchall()
                if status_rows:
                    results = []
                    for row in status_rows:
                        record = dict(zip(status_columns, row))
                        # 从 error_msg 中提取 yt-dlp 错误码
                        error_msg = record.get('error_msg', '') or ''
                        error_code = None
                        import re
                        code_match = re.search(r'\u4ee3\u7801 (\d+)', error_msg)
                        if code_match:
                            error_code = code_match.group(1)
                        elif 'cannot access local variable' in error_msg:
                            error_code = 'SCRIPT_ERROR'
                        elif 'WinError 32' in error_msg:
                            error_code = 'FILE_LOCKED'
                        elif '\u6587\u4ef6\u592a\u5c0f' in error_msg:
                            error_code = 'FILE_TOO_SMALL'
                        results.append({
                            'id': record['id'],
                            'url': record.get('video_url', ''),
                            'title': record.get('video_title', ''),
                            'model_name': record.get('model_name', ''),
                            'error_code': error_code,
                            'error_message': error_msg,
                            'retry_count': 0,
                            'last_attempt_at': record.get('started_at', ''),
                        })
                    return results
            except Exception:
                logger.debug("get_failed_downloads 忽略异常", exc_info=True)

            # 回退到 downloads 表
            if module:
                cursor.execute('''
                    SELECT * FROM downloads
                    WHERE status = ? AND module = ?
                    ORDER BY retry_count DESC, last_attempt_at DESC
                ''', ('failed', module))
            else:
                cursor.execute('''
                    SELECT * FROM downloads
                    WHERE status = ?
                    ORDER BY retry_count DESC, last_attempt_at DESC
                ''', ('failed',))

            columns = ['id', 'url', 'url_hash', 'title', 'module', 'model_name',
                       'file_path', 'file_size', 'status', 'downloaded_at', 'created_at',
                       'error_code', 'error_message', 'retry_count', 'last_attempt_at']

            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            self._close_connection()

    def unlock_failed_download(self, url: str) -> bool:
        """
        解封单个失败的下载记录，允许重新尝试

        Args:
            url: 视频URL

        Returns:
            是否成功
        """
        url_hash = self._get_url_hash(url)

        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            # 先尝试从 download_status 表删除
            try:
                cursor.execute('DELETE FROM download_status WHERE video_url = ? AND status = ?', (url, 'failed'))
                if cursor.rowcount > 0:
                    conn.commit()
                    logger.info(f"解封成功 (download_status): {url}")
                    return True
            except Exception:
                logger.debug("unlock_failed_download 忽略异常", exc_info=True)

            # 再尝试从 downloads 表重置
            cursor.execute('''
                UPDATE downloads 
                SET retry_count = 0, last_attempt_at = NULL
                WHERE url_hash = ? AND status = ?
            ''', (url_hash, 'failed'))

            conn.commit()
            if cursor.rowcount > 0:
                logger.info(f"解封成功: {url}")
                return True
            return False
        except Exception as e:
            logger.error(f"解封失败: {e}")
            return False
        finally:
            self._close_connection()

    def unlock_all_failed_downloads(self, module: str = None) -> int:
        """
        解封所有失败的下载记录（删除记录，允许重新尝试下载）

        Args:
            module: 模块名（可选）

        Returns:
            解封的记录数
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            total = 0
            # 解封 download_status 表的失败记录
            try:
                cursor.execute('DELETE FROM download_status WHERE status = ?', ('failed',))
                total += cursor.rowcount
            except Exception:
                logger.debug("unlock_all_failed_downloads 忽略异常", exc_info=True)
            # 解封 downloads 表的失败记录
            if module:
                cursor.execute('DELETE FROM downloads WHERE status = ? AND module = ?', ('failed', module))
            else:
                cursor.execute('DELETE FROM downloads WHERE status = ?', ('failed',))
            total += cursor.rowcount

            conn.commit()
            logger.info(f"解封了 {total} 条失败下载记录")
            return total
        except Exception as e:
            logger.error(f"解封失败记录失败: {e}")
            return 0
        finally:
            self._close_connection()
    def is_failed_download(self, url: str) -> bool:
        """
        检查URL是否在失败记录中（永久失败）

        Args:
            url: 视频URL

        Returns:
            是否在失败记录中
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            # 方法1: 检查 download_status 表
            try:
                cursor.execute('SELECT COUNT(*) FROM download_status WHERE video_url = ? AND status = ?', (url, 'failed'))
                if cursor.fetchone()[0] > 0:
                    return True
            except Exception:
                logger.debug("is_failed_download 忽略异常", exc_info=True)

            # 方法2: 使用标准化URL哈希查找 downloads 表
            url_hash = self._get_url_hash(url)
            cursor.execute('SELECT retry_count, error_code FROM downloads WHERE url_hash = ? AND status = ?', (url_hash, 'failed'))
            result = cursor.fetchone()

            # 方法2: 通过viewkey匹配
            if result is None:
                import re
                viewkey_match = re.search(r'viewkey=([a-zA-Z0-9]+)', url)
                if viewkey_match:
                    viewkey = viewkey_match.group(1)
                    cursor.execute('SELECT retry_count, error_code FROM downloads WHERE url LIKE ? AND status = ?', (f'%viewkey={viewkey}%', 'failed'))
                    result = cursor.fetchone()

            if result is None:
                return False

            retry_count, error_code = result

            # 如果是410 Gone错误，永久失败
            if error_code == '410':
                return True

            # 如果是视频已被禁用，永久失败
            if error_code == 'DISABLED':
                return True

            # 如果重试次数超过3次，永久失败
            if retry_count and retry_count >= 3:
                return True

            return False
        finally:
            self._close_connection()

    def get_downloads_by_model(self, model_name: str, module: str = None) -> List[Dict]:
        """
        获取模特的下载记录

        Args:
            model_name: 模特名
            module: 模块名（可选）

        Returns:
            下载记录列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            if module:
                cursor.execute('''
                    SELECT * FROM downloads
                    WHERE model_name = ? AND module = ?
                    ORDER BY downloaded_at DESC
                ''', (model_name, module))
            else:
                cursor.execute('''
                    SELECT * FROM downloads
                    WHERE model_name = ?
                    ORDER BY downloaded_at DESC
                ''', (model_name,))

            rows = cursor.fetchall()

            columns = ['id', 'url', 'url_hash', 'title', 'module', 'model_name',
                       'file_path', 'file_size', 'status', 'downloaded_at', 'created_at']

            return [dict(zip(columns, row)) for row in rows]
        finally:
            self._close_connection()

    def get_all_downloaded_urls(self, module: str = None) -> Set[str]:
        """
        获取所有已下载的URL

        Args:
            module: 模块名（可选）

        Returns:
            URL集合
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            if module:
                cursor.execute('SELECT url FROM downloads WHERE module = ?', (module,))
            else:
                cursor.execute('SELECT url FROM downloads')

            urls = {row[0] for row in cursor.fetchall()}

            return urls
        finally:
            self._close_connection()

    def debug_url_matching(self, url: str) -> dict:
        """调试URL匹配过程"""
        normalized = self._normalize_url(url)
        url_hash = self._get_url_hash(url)
        
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # 检查是否存在该URL哈希的记录
            cursor.execute('SELECT url, title, file_path FROM downloads WHERE url_hash = ?', (url_hash,))
            result = cursor.fetchone()
            
            if result:
                return {
                    'input_url': url,
                    'normalized_url': normalized,
                    'url_hash': url_hash,
                    'stored_url': result[0],
                    'title': result[1],
                    'file_path': result[2],
                    'status': 'FOUND'
                }
            else:
                # 检查是否存在相似URL（通过viewkey匹配）
                import re
                viewkey_match = re.search(r'viewkey=([a-zA-Z0-9]+)', url)
                if viewkey_match:
                    viewkey = viewkey_match.group(1)
                    cursor.execute('SELECT url, url_hash FROM downloads WHERE url LIKE ?', (f'%viewkey={viewkey}%',))
                    similar = cursor.fetchall()
                    if similar:
                        return {
                            'input_url': url,
                            'normalized_url': normalized,
                            'url_hash': url_hash,
                            'similar_urls': similar,
                            'status': 'SIMILAR_FOUND'
                        }
                
                return {
                    'input_url': url,
                    'normalized_url': normalized,
                    'url_hash': url_hash,
                    'status': 'NOT_FOUND'
                }
        finally:
            self._close_connection()

    def get_stats(self, module: str = None) -> Dict[str, Any]:
        """
        获取统计信息

        Args:
            module: 模块名（可选）

        Returns:
            统计信息
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            if module:
                cursor.execute('SELECT COUNT(*) FROM downloads WHERE module = ?', (module,))
                total = cursor.fetchone()[0]

                cursor.execute('SELECT SUM(file_size) FROM downloads WHERE module = ?', (module,))
                total_size = cursor.fetchone()[0] or 0
            else:
                cursor.execute('SELECT COUNT(*) FROM downloads')
                total = cursor.fetchone()[0]

                cursor.execute('SELECT SUM(file_size) FROM downloads')
                total_size = cursor.fetchone()[0] or 0

            return {
                'total_downloads': total,
                'total_size': total_size,
                'total_size_mb': round(total_size / (1024 * 1024), 2),
                'total_size_gb': round(total_size / (1024 * 1024 * 1024), 2),
            }
        finally:
            self._close_connection()

    def clear(self, module: str = None):
        """
        清除记录

        Args:
            module: 模块名（可选，不指定则清除全部）
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            if module:
                cursor.execute('DELETE FROM downloads WHERE module = ?', (module,))
            else:
                cursor.execute('DELETE FROM downloads')

            conn.commit()
        finally:
            self._close_connection()
