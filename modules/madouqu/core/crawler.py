# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
Madouqu爬虫核心模块
爬取磁力链接网站，支持可配置域名
支持Selenium绕过JS渲染和反爬
"""
import re
import time
import logging
import urllib.parse
from typing import Dict, List, Any, Optional
from bs4 import BeautifulSoup
import requests
from datetime import datetime

from .models import MagnetLink, VideoInfo, ModelInfo, CrawlResult

logger = logging.getLogger(__name__)


class MadouquCrawler:
    """
    Madouqu爬虫
    
    功能：
    1. 搜索模特资源
    2. 获取磁力链接（从详情页）
    3. 支持分页爬取
    4. 支持可配置域名
    5. 支持Selenium绕过JS渲染
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        """
        初始化爬虫
        
        Args:
            config: 配置字典，可包含 base_url, use_selenium
        """
        self.config = config or {}
        
        # 备用域名列表（当主域名失败时自动切换）
        # 实测（2026-10-28）：
        #   madouqu.com  直连 200 / 108KB / 1.3s  ← 只有这个真能用
        #   madouqu.cc   代理 200 但正文仅 472B（空壳页），直连证书错误
        #   madouqu.sbs  直连 CONNECT 失败、代理 TLS 失败（全挂）
        # 所以 .com 必须排第一，其余保留仅作域名轮换时的兜底。
        self.fallback_urls = [
            'https://madouqu.com',
            'https://madouqu.sbs',
            'https://madouqu.club',
            'https://madouqu.cc',
            'https://madouqu.net',
            'https://madouqu.org',
        ]

        self.base_url = self.config.get('base_url', 'https://madouqu.com')
        
        # 网络配置
        self.timeout = self.config.get('timeout', 30)
        self.max_retries = self.config.get('max_retries', 3)
        self.retry_delay = self.config.get('retry_delay', 2)
        self.request_delay = self.config.get('request_delay', 1.0)
        
        # Selenium配置
        self.use_selenium = self.config.get('use_selenium', True)  # 默认使用Selenium
        self.headless = self.config.get('headless', True)
        self.driver = None
        
        # 创建session
        self.session = requests.Session()
        self._setup_headers()
        self._setup_proxy()
    
    def set_base_url(self, url: str):
        """设置基础URL"""
        self.base_url = url.rstrip('/')
        logger.info(f"域名已更新为: {self.base_url}")
    
    def test_url(self, url: str) -> bool:
        """
        测试URL是否可用
        
        Args:
            url: 要测试的URL
        
        Returns:
            是否可用
        """
        # 先用 curl_cffi 探（与项目其他模块一致）。
        # 实测 2026-10-28：madouqu.com 用 curl_cffi(impersonate=chrome) 1.3s 就 200，
        # 而 requests 直连要 10~30s —— 原来 10s 超时会把明明可用的域名误判成
        # "所有域名都不可用"，进而让 find_working_url() 返回 None、上层直接报错。
        try:
            from curl_cffi import requests as _cr

            kw: Dict[str, Any] = {"timeout": 15, "impersonate": "chrome",
                                  "allow_redirects": True}
            proxy_url = (self.config.get('proxy') or {}).get('url') \
                if (self.config.get('proxy') or {}).get('enabled') else None
            if proxy_url:
                kw["proxy"] = proxy_url
            r = _cr.get(url, **kw)
            if 200 <= r.status_code < 300:
                logger.info(f"域名可用: {url} (curl_cffi 状态码: {r.status_code})")
                return True
            logger.warning(f"域名返回非200状态: {url} (状态码: {r.status_code})")
            return False
        except ImportError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.warning(f"curl_cffi 探测失败，回退 requests: {url} - {e}")

        try:
            response = self.session.get(url, timeout=20, allow_redirects=True)
            # 200-299 都算成功
            if 200 <= response.status_code < 300:
                logger.info(f"域名可用: {url} (状态码: {response.status_code})")
                return True
            else:
                logger.warning(f"域名返回非200状态: {url} (状态码: {response.status_code})")
                return False
        except Exception as e:
            logger.warning(f"域名不可用: {url} - {e}")
            return False
    
    def find_working_url(self) -> Optional[str]:
        """
        查找可用的域名
        
        Returns:
            可用的域名URL，如果都不可用则返回None
        """
        logger.info("正在检测可用域名...")
        
        # 首先测试当前域名
        if self.test_url(self.base_url):
            return self.base_url
        
        # 测试备用域名
        for url in self.fallback_urls:
            if url != self.base_url:  # 跳过已测试的当前域名
                if self.test_url(url):
                    logger.info(f"找到可用域名: {url}")
                    self.base_url = url
                    return url
        
        logger.error("所有域名都不可用")
        return None
    
    def _setup_headers(self):
        """设置请求头"""
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Connection': 'keep-alive',
        })
    
    def _setup_proxy(self):
        """设置代理"""
        if self.config.get('proxy', {}).get('enabled', False):
            proxy_url = self.config.get('proxy', {}).get('url', '')
            if proxy_url:
                self.session.proxies = {
                    'http': proxy_url,
                    'https': proxy_url
                }
                logger.info(f"使用代理: {proxy_url}")
    
    def _init_selenium(self):
        """初始化Selenium"""
        if self.driver:
            return
        
        try:
            # 尝试使用undetected-chromedriver
            import undetected_chromedriver as uc
            
            options = uc.ChromeOptions()
            if self.headless:
                options.add_argument('--headless=new')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')
            options.add_argument('--lang=zh-CN')
            
            self.driver = uc.Chrome(options=options)
            self.driver.set_page_load_timeout(self.timeout)
            
            logger.info("Selenium初始化成功（undetected-chromedriver）")
            
        except ImportError:
            # 回退到普通selenium
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                
                options = Options()
                if self.headless:
                    options.add_argument('--headless')
                options.add_argument('--no-sandbox')
                options.add_argument('--disable-dev-shm-usage')
                
                self.driver = webdriver.Chrome(options=options)
                self.driver.set_page_load_timeout(self.timeout)
                
                logger.info("Selenium初始化成功（普通模式）")
            except ImportError:
                logger.warning("Selenium未安装，使用requests模式")
                self.use_selenium = False
    
    def _close_selenium(self):
        """关闭Selenium"""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                logger.debug("_close_selenium 忽略异常", exc_info=True)
            self.driver = None
    
    def _request(self, url: str, retry_count: int = 0) -> Optional[str]:
        """发起HTTP请求"""
        # 如果启用Selenium，使用Selenium获取页面
        if self.use_selenium:
            return self._request_selenium(url)
        
        # 否则使用requests
        try:
            logger.debug(f"请求: {url} (尝试 {retry_count + 1}/{self.max_retries})")
            
            # 添加Referer
            headers = {'Referer': self.base_url}
            
            response = self.session.get(url, headers=headers, timeout=self.timeout)
            
            # 对于404错误，不重试，直接抛出
            if response.status_code == 404:
                logger.warning(f"页面不存在(404): {url}")
                raise requests.exceptions.HTTPError(f"404 Client Error: Not Found for url: {url}")
            
            response.raise_for_status()
            
            # 确保正确解码
            response.encoding = response.apparent_encoding or 'utf-8'
            
            logger.debug(f"响应长度: {len(response.text)}")
            
            return response.text
            
        except requests.exceptions.HTTPError as e:
            # 404错误不重试
            if '404' in str(e):
                raise
            
            logger.warning(f"请求失败: {e}")
            
            if retry_count < self.max_retries - 1:
                delay = self.retry_delay * (2 ** retry_count)
                time.sleep(delay)
                return self._request(url, retry_count + 1)
            else:
                logger.error(f"达到最大重试次数: {url}")
                raise
        except Exception as e:
            logger.warning(f"请求失败: {e}")
            
            if retry_count < self.max_retries - 1:
                delay = self.retry_delay * (2 ** retry_count)
                time.sleep(delay)
                return self._request(url, retry_count + 1)
            else:
                logger.error(f"达到最大重试次数: {url}")
                raise
    
    def _request_selenium(self, url: str) -> Optional[str]:
        """使用Selenium获取页面"""
        self._init_selenium()
        
        if not self.driver:
            # Selenium初始化失败，回退到requests
            return self._request(url)
        
        try:
            logger.debug(f"Selenium访问: {url}")
            self.driver.get(url)
            
            # 等待页面加载
            time.sleep(2)
            
            # 处理弹窗广告
            self._handle_popups()
            
            # 再等待一下确保内容加载
            time.sleep(1)
            
            # 检查是否被阻止
            page_source = self.driver.page_source
            if "Sorry, you have been blocked" in page_source or "403" in page_source:
                logger.error("被网站阻止")
                return None
            
            return page_source
            
        except Exception as e:
            logger.error(f"Selenium获取页面失败: {e}")
            return None
    
    def _handle_popups(self):
        """处理弹窗广告"""
        if not self.driver:
            return
        
        try:
            # 常见弹窗关闭按钮选择器
            close_selectors = [
                '.close', '.modal-close', '.popup-close', '.ad-close',
                '[class*="close"]', '[class*="dismiss"]', 
                'button.close', 'a.close', '.btn-close',
                '.fc-close', '.fc-button-close',  # cookie弹窗
                '#close-btn', '.close-btn', '.closeBtn',
                'div[class*="overlay"] .close', 
                'span[class*="close"]', 'i[class*="close"]',
            ]
            
            for selector in close_selectors:
                try:
                    elements = self.driver.find_elements('css selector', selector)
                    for elem in elements:
                        if elem.is_displayed():
                            elem.click()
                            time.sleep(0.3)
                            logger.debug(f"关闭弹窗: {selector}")
                except Exception:
                    logger.debug("_handle_popups 忽略异常", exc_info=True)
            
            # 尝试按ESC键关闭弹窗
            try:
                from selenium.webdriver.common.keys import Keys
                body = self.driver.find_element('tag name', 'body')
                body.send_keys(Keys.ESCAPE)
            except Exception:
                logger.debug("_handle_popups 忽略异常", exc_info=True)
                
        except Exception as e:
            logger.debug(f"处理弹窗时出错: {e}")
    
    def search(self, keyword: str, max_pages: int = 5, fetch_details: bool = True) -> CrawlResult:
        """
        搜索模特资源
        
        Args:
            keyword: 搜索关键词（模特名）
            max_pages: 最大搜索页数
            fetch_details: 是否获取详情页BT链接
        
        Returns:
            爬取结果
        """
        logger.info(f"开始搜索: {keyword}")
        start_time = datetime.now()
        
        all_videos = []
        pages_crawled = 0
        error_message = None
        
        try:
            # 先检测可用域名
            working_url = self.find_working_url()
            if not working_url:
                error_message = "所有域名都不可用，请检查网络或手动更换域名"
                logger.error(error_message)
            else:
                if working_url != self.base_url:
                    logger.info(f"已自动切换到可用域名: {working_url}")
            
            encoded_keyword = urllib.parse.quote(keyword, safe='')
            
            for page in range(1, max_pages + 1):
                # 构建URL
                if page == 1:
                    url = f"{self.base_url}/?s={encoded_keyword}"
                else:
                    url = f"{self.base_url}/page/{page}/?s={encoded_keyword}"
                
                logger.info(f"搜索第 {page} 页: {url}")
                
                html = self._request(url)
                if not html:
                    break
                
                soup = BeautifulSoup(html, 'html.parser')
                videos = self._parse_search_results(soup, keyword)
                
                if not videos:
                    logger.info(f"第 {page} 页无结果，停止")
                    break
                
                # 如果搜索结果没有磁力链接，进入详情页获取
                if fetch_details:
                    for video in videos:
                        if not video.magnet_links and video.detail_url:
                            try:
                                logger.info(f"获取详情页BT: {video.detail_url}")
                                detail = self.get_detail(video.detail_url)
                                if detail.get('magnet_links'):
                                    video.magnet_links = detail['magnet_links']
                                    logger.info(f"  获取到 {len(video.magnet_links)} 个BT链接")
                                time.sleep(self.request_delay * 0.5)  # 短暂延迟
                            except Exception as e:
                                logger.warning(f"获取详情页失败: {e}")
                
                all_videos.extend(videos)
                pages_crawled += 1
                logger.info(f"第 {page} 页: {len(videos)} 个结果，总计 {len(all_videos)} 个")
                
                time.sleep(self.request_delay)
            
        except Exception as e:
            error_message = str(e)
            logger.error(f"搜索失败: {e}")
        
        # 计算总磁力链接数
        total_magnets = sum(len(v.magnet_links) for v in all_videos)
        duration = (datetime.now() - start_time).total_seconds()
        
        return CrawlResult(
            model_name=keyword,
            videos=all_videos,
            total_magnets=total_magnets,
            pages_crawled=pages_crawled,
            duration_seconds=round(duration, 2),
            success=error_message is None,
            error_message=error_message,
        )
    
    def _parse_search_results(self, soup: BeautifulSoup, keyword: str) -> List[VideoInfo]:
        """解析搜索结果页"""
        videos = []
        
        # Madouqu网站使用article标签
        items = soup.select('article.post')
        
        if not items:
            # 备选选择器
            item_selectors = [
                'article', '.post', '.result-item', '.item',
                'li[class*="post"]', '.entry', '.content-item',
            ]
            
            for selector in item_selectors:
                items = soup.select(selector)
                if items:
                    break
        
        if not items:
            # 备选：查找包含磁力链接的元素
            magnet_links = soup.find_all('a', href=re.compile(r'^magnet:'))
            for link in magnet_links:
                container = link.find_parent(['article', 'div', 'li'])
                if container:
                    items.append(container)
        
        for item in items:
            video = self._parse_item(item, keyword)
            if video:
                videos.append(video)
        
        return videos
    
    def _parse_item(self, item, keyword: str) -> Optional[VideoInfo]:
        """解析单个项目"""
        # 提取标题 - Madouqu网站标题在h2.entry-title a的title属性中
        title = ''
        
        # 方法1: h2.entry-title a[title]
        title_elem = item.select_one('h2.entry-title a[title]')
        if title_elem:
            title = title_elem.get('title', '')
        
        # 方法2: a[title]
        if not title:
            title_elem = item.select_one('a[title]')
            if title_elem:
                title = title_elem.get('title', '')
        
        # 方法3: h2, h3, .title
        if not title:
            for selector in ['h2', 'h3', '.title', '.entry-title']:
                elem = item.select_one(selector)
                if elem:
                    title = elem.get('title', '') or elem.get_text(strip=True)
                    if title:
                        break
        
        if not title:
            return None
        
        # 提取磁力链接
        magnet_links = []
        for link in item.find_all('a', href=re.compile(r'^magnet:')):
            href = link.get('href', '')
            if href:
                magnet_links.append(MagnetLink(link=href))
        
        # 提取详情页URL - Madouqu网站链接格式: /video/xxx/
        detail_url = ''
        
        # 方法1: h2.entry-title a
        link_elem = item.select_one('h2.entry-title a[href]')
        if link_elem:
            href = link_elem.get('href', '')
            if href and not href.startswith('magnet:'):
                if not href.startswith('http'):
                    href = self.base_url + href
                detail_url = href
        
        # 方法2: 任意a标签
        if not detail_url:
            link_elem = item.find('a', href=True)
            if link_elem:
                href = link_elem.get('href', '')
                if href and not href.startswith('magnet:'):
                    if not href.startswith('http'):
                        href = self.base_url + href
                    detail_url = href
        
        # 提取日期
        date = ''
        for selector in ['.date', '.time', '.post-date', 'time', '.meta-date']:
            elem = item.select_one(selector)
            if elem:
                date = elem.get_text(strip=True)
                if date:
                    break
        
        # 提取大小
        size = ''
        text = item.get_text()
        match = re.search(r'(\d+\.?\d*\s*[GMT]B)', text, re.IGNORECASE)
        if match:
            size = match.group(1)
        
        return VideoInfo(
            title=title,
            model_name=keyword,
            magnet_links=magnet_links,
            detail_url=detail_url,
            date=date,
            size=size,
        )
    
    def get_detail(self, url: str) -> Dict[str, Any]:
        """
        获取详情页信息
        
        Args:
            url: 详情页URL
        
        Returns:
            详情信息
        """
        logger.info(f"获取详情页: {url}")
        
        try:
            html = self._request(url)
            if not html:
                return {}
            
            soup = BeautifulSoup(html, 'html.parser')
            
            # 提取磁力链接 - 多种方式
            magnet_links = []
            
            # 方式1: 直接查找magnet:链接
            for link in soup.find_all('a', href=re.compile(r'^magnet:')):
                href = link.get('href', '')
                if href and href not in [m.link for m in magnet_links]:
                    magnet_links.append(MagnetLink(link=href))
            
            # 方式2: 查找包含"磁力"或"magnet"文本的链接
            for link in soup.find_all('a', string=re.compile(r'(磁力|magnet|bt|下载)', re.I)):
                href = link.get('href', '')
                if href and href.startswith('magnet:') and href not in [m.link for m in magnet_links]:
                    magnet_links.append(MagnetLink(link=href))
            
            # 方式3: 查找特定class的链接
            for selector in ['.magnet-link', '.bt-link', '.download-link', 'a[class*="magnet"]', 'a[class*="bt"]']:
                for link in soup.select(selector):
                    href = link.get('href', '')
                    if href and href.startswith('magnet:') and href not in [m.link for m in magnet_links]:
                        magnet_links.append(MagnetLink(link=href))
            
            # 方式4: 在文本中查找magnet链接
            text = soup.get_text()
            magnet_pattern = re.compile(r'magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^"\s<>]*')
            for match in magnet_pattern.finditer(text):
                href = match.group()
                if href not in [m.link for m in magnet_links]:
                    magnet_links.append(MagnetLink(link=href))
            
            logger.info(f"详情页找到 {len(magnet_links)} 个磁力链接")
            
            # 提取标题
            title = ''
            for selector in ['h1.entry-title', 'h1', '.title', '.entry-title', '.post-title']:
                elem = soup.select_one(selector)
                if elem:
                    title = elem.get_text(strip=True)
                    if title:
                        break
            
            # 提取内容
            content = ''
            for selector in ['.content', '.entry-content', 'article', '.post-content']:
                elem = soup.select_one(selector)
                if elem:
                    content = elem.get_text(strip=True)[:500]
                    if content:
                        break
            
            return {
                'url': url,
                'title': title,
                'magnet_links': [m.to_dict() for m in magnet_links],
                'content': content,
            }
            
        except requests.exceptions.HTTPError as e:
            # 处理HTTP错误（如404）
            if '404' in str(e):
                logger.warning(f"详情页不存在(404): {url}")
            else:
                logger.warning(f"详情页HTTP错误: {url} - {e}")
            return {}
        except Exception as e:
            logger.warning(f"获取详情页失败: {url} - {e}")
            return {}
    
    def get_all_magnets(self, keyword: str, max_pages: int = 5, fetch_detail: bool = False) -> List[str]:
        """
        获取所有磁力链接
        
        Args:
            keyword: 搜索关键词
            max_pages: 最大页数
            fetch_detail: 是否从详情页获取
        
        Returns:
            磁力链接列表
        """
        result = self.search(keyword, max_pages)
        
        magnets = []
        for video in result.videos:
            # 直接的磁力链接
            for m in video.magnet_links:
                magnets.append(m.link)
            
            # 从详情页获取
            if fetch_detail and video.detail_url:
                detail = self.get_detail(video.detail_url)
                for m in detail.get('magnet_links', []):
                    if isinstance(m, dict):
                        magnets.append(m.get('link', ''))
                    else:
                        magnets.append(m)
        
        return list(set(magnets))  # 去重
    
    def crawl_model(self, model_name: str, max_pages: int = 10) -> ModelInfo:
        """
        爬取模特信息
        
        Args:
            model_name: 模特名
            max_pages: 最大页数
        
        Returns:
            模特信息
        """
        result = self.search(model_name, max_pages)
        
        return ModelInfo(
            name=model_name,
            video_count=len(result.videos),
            magnet_count=result.total_magnets,
            last_crawled=datetime.now().isoformat(),
        )
    
    def close(self):
        """关闭爬虫"""
        self._close_selenium()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
