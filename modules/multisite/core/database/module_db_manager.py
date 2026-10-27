# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
模块数据库管理器
为每个模块提供独立的数据库接口，同时兼容models.db
"""
import os
import sqlite3
import logging
from typing import Dict, List, Optional, Any, Set
from pathlib import Path

from .models_db_adapter import ModelsDBAdapter
from .download_cache import DownloadCacheDB
from multisite.core.utils.path_utils import get_data_path, ensure_data_dir

logger = logging.getLogger(__name__)


class ModuleDatabaseManager:
    """
    模块数据库管理器
    
    功能：
    1. 统一管理models.db（兼容现有数据）
    2. 各模块独立的数据接口
    3. 下载缓存独立管理
    
    数据库架构：
    - models.db: 模特数据（通过module字段区分）
    - download_cache.db: 下载缓存（通过module字段区分）
    """
    
    # 模块常量
    MODULE_PORN = 'PORN'
    MODULE_JAVDB = 'JAVDB'
    MODULE_MADOUQU = 'MADOUQU'
    
    def __init__(self, data_dir: str = None):
        """
        初始化管理器
        
        Args:
            data_dir: 数据目录
        """
        self.data_dir = data_dir or get_data_path('db')
        ensure_data_dir('db')
        
        # 共享数据库
        self.models_db = ModelsDBAdapter(os.path.join(self.data_dir, 'models.db'))
        self.download_cache = DownloadCacheDB(os.path.join(self.data_dir, 'download_cache.db'))
    
    # ==================== PORN模块 ====================
    
    def get_porn_models(self) -> List[Dict]:
        """获取PORN模块的所有模特"""
        return self.models_db.get_models_by_module(self.MODULE_PORN)
    
    def add_porn_model(
        self,
        name: str,
        url: str,
        country: str = '未知',
        local_directory: str = None,
        auto_download: bool = False
    ) -> int:
        """添加PORN模特"""
        # 确定模式类型
        mode = 'model' if '/model/' in url else 'pornstar' if '/pornstar/' in url else 'model'
        return self.models_db.add_model(
            name=name,
            url=url,
            module=self.MODULE_PORN,
            country=country,
            local_directory=local_directory,
            auto_download=auto_download,
            mode=mode
        )
    
    def get_porn_stats(self) -> Dict:
        """获取PORN模块统计"""
        return self.models_db.get_stats(self.MODULE_PORN)
    
    # ==================== JAVDB模块 ====================
    
    def get_javdb_models(self) -> List[Dict]:
        """获取JAVDB模块的所有模特"""
        return self.models_db.get_models_by_module(self.MODULE_JAVDB)
    
    def add_javdb_model(
        self,
        name: str,
        url: str,
        country: str = '日本',
        local_directory: str = None,
        auto_download: bool = False
    ) -> int:
        """添加JAVDB模特"""
        return self.models_db.add_model(
            name=name,
            url=url,
            module=self.MODULE_JAVDB,
            country=country,
            local_directory=local_directory,
            auto_download=auto_download
        )
    
    def get_javdb_stats(self) -> Dict:
        """获取JAVDB模块统计"""
        return self.models_db.get_stats(self.MODULE_JAVDB)
    
    # ==================== Madouqu模块 ====================
    
    def get_madouqu_models(self) -> List[Dict]:
        """获取Madouqu模块的所有模特"""
        return self.models_db.get_models_by_module(self.MODULE_MADOUQU)
    
    def add_madouqu_model(
        self,
        name: str,
        url: str = '',
        country: str = '未知',
        local_directory: str = None
    ) -> int:
        """添加Madouqu模特"""
        return self.models_db.add_model(
            name=name,
            url=url or f'madouqu://{name}',
            module=self.MODULE_MADOUQU,
            country=country,
            local_directory=local_directory
        )
    
    def get_madouqu_stats(self) -> Dict:
        """获取Madouqu模块统计"""
        return self.models_db.get_stats(self.MODULE_MADOUQU)
    
    # ==================== 通用操作 ====================
    
    def get_model(self, model_id: int) -> Optional[Dict]:
        """获取模特"""
        return self.models_db.get_model(model_id=model_id)
    
    def get_model_by_id(self, model_id: int) -> Optional[Dict]:
        """通过ID获取模特"""
        return self.get_model(model_id)
    
    def update_model_stats(
        self,
        model_id: int,
        online_count: int = None,
        local_count: int = None,
        missing_count: int = None,
        downloaded_count: int = None,
        shorty_count: int = None
    ):
        """更新模特统计

        ``downloaded_count`` / ``shorty_count`` 过去在此被静默丢弃 ——
        download_manager 算好了传进来，却没落到库里。现在透传给适配器
        （适配器会按列存在性决定是否真的写）。
        """
        self.models_db.update_model_stats(
            model_id,
            online_count=online_count,
            local_count=local_count,
            missing_count=missing_count,
            downloaded_count=downloaded_count,
            shorty_count=shorty_count,
        )
    
    def update_model_local_directory(self, model_id: int, local_directory: str):
        """更新模特的本地目录"""
        self.models_db.update_model_local_directory(model_id, local_directory)
    
    def update_model(self, model_id: int, **kwargs):
        """更新模特信息"""
        # 获取现有模特信息
        model = self.get_model(model_id)
        if not model:
            logger.error(f"更新模特失败: 模特不存在 (ID: {model_id})")
            return False
        
        # 准备更新数据
        name = kwargs.get('name', model.get('name'))
        url = kwargs.get('url', model.get('url'))
        country = kwargs.get('country', model.get('country'))
        local_directory = kwargs.get('local_directory', model.get('local_directory'))
        auto_download = kwargs.get('auto_download', model.get('auto_download', False))
        mode = kwargs.get('mode', model.get('mode', 'model'))
        
        # 调用 ModelsDBAdapter 的 add_model 方法（它会自动更新现有模特）
        return self.models_db.add_model(
            name=name,
            url=url,
            module=model.get('module', 'PORN'),
            country=country,
            local_directory=local_directory,
            auto_download=auto_download,
            mode=mode
        ) > 0
    
    def delete_model(self, model_id: int):
        """删除模特"""
        self.models_db.delete_model(model_id)
    
    def add_videos(self, model_id: int, videos: List[Dict]):
        """添加视频"""
        self.models_db.add_videos(model_id, videos)
    
    def get_videos(self, model_id: int, status: str = None) -> List[Dict]:
        """获取视频"""
        return self.models_db.get_videos_by_model(model_id, status)
    
    def get_missing_videos(self, model_id: int, downloaded_hashes: Set[str] = None) -> List[Dict]:
        """获取缺失视频（已去重，并剔除已下载/已有本地副本的幽灵行）。

        历史问题：videos 表存在大量重复行，且同一 URL 常常“既有 local 行、又有
        missing 行”。旧实现直接 ``SELECT * FROM videos WHERE status='missing'`` 会把
        这些已下好的幽灵行原样返回，导致批量下载排队几千条根本下不下来的任务，
        把带宽和重试全耗在 phantom 上。

        这里做三层过滤（去重 + 已满足剔除 + 下载缓存剔除）：
          1) 按归一化 URL 去重：同 URL 多行只保留一条；
          2) 若某 URL 任一行的 status 为 local/downloaded → 视为已满足，不列入缺失；
          3) 若某 URL 已在 download_cache 成功下载 → 也不列入缺失。

        ``downloaded_hashes`` 可选：外部预计算好的已下载哈希集合，批量调用时传入，
        避免逐模特重复全表扫描 download_cache（27824 行的全表扫 × N 模特会很慢）。
        """
        try:
            all_videos = self.models_db.get_videos_by_model(model_id)
        except Exception as e:
            logger.error(f"获取模特视频失败 model_id={model_id}: {e}")
            return []
        if not all_videos:
            return []

        # 已成功下载的 URL 哈希集合（可外部传入，避免逐模特重复全表扫描 download_cache）
        if downloaded_hashes is None:
            try:
                downloaded_hashes = self.download_cache.get_downloaded_url_hash_set()
            except Exception:
                downloaded_hashes = set()

        SATISFIED = {'local', 'downloaded'}
        by_url = {}          # norm -> {'statuses': set(), 'missing_row': dict|None}
        order = []
        for v in all_videos:
            url = (v.get('url') or '').strip()
            if not url:
                continue
            norm = self.download_cache.normalize_url(url)
            if norm not in by_url:
                by_url[norm] = {'statuses': set(), 'missing_row': None}
                order.append(norm)
            rec = by_url[norm]
            rec['statuses'].add(v.get('status'))
            if rec['missing_row'] is None and v.get('status') == 'missing':
                rec['missing_row'] = v

        result = []
        for norm in order:
            rec = by_url[norm]
            if rec['statuses'] & SATISFIED:
                continue  # 已有本地/已下载副本 → 跳过幽灵 missing 行
            row = rec['missing_row']
            if row is None:
                continue  # 没有 missing 行（如仅 online），无法下载，跳过
            if self.download_cache.get_url_hash(row.get('url', '')) in downloaded_hashes:
                continue  # 已在下载缓存（含不同域名格式）
            result.append(row)
        return result

    def get_missing_count(self, model_id: int, downloaded_hashes: Set[str] = None) -> int:
        """返回该模特去重后的缺失数（与 ``get_missing_videos`` 完全一致）。

        供扫描落库 / 统计回写使用，使 ``models.missing_count`` 与真实可下载数保持
        一致，消除“扫描显示几百/上千、实际下载远少”的幽灵计数。
        """
        return len(self.get_missing_videos(model_id, downloaded_hashes))
    
    def update_video_status(self, video_id: int, status: str, local_path: str = None):
        """更新视频状态"""
        self.models_db.update_video_status(video_id, status, local_path)
    
    # ==================== 下载缓存 ====================
    
    def is_downloaded(self, url: str, verify_file_exists: bool = True) -> bool:
        """检查是否已下载"""
        return self.download_cache.is_downloaded(url, verify_file_exists)
    
    def add_download(
        self,
        url: str,
        title: str,
        module: str,
        model_name: str = None,
        file_path: str = None
    ):
        """添加下载记录"""
        self.download_cache.add_download(
            url=url,
            title=title,
            module=module,
            model_name=model_name,
            file_path=file_path
        )
    
    def get_downloaded_urls(self, module: str = None) -> set:
        """获取已下载URL"""
        return self.download_cache.get_all_downloaded_urls(module)
    
    # ==================== 统计 ====================
    
    def get_all_stats(self) -> Dict[str, Dict]:
        """获取所有模块统计"""
        return self.models_db.get_module_stats()
    
    def get_download_stats(self, module: str = None) -> Dict:
        """获取下载统计"""
        return self.download_cache.get_stats(module)
    
    def get_all_countries(self, module: str = None) -> List[str]:
        """
        获取指定模块的所有国家列表
        
        Args:
            module: 模块名（可选）
        
        Returns:
            国家列表
        """
        conn = sqlite3.connect(self.models_db.db_path)
        cursor = conn.cursor()
        
        try:
            if module:
                cursor.execute('SELECT DISTINCT country FROM models WHERE module = ? ORDER BY country', (module,))
            else:
                cursor.execute('SELECT DISTINCT country FROM models ORDER BY country')
            
            countries = [row[0] for row in cursor.fetchall()]
            
            # 合并地区管理器中的国家列表
            from multisite.core.utils.disk_region_manager import get_disk_region_manager
            disk_region_manager = get_disk_region_manager()
            manager_countries = set(disk_region_manager.COUNTRY_REGION_MAP.keys())
            db_countries = set(countries)
            all_countries = manager_countries.union(db_countries)
            
            # 转换为列表并排序
            sorted_countries = sorted(list(all_countries))
            
            # 确保'未知'在列表中
            if '未知' not in sorted_countries:
                sorted_countries.insert(0, '未知')
            
            return sorted_countries
        except Exception as e:
            logger.error(f"获取国家列表失败: {e}")
            return ['未知']
        finally:
            conn.close()
    
    def get_shorty_video_count(self, model_id: int) -> int:
        """
        获取指定模特的小视频数量
        
        Args:
            model_id: 模特ID
        
        Returns:
            小视频数量
        """
        conn = sqlite3.connect(self.models_db.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT COUNT(*)
                FROM videos
                WHERE model_id = ? AND status = 'shorty'
            ''', (model_id,))
            result = cursor.fetchone()
            return result[0] if result else 0
        except Exception as e:
            logger.error(f"获取小视频数量失败: {e}")
            return 0
        finally:
            conn.close()
    
    def find_duplicate_models(self) -> List[List[Dict]]:
        """
        查找重复的模特（按名称不区分大小写）
        
        Returns:
            重复模特列表，每个元素是一个包含重复模特的列表
        """
        conn = sqlite3.connect(self.models_db.db_path)
        cursor = conn.cursor()
        
        try:
            # 查找名称重复的模特
            cursor.execute('''
                SELECT LOWER(name) as lower_name, GROUP_CONCAT(id, ',') as ids
                FROM models
                GROUP BY lower_name
                HAVING COUNT(*) > 1
            ''')
            
            duplicate_groups = []
            for row in cursor.fetchall():
                lower_name, id_str = row
                ids = [int(id) for id in id_str.split(',')]
                
                # 获取每个重复模特的详细信息
                models = []
                for model_id in ids:
                    model = self.get_model(model_id)
                    if model:
                        models.append(model)
                
                if models:
                    duplicate_groups.append(models)
            
            return duplicate_groups
        except Exception as e:
            logger.error(f"查找重复模特失败: {e}")
            return []
        finally:
            conn.close()


# 便捷函数：获取各模块的数据库管理器
def get_porn_db() -> ModuleDatabaseManager:
    """获取PORN模块数据库"""
    return ModuleDatabaseManager()

def get_javdb_db() -> ModuleDatabaseManager:
    """获取JAVDB模块数据库"""
    return ModuleDatabaseManager()

def get_madouqu_db() -> ModuleDatabaseManager:
    """获取Madouqu模块数据库"""
    return ModuleDatabaseManager()
