# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
数据库适配器 - 模特数据操作
"""
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Optional, Any
from pathlib import Path
import os

logger = logging.getLogger(__name__)


class ModelsDBAdapter:
    """模特数据库适配器
    
    提供对模特数据的CRUD操作，支持：
    - 模特信息的增删改查
    - 视频记录管理
    - 下载状态跟踪
    """
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_db_exists()
    
    def _get_connection(self):
        """获取数据库连接（带 WAL 模式和 timeout，避免并发锁冲突）"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA busy_timeout=30000')
        return conn
    
    def _ensure_db_exists(self):
        """确保数据库和表存在"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 创建模特表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                url TEXT,
                module TEXT DEFAULT 'PORN',
                country TEXT DEFAULT '未知',
                local_directory TEXT,
                auto_download INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                mode TEXT DEFAULT 'model',
                online_count INTEGER DEFAULT 0,
                local_count INTEGER DEFAULT 0,
                missing_count INTEGER DEFAULT 0,
                last_checked TEXT
            )
        ''')
        
        # 创建视频表
        # ⚠️ 2026-09-22 体检修复：路径列叫 local_path（现场库就是这个名字），
        # 原来这里写的是 file_path —— 而 update_video_status(status='completed')
        # 也去写 file_path，库里没这列 ⇒ 抛 OperationalError 被 except 吞掉，
        # 下载完成状态永远写不进库（静默）。两处已统一为 local_path。
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER,
                title TEXT,
                url TEXT UNIQUE,
                local_path TEXT,
                file_size INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                download_time TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (model_id) REFERENCES models(id)
            )
        ''')
        
        # 创建索引
        # 命名与 porn_db.py 保持一致（idx_model_* / idx_video_*）——
        # 历史上两套 DDL 各建了一套同义索引，同库里出现
        # models.name ×3、videos.model_id ×3、videos.status ×2 的冗余
        # （2026-09-22 体检 C2）。这里只保留一套；历史冗余索引的
        # DROP 由 temp/clean_redundant_indexes.py 单独执行。
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_model_name ON models(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_model_module ON models(module)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_model_url ON models(url)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_video_model ON videos(model_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_video_status ON videos(status)')
        
        self._ensure_legacy_columns(cursor)

        conn.commit()
        conn.close()
    
    def _ensure_legacy_columns(self, cursor):
        """补齐历史库缺失的列（幂等）。

        现场库（服务端 .110 / 本地 data/db）是由**更早版本**的 DDL 建的，
        之后 ``CREATE TABLE IF NOT EXISTS`` 全部跳过 ⇒ 新加的列永远不存在。
        典型受害者：``models.last_checked``（检测时间从未回写，体检 B2）。
        这里按 PRAGMA 探测后补齐；对已有该列的库是完全的 no-op。
        """
        wanted = {
            'models': [('last_checked', 'TEXT')],
            'videos': [('local_path', 'TEXT')],
        }
        for table, cols in wanted.items():
            try:
                cursor.execute(f'PRAGMA table_info({table})')
                existing = {row[1] for row in cursor.fetchall()}
            except Exception:
                logger.debug(f"读取 {table} 表结构失败", exc_info=True)
                continue
            for col, decl in cols:
                if col in existing:
                    continue
                try:
                    cursor.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')
                    logger.info(f"补充历史库缺失列: {table}.{col} ({decl})")
                except Exception as e:
                    logger.warning(f"补充列失败 {table}.{col}: {e}")

    # 表结构缓存：{table: frozenset(column_names)}。
    # 现场库由多代 DDL 混建，同名表在不同部署上列集不同（如 videos 在服务端
    # 有 is_missing/source、没有 created_at/download_time；adapter 新建的库反之）。
    # 拼 SQL 前必须先探测，否则会踩 no such column。
    _columns_cache: Dict[str, frozenset] = None

    def _table_columns(self, cursor, table: str) -> frozenset:
        """返回表的列名集合（进程内缓存）。探测失败返回空集合（调用方走保守分支）。"""
        if ModelsDBAdapter._columns_cache is None:
            ModelsDBAdapter._columns_cache = {}
        cache = ModelsDBAdapter._columns_cache
        if table in cache:
            return cache[table]
        try:
            cursor.execute(f'PRAGMA table_info({table})')
            cols = frozenset(row[1] for row in cursor.fetchall())
        except Exception:
            logger.debug(f"读取 {table} 表结构失败", exc_info=True)
            cols = frozenset()
        cache[table] = cols
        return cols
    
    # ==================== 模特操作 ====================
    
    def add_model(
        self,
        name: str,
        url: str,
        module: str = 'PORN',
        country: str = '未知',
        local_directory: str = None,
        auto_download: bool = False,
        mode: str = 'model'
    ) -> int:
        """
        添加或更新模特（智能合并，不覆盖已有数据）
        
        Args:
            name: 模特名
            url: 模特URL
            module: 模块（PORN/JAVDB/MADOUQU）
            country: 国家
            local_directory: 本地目录
            auto_download: 是否自动下载
        
        Returns:
            模特ID
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # 先检查是否已存在同名模特（不区分大小写）
            cursor.execute('SELECT id FROM models WHERE name COLLATE NOCASE = ?', (name,))
            existing_model = cursor.fetchone()
            
            if existing_model:
                # 获取现有模特信息，只更新非空字段
                cursor.execute('SELECT * FROM models WHERE id = ?', (existing_model[0],))
                existing_data = cursor.fetchone()
                
                # 只更新非空字段，保留原有值（避免覆盖）
                update_url = url if url else existing_data[2]  # 保留原URL
                update_module = module if module else existing_data[3] or module
                update_country = country if country else existing_data[4] or country
                update_local_dir = local_directory if local_directory else existing_data[5]
                
                # 如果没有明确指定auto_download，保留原值
                if auto_download is not None:
                    update_auto = auto_download
                else:
                    update_auto = bool(existing_data[6])
                
                # 如果没有明确指定mode，保留原值
                if mode is not None:
                    update_mode = mode
                else:
                    update_mode = existing_data[9] if len(existing_data) > 9 else mode
                
                cursor.execute('''
                    UPDATE models 
                    SET url = ?, module = ?, country = ?, local_directory = ?, auto_download = ?, mode = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (update_url, update_module, update_country, update_local_dir, 1 if update_auto else 0, update_mode, existing_model[0]))
                model_id = existing_model[0]
                logger.info(f"更新模特(保留原数据): {name} [{update_module}]")
            else:
                # 插入新模特
                cursor.execute('''
                    INSERT INTO models 
                    (name, url, module, country, local_directory, auto_download, mode, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (name, url, module, country, local_directory, 1 if auto_download else 0, mode))
                model_id = cursor.lastrowid
                logger.info(f"添加新模特: {name} [{module}]")
            
            conn.commit()
            
            # 自动备份（新增或更新模特后）
            try:
                from multisite.core.utils.db_backup_manager import auto_backup
                backup_result = auto_backup(reason=f"add_model_{name}")
                if backup_result['success']:
                    logger.info(f"  📦 数据库已自动备份 (#{backup_result['backup_number']})")
            except Exception as backup_err:
                logger.warning(f"  ⚠️ 自动备份失败（不影响主操作）: {backup_err}")
            
            return model_id
            
        except Exception as e:
            logger.error(f"添加模特失败: {e}")
            return 0
        finally:
            conn.close()
    
    def get_model(self, model_id: int = None, name: str = None) -> Optional[Dict]:
        """获取模特信息"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        if model_id:
            cursor.execute('SELECT * FROM models WHERE id = ?', (model_id,))
        elif name:
            cursor.execute('SELECT * FROM models WHERE name COLLATE NOCASE = ?', (name,))
        else:
            conn.close()
            return None
        
        row = cursor.fetchone()
        
        if row:
            # 动态获取列名
            cursor.execute('PRAGMA table_info(models)')
            columns = [col[1] for col in cursor.fetchall()]
            conn.close()
            return dict(zip(columns, row))
        
        conn.close()
        return None
    
    def get_models_by_module(self, module: str) -> List[Dict]:
        """
        获取指定模块的所有模特
        
        Args:
            module: 模块名（PORN/JAVDB/MADOUQU）
        
        Returns:
            模特列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM models 
            WHERE module = ? 
            ORDER BY updated_at DESC
        ''', (module,))
        
        rows = cursor.fetchall()
        
        # 动态获取列名
        cursor.execute('PRAGMA table_info(models)')
        columns = [col[1] for col in cursor.fetchall()]
        conn.close()
        
        return [dict(zip(columns, row)) for row in rows]
    
    def get_all_models(self) -> List[Dict]:
        """获取所有模特"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM models ORDER BY updated_at DESC')
        rows = cursor.fetchall()
        
        cursor.execute('PRAGMA table_info(models)')
        columns = [col[1] for col in cursor.fetchall()]
        conn.close()
        
        return [dict(zip(columns, row)) for row in rows]
    
    def delete_model(self, model_id: int) -> bool:
        """删除模特及其所有视频"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # 先删除关联的视频
            cursor.execute('DELETE FROM videos WHERE model_id = ?', (model_id,))
            deleted_videos = cursor.rowcount
            
            # 删除模特
            cursor.execute('DELETE FROM models WHERE id = ?', (model_id,))
            deleted_models = cursor.rowcount
            
            conn.commit()
            
            if deleted_models > 0:
                logger.info(f"删除模特 ID={model_id} 及其 {deleted_videos} 个视频")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"删除模特失败: {e}")
            return False
        finally:
            conn.close()
    
    def update_model_url(self, model_id: int, url: str) -> bool:
        """更新模特URL"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('UPDATE models SET url = ? WHERE id = ?', (url, model_id))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"更新模特URL失败: {e}")
            return False
        finally:
            conn.close()
    
    # ==================== 视频操作 ====================
    
    def add_video(self, model_id: int, title: str, url: str, status: str = 'pending') -> int:
        """添加视频记录"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO videos (model_id, title, url, status)
                VALUES (?, ?, ?, ?)
            ''', (model_id, title, url, status))
            
            video_id = cursor.lastrowid
            conn.commit()
            return video_id
            
        except Exception as e:
            logger.error(f"添加视频失败: {e}")
            return 0
        finally:
            conn.close()
    
    def get_video_by_url(self, url: str) -> Optional[Dict]:
        """通过URL获取视频信息"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM videos WHERE url = ?', (url,))
        row = cursor.fetchone()
        
        if row:
            cursor.execute('PRAGMA table_info(videos)')
            columns = [col[1] for col in cursor.fetchall()]
            conn.close()
            return dict(zip(columns, row))
        
        conn.close()
        return None
    
    def get_videos_by_model(self, model_id: int, status: str = None) -> List[Dict]:
        """获取模特的所有视频"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # 检查 videos 表是否有 created_at 字段
            cursor.execute('PRAGMA table_info(videos)')
            columns = [col[1] for col in cursor.fetchall()]
            has_created_at = 'created_at' in columns
            
            if status:
                if has_created_at:
                    cursor.execute('SELECT * FROM videos WHERE model_id = ? AND status = ? ORDER BY created_at DESC', (model_id, status))
                else:
                    cursor.execute('SELECT * FROM videos WHERE model_id = ? AND status = ?', (model_id, status))
            else:
                if has_created_at:
                    cursor.execute('SELECT * FROM videos WHERE model_id = ? ORDER BY created_at DESC', (model_id,))
                else:
                    cursor.execute('SELECT * FROM videos WHERE model_id = ?', (model_id,))
            
            rows = cursor.fetchall()
            
            # 动态获取列名
            cursor.execute('PRAGMA table_info(videos)')
            columns = [col[1] for col in cursor.fetchall()]
            return [dict(zip(columns, row)) for row in rows]
        except Exception as e:
            logger.error(f"获取视频失败: {e}")
            return []
        finally:
            conn.close()
    
    def get_pending_downloads(self, limit: int = 100) -> List[Dict]:
        """获取待下载的视频列表"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # 检查 videos 表是否有 created_at 字段
            cursor.execute('PRAGMA table_info(videos)')
            columns = [col[1] for col in cursor.fetchall()]
            has_created_at = 'created_at' in columns
            
            if has_created_at:
                cursor.execute('''
                    SELECT v.*, m.name as model_name, m.module, m.local_directory
                    FROM videos v
                    JOIN models m ON v.model_id = m.id
                    WHERE v.status = 'pending'
                    ORDER BY v.created_at ASC
                    LIMIT ?
                ''', (limit,))
            else:
                cursor.execute('''
                    SELECT v.*, m.name as model_name, m.module, m.local_directory
                    FROM videos v
                    JOIN models m ON v.model_id = m.id
                    WHERE v.status = 'pending'
                    LIMIT ?
                ''', (limit,))
            
            rows = cursor.fetchall()
            
            cursor.execute('PRAGMA table_info(videos)')
            video_columns = [col[1] for col in cursor.fetchall()]
            video_columns.extend(['model_name', 'module', 'local_directory'])
            return [dict(zip(video_columns, row)) for row in rows]
        except Exception as e:
            logger.error(f"获取待下载视频失败: {e}")
            return []
        finally:
            conn.close()
    
    def update_video_status(self, video_id: int, status: str, local_path: str = None, file_size: int = 0) -> bool:
        """更新视频下载状态

        ⚠️ 2026-09-22 体检修复两处：

        1. 原实现写的是 ``file_path`` / ``download_time`` 两列，而**现场库
           （服务端 .110 与本地 data/db）根本没有这两列** —— SQL 抛
           ``OperationalError: no such column`` 后被 except 吞掉，只留下一条
           logger.error，**调用方拿到 False 却不知道原因**。凡走到
           ``status='completed'`` 分支的完成状态都写不进库。
        2. ``else`` 分支只更新 status、把传进来的 ``local_path`` 丢掉
           ⇒ 体检 A4：82,036 行 ``status='local'`` 却 100% 没有 ``local_path``。
           有路径就应该落盘。

        列是否存在改为运行时探测，兼容历代 DDL 混建出来的库。
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cols = self._table_columns(cursor, 'videos')

            sets = ['status = ?']
            params: List[Any] = [status]

            if local_path and (not cols or 'local_path' in cols):
                sets.append('local_path = ?')
                params.append(local_path)
                if file_size and (not cols or 'file_size' in cols):
                    sets.append('file_size = ?')
                    params.append(file_size)

            # download_time / completed_at 二选一（现场库两者都没有则跳过）
            if status == 'completed':
                if 'download_time' in cols:
                    sets.append('download_time = CURRENT_TIMESTAMP')
                elif 'completed_at' in cols:
                    sets.append('completed_at = CURRENT_TIMESTAMP')

            params.append(video_id)
            cursor.execute(f'UPDATE videos SET {", ".join(sets)} WHERE id = ?', params)

            conn.commit()
            return cursor.rowcount > 0

        except Exception as e:
            logger.error(f"更新视频状态失败: {e}")
            return False
        finally:
            conn.close()
    
    def get_download_statistics(self) -> Dict[str, Any]:
        """获取下载统计信息"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        stats = {}
        
        # 总模特数
        cursor.execute('SELECT COUNT(*) FROM models')
        stats['total_models'] = cursor.fetchone()[0]
        
        # 各模块模特数
        cursor.execute('SELECT module, COUNT(*) FROM models GROUP BY module')
        stats['models_by_module'] = {row[0]: row[1] for row in cursor.fetchall()}
        
        # 总视频数
        cursor.execute('SELECT COUNT(*) FROM videos')
        stats['total_videos'] = cursor.fetchone()[0]
        
        # 各状态视频数
        cursor.execute('SELECT status, COUNT(*) FROM videos GROUP BY status')
        stats['videos_by_status'] = {row[0]: row[1] for row in cursor.fetchall()}
        
        # 已下载总大小
        cursor.execute('SELECT COALESCE(SUM(file_size), 0) FROM videos WHERE status = "completed"')
        stats['total_size'] = cursor.fetchone()[0]
        
        conn.close()
        return stats
    
    def update_model_stats(self, model_id: int, online_count: int = None, local_count: int = None,
                           missing_count: int = None, downloaded_count: int = None,
                           shorty_count: int = None, touch_last_checked: bool = True):
        """更新模特统计信息

        ⚠️ 2026-09-22 体检修复 B2：原实现**从不写 ``last_checked``**，
        而 ``last_checked`` 是「上次检测时间」的唯一存储位 —— 结果是
        828 个模特里只有 13 个有值、最新的停在 2026-04-02，检测流程一直在跑
        却没有任何时间证据（也是 S1「单日重复跑两整轮」缺判据的同源问题）。
        现在每次写入统计都会顺带刷新它。

        ``downloaded_count`` / ``shorty_count`` 在部分库中不存在，按列探测后写，
        不再静默丢弃（原实现连形参都没有，调用方传了等于没传）。
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cols = self._table_columns(cursor, 'models')

            updates = []
            params = []

            for name, value in (
                ('online_count', online_count),
                ('local_count', local_count),
                ('missing_count', missing_count),
                ('downloaded_count', downloaded_count),
                ('shorty_count', shorty_count),
            ):
                if value is None:
                    continue
                if cols and name not in cols:
                    logger.debug(f"models 表无 {name} 列，跳过")
                    continue
                updates.append(f'{name} = ?')
                params.append(value)

            if touch_last_checked and (not cols or 'last_checked' in cols):
                updates.append('last_checked = ?')
                params.append(datetime.now().isoformat())

            if updates:
                params.append(model_id)
                query = f'UPDATE models SET {", ".join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?'
                cursor.execute(query, params)
                conn.commit()
                return cursor.rowcount > 0

            return False

        except Exception as e:
            logger.error(f"更新模特统计失败: {e}")
            return False
        finally:
            conn.close()

    def update_model_local_directory(self, model_id: int, local_directory: str) -> bool:
        """更新模特的本地目录（module_db_manager 依赖此方法）"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                'UPDATE models SET local_directory = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                (local_directory, model_id))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"更新模特本地目录失败: {e}")
            return False
        finally:
            conn.close()

    def get_stats(self, module: str = None) -> Dict[str, Any]:
        """获取统计信息。

        ⚠️ 2026-09-22 修复：``ModuleDatabaseManager.get_porn_stats()`` 调用的就是
        本方法，但本类此前**没有实现它** —— 而 ``ModuleDatabaseManager.get_porn_stats``
        又被 ``PornDownloadManager.get_stats()``（download_manager.py）调用，
        属活路径，一旦走到必然 ``AttributeError``。

        返回键名对齐 ``PornDatabase.get_stats()``（model_count / video_count /
        missing_count / downloaded_count），保持"兼容 PornDatabase 接口"的语义。
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            if module:
                cursor.execute('SELECT COUNT(*) FROM models WHERE module = ?', (module,))
            else:
                cursor.execute('SELECT COUNT(*) FROM models')
            model_count = cursor.fetchone()[0]

            if module:
                query = ('SELECT COUNT(*) FROM videos v JOIN models m ON v.model_id = m.id '
                         'WHERE m.module = ?')
                args = (module,)
            else:
                query = 'SELECT COUNT(*) FROM videos'
                args = ()

            cursor.execute(query, args)
            video_count = cursor.fetchone()[0]

            cols = self._table_columns(cursor, 'videos')
            if cols and 'is_missing' in cols:
                # 现场库的真相字段是 is_missing（status 会漂移，体检 A2）
                missing_where = 'v.is_missing = 1'
            else:
                missing_where = "v.status = 'missing'"

            if module:
                cursor.execute(
                    f'SELECT COUNT(*) FROM videos v JOIN models m ON v.model_id = m.id '
                    f'WHERE m.module = ? AND {missing_where}', (module,))
            else:
                cursor.execute(f'SELECT COUNT(*) FROM videos v WHERE {missing_where}')
            missing_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM videos WHERE status = 'downloaded'")
            downloaded_count = cursor.fetchone()[0]

            return {
                'model_count': model_count,
                'video_count': video_count,
                'missing_count': missing_count,
                'downloaded_count': downloaded_count,
            }
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {'model_count': 0, 'video_count': 0, 'missing_count': 0, 'downloaded_count': 0}
        finally:
            conn.close()

    def get_module_stats(self) -> Dict[str, Dict]:
        """按模块汇总统计（module_db_manager.get_all_stats 依赖此方法）"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute('SELECT DISTINCT module FROM models WHERE module IS NOT NULL')
            modules = [row[0] for row in cursor.fetchall()]
            return {m: self.get_stats(m) for m in modules}
        except Exception as e:
            logger.error(f"获取模块统计失败: {e}")
            return {}
        finally:
            conn.close()
    
    def add_videos(self, model_id: int, videos: List[Dict]):
        """批量添加视频"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            for video in videos:
                title = video.get('title', '')
                url = video.get('url', '')
                status = video.get('status', 'pending')
                
                if url:
                    cursor.execute('''
                        INSERT OR IGNORE INTO videos (model_id, title, url, status)
                        VALUES (?, ?, ?, ?)
                    ''', (model_id, title, url, status))
            
            conn.commit()
        except Exception as e:
            logger.error(f"批量添加视频失败: {e}")
        finally:
            conn.close()