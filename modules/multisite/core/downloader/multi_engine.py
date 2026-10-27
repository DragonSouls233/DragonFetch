# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
多引擎下载器核心模块
支持多种下载方式，提供分流策略
"""
import os
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Callable, List
from enum import Enum
import concurrent.futures

logger = logging.getLogger(__name__)


class DownloadEngine(Enum):
    """下载引擎枚举"""
    YTDLP = "ytdlp"           # yt-dlp (首选)
    M3U8 = "m3u8"             # M3U8 直接下载
    DIRECT = "direct"         # 直接 URL 下载
    FALLBACK = "fallback"     # 回退引擎


class BaseDownloadEngine(ABC):
    """下载引擎基类"""
    
    name: str = "base"
    priority: int = 0  # 优先级，数字越大优先级越高
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self._is_cancelled = False
        self._progress_callback = None
    
    @abstractmethod
    def can_handle(self, url: str, info: Dict[str, Any] = None) -> bool:
        """
        检查此引擎是否能处理该URL
        
        Args:
            url: 视频URL
            info: 视频信息
        
        Returns:
            是否能处理
        """
        pass
    
    @abstractmethod
    def download(self, url: str, output_path: str, 
                 progress_callback: Callable[[Dict], None] = None) -> bool:
        """
        下载视频
        
        Args:
            url: 视频URL
            output_path: 输出路径
            progress_callback: 进度回调
        
        Returns:
            是否成功
        """
        pass
    
    @abstractmethod
    def extract_info(self, url: str) -> Dict[str, Any]:
        """
        提取视频信息
        
        Args:
            url: 视频URL
        
        Returns:
            视频信息字典
        """
        pass
    
    def cancel(self):
        """取消下载"""
        self._is_cancelled = True
    
    def set_progress_callback(self, callback: Callable[[Dict], None]):
        """设置进度回调"""
        self._progress_callback = callback


class YTDLPDownloadEngine(BaseDownloadEngine):
    """yt-dlp 下载引擎"""
    
    name = "YT-DLP"
    priority = 100
    
    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    
    def can_handle(self, url: str, info: Dict[str, Any] = None) -> bool:
        """yt-dlp 是通用引擎，可以处理大多数URL"""
        # 如果已经有直接的媒体URL，不需要 yt-dlp
        if info and info.get('formats'):
            # 检查是否有可用的直接下载URL
            for fmt in info.get('formats', []):
                if fmt.get('url') and self._is_direct_url(fmt.get('url')):
                    return False
        return True
    
    def _is_direct_url(self, url: str) -> bool:
        """检查是否是可直接下载的URL"""
        if not url:
            return False
        # 排除 m3u8, mpd (DASH), token URL 等
        exclude_patterns = ['m3u8', '.mpd', 'token=', 'Manifest']
        return not any(p in url.lower() for p in exclude_patterns)
    
    def _get_ydl_options(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """获取 yt-dlp 配置"""
        opts = {
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'socket_timeout': self.config.get('timeout', 60),
            'retries': self.config.get('retry_times', 3),
            'fragment_retries': self.config.get('retry_times', 3),
            'extractor_retries': self.config.get('retry_times', 3),
            'concurrent_fragment_downloads': 3,
        }
        
        # 尝试使用 curl_cffi 模拟浏览器 - 这是绕过 PornHub 410 错误的关键
        # 根据用户测试，--impersonate chrome --no-cookies 可以成功绕过 410 错误
        cookie_file = self.config.get('cookie_file')
        use_impersonate = False
        
        try:
            import curl_cffi
            opts['impersonate'] = 'chrome'  # 使用 curl_cffi 模拟 Chrome 浏览器
            use_impersonate = True
            logger.info("MultiEngine: 使用 curl_cffi 模拟 Chrome 浏览器访问 (绕过 410 错误)")
        except ImportError:
            logger.warning("curl_cffi 未安装，无法模拟浏览器。可能无法绕过 PornHub 410 错误")
            # 回退到基本的 HTTP 头配置
            opts['http_headers'] = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
            }
        
        # 代理支持
        proxy = self.config.get('proxy')
        if proxy:
            opts['proxy'] = proxy
        
        # Cookie支持 - 如果使用 impersonate 则不使用 cookies（--no-cookies）
        if use_impersonate:
            logger.info("使用 impersonate 模式，不加载 cookies 文件")
        elif cookie_file and os.path.exists(cookie_file):
            opts['cookiefile'] = cookie_file
            logger.info(f"使用 cookie 文件: {cookie_file}")
        
        # 输出路径
        if output_path:
            output_dir = os.path.dirname(output_path)
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
            opts['outtmpl'] = output_path
        
        return opts
    
    def _progress_hook(self, d: Dict[str, Any]) -> None:
        """进度回调钩子"""
        if self._is_cancelled:
            raise Exception("下载已取消")
        
        if d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            
            if total > 0:
                progress = (downloaded / total) * 100
            else:
                progress = 0
            
            speed = d.get('speed', 0) or 0
            eta = d.get('eta', 0) or 0
            
            if self._progress_callback:
                self._progress_callback({
                    'status': 'downloading',
                    'progress': round(progress, 1),
                    'speed': speed,
                    'eta': eta,
                    'engine': self.name,
                })
        
        elif d['status'] == 'finished' and self._progress_callback:
            self._progress_callback({
                'status': 'finished',
                'progress': 100,
                'filename': d.get('filename', ''),
                'engine': self.name,
            })
    
    def download(self, url: str, output_path: str,
                progress_callback: Callable[[Dict], None] = None) -> bool:
        """使用 yt-dlp 下载"""
        try:
            import yt_dlp
            
            self._progress_callback = progress_callback
            opts = self._get_ydl_options(output_path)
            opts['progress_hooks'] = [self._progress_hook]
            
            logger.info(f"[{self.name}] 开始下载: {url}")
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            
            logger.info(f"[{self.name}] 下载完成: {output_path}")
            return True
        
        except Exception as e:
            logger.error(f"[{self.name}] 下载失败: {e}")
            raise
    
    def extract_info(self, url: str) -> Dict[str, Any]:
        """提取视频信息"""
        try:
            import yt_dlp
            
            opts = self._get_ydl_options()
            opts['quiet'] = True
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            
            return info or {}
        
        except Exception as e:
            logger.error(f"[{self.name}] 获取视频信息失败: {e}")
            return {}


class M3U8DownloadEngine(BaseDownloadEngine):
    """M3U8 直接下载引擎 (无需 yt-dlp)"""
    
    name = "M3U8"
    priority = 90
    
    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._session = None
    
    def can_handle(self, url: str, info: Dict[str, Any] = None) -> bool:
        """检查是否是 M3U8 URL"""
        if not url:
            return False
        url_lower = url.lower()
        return 'm3u8' in url_lower or (info and info.get('ext') == 'm3u8')
    
    def _get_session(self):
        """获取 HTTP 会话"""
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': self.config.get('referer', ''),
            })
            
            # 添加 cookie 支持
            cookie_file = self.config.get('cookie_file')
            if cookie_file and os.path.exists(cookie_file):
                import requests_cookie_jar
                self._session.cookies = requests_cookie_jar_from_file(cookie_file)
        return self._session
    
    def _parse_m3u8(self, content: str) -> List[str]:
        """解析 M3U8 内容，提取所有 segment URL"""
        segments = []
        for line in content.strip().split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                segments.append(line)
        return segments
    
    def _get_best_quality_url(self, m3u8_content: str, base_url: str) -> str:
        """从 M3U8 主文件获取最佳质量的流URL"""
        import re
        
        # 查找 #EXT-X-STREAM-INF 行获取不同质量的流
        stream_pattern = re.compile(r'#EXT-X-STREAM-INF:.*?\n(.+?)(?=\n#|\n$)', re.DOTALL)
        matches = stream_pattern.findall(m3u8_content)
        
        if matches:
            # 选择最高质量的 (通常最后一个)
            best_url = matches[-1].strip()
            return self._resolve_url(base_url, best_url)
        
        # 如果没有主文件格式，返回第一行非注释内容
        for line in m3u8_content.split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                return self._resolve_url(base_url, line)
        
        return None
    
    def _resolve_url(self, base_url: str, segment_url: str) -> str:
        """解析相对/绝对 URL"""
        from urllib.parse import urljoin, urlparse
        
        if segment_url.startswith('http'):
            return segment_url
        
        # 处理绝对路径
        if segment_url.startswith('/'):
            parsed = urlparse(base_url)
            return f"{parsed.scheme}://{parsed.netloc}{segment_url}"
        
        return urljoin(base_url, segment_url)
    
    def download(self, url: str, output_path: str,
                 progress_callback: Callable[[Dict], None] = None) -> bool:
        """下载 M3U8 流"""
        import requests
        from urllib.parse import urlparse
        import threading
        
        self._progress_callback = progress_callback
        self._is_cancelled = False
        
        try:
            logger.info(f"[{self.name}] 开始下载 M3U8: {url}")
            
            session = self._get_session()
            
            # 获取 M3U8 内容
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            content = resp.text
            
            # 检查是否是主文件 (包含 #EXT-X-STREAM-INF)
            if '#EXT-X-STREAM-INF' in content:
                stream_url = self._get_best_quality_url(content, url)
                if stream_url:
                    logger.info(f"[{self.name}] 选择流: {stream_url}")
                    url = stream_url
                    resp = session.get(url, timeout=30)
                    resp. raise_for_status()
                    content = resp.text
            
            # 解析所有 segments
            segments = self._parse_m3u8(content)
            total_segments = len(segments)
            
            if total_segments == 0:
                raise Exception("无法解析 M3U8 内容")
            
            # 获取 base URL
            base_url = url.rsplit('/', 1)[0] + '/'
            
            # 解析 segment URLs
            segment_urls = [self._resolve_url(base_url, seg) for seg in segments]
            
            # 下载并合并
            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            
            downloaded = 0
            segment_data = [None] * total_segments
            
            def download_segment(idx, seg_url):
                """下载单个 segment"""
                if self._is_cancelled:
                    return
                try:
                    resp = session.get(seg_url, timeout=30, stream=False)
                    resp.raise_for_status()
                    segment_data[idx] = resp.content
                except Exception as e:
                    logger.error(f"[{self.name}] Segment {idx} 下载失败: {e}")
                    # 重试一次
                    resp = session.get(seg_url, timeout=60, stream=False)
                    resp.raise_for_status()
                    segment_data[idx] = resp.content
            
            # 使用线程池并发下载
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(download_segment, i, url) 
                    for i, url in enumerate(segment_urls)
                ]
                
                for future in concurrent.futures.as_completed(futures):
                    if self._is_cancelled:
                        break
                    downloaded += 1
                    if progress_callback:
                        progress_callback({
                            'status': 'downloading',
                            'progress': (downloaded / total_segments) * 100,
                            'speed': 0,
                            'eta': 0,
                            'engine': self.name,
                            'detail': f'下载片段 {downloaded}/{total_segments}',
                        })
            
            if self._is_cancelled:
                raise Exception("下载已取消")
            
            # 合并所有 segment
            logger.info(f"[{self.name}] 合并 {total_segments} 个片段...")
            
            # 检测是否为 ts 流，转换为 mp4
            first_segment = segment_data[0]
            if first_segment[:3] == b'\x00\x00\x01' or first_segment[:4] in [b'GERT', b'\x47\x40\x11']:
                # 这是 TS 流，需要转换
                self._write_ts_segments(segment_data, output_path, progress_callback)
            else:
                # 直接写入
                with open(output_path, 'wb') as f:
                    for data in segment_data:
                        if data:
                            f.write(data)
            
            if progress_callback:
                progress_callback({
                    'status': 'finished',
                    'progress': 100,
                    'filename': output_path,
                    'engine': self.name,
                })
            
            logger.info(f"[{self.name}] 下载完成: {output_path}")
            return True
        
        except Exception as e:
            logger.error(f"[{self.name}] 下载失败: {e}")
            raise
    
    def _write_ts_segments(self, segment_data: List[bytes], output_path: str,
                           progress_callback: Callable[[Dict], None] = None):
        """写入 TS 片段并转换"""
        import subprocess
        import tempfile
        
        # 先写入所有 ts 文件
        ts_files = []
        temp_dir = tempfile.mkdtemp()
        
        try:
            for i, data in enumerate(segment_data):
                if data:
                    ts_file = os.path.join(temp_dir, f'segment_{i:04d}.ts')
                    with open(ts_file, 'wb') as f:
                        f.write(data)
                    ts_files.append(ts_file)
            
            # 使用 ffmpeg 合并并转换为 mp4
            if progress_callback:
                progress_callback({
                    'status': 'converting',
                    'progress': 50,
                    'engine': self.name,
                    'detail': '正在转换格式...',
                })
            
            # 尝试使用 ffmpeg
            try:
                import shutil
                ffmpeg_path = shutil.which('ffmpeg')
                if ffmpeg_path:
                    # 创建文件列表
                    list_file = os.path.join(temp_dir, 'concat.txt')
                    with open(list_file, 'w') as f:
                        for ts_file in ts_files:
                            f.write(f"file '{ts_file}'\n")
                    
                    # 使用 ffmpeg concat demuxer
                    cmd = [
                        ffmpeg_path, '-f', 'concat', '-safe', '0',
                        '-i', list_file, '-c', 'copy', '-y', output_path
                    ]
                    
                    result = subprocess.run(cmd, capture_output=True, timeout=600)
                    if result.returncode != 0:
                        raise Exception(f"ffmpeg 失败: {result.stderr.decode()}")
                else:
                    # 没有 ffmpeg，直接合并 TS 文件
                    with open(output_path, 'wb') as out:
                        for ts_file in ts_files:
                            with open(ts_file, 'rb') as inp:
                                out.write(inp.read())
            
            except Exception as e:
                logger.warning(f"[{self.name}] ffmpeg 不可用，直接合并 TS: {e}")
                with open(output_path, 'wb') as out:
                    for ts_file in ts_files:
                        with open(ts_file, 'rb') as inp:
                            out.write(inp.read())
        
        finally:
            # 清理临时文件
            import shutil
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                logger.debug("_write_ts_segments 忽略异常", exc_info=True)
    
    def extract_info(self, url: str) -> Dict[str, Any]:
        """获取 M3U8 信息"""
        try:
            import requests
            
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            
            return {
                'url': url,
                'ext': 'm3u8',
                'title': url.split('/')[-1],
                'formats': [{'url': url, 'ext': 'm3u8'}],
            }
        except Exception as e:
            logger.error(f"[{self.name}] 获取信息失败: {e}")
            return {}


class DirectDownloadEngine(BaseDownloadEngine):
    """直接 URL 下载引擎 (使用 requests 或 curl_cffi)"""
    
    name = "Direct"
    priority = 80
    
    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self._chunk_size = 1024 * 1024  # 1MB chunks
    
    def can_handle(self, url: str, info: Dict[str, Any] = None) -> bool:
        """检查是否是可直接下载的 URL"""
        if not url:
            return False
        
        url_lower = url.lower()
        
        # 排除流媒体格式
        exclude_exts = ['m3u8', 'mpd', 'ts']
        for ext in exclude_exts:
            if f'.{ext}' in url_lower:
                return False
        
        # 检查是否是视频文件扩展名
        video_exts = ['.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.m4v']
        return any(url_lower.endswith(ext) for ext in video_exts)
    
    def download(self, url: str, output_path: str,
                 progress_callback: Callable[[Dict], None] = None) -> bool:
        """直接下载 URL"""
        self._progress_callback = progress_callback
        self._is_cancelled = False
        
        try:
            logger.info(f"[{self.name}] 开始直接下载: {url}")
            
            # 优先使用 curl_cffi
            try:
                from curl_cffi import requests as curl_requests
                use_curl = True
            except ImportError:
                import requests
                use_curl = False
            
            # 获取文件大小
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }
            
            if use_curl:
                resp = curl_requests.get(url, headers=headers, stream=True, 
                                         impersonate='chrome', timeout=30)
            else:
                resp = requests.get(url, headers=headers, stream=True, timeout=30)
            
            resp.raise_for_status()
            
            total_size = int(resp.headers.get('content-length', 0))
            
            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            
            downloaded = 0
            
            with open(output_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=self._chunk_size):
                    if self._is_cancelled:
                        raise Exception("下载已取消")
                    
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        if progress_callback and total_size > 0:
                            progress = (downloaded / total_size) * 100
                            progress_callback({
                                'status': 'downloading',
                                'progress': round(progress, 1),
                                'speed': 0,
                                'eta': 0,
                                'engine': self.name,
                            })
            
            if progress_callback:
                progress_callback({
                    'status': 'finished',
                    'progress': 100,
                    'filename': output_path,
                    'engine': self.name,
                })
            
            logger.info(f"[{self.name}] 下载完成: {output_path}")
            return True
        
        except Exception as e:
            logger.error(f"[{self.name}] 下载失败: {e}")
            raise
    
    def extract_info(self, url: str) -> Dict[str, Any]:
        """获取直接 URL 信息"""
        from urllib.parse import urlparse
        
        parsed = urlparse(url)
        
        return {
            'url': url,
            'title': os.path.basename(parsed.path),
            'ext': os.path.splitext(parsed.path)[1].lstrip('.'),
        }


class MultiEngineDownloader:
    """
    多引擎下载器
    
    支持多种下载方式，提供自动回退策略：
    1. 优先使用 yt-dlp (最通用)
    2. 如果yt-dlp失败，尝试 M3U8 直接下载
    3. 如果M3U8失败，尝试直接 URL 下载
    4. 如果都是失败，使用 curl_cffi 直接下载 (最后手段)
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self._engines: List[BaseDownloadEngine] = []
        self._setup_engines()
    
    def _setup_engines(self):
        """初始化所有引擎"""
        # 按优先级排序
        self._engines = [
            YTDLPDownloadEngine(self.config),
            M3U8DownloadEngine(self.config),
            DirectDownloadEngine(self.config),
        ]
        
        # 按优先级排序
        self._engines.sort(key=lambda e: e.priority, reverse=True)
        
        logger.info(f"已加载 {len(self._engines)} 个下载引擎: " + 
                    ", ".join([f"{e.name}(优先级:{e.priority})" for e in self._engines]))
    
    def get_best_engine(self, url: str, info: Dict[str, Any] = None) -> Optional[BaseDownloadEngine]:
        """获取最佳引擎"""
        for engine in self._engines:
            if engine.can_handle(url, info):
                return engine
        return None
    
    def download(self, url: str, output_path: str,
                 progress_callback: Callable[[Dict], None] = None,
                 fallback: bool = True) -> bool:
        """
        使用分流策略下载视频
        
        Args:
            url: 视频URL
            output_path: 输出路径
            progress_callback: 进度回调
            fallback: 是否启用回退策略
        
        Returns:
            是否成功
        """
        errors = []
        
        # 尝试每个引擎
        engines_to_try = self._engines if fallback else [self.get_best_engine(url)]
        
        for engine in engines_to_try:
            if engine is None:
                continue
            
            if not engine.can_handle(url):
                logger.debug(f"[{engine.name}] 无法处理此URL，跳过")
                continue
            
            try:
                logger.info(f"[{engine.name}] 尝试下载...")
                
                result = engine.download(url, output_path, progress_callback)
                
                if result:
                    logger.info(f"[{engine.name}] 成功!")
                    return True
            
            except Exception as e:
                error_msg = f"[{engine.name}] {str(e)}"
                errors.append(error_msg)
                logger.warning(f"{error_msg}，尝试下一个引擎...")
                
                # 如果是取消，不继续
                if "取消" in str(e):
                    raise
        
        # 所有引擎都失败
        error_summary = "; ".join(errors)
        logger.error(f"所有引擎都失败: {error_summary}")
        raise Exception(f"下载失败: {error_summary}")
    
    async def download_async(self, url: str, output_path: str,
                            progress_callback: Callable[[Dict], None] = None) -> bool:
        """异步下载"""
        # ===== URL 规范化：处理 PornHub 中国域名问题 =====
        if 'cn.pornhub.com' in url:
            logger.info(f"MultiEngine: 检测到中国域名，转换为 www.pornhub.com")
            url = url.replace('cn.pornhub.com', 'www.pornhub.com')
        # ===== URL 规范化结束 =====
        
        loop = asyncio.get_event_loop()
        
        def _download():
            return self.download(url, output_path, progress_callback)
        
        return await loop.run_in_executor(None, _download)
    
    def extract_info(self, url: str) -> Dict[str, Any]:
        """提取视频信息，优先使用 yt-dlp"""
        try:
            ytdlp_engine = self._engines[0]  # YTDLPDownloadEngine
            return ytdlp_engine.extract_info(url)
        except Exception as e:
            logger.error(f"提取信息失败: {e}")
            return {}


def create_multi_engine_downloader(config: Dict[str, Any] = None) -> MultiEngineDownloader:
    """
    创建多引擎下载器
    
    Args:
        config: 配置字典
    
    Returns:
        MultiEngineDownloader 实例
    """
    return MultiEngineDownloader(config)


def requests_cookie_jar_from_file(file_path: str):
    """
    从 Netscape 格式的 cookie 文件加载 cookies
    
    Args:
        file_path: cookie 文件路径
    
    Returns:
        requests.cookies.RequestsCookieJar
    """
    import requests
    
    jar = requests.cookies.RequestsCookieJar()
    
    if not os.path.exists(file_path):
        return jar
    
    with open(file_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            parts = line.split('\t')
            if len(parts) >= 7:
                domain, _, path, secure, expiration, name, value = parts[:7]
                jar.set(name, value, domain=domain, path=path, secure=secure == 'TRUE')
    
    return jar
