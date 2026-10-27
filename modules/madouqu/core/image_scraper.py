# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
Madouqu图片刮削模块
用于扫描页面内容并刮削图片作为参考
"""
import os
import re
import logging
import requests
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
import hashlib
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ImageInfo:
    """图片信息"""
    url: str
    title: str = ""
    alt_text: str = ""
    width: int = 0
    height: int = 0
    file_size: int = 0
    format: str = ""
    source_page: str = ""
    index: int = 0

    def get_filename(self) -> str:
        """生成文件名"""
        if self.title:
            safe_title = re.sub(r'[\\/*?:"<>|]', '', self.title)
            return f"{safe_title}_{self.index}.jpg"
        else:
            ext = self.format if self.format else 'jpg'
            hash = hashlib.md5(self.url.encode()).hexdigest()[:8]
            return f"image_{hash}.{ext}"


@dataclass
class ScrapingResult:
    """刮削结果"""
    success: bool
    page_url: str
    images: List[ImageInfo] = field(default_factory=list)
    downloaded_count: int = 0
    failed_count: int = 0
    total_size: int = 0
    error: str = ""


class ImageScraper:
    """图片刮削器"""

    def __init__(self, output_dir: str = None, max_workers: int = 5):
        """
        初始化图片刮削器

        Args:
            output_dir: 图片保存目录
            max_workers: 最大下载线程数
        """
        self.output_dir = output_dir or self._get_default_output_dir()
        self.max_workers = max_workers

        # 创建输出目录
        os.makedirs(self.output_dir, exist_ok=True)

        # 支持的图片格式
        self.image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg'}

        # 用户代理
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }

        # 请求会话
        self.session = requests.Session()
        self.session.headers.update(self.headers)

        # 已下载的图片URL（去重）
        self.downloaded_urls: Set[str] = set()

    def _get_default_output_dir(self) -> str:
        """获取默认输出目录"""
        base_dir = Path(__file__).parent.parent.parent.parent / 'data' / 'madouqu_images'
        return str(base_dir)

    def scrape_page(self, page_url: str, max_images: int = 100) -> ScrapingResult:
        """
        刮削单个页面的图片

        Args:
            page_url: 页面URL
            max_images: 最大图片数量

        Returns:
            刮削结果
        """
        result = ScrapingResult(success=False, page_url=page_url)

        try:
            logger.info(f"开始刮削页面: {page_url}")

            # 获取页面内容
            html = self._fetch_page(page_url)
            if not html:
                result.error = "无法获取页面内容"
                return result

            # 解析页面，提取图片
            images = self._extract_images(html, page_url)

            # 限制图片数量
            images = images[:max_images]

            result.images = images
            result.success = True

            logger.info(f"页面 {page_url} 提取到 {len(images)} 张图片")

        except Exception as e:
            logger.error(f"刮削页面失败 {page_url}: {e}")
            result.error = str(e)

        return result

    def scrape_multiple_pages(self, page_urls: List[str], max_images_per_page: int = 100) -> List[ScrapingResult]:
        """
        刮削多个页面的图片

        Args:
            page_urls: 页面URL列表
            max_images_per_page: 每页最大图片数量

        Returns:
            刮削结果列表
        """
        results = []

        logger.info(f"开始刮削 {len(page_urls)} 个页面")

        for i, page_url in enumerate(page_urls, 1):
            logger.info(f"处理第 {i}/{len(page_urls)} 个页面")

            result = self.scrape_page(page_url, max_images_per_page)
            results.append(result)

        return results

    def download_images(self, images: List[ImageInfo], subfolder: str = None) -> Dict[str, int]:
        """
        下载图片

        Args:
            images: 图片信息列表
            subfolder: 子文件夹名称

        Returns:
            统计信息 {'success': 成功数量, 'failed': 失败数量, 'total_size': 总大小}
        """
        # 确定保存目录
        save_dir = self.output_dir
        if subfolder:
            save_dir = os.path.join(self.output_dir, subfolder)

        os.makedirs(save_dir, exist_ok=True)

        stats = {'success': 0, 'failed': 0, 'total_size': 0}

        # 过滤已下载的图片
        new_images = [img for img in images if img.url not in self.downloaded_urls]

        if not new_images:
            logger.info("没有新图片需要下载")
            return stats

        logger.info(f"开始下载 {len(new_images)} 张图片到 {save_dir}")

        # 使用线程池并发下载
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交下载任务
            future_to_image = {
                executor.submit(self._download_single_image, img, save_dir): img
                for img in new_images
            }

            # 等待所有任务完成
            for future in as_completed(future_to_image):
                image = future_to_image[future]
                try:
                    file_path, file_size = future.result()
                    if file_path:
                        stats['success'] += 1
                        stats['total_size'] += file_size
                        self.downloaded_urls.add(image.url)
                        logger.info(f"下载成功: {file_path} ({file_size/1024:.1f}KB)")
                    else:
                        stats['failed'] += 1
                except Exception as e:
                    logger.error(f"下载失败 {image.url}: {e}")
                    stats['failed'] += 1

        logger.info(f"下载完成: 成功 {stats['success']}, 失败 {stats['failed']}, 总大小 {stats['total_size']/1024/1024:.2f}MB")

        return stats

    def _fetch_page(self, url: str) -> Optional[str]:
        """
        获取页面内容

        Args:
            url: 页面URL

        Returns:
            页面HTML内容
        """
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or 'utf-8'
            return response.text
        except Exception as e:
            logger.error(f"获取页面失败 {url}: {e}")
            return None

    def _extract_images(self, html: str, page_url: str) -> List[ImageInfo]:
        """
        从HTML中提取图片信息

        Args:
            html: HTML内容
            page_url: 页面URL

        Returns:
            图片信息列表
        """
        soup = BeautifulSoup(html, 'html.parser')
        images = []

        # 提取所有img标签
        img_tags = soup.find_all('img')

        for idx, img in enumerate(img_tags):
            try:
                # 获取图片URL
                src = img.get('src') or img.get('data-src') or img.get('data-original')
                if not src:
                    continue

                # 转换为绝对URL
                abs_url = urljoin(page_url, src)

                # 验证是否为图片URL
                if not self._is_image_url(abs_url):
                    continue

                # 提取图片信息
                image_info = ImageInfo(
                    url=abs_url,
                    title=img.get('title', ''),
                    alt_text=img.get('alt', ''),
                    width=int(img.get('width', 0)) if img.get('width') else 0,
                    height=int(img.get('height', 0)) if img.get('height') else 0,
                    source_page=page_url,
                    index=idx + 1
                )

                # 获取图片格式
                parsed = urlparse(abs_url)
                path = parsed.path.lower()
                for ext in self.image_extensions:
                    if path.endswith(ext):
                        image_info.format = ext[1:]  # 去掉点
                        break

                images.append(image_info)

            except Exception as e:
                logger.warning(f"提取图片信息失败: {e}")
                continue

        # 提取a标签中的图片链接
        a_tags = soup.find_all('a', href=True)
        for idx, a in enumerate(a_tags):
            try:
                href = a.get('href')
                if not href:
                    continue

                abs_url = urljoin(page_url, href)

                if not self._is_image_url(abs_url):
                    continue

                # 检查是否已存在
                if any(img.url == abs_url for img in images):
                    continue

                image_info = ImageInfo(
                    url=abs_url,
                    title=a.get_text(strip=True)[:50],  # 使用链接文本作为标题
                    source_page=page_url,
                    index=len(images) + 1
                )

                parsed = urlparse(abs_url)
                path = parsed.path.lower()
                for ext in self.image_extensions:
                    if path.endswith(ext):
                        image_info.format = ext[1:]
                        break

                images.append(image_info)

            except Exception as e:
                logger.warning(f"提取链接图片失败: {e}")
                continue

        # 去重
        seen_urls = set()
        unique_images = []
        for img in images:
            if img.url not in seen_urls:
                seen_urls.add(img.url)
                unique_images.append(img)

        logger.info(f"从页面提取到 {len(unique_images)} 张唯一图片")
        return unique_images

    def _is_image_url(self, url: str) -> bool:
        """
        判断URL是否为图片

        Args:
            url: URL

        Returns:
            是否为图片
        """
        # 检查文件扩展名
        parsed = urlparse(url)
        path = parsed.path.lower()

        for ext in self.image_extensions:
            if path.endswith(ext):
                return True

        # 检查URL中是否包含图片关键词
        image_keywords = ['/image/', '/img/', '/photo/', '/thumb/', '/preview/']
        for keyword in image_keywords:
            if keyword in url.lower():
                return True

        return False

    def _download_single_image(self, image: ImageInfo, save_dir: str) -> tuple:
        """
        下载单张图片

        Args:
            image: 图片信息
            save_dir: 保存目录

        Returns:
            (文件路径, 文件大小) 如果失败则返回 (None, 0)
        """
        try:
            # 生成文件名
            filename = image.get_filename()
            filepath = os.path.join(save_dir, filename)

            # 如果文件已存在，跳过
            if os.path.exists(filepath):
                logger.debug(f"文件已存在，跳过: {filepath}")
                return filepath, os.path.getsize(filepath)

            # 下载图片
            response = self.session.get(image.url, timeout=30, stream=True)
            response.raise_for_status()

            # 保存文件
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            file_size = os.path.getsize(filepath)
            logger.info(f"下载成功: {filepath} ({file_size/1024:.1f}KB)")

            return filepath, file_size

        except Exception as e:
            logger.error(f"下载图片失败 {image.url}: {e}")
            return None, 0

    def scan_all_pages(self, base_url: str, max_pages: int = 10) -> List[str]:
        """
        扫描所有页面URL

        Args:
            base_url: 基础URL
            max_pages: 最大页面数

        Returns:
            页面URL列表
        """
        page_urls = []

        try:
            logger.info(f"开始扫描页面: {base_url}")

            # 首页
            page_urls.append(base_url)

            # 扫描分页
            for page in range(2, max_pages + 1):
                page_url = f"{base_url}/page/{page}/"
                page_urls.append(page_url)

            logger.info(f"扫描到 {len(page_urls)} 个页面")

        except Exception as e:
            logger.error(f"扫描页面失败: {e}")

        return page_urls

    def get_statistics(self) -> Dict[str, any]:
        """
        获取统计信息

        Returns:
            统计信息字典
        """
        total_images = 0
        total_size = 0

        if os.path.exists(self.output_dir):
            for root, dirs, files in os.walk(self.output_dir):
                for file in files:
                    filepath = os.path.join(root, file)
                    if os.path.isfile(filepath):
                        total_images += 1
                        total_size += os.path.getsize(filepath)

        return {
            'total_images': total_images,
            'total_size': total_size,
            'total_size_mb': round(total_size / 1024 / 1024, 2),
            'output_dir': self.output_dir,
            'downloaded_urls_count': len(self.downloaded_urls)
        }

    def clear_cache(self):
        """清除缓存"""
        self.downloaded_urls.clear()
        logger.info("已清除下载缓存")


# 全局实例
_image_scraper = None


def get_image_scraper() -> ImageScraper:
    """获取全局图片刮削器实例"""
    global _image_scraper
    if _image_scraper is None:
        _image_scraper = ImageScraper()
    return _image_scraper
