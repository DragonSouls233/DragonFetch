# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
HTTP多线程分片下载引擎
支持断点续传、多线程并行下载、进度追踪
"""
import asyncio
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from threading import Lock
import logging

try:
    import aiohttp
    import aiofiles
except ImportError:
    aiohttp = None
    aiofiles = None

from ..base import BaseDownloader

logger = logging.getLogger(__name__)


@dataclass
class ChunkInfo:
    """分片信息"""
    index: int
    start: int
    end: int
    downloaded: int = 0
    status: str = 'pending'  # pending, downloading, completed, failed
    retry_count: int = 0


@dataclass
class DownloadState:
    """下载状态"""
    url: str
    output_path: str
    total_size: int = 0
    downloaded: int = 0
    chunks: List[ChunkInfo] = field(default_factory=list)
    start_time: float = 0
    last_update_time: float = 0
    last_downloaded: int = 0
    speed: float = 0
    eta: int = 0
    status: str = 'pending'
    supports_range: bool = False
    temp_dir: str = ''


class DownloadError(Exception):
    """下载错误"""
    pass


class HttpEngine(BaseDownloader):
    """HTTP多线程分片下载引擎"""

    DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024  # 8MB per chunk
    DEFAULT_THREAD_COUNT = 8
    MAX_RETRY_COUNT = 3
    MIN_CHUNK_SIZE = 1024 * 1024
    MAX_CHUNK_SIZE = 50 * 1024 * 1024
    BUFFER_SIZE = 64 * 1024
    SPEED_CALC_INTERVAL = 1.0

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        if aiohttp is None:
            raise ImportError("aiohttp is not installed. Please install it with: pip install aiohttp aiofiles")
        
        self._state: Optional[DownloadState] = None
        self._lock = Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._pause_event = asyncio.Event()
        self._pause_event.set()

        self.thread_count = config.get('thread_count', self.DEFAULT_THREAD_COUNT)
        self.chunk_size = config.get('chunk_size', self.DEFAULT_CHUNK_SIZE)
        self.max_retry = config.get('max_retry', self.MAX_RETRY_COUNT)
        self.timeout = config.get('timeout', 30)
        self.speed_limit = config.get('speed_limit', 0)

        self.chunk_size = max(self.MIN_CHUNK_SIZE, min(self.chunk_size, self.MAX_CHUNK_SIZE))

    async def _create_session(self) -> 'aiohttp.ClientSession':
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout, connect=10)
            connector = aiohttp.TCPConnector(limit=self.thread_count * 2, limit_per_host=self.thread_count)

            headers = {
                'User-Agent': self._get_user_agent(),
            }

            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers=headers,
                trust_env=True
            )

        return self._session

    def _get_user_agent(self) -> str:
        return self.config.get('user_agent',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

    async def _get_file_info(self, url: str) -> Dict[str, Any]:
        session = await self._create_session()

        try:
            async with session.head(url, allow_redirects=True) as response:
                if response.status not in (200, 206):
                    raise DownloadError(f"Failed to get file info, status: {response.status}")

                info = {
                    'url': str(response.url),
                    'status': response.status,
                    'content_length': int(response.headers.get('Content-Length', 0)),
                    'content_type': response.headers.get('Content-Type', 'application/octet-stream'),
                    'accept_ranges': response.headers.get('Accept-Ranges', '').lower() == 'bytes',
                    'etag': response.headers.get('ETag', ''),
                    'last_modified': response.headers.get('Last-Modified', ''),
                }

                logger.info(f"File info: size={info['content_length']}, accept_ranges={info['accept_ranges']}")
                return info

        except aiohttp.ClientError as e:
            logger.error(f"Failed to get file info: {e}")
            raise DownloadError(f"Failed to get file info: {str(e)}")

    def _calculate_chunks(self, total_size: int) -> List[ChunkInfo]:
        if total_size <= 0:
            return []

        chunk_count = max(1, min(self.thread_count, total_size // self.chunk_size))

        if total_size < self.chunk_size * 2:
            chunk_count = 1

        chunk_size = total_size // chunk_count
        chunks = []

        for i in range(chunk_count):
            start = i * chunk_size
            if i == chunk_count - 1:
                end = total_size - 1
            else:
                end = start + chunk_size - 1

            chunks.append(ChunkInfo(
                index=i,
                start=start,
                end=end,
                downloaded=0,
                status='pending'
            ))

        logger.info(f"Created {len(chunks)} chunks for {total_size} bytes")
        return chunks

    async def _download_chunk(self, chunk: ChunkInfo, url: str, temp_dir: str) -> bool:
        session = await self._create_session()
        chunk_file = os.path.join(temp_dir, f"chunk_{chunk.index}.tmp")

        range_start = chunk.start + chunk.downloaded
        range_end = chunk.end

        if range_start > range_end:
            chunk.status = 'completed'
            return True

        headers = {
            'Range': f'bytes={range_start}-{range_end}'
        }

        try:
            async with session.get(url, headers=headers) as response:
                if response.status not in (200, 206):
                    raise DownloadError(f"Unexpected status: {response.status}")

                mode = 'ab' if chunk.downloaded > 0 else 'wb'

                async with aiofiles.open(chunk_file, mode) as f:
                    async for data in response.content.iter_chunked(self.BUFFER_SIZE):
                        await self._pause_event.wait()

                        if self._is_cancelled:
                            chunk.status = 'cancelled'
                            return False

                        await f.write(data)

                        with self._lock:
                            chunk.downloaded += len(data)
                            self._state.downloaded += len(data)

                        if self.speed_limit > 0:
                            await self._apply_speed_limit(len(data))

                chunk.status = 'completed'
                return True

        except asyncio.CancelledError:
            chunk.status = 'cancelled'
            return False

        except Exception as e:
            chunk.status = 'failed'
            chunk.retry_count += 1
            logger.error(f"Chunk {chunk.index} failed: {e}")
            return False

    async def _apply_speed_limit(self, bytes_written: int) -> None:
        if self.speed_limit <= 0:
            return

        expected_time = bytes_written / self.speed_limit
        if expected_time > 0:
            await asyncio.sleep(expected_time)

    def _update_progress(self) -> None:
        if not self._state:
            return

        current_time = time.time()
        elapsed = current_time - self._state.last_update_time

        if elapsed >= self.SPEED_CALC_INTERVAL:
            with self._lock:
                bytes_diff = self._state.downloaded - self._state.last_downloaded
                self._state.speed = bytes_diff / elapsed if elapsed > 0 else 0

                if self._state.speed > 0:
                    remaining_bytes = self._state.total_size - self._state.downloaded
                    self._state.eta = int(remaining_bytes / self._state.speed)
                else:
                    self._state.eta = 0

                self._state.last_update_time = current_time
                self._state.last_downloaded = self._state.downloaded

            self._report_progress({
                'status': 'downloading',
                'progress': round((self._state.downloaded / self._state.total_size) * 100, 1) if self._state.total_size > 0 else 0,
                'speed': self._format_speed(self._state.speed),
                'eta': self._format_eta(self._state.eta),
                'downloaded_bytes': self._state.downloaded,
                'total_bytes': self._state.total_size,
            })

    def _format_speed(self, speed: float) -> str:
        if speed >= 1024 * 1024:
            return f"{speed / (1024 * 1024):.2f} MB/s"
        elif speed >= 1024:
            return f"{speed / 1024:.2f} KB/s"
        else:
            return f"{speed:.2f} B/s"

    def _format_eta(self, eta: int) -> str:
        if eta <= 0:
            return "计算中..."

        hours = eta // 3600
        minutes = (eta % 3600) // 60
        seconds = eta % 60

        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"

    async def _merge_chunks(self, output_path: str, temp_dir: str) -> bool:
        logger.info(f"Merging {len(self._state.chunks)} chunks to {output_path}")

        self.ensure_dir(str(Path(output_path).parent))

        try:
            async with aiofiles.open(output_path, 'wb') as outfile:
                for chunk in sorted(self._state.chunks, key=lambda x: x.index):
                    chunk_file = os.path.join(temp_dir, f"chunk_{chunk.index}.tmp")

                    if not os.path.exists(chunk_file):
                        raise DownloadError(f"Chunk file not found: {chunk_file}")

                    async with aiofiles.open(chunk_file, 'rb') as infile:
                        while True:
                            data = await infile.read(self.BUFFER_SIZE)
                            if not data:
                                break
                            await outfile.write(data)

                    os.remove(chunk_file)

            logger.info(f"Merge completed: {output_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to merge chunks: {e}")
            raise DownloadError(f"Failed to merge chunks: {str(e)}")

    async def _cleanup_temp_files(self, temp_dir: str) -> None:
        try:
            if os.path.exists(temp_dir):
                import shutil
                shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"Failed to cleanup temp files: {e}")

    async def download(self, url: str, output_path: str) -> bool:
        try:
            logger.info(f"Starting HTTP download: {url}")

            file_info = await self._get_file_info(url)
            total_size = file_info['content_length']
            supports_range = file_info['accept_ranges']

            temp_dir = os.path.join(
                Path(output_path).parent,
                f".temp_{Path(output_path).stem}_{int(time.time())}"
            )
            os.makedirs(temp_dir, exist_ok=True)

            self._state = DownloadState(
                url=url,
                output_path=output_path,
                total_size=total_size,
                downloaded=0,
                start_time=time.time(),
                last_update_time=time.time(),
                last_downloaded=0,
                supports_range=supports_range,
                temp_dir=temp_dir,
                status='downloading'
            )

            if supports_range and total_size > 0:
                self._state.chunks = self._calculate_chunks(total_size)
            else:
                self._state.chunks = [ChunkInfo(index=0, start=0, end=total_size - 1 if total_size > 0 else 0)]
                self.thread_count = 1

            tasks = []
            semaphore = asyncio.Semaphore(self.thread_count)

            async def download_with_semaphore(chunk: ChunkInfo):
                async with semaphore:
                    chunk.status = 'downloading'
                    for retry in range(self.max_retry):
                        if self._is_cancelled:
                            return False

                        success = await self._download_chunk(chunk, url, temp_dir)
                        if success:
                            return True

                        if retry < self.max_retry - 1:
                            await asyncio.sleep(2 ** retry)

                    return False

            for chunk in self._state.chunks:
                task = asyncio.create_task(download_with_semaphore(chunk))
                tasks.append(task)

            while not all(t.done() for t in tasks):
                self._update_progress()
                await asyncio.sleep(0.5)

                if self._is_cancelled:
                    for t in tasks:
                        t.cancel()
                    break

            results = await asyncio.gather(*tasks, return_exceptions=True)
            failed_chunks = [c for c in self._state.chunks if c.status != 'completed']

            if failed_chunks:
                raise DownloadError(f"Failed to download {len(failed_chunks)} chunks")

            if self._is_cancelled:
                self._state.status = 'cancelled'
                return False

            await self._merge_chunks(output_path, temp_dir)
            await self._cleanup_temp_files(temp_dir)

            self._state.status = 'completed'
            self._report_progress({
                'status': 'finished',
                'progress': 100,
                'speed': self._format_speed(self._state.speed),
                'filename': output_path,
                'total_bytes': self._state.total_size,
                'elapsed': time.time() - self._state.start_time,
            })

            logger.info(f"Download completed: {output_path}")
            return True

        except Exception as e:
            logger.error(f"Download failed: {url}, error: {str(e)}")
            if self._state:
                self._state.status = 'failed'
            return False

        finally:
            if self._session and not self._session.closed:
                await self._session.close()

    async def get_video_info(self, url: str) -> Dict[str, Any]:
        file_info = await self._get_file_info(url)

        return {
            'id': '',
            'title': Path(url).stem,
            'description': '',
            'duration': 0,
            'url': url,
            'filesize': file_info['content_length'],
            'content_type': file_info['content_type'],
            'supports_range': file_info['accept_ranges'],
        }
