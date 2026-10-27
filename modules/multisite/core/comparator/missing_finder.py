# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
缺失视频查找器
核心功能：对比在线视频和本地视频，找出缺失的视频
"""
import asyncio
import logging
from typing import List, Dict, Set, Any, Optional, Tuple
from ..parser import ParserFactory
from ..local_scanner import LocalScanner, TitleNormalizer
try:  # 可选依赖：PORNHUB 后端已从本模块剥离，缺失时降级
    from multisite.porn.core.utils.pornhub_url_detector import PornHubUrlDetector
except Exception:  # pragma: no cover
    class PornHubUrlDetector:  # type: ignore
        @staticmethod
        def is_pornhub_url(url: str) -> bool:
            return "pornhub" in (url or "").lower()

from multisite.core.utils.url_sync_logger import log_url_check, log_title_sync, log_download_skip, log_summary

logger = logging.getLogger(__name__)


class MissingFinder:
    """
    缺失视频查找器
    
    核心功能：
    1. 使用API解析器获取准确的在线视频列表
    2. 扫描本地视频目录
    3. 使用统一归一化逻辑对比
    4. 输出缺失视频列表
    
    解决的问题：
    - 本地100个 + 网站158个 → 显示需要下载58个（而不是158个）
    """
    
    def __init__(self, config: dict = None):
        """
        初始化查找器
        
        Args:
            config: 配置字典
        """
        self.config = config or {}
        self.normalizer = TitleNormalizer(config)
        self.scanner = LocalScanner(config)
    
    async def find_missing(
        self,
        model_url: str,
        local_directory: str,
        max_pages: int = 100,
        similarity_threshold: float = 0.85
    ) -> Dict[str, Any]:
        """
        查找缺失视频
        
        Args:
            model_url: 模特页面URL
            local_directory: 本地视频目录
            max_pages: 最大解析页数
            similarity_threshold: 相似度阈值
        
        Returns:
            结果字典:
            {
                'online_count': 在线视频数量,
                'local_count': 本地视频数量,
                'missing_count': 缺失视频数量,
                'missing_videos': [{url, title, ...}, ...],
                'matched_count': 已匹配数量,
                'shorty_count': 小视频数量(被过滤),
            }
        """
        logger.info(f"开始查找缺失视频")
        logger.info(f"  模特URL: {model_url}")
        logger.info(f"  本地目录: {local_directory}")
        
        result = {
            'online_count': 0,
            'local_count': 0,
            'missing_count': 0,
            'missing_videos': [],
            'matched_count': 0,
            'local_videos': [],
            'online_videos': [],
            'shorty_count': 0,  # 小视频数量
            'shorty_videos': [],  # 小视频列表
        }
        
        try:
            # 1. 获取在线视频列表（使用API解析器）
            logger.info("步骤1: 获取在线视频列表...")
            online_videos = await self._fetch_online_videos(model_url, max_pages)
            
            # 过滤小视频(shorties)和虚拟视频
            filtered_videos = []
            shorty_videos = []
            virtual_videos = []
            for video in online_videos:
                # 过滤虚拟视频
                if video.get('is_virtual', False) or video.get('is_statistics_only', False):
                    virtual_videos.append(video)
                    logger.debug(f"  过滤虚拟视频: {video.get('title', '')[:50]}...")
                    continue
                
                # 过滤小视频
                video_url = video.get('url', '')
                if PornHubUrlDetector.is_shorty(video_url):
                    shorty_videos.append(video)
                    logger.debug(f"  过滤小视频: {video.get('title', '')[:50]}...")
                else:
                    filtered_videos.append(video)
            
            result['shorty_videos'] = shorty_videos
            result['shorty_count'] = len(shorty_videos)
            result['online_videos'] = filtered_videos  # 只保留可下载的视频
            result['online_count'] = len(filtered_videos)
            
            if shorty_videos:
                logger.info(f"  过滤了 {len(shorty_videos)} 个小视频(Shorties)")
            if virtual_videos:
                logger.info(f"  过滤了 {len(virtual_videos)} 个虚拟视频")
            logger.info(f"  有效在线视频数量: {result['online_count']}")
            
            # 2. 扫描本地视频
            logger.info("步骤2: 扫描本地视频...")
            if not local_directory or not local_directory.strip():
                logger.warning("  本地目录为空，跳过本地扫描")
                local_videos = []
            else:
                # 检查目录是否存在，不存在则创建
                import os
                if not os.path.exists(local_directory):
                    logger.info(f"  本地目录不存在，自动创建: {local_directory}")
                    try:
                        os.makedirs(local_directory, exist_ok=True)
                        logger.info(f"  目录创建成功")
                    except Exception as e:
                        logger.error(f"  目录创建失败: {e}")
                
                local_videos = self.scanner.scan_directory(local_directory)
            result['local_videos'] = local_videos
            result['local_count'] = len(local_videos)
            logger.info(f"  本地视频数量: {result['local_count']}")
            
            # 3. 构建本地标题集合（归一化后）
            logger.info("步骤3: 构建本地标题集合...")
            local_normalized = {}
            for video in local_videos:
                norm_title = video.get('normalized_title', '')
                if norm_title:
                    local_normalized[norm_title] = video
            
            # 3.5 URL查重：检查在线视频的URL是否已经在数据库下载过
            logger.info("步骤3.5: URL查重检查...")
            try:
                from multisite.core.database.download_cache import DownloadCacheDB
                download_cache = DownloadCacheDB()
                already_downloaded_count = 0
                url_check_count = 0
                
                # 从model_url提取model_name
                model_name = "unknown"
                if model_url:
                    if '/model/' in model_url:
                        model_name = model_url.split('/model/')[-1].split('?')[0].split('/')[0]
                    elif '/pornstar/' in model_url:
                        model_name = model_url.split('/pornstar/')[-1].split('?')[0].split('/')[0]
                    elif '/channels/' in model_url:
                        model_name = model_url.split('/channels/')[-1].split('?')[0].split('/')[0]
                
                for online_video in filtered_videos:
                    video_url = online_video.get('url', '')
                    video_title = online_video.get('title', '')
                    if video_url:
                        url_check_count += 1
                        is_dl = download_cache.is_downloaded(video_url, verify_file_exists=False)
                        if is_dl:
                            already_downloaded_count += 1
                            # 记录到独立日志
                            log_url_check(model_name, video_url, video_title, True, "FOUND")
                            logger.debug(f"  URL已下载: {video_title[:30]}...")
                
                if already_downloaded_count > 0:
                    logger.info(f"  通过URL查重排除 {already_downloaded_count} 个已下载视频")
                    # 记录摘要到独立日志
                    log_summary(model_name, url_check_count, already_downloaded_count, 0)
            except Exception as e:
                logger.warning(f"  URL查重检查失败: {e}")
            
            # 4. 对比找出缺失视频
            logger.info("步骤4: 对比找出缺失视频...")
            missing_videos = []
            matched_count = 0
            
            # 获取已下载的URL集合和失败记录
            try:
                from multisite.core.database.download_cache import DownloadCacheDB
                download_cache = DownloadCacheDB()
                downloaded_urls = set()
                failed_urls = set()
                
                for online_video in filtered_videos:
                    video_url = online_video.get('url', '')
                    if video_url:
                        # 检查是否已下载
                        if download_cache.is_downloaded(video_url, verify_file_exists=False):
                            downloaded_urls.add(video_url)
                        # 检查是否在失败记录中（永久失败）
                        elif download_cache.is_failed_download(video_url):
                            failed_urls.add(video_url)
                            logger.debug(f"  跳过失败记录: {online_video.get('title', '')[:30]}...")
            except Exception as e:
                logger.warning(f"  获取已下载/失败URL集合失败: {e}")
                downloaded_urls = set()
                failed_urls = set()
            
            # 统计失败记录数量
            if failed_urls:
                logger.info(f"  跳过 {len(failed_urls)} 个永久失败的URL")
            
            for online_video in filtered_videos:
                video_url = online_video.get('url', '')
                
                # 如果URL已在下载缓存中，排除
                if video_url in downloaded_urls:
                    matched_count += 1
                    continue
                
                # 如果URL在失败记录中（永久失败），也排除
                if video_url in failed_urls:
                    matched_count += 1
                    continue
                
                online_title = online_video.get('title', '')
                online_norm = self.normalizer.normalize(online_title)
                
                # 尝试精确匹配
                if online_norm in local_normalized:
                    matched_count += 1
                    logger.debug(f"  匹配: {online_title[:50]}...")
                    continue
                
                # 尝试相似度匹配
                matched = False
                for local_norm, local_video in local_normalized.items():
                    if self.normalizer.are_similar(online_title, local_video.get('title', ''), similarity_threshold):
                        matched = True
                        matched_count += 1
                        break
                
                if not matched:
                    missing_videos.append(online_video)
                    logger.debug(f"  缺失: {online_title[:50]}...")
            
            result['missing_videos'] = missing_videos
            result['missing_count'] = len(missing_videos)
            result['matched_count'] = matched_count
            
            # 5. 输出结果
            logger.info("=" * 60)
            logger.info("查找结果:")
            logger.info(f"  在线视频: {result['online_count']} 个 (过滤了 {result['shorty_count']} 个小视频)")
            logger.info(f"  本地视频: {result['local_count']} 个")
            logger.info(f"  已匹配:   {result['matched_count']} 个")
            logger.info(f"  需下载:   {result['missing_count']} 个")
            logger.info("=" * 60)
            
            return result
            
        except Exception as e:
            logger.error(f"查找缺失视频失败: {e}")
            import traceback
            traceback.print_exc()
            return result
    
    async def _fetch_online_videos(self, model_url: str, max_pages: int) -> List[Dict[str, Any]]:
        """
        获取在线视频列表
        
        Args:
            model_url: 模特页面URL
            max_pages: 最大解析页数
        
        Returns:
            视频列表
        """
        try:
            # 检测站点类型
            site = ParserFactory.detect_site(model_url)
            logger.info(f"  检测到站点: {site}")
            
            # 创建解析器
            parser = ParserFactory.create_parser(site, self.config)
            logger.info(f"  创建解析器: {type(parser).__name__}")
            
            # 获取视频列表
            logger.info(f"  开始获取视频列表, max_pages={max_pages}")
            videos = await parser.parse_model_page(model_url, max_pages=max_pages)
            logger.info(f"  获取到 {len(videos)} 个视频")
            
            return videos
            
        except Exception as e:
            logger.error(f"获取在线视频失败: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def find_missing_sync(
        self,
        model_url: str,
        local_directory: str,
        max_pages: int = 100,
        similarity_threshold: float = 0.85
    ) -> Dict[str, Any]:
        """
        同步版本的查找缺失视频
        
        Args:
            model_url: 模特页面URL
            local_directory: 本地视频目录
            max_pages: 最大解析页数
            similarity_threshold: 相似度阈值
        
        Returns:
            结果字典
        """
        return asyncio.run(self.find_missing(
            model_url, local_directory, max_pages, similarity_threshold
        ))
    
    def batch_find_missing(
        self,
        models: List[Dict[str, str]],
        max_pages: int = 100
    ) -> List[Dict[str, Any]]:
        """
        批量查找多个模特的缺失视频
        
        Args:
            models: 模特列表 [{'name': '模特名', 'url': 'URL', 'directory': '本地目录'}, ...]
            max_pages: 最大解析页数
        
        Returns:
            结果列表
        """
        results = []
        
        for model in models:
            logger.info(f"处理模特: {model.get('name', 'Unknown')}")
            
            result = self.find_missing_sync(
                model_url=model.get('url', ''),
                local_directory=model.get('directory', ''),
                max_pages=max_pages
            )
            
            result['model_name'] = model.get('name', 'Unknown')
            results.append(result)
        
        return results
    
    def generate_report(self, result: Dict[str, Any], output_format: str = 'text') -> str:
        """
        生成报告
        
        Args:
            result: 查找结果
            output_format: 输出格式 ('text', 'markdown', 'json')
        
        Returns:
            报告字符串
        """
        if output_format == 'markdown':
            return self._generate_markdown_report(result)
        elif output_format == 'json':
            import json
            return json.dumps(result, ensure_ascii=False, indent=2)
        else:
            return self._generate_text_report(result)
    
    def _generate_text_report(self, result: Dict[str, Any]) -> str:
        """生成文本格式报告"""
        lines = [
            "=" * 60,
            "缺失视频报告",
            "=" * 60,
            f"在线视频数量: {result.get('online_count', 0)}",
            f"本地视频数量: {result.get('local_count', 0)}",
            f"已匹配数量:   {result.get('matched_count', 0)}",
            f"需要下载:     {result.get('missing_count', 0)}",
            "=" * 60,
            "",
            "缺失视频列表:",
            "-" * 60,
        ]
        
        for i, video in enumerate(result.get('missing_videos', []), 1):
            lines.append(f"{i}. {video.get('title', 'Unknown')}")
            lines.append(f"   URL: {video.get('url', '')}")
            if video.get('duration'):
                lines.append(f"   时长: {video.get('duration')}")
            lines.append("")
        
        return "\n".join(lines)
    
    def _generate_markdown_report(self, result: Dict[str, Any]) -> str:
        """生成Markdown格式报告"""
        lines = [
            "# 缺失视频报告",
            "",
            "## 统计信息",
            "",
            f"| 项目 | 数量 |",
            f"|------|------|",
            f"| 在线视频 | {result.get('online_count', 0)} |",
            f"| 本地视频 | {result.get('local_count', 0)} |",
            f"| 已匹配 | {result.get('matched_count', 0)} |",
            f"| 需要下载 | {result.get('missing_count', 0)} |",
            "",
            "## 缺失视频列表",
            "",
        ]
        
        for i, video in enumerate(result.get('missing_videos', []), 1):
            lines.append(f"### {i}. {video.get('title', 'Unknown')}")
            lines.append(f"- **URL**: {video.get('url', '')}")
            if video.get('duration'):
                lines.append(f"- **时长**: {video.get('duration')}")
            lines.append("")
        
        return "\n".join(lines)
