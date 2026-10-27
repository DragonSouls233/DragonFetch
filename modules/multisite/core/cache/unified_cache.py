"""
统一缓存管理器
整合 smart_cache 和 dup_cache，提供统一的缓存接口
"""
import os
import json
import hashlib
import logging
from typing import Any, Dict, List, Optional, Set
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class UnifiedCache:
    """
    统一缓存管理器
    
    功能：
    1. 缓存在线视频列表
    2. 缓存本地视频签名
    3. 缓存缺失视频列表
    4. 支持增量更新
    """
    
    def __init__(self, cache_dir: str = None, enabled: bool = True):
        """
        初始化缓存管理器
        
        Args:
            cache_dir: 缓存目录
            enabled: 是否启用缓存
        """
        self.cache_dir = cache_dir or os.path.join(os.getcwd(), 'cache')
        self.enabled = enabled
        
        # 确保缓存目录存在
        if self.enabled:
            Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
            # 清理过期缓存（超过7天）
            self._clean_expired_cache()
        
        # 内存缓存
        self._memory_cache: Dict[str, Dict[str, Any]] = {}
    
    def _clean_expired_cache(self, days: int = 7):
        """
        清理过期的缓存文件
        
        Args:
            days: 过期天数
        """
        try:
            from datetime import timedelta
            now = datetime.now()
            expired_count = 0
            
            for file in os.listdir(self.cache_dir):
                if file.endswith('.json'):
                    file_path = os.path.join(self.cache_dir, file)
                    try:
                        # 检查文件修改时间
                        mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                        if (now - mtime) > timedelta(days=days):
                            os.remove(file_path)
                            expired_count += 1
                    except Exception:
                        logger.debug("_clean_expired_cache 忽略异常", exc_info=True)
            
            if expired_count > 0:
                logger.info(f"清理了 {expired_count} 个过期缓存文件（超过{days}天）")
        except Exception as e:
            logger.warning(f"清理过期缓存失败: {e}")
    
    def get_cache_path(self, model_name: str) -> str:
        """
        获取模特缓存文件路径
        
        Args:
            model_name: 模特名
        
        Returns:
            缓存文件路径
        """
        # 使用hash避免文件名中的特殊字符问题
        safe_name = hashlib.md5(model_name.encode('utf-8')).hexdigest()
        return os.path.join(self.cache_dir, f"{safe_name}.json")
    
    def load(self, model_name: str) -> Dict[str, Any]:
        """
        加载模特缓存
        
        Args:
            model_name: 模特名
        
        Returns:
            缓存数据
        """
        if not self.enabled:
            return {}
        
        # 先检查内存缓存
        if model_name in self._memory_cache:
            return self._memory_cache[model_name]
        
        cache_path = self.get_cache_path(model_name)
        
        if not os.path.exists(cache_path):
            return {}
        
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self._memory_cache[model_name] = data
                return data
        except Exception as e:
            logger.warning(f"加载缓存失败: {e}")
            return {}
    
    def save(self, model_name: str, data: Dict[str, Any]) -> bool:
        """
        保存模特缓存
        
        Args:
            model_name: 模特名
            data: 缓存数据
        
        Returns:
            是否成功
        """
        if not self.enabled:
            return False
        
        cache_path = self.get_cache_path(model_name)
        
        try:
            # 添加时间戳
            data['updated_at'] = datetime.now().isoformat()
            
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            # 更新内存缓存
            self._memory_cache[model_name] = data
            
            return True
        except Exception as e:
            logger.error(f"保存缓存失败: {e}")
            return False
    
    def clear(self, model_name: str) -> bool:
        """
        清除模特缓存
        
        Args:
            model_name: 模特名
        
        Returns:
            是否成功
        """
        # 清除内存缓存
        if model_name in self._memory_cache:
            del self._memory_cache[model_name]
        
        # 清除文件缓存
        cache_path = self.get_cache_path(model_name)
        if os.path.exists(cache_path):
            try:
                os.remove(cache_path)
                return True
            except Exception as e:
                logger.error(f"清除缓存失败: {e}")
                return False
        
        return True
    
    def clear_all(self) -> bool:
        """
        清除所有缓存
        
        Returns:
            是否成功
        """
        self._memory_cache.clear()
        
        if os.path.exists(self.cache_dir):
            try:
                for file in os.listdir(self.cache_dir):
                    if file.endswith('.json'):
                        os.remove(os.path.join(self.cache_dir, file))
                return True
            except Exception as e:
                logger.error(f"清除所有缓存失败: {e}")
                return False
        
        return True
    
    def get_online_videos(self, model_name: str) -> List[Dict[str, Any]]:
        """
        获取缓存的在线视频列表
        
        Args:
            model_name: 模特名
        
        Returns:
            视频列表（包含URL和标题）
        """
        data = self.load(model_name)
        # 返回完整的视频信息（包含URL和标题）
        online_data = data.get('online_video_data', [])
        return online_data
    
    def set_online_videos(self, model_name: str, videos: List[Dict[str, Any]]) -> bool:
        """
        设置缓存的在线视频列表
        
        Args:
            model_name: 模特名
            videos: 视频列表
        
        Returns:
            是否成功
        """
        # 缓存完整的视频信息（包含URL和标题），确保查重检测时能获取完整信息
        video_count = len(videos)
        online_data = [
            {
                'url': v.get('url', ''),
                'title': v.get('title', ''),
                'duration': v.get('duration', ''),
                'upload_date': v.get('upload_date', '')
            }
            for v in videos
        ]
        
        data = self.load(model_name)
        data['online_video_count'] = video_count
        data['online_video_data'] = online_data
        return self.save(model_name, data)
    
    def get_missing_videos(self, model_name: str) -> List[Dict[str, Any]]:
        """
        获取缓存的缺失视频列表
        
        Args:
            model_name: 模特名
        
        Returns:
            缺失视频列表（只包含URL和标题）
        """
        data = self.load(model_name)
        # 返回简化的缺失视频列表
        missing_data = data.get('missing_video_data', [])
        return missing_data
    
    def set_missing_videos(self, model_name: str, videos: List[Dict[str, Any]]) -> bool:
        """
        设置缓存的缺失视频列表
        
        Args:
            model_name: 模特名
            videos: 缺失视频列表
        
        Returns:
            是否成功
        """
        # 优化：只缓存URL和标题，不缓存完整信息
        missing_count = len(videos)
        missing_data = [
            {
                'url': v.get('url', ''),
                'title': v.get('title', '')[:100]  # 只保存前100个字符
            }
            for v in videos
        ]
        
        data = self.load(model_name)
        data['missing_video_count'] = missing_count
        data['missing_video_data'] = missing_data
        # 不保存完整的视频信息
        # data['missing_videos'] = videos
        return self.save(model_name, data)
    
    def get_local_signature(self, model_name: str) -> str:
        """
        获取本地视频签名
        
        Args:
            model_name: 模特名
        
        Returns:
            签名字符串
        """
        data = self.load(model_name)
        return data.get('local_signature', '')
    
    def set_local_signature(self, model_name: str, signature: str) -> bool:
        """
        设置本地视频签名
        
        Args:
            model_name: 模特名
            signature: 签名字符串
        
        Returns:
            是否成功
        """
        data = self.load(model_name)
        data['local_signature'] = signature
        return self.save(model_name, data)
    
    def compute_local_signature(self, titles: Set[str], directory: str = None) -> str:
        """
        计算本地视频签名
        
        Args:
            titles: 标题集合
            directory: 目录路径（用于计算修改时间）
        
        Returns:
            签名字符串
        """
        # 基于标题集合计算签名
        sorted_titles = sorted(titles)
        payload = "|".join(sorted_titles)
        
        # 如果有目录，加入修改时间
        if directory and os.path.exists(directory):
            latest_mtime = 0
            for root, dirs, files in os.walk(directory):
                for f in files:
                    try:
                        mtime = os.path.getmtime(os.path.join(root, f))
                        latest_mtime = max(latest_mtime, mtime)
                    except Exception:
                        logger.debug("compute_local_signature 忽略异常", exc_info=True)
            payload = f"{len(titles)}|{latest_mtime}|{payload}"
        
        return hashlib.sha1(payload.encode('utf-8')).hexdigest()
    
    def is_local_changed(self, model_name: str, current_signature: str) -> bool:
        """
        检查本地视频是否发生变化
        
        Args:
            model_name: 模特名
            current_signature: 当前签名
        
        Returns:
            是否发生变化
        """
        cached_signature = self.get_local_signature(model_name)
        return cached_signature != current_signature
    
    def get_cache_time(self, model_name: str) -> Optional[datetime]:
        """
        获取缓存时间
        
        Args:
            model_name: 模特名
        
        Returns:
            缓存时间
        """
        data = self.load(model_name)
        cache_time_str = data.get('cache_time')
        if cache_time_str:
            try:
                return datetime.fromisoformat(cache_time_str)
            except Exception:
                return None
        return None
    
    def set_cache_time(self, model_name: str, cache_time: datetime) -> bool:
        """
        设置缓存时间
        
        Args:
            model_name: 模特名
            cache_time: 缓存时间
        
        Returns:
            是否成功
        """
        data = self.load(model_name)
        data['cache_time'] = cache_time.isoformat()
        return self.save(model_name, data)
    
    def get_downloaded_videos(self, model_name: str) -> Set[str]:
        """
        获取已下载的视频标题集合
        
        Args:
            model_name: 模特名
        
        Returns:
            已下载视频标题集合
        """
        data = self.load(model_name)
        downloaded = data.get('downloaded_videos', {})
        return set(downloaded.keys())
    
    def mark_downloaded(self, model_name: str, title: str, url: str) -> bool:
        """
        标记视频为已下载
        
        Args:
            model_name: 模特名
            title: 视频标题
            url: 视频URL
        
        Returns:
            是否成功
        """
        data = self.load(model_name)
        if 'downloaded_videos' not in data:
            data['downloaded_videos'] = {}
        
        data['downloaded_videos'][title] = {
            'url': url,
            'downloaded_at': datetime.now().isoformat(),
        }
        
        return self.save(model_name, data)


# 导入List类型
from typing import List
