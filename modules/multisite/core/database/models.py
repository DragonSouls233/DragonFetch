# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
数据库模型 - 模块区分架构
支持不同模块（PORN、JAVDB、Madouqu等）的数据隔离
"""
import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path

from multisite.core.utils.path_utils import get_data_path, ensure_data_dir

logger = logging.getLogger(__name__)


class ModuleType(Enum):
    """模块类型枚举"""
    PORN = "porn"
    JAVDB = "javdb"
    MADOUQU = "madouqu"
    PORN91 = "porn91"
    XVIDEOS = "xvideos"
    XHAMSTER = "xhamster"


@dataclass
class ModelInfo:
    """模特信息"""
    name: str
    module: str  # 所属模块：porn, javdb, madouqu等
    url: Optional[str] = None
    avatar: Optional[str] = None
    video_count: int = 0
    local_count: int = 0
    missing_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()
        if self.updated_at is None:
            self.updated_at = datetime.now().isoformat()


@dataclass
class VideoInfo:
    """视频信息"""
    title: str
    model_name: str
    module: str  # 所属模块
    url: Optional[str] = None
    magnet_link: Optional[str] = None  # BT磁力链接（Madouqu用）
    thumbnail: Optional[str] = None
    duration: Optional[str] = None
    size: Optional[str] = None
    date: Optional[str] = None
    status: str = "missing"  # missing, downloaded, local
    local_path: Optional[str] = None
    created_at: Optional[str] = None
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()


class ModuleDatabase:
    """
    模块化数据库管理器
    
    特点：
    1. 每个模块独立的数据存储
    2. 支持PORN、JAVDB、Madouqu等模块
    3. 统一的接口，隔离的数据
    """
    
    def __init__(self, db_dir: str = None):
        """
        初始化数据库
        
        Args:
            db_dir: 数据库目录
        """
        self.db_dir = db_dir or get_data_path('db')
        ensure_data_dir('db')
        
        # 各模块的数据文件
        self.module_files = {
            ModuleType.PORN: os.path.join(self.db_dir, 'porn_models.json'),
            ModuleType.JAVDB: os.path.join(self.db_dir, 'javdb_models.json'),
            ModuleType.MADOUQU: os.path.join(self.db_dir, 'madouqu_models.json'),
            ModuleType.PORN91: os.path.join(self.db_dir, 'porn91_models.json'),
            ModuleType.XVIDEOS: os.path.join(self.db_dir, 'xvideos_models.json'),
            ModuleType.XHAMSTER: os.path.join(self.db_dir, 'xhamster_models.json'),
        }
        
        # 内存缓存
        self._cache: Dict[ModuleType, Dict[str, Any]] = {}
        
        # 加载所有模块数据
        self._load_all()
    
    def _load_all(self):
        """加载所有模块数据"""
        for module_type, file_path in self.module_files.items():
            self._cache[module_type] = self._load_module(module_type)
    
    def _load_module(self, module_type: ModuleType) -> Dict[str, Any]:
        """
        加载模块数据
        
        Args:
            module_type: 模块类型
        
        Returns:
            模块数据
        """
        file_path = self.module_files.get(module_type)
        if not file_path or not os.path.exists(file_path):
            return {'models': {}, 'videos': {}}
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载模块 {module_type.value} 数据失败: {e}")
            return {'models': {}, 'videos': {}}
    
    def _save_module(self, module_type: ModuleType) -> bool:
        """
        保存模块数据
        
        Args:
            module_type: 模块类型
        
        Returns:
            是否成功
        """
        file_path = self.module_files.get(module_type)
        if not file_path:
            return False
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(self._cache.get(module_type, {}), f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"保存模块 {module_type.value} 数据失败: {e}")
            return False
    
    # ==================== 模特操作 ====================
    
    def add_model(self, model: ModelInfo) -> bool:
        """
        添加模特
        
        Args:
            model: 模特信息
        
        Returns:
            是否成功
        """
        module_type = ModuleType(model.module)
        if module_type not in self._cache:
            self._cache[module_type] = {'models': {}, 'videos': {}}
        
        self._cache[module_type]['models'][model.name] = asdict(model)
        return self._save_module(module_type)
    
    def get_model(self, name: str, module: str) -> Optional[ModelInfo]:
        """
        获取模特信息
        
        Args:
            name: 模特名
            module: 模块名
        
        Returns:
            模特信息
        """
        module_type = ModuleType(module)
        models = self._cache.get(module_type, {}).get('models', {})
        data = models.get(name)
        if data:
            return ModelInfo(**data)
        return None
    
    def get_models_by_module(self, module: str) -> List[ModelInfo]:
        """
        获取模块下的所有模特
        
        Args:
            module: 模块名
        
        Returns:
            模特列表
        """
        module_type = ModuleType(module)
        models = self._cache.get(module_type, {}).get('models', {})
        return [ModelInfo(**data) for data in models.values()]
    
    def update_model(self, model: ModelInfo) -> bool:
        """更新模特信息"""
        model.updated_at = datetime.now().isoformat()
        return self.add_model(model)
    
    def delete_model(self, name: str, module: str) -> bool:
        """删除模特"""
        module_type = ModuleType(module)
        models = self._cache.get(module_type, {}).get('models', {})
        if name in models:
            del models[name]
            return self._save_module(module_type)
        return False
    
    # ==================== 视频操作 ====================
    
    def add_video(self, video: VideoInfo) -> bool:
        """添加视频"""
        module_type = ModuleType(video.module)
        if module_type not in self._cache:
            self._cache[module_type] = {'models': {}, 'videos': {}}
        
        videos = self._cache[module_type].setdefault('videos', {})
        key = f"{video.model_name}_{video.title}"
        videos[key] = asdict(video)
        return self._save_module(module_type)
    
    def add_videos(self, videos: List[VideoInfo]) -> bool:
        """批量添加视频"""
        if not videos:
            return True
        
        # 按模块分组
        by_module: Dict[str, List[VideoInfo]] = {}
        for video in videos:
            if video.module not in by_module:
                by_module[video.module] = []
            by_module[video.module].append(video)
        
        # 分别保存
        success = True
        for module, vids in by_module.items():
            module_type = ModuleType(module)
            if module_type not in self._cache:
                self._cache[module_type] = {'models': {}, 'videos': {}}
            
            cache_videos = self._cache[module_type].setdefault('videos', {})
            for video in vids:
                key = f"{video.model_name}_{video.title}"
                cache_videos[key] = asdict(video)
            
            if not self._save_module(module_type):
                success = False
        
        return success
    
    def get_videos_by_model(self, model_name: str, module: str) -> List[VideoInfo]:
        """获取模特的所有视频"""
        module_type = ModuleType(module)
        videos = self._cache.get(module_type, {}).get('videos', {})
        result = []
        for key, data in videos.items():
            if data.get('model_name') == model_name:
                result.append(VideoInfo(**data))
        return result
    
    def get_missing_videos(self, module: str) -> List[VideoInfo]:
        """获取模块下所有缺失的视频"""
        module_type = ModuleType(module)
        videos = self._cache.get(module_type, {}).get('videos', {})
        return [VideoInfo(**data) for data in videos.values() if data.get('status') == 'missing']
    
    def update_video_status(self, model_name: str, title: str, module: str, status: str, local_path: str = None) -> bool:
        """更新视频状态"""
        module_type = ModuleType(module)
        videos = self._cache.get(module_type, {}).get('videos', {})
        key = f"{model_name}_{title}"
        if key in videos:
            videos[key]['status'] = status
            if local_path:
                videos[key]['local_path'] = local_path
            return self._save_module(module_type)
        return False
    
    # ==================== 统计操作 ====================
    
    def get_module_stats(self, module: str) -> Dict[str, Any]:
        """
        获取模块统计信息
        
        Args:
            module: 模块名
        
        Returns:
            统计信息
        """
        module_type = ModuleType(module)
        data = self._cache.get(module_type, {})
        
        models = data.get('models', {})
        videos = data.get('videos', {})
        
        total_videos = len(videos)
        missing_count = sum(1 for v in videos.values() if v.get('status') == 'missing')
        downloaded_count = sum(1 for v in videos.values() if v.get('status') == 'downloaded')
        local_count = sum(1 for v in videos.values() if v.get('status') == 'local')
        
        return {
            'module': module,
            'model_count': len(models),
            'total_videos': total_videos,
            'missing_count': missing_count,
            'downloaded_count': downloaded_count,
            'local_count': local_count,
        }
    
    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有模块的统计信息"""
        stats = {}
        for module_type in ModuleType:
            stats[module_type.value] = self.get_module_stats(module_type.value)
        return stats
    
    # ==================== 导入导出 ====================
    
    def export_module(self, module: str, output_path: str) -> bool:
        """导出模块数据"""
        module_type = ModuleType(module)
        data = self._cache.get(module_type, {})
        
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"导出模块 {module} 数据到: {output_path}")
            return True
        except Exception as e:
            logger.error(f"导出失败: {e}")
            return False
    
    def import_module(self, module: str, input_path: str, merge: bool = True) -> bool:
        """导入模块数据"""
        module_type = ModuleType(module)
        
        try:
            with open(input_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if merge:
                # 合并数据
                existing = self._cache.get(module_type, {'models': {}, 'videos': {}})
                existing['models'].update(data.get('models', {}))
                existing['videos'].update(data.get('videos', {}))
                self._cache[module_type] = existing
            else:
                self._cache[module_type] = data
            
            return self._save_module(module_type)
        except Exception as e:
            logger.error(f"导入失败: {e}")
            return False
