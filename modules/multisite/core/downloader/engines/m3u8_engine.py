# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
M3U8流媒体下载引擎
支持HLS流媒体下载、多线程分片下载
"""
import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from urllib.parse import urljoin
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
class SegmentInfo:
    """分片信息"""
    index: int
    url: str
    duration: float = 0
    downloaded: bool = False
    retry_count: int = 0
    key: Optional[str] = None
    iv: Optional[bytes] = None


@dataclass
class M3U8State:
    """M3U8下载状态"""
    url: str
    output_path: str
    playlist_url: str
    segments: List[SegmentInfo] = field(default_factory=list)
    total_duration: float = 0
    downloaded_duration: float = 0
    downloaded_count: int = 0
    start_time: float = 0
    last_update_time: float = 0
    last_downloaded_count: int = 0
    speed: float = 0
    eta: int = 0
    status: str = 'pending'
    temp_dir: str = ''
    is_encrypted: bool = False
    keys: Dict[str, bytes] = field(default_factory=dict)


class DownloadError(Exception):
    """下载错误"""
    pass


class M3U8Engine(BaseDownloader):
    """M3U8流媒体下载引擎"""

    DEFAULT_THREAD_COUNT = 16
    MAX_RETRY_COUNT = 5
    BUFFER_SIZE = 64 * 1024
    SPEED_CALC_INTERVAL = 1.0

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        if aiohttp is None:
            raise ImportError("aiohttp is not installed. Please install it with: pip install aiohttp aiofiles")
        
        self._state: Optional[M3U8State] = None
        self._lock = Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._pause_event = asyncio.Event()
        self._pause_event.set()

        self.thread_count = config.get('thread_count', self.DEFAULT_THREAD_COUNT)
        self.max_retry = config.get('max_retry', self.MAX_RETRY_COUNT)
        self.timeout = config.get('timeout', 30)

    async def _create_session(self) -> 'aiohttp.ClientSession':
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout, connect=10)
            connector = aiohttp.TCPConnector(limit=self.thread_count * 2)

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

    async def _fetch_playlist(self, url: str) -> str:
        session = await self._create_session()

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    raise DownloadError(f"Failed to fetch playlist, status: {response.status}")
                return await response.text()

        except aiohttp.ClientError as e:
            logger.error(f"Failed to fetch playlist: {e}")
            raise DownloadError(f"Failed to fetch playlist: {str(e)}")

    def _parse_m3u8(self, content: str, base_url: str) -> Dict[str, Any]:
        lines = content.strip().split('\n')
        result = {
            'is_master': False,
            'variants': [],
            'segments': [],
            'total_duration': 0,
            'target_duration': 0,
            'is_encrypted': False,
            'keys': {},
        }

        i = 0
        current_key_uri = None
        current_iv = None

        while i < len(lines):
            line = lines[i].strip()

            if line.startswith('#EXT-X-STREAM-INF:'):
                result['is_master'] = True
                bandwidth = 0
                resolution = ''
                match = re.search(r'BANDWIDTH=(\d+)', line)
                if match:
                    bandwidth = int(match.group(1))
                match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                if match:
                    resolution = match.group(1)

                i += 1
                if i < len(lines):
                    variant_url = lines[i].strip()
                    if not variant_url.startswith('http'):
                        variant_url = urljoin(base_url, variant_url)
                    result['variants'].append({
                        'url': variant_url,
                        'bandwidth': bandwidth,
                        'resolution': resolution,
                    })

            elif line.startswith('#EXT-X-KEY:'):
                result['is_encrypted'] = True
                match = re.search(r'URI="([^"]+)"', line)
                if match:
                    key_uri = match.group(1)
                    if not key_uri.startswith('http'):
                        key_uri = urljoin(base_url, key_uri)
                    current_key_uri = key_uri

                match = re.search(r'IV=0x([0-9a-fA-F]+)', line)
                if match:
                    current_iv = bytes.fromhex(match.group(1))

                if current_key_uri:
                    result['keys'][current_key_uri] = {
                        'uri': current_key_uri,
                        'iv': current_iv,
                    }

            elif line.startswith('#EXTINF:'):
                match = re.search(r'#EXTINF:([\d.]+)', line)
                if match:
                    duration = float(match.group(1))
                    result['target_duration'] = max(result['target_duration'], duration)

                    i += 1
                    if i < len(lines):
                        segment_url = lines[i].strip()
                        if not segment_url.startswith('http'):
                            segment_url = urljoin(base_url, segment_url)

                        result['segments'].append({
                            'url': segment_url,
                            'duration': duration,
                            'key_uri': current_key_uri,
                            'iv': current_iv,
                        })
                        result['total_duration'] += duration

            i += 1

        return result

    async def _select_best_variant(self, variants: List[Dict[str, Any]]) -> str:
        if not variants:
            raise DownloadError("No variants found in master playlist")

        best = max(variants, key=lambda v: v.get('bandwidth', 0))
        logger.info(f"Selected variant: bandwidth={best.get('bandwidth')}, resolution={best.get('resolution')}")
        return best['url']

    async def _fetch_key(self, key_uri: str) -> bytes:
        session = await self._create_session()

        try:
            async with session.get(key_uri) as response:
                if response.status != 200:
                    raise DownloadError(f"Failed to fetch key, status: {response.status}")
                return await response.read()

        except aiohttp.ClientError as e:
            logger.error(f"Failed to fetch key: {e}")
            raise DownloadError(f"Failed to fetch key: {str(e)}")

    async def _download_segment(self, segment: SegmentInfo, temp_dir: str) -> bool:
        session = await self._create_session()
        segment_file = os.path.join(temp_dir, f"segment_{segment.index:05d}.ts")

        try:
            async with session.get(segment.url) as response:
                if response.status != 200:
                    raise DownloadError(f"Unexpected status: {response.status}")

                data = await response.read()

                if segment.key and segment.key in self._state.keys:
                    data = self._decrypt_segment(data, self._state.keys[segment.key], segment.iv)

                async with aiofiles.open(segment_file, 'wb') as f:
                    await f.write(data)

                segment.downloaded = True

                with self._lock:
                    self._state.downloaded_count += 1
                    self._state.downloaded_duration += segment.duration

                return True

        except asyncio.CancelledError:
            return False

        except Exception as e:
            segment.retry_count += 1
            logger.error(f"Segment {segment.index} failed: {e}")
            return False

    def _decrypt_segment(self, data: bytes, key: bytes, iv: Optional[bytes] = None) -> bytes:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend

            if iv is None:
                iv = bytes(16)

            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted = decryptor.update(data) + decryptor.finalize()

            padding_len = decrypted[-1]
            if padding_len <= 16:
                decrypted = decrypted[:-padding_len]

            return decrypted

        except ImportError:
            logger.warning("cryptography not installed, cannot decrypt segment")
            return data
        except Exception as e:
            logger.error(f"Failed to decrypt segment: {e}")
            return data

    def _update_progress(self) -> None:
        if not self._state:
            return

        current_time = time.time()
        elapsed = current_time - self._state.last_update_time

        if elapsed >= self.SPEED_CALC_INTERVAL:
            with self._lock:
                count_diff = self._state.downloaded_count - self._state.last_downloaded_count
                self._state.speed = count_diff / elapsed if elapsed > 0 else 0

                if self._state.speed > 0:
                    remaining_segments = len(self._state.segments) - self._state.downloaded_count
                    self._state.eta = int(remaining_segments / self._state.speed)
                else:
                    self._state.eta = 0

                self._state.last_update_time = current_time
                self._state.last_downloaded_count = self._state.downloaded_count

            progress = (self._state.downloaded_count / len(self._state.segments)) * 100 if self._state.segments else 0
            self._report_progress({
                'status': 'downloading',
                'progress': round(progress, 1),
                'speed': f"{self._state.speed:.1f} seg/s",
                'eta': self._format_eta(self._state.eta),
                'downloaded_segments': self._state.downloaded_count,
                'total_segments': len(self._state.segments),
            })

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

    async def _merge_segments(self, output_path: str, temp_dir: str) -> bool:
        logger.info(f"Merging {len(self._state.segments)} segments to {output_path}")

        self.ensure_dir(str(Path(output_path).parent))

        try:
            async with aiofiles.open(output_path, 'wb') as outfile:
                for segment in sorted(self._state.segments, key=lambda x: x.index):
                    segment_file = os.path.join(temp_dir, f"segment_{segment.index:05d}.ts")

                    if not os.path.exists(segment_file):
                        raise DownloadError(f"Segment file not found: {segment_file}")

                    async with aiofiles.open(segment_file, 'rb') as infile:
                        while True:
                            data = await infile.read(self.BUFFER_SIZE)
                            if not data:
                                break
                            await outfile.write(data)

                    os.remove(segment_file)

            logger.info(f"Merge completed: {output_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to merge segments: {e}")
            raise DownloadError(f"Failed to merge segments: {str(e)}")

    async def _cleanup_temp_files(self, temp_dir: str) -> None:
        try:
            if os.path.exists(temp_dir):
                import shutil
                shutil.rmtree(temp_dir)
        except Exception as e:
            logger.warning(f"Failed to cleanup temp files: {e}")

    async def download(self, url: str, output_path: str) -> bool:
        try:
            logger.info(f"Starting M3U8 download: {url}")

            playlist_content = await self._fetch_playlist(url)
            parsed = self._parse_m3u8(playlist_content, url)

            if parsed['is_master']:
                logger.info("Master playlist detected, selecting best variant")
                variant_url = await self._select_best_variant(parsed['variants'])
                playlist_content = await self._fetch_playlist(variant_url)
                parsed = self._parse_m3u8(playlist_content, variant_url)
                url = variant_url

            if not parsed['segments']:
                raise DownloadError("No segments found in playlist")

            temp_dir = os.path.join(
                Path(output_path).parent,
                f".temp_{Path(output_path).stem}_{int(time.time())}"
            )
            os.makedirs(temp_dir, exist_ok=True)

            keys = {}
            if parsed['is_encrypted']:
                logger.info("Encrypted stream detected, fetching keys...")
                for key_uri, key_info in parsed['keys'].items():
                    keys[key_uri] = await self._fetch_key(key_uri)

            self._state = M3U8State(
                url=url,
                output_path=output_path,
                playlist_url=url,
                total_duration=parsed['total_duration'],
                start_time=time.time(),
                last_update_time=time.time(),
                temp_dir=temp_dir,
                is_encrypted=parsed['is_encrypted'],
                keys=keys,
                status='downloading'
            )

            for i, seg in enumerate(parsed['segments']):
                self._state.segments.append(SegmentInfo(
                    index=i,
                    url=seg['url'],
                    duration=seg['duration'],
                    key=seg.get('key_uri'),
                    iv=seg.get('iv'),
                ))

            logger.info(f"Total segments: {len(self._state.segments)}, duration: {parsed['total_duration']:.1f}s")

            semaphore = asyncio.Semaphore(self.thread_count)

            async def download_with_semaphore(segment: SegmentInfo):
                async with semaphore:
                    for retry in range(self.max_retry):
                        if self._is_cancelled:
                            return False

                        success = await self._download_segment(segment, temp_dir)
                        if success:
                            return True

                        if retry < self.max_retry - 1:
                            await asyncio.sleep(2 ** retry)

                    return False

            tasks = [
                asyncio.create_task(download_with_semaphore(seg))
                for seg in self._state.segments
            ]

            while not all(t.done() for t in tasks):
                self._update_progress()
                await asyncio.sleep(0.5)

                if self._is_cancelled:
                    for t in tasks:
                        t.cancel()
                    break

            results = await asyncio.gather(*tasks, return_exceptions=True)
            failed_segments = [s for s in self._state.segments if not s.downloaded]

            if failed_segments:
                raise DownloadError(f"Failed to download {len(failed_segments)} segments")

            if self._is_cancelled:
                self._state.status = 'cancelled'
                return False

            await self._merge_segments(output_path, temp_dir)
            await self._cleanup_temp_files(temp_dir)

            self._state.status = 'completed'
            self._report_progress({
                'status': 'finished',
                'progress': 100,
                'filename': output_path,
                'total_segments': len(self._state.segments),
                'total_duration': self._state.total_duration,
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
        playlist_content = await self._fetch_playlist(url)
        parsed = self._parse_m3u8(playlist_content, url)

        if parsed['is_master'] and parsed['variants']:
            best = max(parsed['variants'], key=lambda v: v.get('bandwidth', 0))
            return {
                'id': '',
                'title': Path(url).stem,
                'url': url,
                'duration': 0,
                'is_master': True,
                'variants': parsed['variants'],
                'best_variant': best,
            }

        return {
            'id': '',
            'title': Path(url).stem,
            'url': url,
            'duration': parsed['total_duration'],
            'is_master': False,
            'segment_count': len(parsed['segments']),
            'is_encrypted': parsed['is_encrypted'],
        }
