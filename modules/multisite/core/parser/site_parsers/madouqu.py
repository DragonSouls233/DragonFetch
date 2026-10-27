# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
Madouqu站点爬虫模块
爬取 https://madouqu.sbs 网站的模特BT链接
"""
import re
import time
import logging
import urllib.parse
from typing import Dict, List, Any, Optional
from bs4 import BeautifulSoup
import requests
from ..base_parser import BaseParser

logger = logging.getLogger(__name__)


class MadouquParser(BaseParser):
    """Madouqu站点爬虫 - 爬取模特BT链接"""

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.site_name = "Madouqu"
        self.base_url = config.get('base_url', 'https://madouqu.sbs') if config else 'https://madouqu.sbs'

        # 创建 session
        self.session = requests.Session()

        # 设置请求头
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            # 不要声明 br！缺 brotli 解码器时会拿到乱码，详见 modules/scraper/core/base.py
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Referer': self.base_url,
        })

        # 设置代理
        if config and config.get('proxy', {}).get('enabled', False):
            proxy_url = config.get('proxy', {}).get('url', '')
            if proxy_url:
                self.session.proxies = {
                    'http': proxy_url,
                    'https': proxy_url
                }
                logger.info(f"MadouquParser使用代理: {proxy_url}")

    def _make_request(self, url: str, retry_count: int = 0) -> Optional[str]:
        """发起HTTP请求"""
        try:
            logger.debug(f"正在请求: {url} (尝试 {retry_count + 1}/{self.max_retries})")

            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()

            # 检测编码
            if response.encoding is None or response.encoding == 'ISO-8859-1':
                response.encoding = response.apparent_encoding or 'utf-8'

            logger.debug(f"成功获取页面，长度: {len(response.text)}")
            return response.text

        except Exception as e:
            logger.warning(f"请求失败: {e} (尝试 {retry_count + 1}/{self.max_retries})")

            if retry_count < self.max_retries - 1:
                delay = self.retry_delay * (2 ** retry_count)
                logger.info(f"等待 {delay} 秒后重试...")
                time.sleep(delay)
                return self._make_request(url, retry_count + 1)
            else:
                logger.error(f"达到最大重试次数，请求失败: {url}")
                raise

    def search(self, keyword: str, max_pages: int = 5) -> List[Dict[str, Any]]:
        """
        搜索模特资源
        
        Args:
            keyword: 搜索关键词（模特名）
            max_pages: 最大搜索页数
        
        Returns:
            资源列表 [{title, model, magnet_link, size, date, url}, ...]
        """
        logger.info(f"开始搜索: {keyword}")
        
        all_results = []
        
        # URL编码关键词
        encoded_keyword = urllib.parse.quote(keyword, safe='')
        
        for page in range(1, max_pages + 1):
            try:
                # 构建搜索URL
                if page == 1:
                    search_url = f"{self.base_url}/?s={encoded_keyword}"
                else:
                    search_url = f"{self.base_url}/page/{page}/?s={encoded_keyword}"
                
                logger.info(f"搜索第 {page} 页: {search_url}")
                
                # 请求页面
                html = self._make_request(search_url)
                if not html:
                    break
                
                # 解析结果
                soup = BeautifulSoup(html, 'html.parser')
                results = self._extract_search_results(soup, keyword)
                
                if not results:
                    logger.info(f"第 {page} 页没有结果，停止搜索")
                    break
                
                all_results.extend(results)
                logger.info(f"第 {page} 页找到 {len(results)} 个结果，总计 {len(all_results)} 个")
                
                # 延迟
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"搜索第 {page} 页失败: {e}")
                break
        
        logger.info(f"搜索完成，共找到 {len(all_results)} 个结果")
        return all_results

    def _extract_search_results(self, soup: BeautifulSoup, keyword: str) -> List[Dict[str, Any]]:
        """
        从搜索结果页面提取资源信息
        
        Args:
            soup: BeautifulSoup对象
            keyword: 搜索关键词
        
        Returns:
            资源列表
        """
        results = []
        
        # 尝试多种选择器（需要根据实际网站结构调整）
        # 常见的结果项选择器
        item_selectors = [
            'article',                    # WordPress文章结构
            '.post',                      # 文章类
            '.result-item',               # 结果项
            '.item',                      # 项目类
            'li[class*="post"]',          # 列表项
            '.entry',                     # 条目
            '.content-item',              # 内容项
        ]
        
        items = []
        for selector in item_selectors:
            items = soup.select(selector)
            if items:
                logger.debug(f"使用选择器 '{selector}' 找到 {len(items)} 个项目")
                break
        
        if not items:
            # 备选：查找所有包含磁力链接的元素
            logger.debug("使用备选方法：查找磁力链接")
            magnet_links = soup.find_all('a', href=re.compile(r'^magnet:'))
            for link in magnet_links:
                # 向上查找父容器
                container = link.find_parent(['article', 'div', 'li'])
                if container:
                    items.append(container)
        
        for item in items:
            try:
                result = self._extract_item_info(item, keyword)
                if result:
                    results.append(result)
            except Exception as e:
                logger.debug(f"提取项目信息失败: {e}")
                continue
        
        return results

    def _extract_item_info(self, item, keyword: str) -> Optional[Dict[str, Any]]:
        """
        从单个项目提取信息
        
        Args:
            item: BeautifulSoup元素
            keyword: 搜索关键词
        
        Returns:
            资源信息字典
        """
        # 提取标题
        title = ''
        title_selectors = ['h2', 'h3', '.title', '.entry-title', 'a[title]']
        for selector in title_selectors:
            title_elem = item.select_one(selector)
            if title_elem:
                title = title_elem.get('title', '') or title_elem.get_text(strip=True)
                if title:
                    break
        
        if not title:
            return None
        
        # 提取磁力链接
        magnet_link = ''
        magnet_elem = item.find('a', href=re.compile(r'^magnet:'))
        if magnet_elem:
            magnet_link = magnet_elem.get('href', '')
        
        # 提取详情页链接
        detail_url = ''
        link_elem = item.find('a', href=True)
        if link_elem:
            href = link_elem.get('href', '')
            if href and not href.startswith('magnet:'):
                if not href.startswith('http'):
                    href = self.base_url + href
                detail_url = href
        
        # 如果没有磁力链接但有详情页，标记需要进一步获取
        if not magnet_link and detail_url:
            magnet_link = None  # 标记需要从详情页获取
        
        # 提取发布时间
        date = ''
        date_selectors = ['.date', '.time', '.post-date', 'time']
        for selector in date_selectors:
            date_elem = item.select_one(selector)
            if date_elem:
                date = date_elem.get_text(strip=True)
                if date:
                    break
        
        # 提取文件大小
        size = ''
        size_patterns = [
            r'(\d+\.?\d*\s*[GMT]B)',
            r'大小[：:]\s*(\d+\.?\d*\s*[GMT]B)',
        ]
        text = item.get_text()
        for pattern in size_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                size = match.group(1)
                break
        
        return {
            'title': title,
            'model': keyword,
            'magnet_link': magnet_link,
            'detail_url': detail_url,
            'date': date,
            'size': size,
            'site': 'madouqu',
        }

    def get_detail(self, url: str) -> Dict[str, Any]:
        """
        获取详情页信息（获取磁力链接）
        
        Args:
            url: 详情页URL
        
        Returns:
            详情信息
        """
        logger.info(f"获取详情页: {url}")
        
        try:
            html = self._make_request(url)
            if not html:
                return {}
            
            soup = BeautifulSoup(html, 'html.parser')
            
            # 提取磁力链接
            magnet_links = []
            for link in soup.find_all('a', href=re.compile(r'^magnet:')):
                href = link.get('href', '')
                if href:
                    magnet_links.append(href)
            
            # 提取标题
            title = ''
            title_elem = soup.select_one('h1, .title, .entry-title')
            if title_elem:
                title = title_elem.get_text(strip=True)
            
            # 提取内容描述
            content = ''
            content_elem = soup.select_one('.content, .entry-content, article')
            if content_elem:
                content = content_elem.get_text(strip=True)[:500]
            
            return {
                'url': url,
                'title': title,
                'magnet_links': magnet_links,
                'content': content,
            }
            
        except Exception as e:
            logger.error(f"获取详情页失败: {e}")
            return {}

    async def parse_model_page(self, url: str, html: str = None, max_pages: int = 5) -> List[Dict[str, Any]]:
        """
        解析模特页面（搜索模式）
        
        Args:
            url: 搜索URL或模特名
            html: HTML内容（可选）
            max_pages: 最大页数
        
        Returns:
            资源列表
        """
        # 如果是搜索URL，提取关键词
        if '?s=' in url:
            keyword = urllib.parse.unquote(url.split('?s=')[1].split('&')[0])
        else:
            # 假设是模特名
            keyword = url
        
        return self.search(keyword, max_pages)

    async def parse_video_page(self, url: str, html: str = None) -> Dict[str, Any]:
        """解析详情页"""
        return self.get_detail(url)

    def get_magnet_links(self, keyword: str, max_pages: int = 5) -> List[str]:
        """
        获取模特的所有磁力链接
        
        Args:
            keyword: 搜索关键词
            max_pages: 最大页数
        
        Returns:
            磁力链接列表
        """
        results = self.search(keyword, max_pages)
        
        magnet_links = []
        for result in results:
            if result.get('magnet_link'):
                magnet_links.append(result['magnet_link'])
            elif result.get('detail_url'):
                # 从详情页获取
                detail = self.get_detail(result['detail_url'])
                magnet_links.extend(detail.get('magnet_links', []))
        
        return list(set(magnet_links))  # 去重
