# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
Madouqu数据库模块
独立存储Madouqu模块的数据
"""
import os
import json
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path

from .models import ModelInfo, VideoInfo, MagnetLink, CrawlResult

logger = logging.getLogger(__name__)


class MadouquDatabase:
    """
    Madouqu数据库
    
    功能：
    1. 存储模特信息
    2. 存储视频和磁力链接
    3. 支持导入导出
    """
    
    def __init__(self, db_dir: str = None):
        """
        初始化数据库
        
        Args:
            db_dir: 数据库目录
        """
        self.db_dir = db_dir or os.path.join(os.getcwd(), 'modules', 'madouqu', 'data')
        Path(self.db_dir).mkdir(parents=True, exist_ok=True)
        
        self.models_file = os.path.join(self.db_dir, 'models.json')
        self.videos_file = os.path.join(self.db_dir, 'videos.json')
        
        # 内存缓存
        self._models: Dict[str, Dict] = {}
        self._videos: Dict[str, Dict] = {}
        
        self._load()
    
    def _load(self):
        """加载数据"""
        # 加载模特
        if os.path.exists(self.models_file):
            try:
                with open(self.models_file, 'r', encoding='utf-8') as f:
                    self._models = json.load(f)
            except Exception as e:
                logger.warning(f"加载模特数据失败: {e}")
        
        # 加载视频
        if os.path.exists(self.videos_file):
            try:
                with open(self.videos_file, 'r', encoding='utf-8') as f:
                    self._videos = json.load(f)
            except Exception as e:
                logger.warning(f"加载视频数据失败: {e}")
    
    def _save_models(self) -> bool:
        """保存模特数据"""
        try:
            with open(self.models_file, 'w', encoding='utf-8') as f:
                json.dump(self._models, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"保存模特数据失败: {e}")
            return False
    
    def _save_videos(self) -> bool:
        """保存视频数据"""
        try:
            with open(self.videos_file, 'w', encoding='utf-8') as f:
                json.dump(self._videos, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"保存视频数据失败: {e}")
            return False
    
    # ==================== 模特操作 ====================
    
    def add_model(self, model: ModelInfo) -> bool:
        """添加模特"""
        self._models[model.name] = model.to_dict()
        return self._save_models()
    
    def get_model(self, name: str) -> Optional[ModelInfo]:
        """获取模特"""
        data = self._models.get(name)
        if data:
            return ModelInfo(**data)
        return None
    
    def get_all_models(self) -> List[ModelInfo]:
        """获取所有模特"""
        return [ModelInfo(**data) for data in self._models.values()]
    
    def update_model(self, model: ModelInfo) -> bool:
        """更新模特"""
        model.updated_at = datetime.now().isoformat()
        return self.add_model(model)
    
    def delete_model(self, name: str) -> bool:
        """删除模特"""
        if name in self._models:
            del self._models[name]
            return self._save_models()
        return False
    
    # ==================== 视频操作 ====================
    
    def add_video(self, video: VideoInfo) -> bool:
        """添加视频"""
        key = f"{video.model_name}_{video.title}"
        self._videos[key] = video.to_dict()
        return self._save_videos()
    
    def add_videos(self, videos: List[VideoInfo]) -> bool:
        """批量添加视频"""
        for video in videos:
            key = f"{video.model_name}_{video.title}"
            self._videos[key] = video.to_dict()
        return self._save_videos()
    
    def get_videos_by_model(self, model_name: str) -> List[VideoInfo]:
        """获取模特的视频"""
        result = []
        for data in self._videos.values():
            if data.get('model_name') == model_name:
                # 转换magnet_links
                magnets = [MagnetLink(**m) if isinstance(m, dict) else m for m in data.get('magnet_links', [])]
                data['magnet_links'] = magnets
                result.append(VideoInfo(**data))
        return result
    
    def get_all_videos(self) -> List[VideoInfo]:
        """获取所有视频"""
        result = []
        for data in self._videos.values():
            magnets = [MagnetLink(**m) if isinstance(m, dict) else m for m in data.get('magnet_links', [])]
            data['magnet_links'] = magnets
            result.append(VideoInfo(**data))
        return result
    
    # ==================== 爬取结果保存 ====================
    
    def save_crawl_result(self, result: CrawlResult) -> bool:
        """
        保存爬取结果
        
        Args:
            result: 爬取结果
        
        Returns:
            是否成功
        """
        # 更新或创建模特信息
        model = self.get_model(result.model_name)
        if model:
            model.video_count = len(result.videos)
            model.magnet_count = result.total_magnets
            model.last_crawled = datetime.now().isoformat()
            self.update_model(model)
        else:
            model = ModelInfo(
                name=result.model_name,
                video_count=len(result.videos),
                magnet_count=result.total_magnets,
                last_crawled=datetime.now().isoformat(),
            )
            self.add_model(model)
        
        # 保存视频
        return self.add_videos(result.videos)
    
    def get_existing_titles(self, model_name: str) -> set:
        """
        获取模特已有的视频标题（用于增量更新）
        
        Args:
            model_name: 模特名
        
        Returns:
            已有标题集合
        """
        titles = set()
        for data in self._videos.values():
            if data.get('model_name') == model_name:
                titles.add(data.get('title', ''))
        return titles
    
    def get_existing_magnets(self, model_name: str) -> set:
        """
        获取模特已有的磁力链接（用于去重）
        
        Args:
            model_name: 模特名
        
        Returns:
            已有磁力链接集合
        """
        magnets = set()
        for data in self._videos.values():
            if data.get('model_name') == model_name:
                for m in data.get('magnet_links', []):
                    if isinstance(m, dict):
                        magnets.add(m.get('link', ''))
                    else:
                        magnets.add(str(m))
        return magnets
    
    def add_new_videos(self, model_name: str, videos: List[VideoInfo]) -> int:
        """
        增量添加新视频（只添加不存在的）
        
        Args:
            model_name: 模特名
            videos: 视频列表
        
        Returns:
            新增数量
        """
        existing_titles = self.get_existing_titles(model_name)
        existing_magnets = self.get_existing_magnets(model_name)
        
        new_count = 0
        for video in videos:
            # 检查标题是否已存在
            if video.title in existing_titles:
                continue
            
            # 检查磁力链接是否已存在
            video_magnets = set()
            for m in video.magnet_links:
                if isinstance(m, dict):
                    video_magnets.add(m.get('link', ''))
                else:
                    video_magnets.add(str(m))
            
            # 如果所有磁力链接都已存在，跳过
            if video_magnets and video_magnets.issubset(existing_magnets):
                continue
            
            # 添加新视频
            key = f"{video.model_name}_{video.title}"
            self._videos[key] = video.to_dict()
            new_count += 1
        
        if new_count > 0:
            self._save_videos()
            
            # 更新模特统计
            model = self.get_model(model_name)
            if model:
                model.video_count = len(self.get_videos_by_model(model_name))
                model.magnet_count = sum(len(v.magnet_links) for v in self.get_videos_by_model(model_name))
                model.last_crawled = datetime.now().isoformat()
                self.update_model(model)
        
        return new_count
    
    # ==================== 统计 ====================
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        total_magnets = 0
        for data in self._videos.values():
            total_magnets += len(data.get('magnet_links', []))
        
        return {
            'model_count': len(self._models),
            'video_count': len(self._videos),
            'magnet_count': total_magnets,
        }
    
    # ==================== 导入导出 ====================
    
    def export_to_file(self, output_path: str) -> bool:
        """导出数据"""
        data = {
            'models': self._models,
            'videos': self._videos,
            'exported_at': datetime.now().isoformat(),
        }
        
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"导出数据到: {output_path}")
            return True
        except Exception as e:
            logger.error(f"导出失败: {e}")
            return False
    
    def import_from_file(self, input_path: str, merge: bool = True) -> bool:
        """导入数据"""
        try:
            with open(input_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            if merge:
                self._models.update(data.get('models', {}))
                self._videos.update(data.get('videos', {}))
            else:
                self._models = data.get('models', {})
                self._videos = data.get('videos', {})
            
            self._save_models()
            self._save_videos()
            
            logger.info(f"导入数据: {input_path}")
            return True
        except Exception as e:
            logger.error(f"导入失败: {e}")
            return False
