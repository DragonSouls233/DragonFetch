# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
统一查重入口
整合查重和下载功能，提供统一的API
"""
import asyncio
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from .parser import ParserFactory
from .local_scanner import LocalScanner, TitleNormalizer
from .comparator import MissingFinder
from .cache import UnifiedCache

logger = logging.getLogger(__name__)


class UnifiedDetector:
    """
    统一查重检测器
    
    整合功能：
    1. 准确获取在线视频列表（使用API解析器）
    2. 扫描本地视频目录
    3. 对比计算缺失视频
    4. 支持缓存和增量更新
    5. 支持批量处理
    
    解决的问题：
    - 查重不准确 → 使用API解析器
    - 结果重复 → 统一归一化逻辑
    - 无法读取本地视频修正 → 本地扫描 + 对比
    - 本地100个 + 网站158个 → 显示需要下载58个
    """
    
    def __init__(self, config: dict = None):
        """
        初始化检测器
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        self.normalizer = TitleNormalizer(config)
        self.scanner = LocalScanner(config)
        self.finder = MissingFinder(config)
        
        # 缓存
        cache_dir = self.config.get('cache_dir', 'cache')
        cache_enabled = self.config.get('cache_enabled', True)
        self.cache = UnifiedCache(cache_dir, cache_enabled)
    
    async def detect(
        self,
        model_url: str,
        local_directory: str,
        use_cache: bool = True,
        max_pages: int = 100,
        similarity_threshold: float = 0.85
    ) -> Dict[str, Any]:
        """
        执行查重检测
        
        Args:
            model_url: 模特页面URL
            local_directory: 本地视频目录
            use_cache: 是否使用缓存
            max_pages: 最大解析页数
            similarity_threshold: 相似度阈值
        
        Returns:
            检测结果
        """
        logger.info("=" * 60)
        logger.info("开始统一查重检测")
        logger.info(f"  URL: {model_url}")
        logger.info(f"  目录: {local_directory}")
        logger.info("=" * 60)
        
        start_time = datetime.now()
        
        # 检查并创建本地目录
        if local_directory and local_directory.strip():
            import os
            if not os.path.exists(local_directory):
                logger.info(f"本地目录不存在，自动创建: {local_directory}")
                try:
                    os.makedirs(local_directory, exist_ok=True)
                    logger.info("目录创建成功")
                except Exception as e:
                    logger.error(f"目录创建失败: {e}")
        
        result = {
            'model_url': model_url,
            'local_directory': local_directory,
            'online_count': 0,
            'local_count': 0,
            'missing_count': 0,
            'matched_count': 0,
            'missing_videos': [],
            'online_videos': [],
            'local_videos': [],
            'cache_used': False,
            'duration_seconds': 0,
        }
        
        try:
            # 计算本地签名
            local_titles = self.scanner.get_local_titles(local_directory)
            local_signature = self.cache.compute_local_signature(local_titles, local_directory)
            
            # 使用 model_url 作为缓存key，而不是 local_directory
            # 因为不同模特可能使用相同的本地目录
            cache_key = model_url
            
            # 检查是否可以使用缓存
            # 注意：缓存只用于missing_videos，不用于online_count
            # 因为在线视频可能会增加，需要每次都重新获取
            use_cache_for_missing = False
            if use_cache and not self.cache.is_local_changed(cache_key, local_signature):
                cached_missing = self.cache.get_missing_videos(cache_key)
                if cached_missing:
                    # 检查缓存是否过期（超过1小时）
                    cached_time = self.cache.get_cache_time(cache_key)
                    if cached_time:
                        from datetime import timedelta
                        cache_age = datetime.now() - cached_time
                        if cache_age < timedelta(hours=1):
                            use_cache_for_missing = True
                            logger.info("使用缓存数据（缓存未过期）")
            
            if use_cache_for_missing:
                # 使用缓存的missing_videos，但仍需获取最新的在线视频数量
                cached_online = self.cache.get_online_videos(cache_key)
                result['missing_videos'] = cached_missing
                result['missing_count'] = len(cached_missing)
                result['online_videos'] = cached_online
                result['online_count'] = len(cached_online)
                result['local_count'] = len(local_titles)
                result['matched_count'] = result['online_count'] - result['missing_count']
                result['cache_used'] = True
            else:
                # 执行完整检测
                find_result = await self.finder.find_missing(
                    model_url, local_directory, max_pages, similarity_threshold
                )
                
                result.update(find_result)
                
                # 更新缓存 - 使用 model_url 作为key
                self.cache.set_online_videos(cache_key, find_result.get('online_videos', []))
                self.cache.set_missing_videos(cache_key, find_result.get('missing_videos', []))
                self.cache.set_local_signature(cache_key, local_signature)
                self.cache.set_cache_time(cache_key, datetime.now())
            
            # 计算耗时
            duration = (datetime.now() - start_time).total_seconds()
            result['duration_seconds'] = round(duration, 2)
            
            # 输出结果
            self._print_result(result)
            
            return result
            
        except Exception as e:
            logger.error(f"查重检测失败: {e}")
            import traceback
            traceback.print_exc()
            return result
    
    def detect_sync(
        self,
        model_url: str,
        local_directory: str,
        use_cache: bool = True,
        max_pages: int = 100
    ) -> Dict[str, Any]:
        """
        同步版本的查重检测
        """
        return asyncio.run(self.detect(
            model_url, local_directory, use_cache, max_pages
        ))
    
    async def batch_detect(
        self,
        models: List[Dict[str, str]],
        use_cache: bool = True,
        max_pages: int = 100
    ) -> List[Dict[str, Any]]:
        """
        批量查重检测
        
        Args:
            models: 模特列表 [{'name': '模特名', 'url': 'URL', 'directory': '本地目录'}, ...]
            use_cache: 是否使用缓存
            max_pages: 最大解析页数
        
        Returns:
            结果列表
        """
        results = []
        
        for i, model in enumerate(models, 1):
            logger.info(f"处理 [{i}/{len(models)}]: {model.get('name', 'Unknown')}")
            
            result = await self.detect(
                model_url=model.get('url', ''),
                local_directory=model.get('directory', ''),
                use_cache=use_cache,
                max_pages=max_pages
            )
            
            result['model_name'] = model.get('name', 'Unknown')
            results.append(result)
        
        # 汇总统计
        self._print_batch_summary(results)
        
        return results
    
    def _print_result(self, result: Dict[str, Any]) -> None:
        """打印检测结果"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("检测结果:")
        logger.info(f"  在线视频: {result.get('online_count', 0)} 个")
        logger.info(f"  本地视频: {result.get('local_count', 0)} 个")
        logger.info(f"  已匹配:   {result.get('matched_count', 0)} 个")
        logger.info(f"  需下载:   {result.get('missing_count', 0)} 个")
        logger.info(f"  耗时:     {result.get('duration_seconds', 0)} 秒")
        logger.info("=" * 60)
    
    def _print_batch_summary(self, results: List[Dict[str, Any]]) -> None:
        """打印批量检测汇总"""
        total_online = sum(r.get('online_count', 0) for r in results)
        total_local = sum(r.get('local_count', 0) for r in results)
        total_missing = sum(r.get('missing_count', 0) for r in results)
        total_matched = sum(r.get('matched_count', 0) for r in results)
        
        logger.info("")
        logger.info("=" * 60)
        logger.info("批量检测汇总:")
        logger.info(f"  模特数量: {len(results)}")
        logger.info(f"  在线视频: {total_online} 个")
        logger.info(f"  本地视频: {total_local} 个")
        logger.info(f"  已匹配:   {total_matched} 个")
        logger.info(f"  需下载:   {total_missing} 个")
        logger.info("=" * 60)
    
    def generate_download_list(
        self,
        result: Dict[str, Any],
        output_path: str = None
    ) -> str:
        """
        生成下载列表
        
        Args:
            result: 检测结果
            output_path: 输出文件路径（可选）
        
        Returns:
            下载列表内容
        """
        lines = [
            f"# 下载列表 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"# 在线: {result.get('online_count', 0)} | 本地: {result.get('local_count', 0)} | 需下载: {result.get('missing_count', 0)}",
            "",
        ]
        
        for i, video in enumerate(result.get('missing_videos', []), 1):
            lines.append(f"{i}. {video.get('title', 'Unknown')}")
            lines.append(f"   {video.get('url', '')}")
            lines.append("")
        
        content = "\n".join(lines)
        
        if output_path:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"下载列表已保存: {output_path}")
        
        return content
    
    def get_statistics(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        获取统计信息
        
        Args:
            result: 检测结果
        
        Returns:
            统计信息
        """
        return {
            'online_count': result.get('online_count', 0),
            'local_count': result.get('local_count', 0),
            'missing_count': result.get('missing_count', 0),
            'matched_count': result.get('matched_count', 0),
            'completion_rate': 0 if result.get('online_count', 0) == 0 
                else round(result.get('matched_count', 0) / result.get('online_count', 1) * 100, 2),
        }


# 便捷函数
async def quick_detect(model_url: str, local_directory: str, config: dict = None) -> Dict[str, Any]:
    """
    快速查重检测
    
    Args:
        model_url: 模特页面URL
        local_directory: 本地视频目录
        config: 配置字典
    
    Returns:
        检测结果
    """
    detector = UnifiedDetector(config)
    return await detector.detect(model_url, local_directory)


def quick_detect_sync(model_url: str, local_directory: str, config: dict = None) -> Dict[str, Any]:
    """
    快速查重检测（同步版本）
    """
    return asyncio.run(quick_detect(model_url, local_directory, config))
