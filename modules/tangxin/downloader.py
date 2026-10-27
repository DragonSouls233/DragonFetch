# -*- coding: utf-8 -*-
"""糖心下载器。

实测确认：
- m3u8 为单级媒体播放列表（无 master/variant）。
- #EXT-X-KEY:METHOD=AES-128,URI="enc.key",IV=0x....  （key URI 相对，IV 显式且全片共用）
- 分片为 seg0.ts / seg1.ts ...（相对路径）。
- enc.key 16 字节；CDN 仅需 Referer 头（否则 403），无鉴权。

两种模式：
- "python"（默认）：requests 经 socks5h 代理抓取 + cryptography 解密 + ffmpeg 本地转封装。
  全环境可用（桌面/服务器都行），因为代理走得通。
- "ffmpeg"：直接让 ffmpeg 拉 m3u8/key/分片并原生解密（需 CDN 直连，服务器更快）。

下载流程（与主平台一致，见 transfer.py）：
    先落**本地临时目录** temp/downloads → 成功后搬运到下载目录 → 搬运成功才写 NFO。
若主下载器失败且 yt-dlp 可用，自动降级到 yt-dlp 兜底（代理同样透传）。
"""
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import config
from . import db
from . import existing
from . import logger
from . import transfer
from . import ytdlp_engine
from . import nfo as nfo_mod
from .models import Video
from .httpclient import HttpClient
from .utils import run_ffmpeg, sanitize_filename, DownloadCancelled


def _emit_progress(progress, cur, tot, phase, msg, **kw):
    """统一回传进度；兼容只接受 4 个位置参数的旧回调。

    kw 可携带：speed(字节/秒)、bytes_done、bytes_total、eta(秒)。
    """
    if not progress:
        return
    try:
        progress(cur, tot, phase, msg, **kw)
    except TypeError:
        progress(cur, tot, phase, msg)


# ---- DB 记录（best-effort，任何异常都不影响下载本体）----
def _db_start(vid, actor, title, fp):
    try:
        db.record_start(vid, actor, title, fp)
    except Exception:
        pass


def _db_done(vid, fp, poster, nfo_path, size, dur):
    try:
        db.record_done(vid, fp, poster, nfo_path, size, dur)
    except Exception:
        pass


def _db_fail(vid, err):
    try:
        db.record_failed(vid, str(err))
    except Exception:
        pass


def _db_skip(vid):
    try:
        db.record_skipped(vid)
    except Exception:
        pass


def _nfo_path_of(mp4_path):
    return os.path.splitext(mp4_path)[0] + ".nfo"


class TangXinDownloader:
    def __init__(self, mode: str = None, proxy: str = None, stop_event=None):
        self.mode = (mode or config.DOWNLOAD_MODE).lower()
        if self.mode not in ("python", "ffmpeg"):
            self.mode = "python"
        self.client = HttpClient()
        # 允许按任务固定一个节点专属入口（避免负载均衡逐连接换出口）
        if proxy:
            self.client.set_proxy(proxy)
        # 用户中止信号（线程安全的 Event）。None = 不可中止。
        self.stop_event = stop_event

    # ---------- 解析 m3u8 ----------
    @staticmethod
    def _parse_playlist(text: str, base_url: str):
        """返回 (segments, key_bytes_or_None)。
        segments: [(seg_url, key_bytes_or_None, iv_bytes_or_None), ...]
        """
        lines = text.splitlines()
        segments = []
        cur_key = None   # (key_bytes, iv_bytes)
        key_uri_base = base_url
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-KEY"):
                method = None
                uri = None
                iv = None
                m = re.search(r'METHOD=([^,]+)', line)
                if m:
                    method = m.group(1).strip()
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    uri = m.group(1)
                m = re.search(r'IV=0x([0-9A-Fa-f]+)', line)
                if m:
                    iv = bytes.fromhex(m.group(1))
                if method == "NONE" or not uri:
                    cur_key = None
                    continue
                # key 相对 m3u8 所在目录
                key_url = urljoin(key_uri_base, uri)
                key_bytes = None
                iv_bytes = iv
                if key_bytes is None:
                    # 由调用方填充（需要会话）；这里先记录 url
                    cur_key = (key_url, iv_bytes)
                else:
                    cur_key = (key_bytes, iv_bytes)
            elif line.startswith("#"):
                continue
            else:
                # 分片 URL（可能相对）
                seg_url = urljoin(base_url, line)
                segments.append((seg_url, cur_key))
        return segments

    # ---------- 清晰度选择（最高清优先） ----------
    @staticmethod
    def _parse_master_variants(text: str, base_url: str) -> list:
        """从 master 播放列表解析全部变体。

        返回 [(bandwidth, width, height, url), ...]；非 master 时返回空列表。
        """
        if "#EXT-X-STREAM-INF" not in text:
            return []
        lines = text.splitlines()
        variants = []
        for i, ln in enumerate(lines):
            ln = ln.strip()
            if not ln.startswith("#EXT-X-STREAM-INF"):
                continue
            bw = 0
            w = h = 0
            m = re.search(r"BANDWIDTH=(\d+)", ln)
            if m:
                bw = int(m.group(1))
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", ln)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            # STREAM-INF 之后紧跟的非注释行即变体地址
            url = None
            for j in range(i + 1, min(i + 4, len(lines))):
                s = lines[j].strip()
                if s and not s.startswith("#"):
                    url = urljoin(base_url, s)
                    break
            if url:
                variants.append((bw, w, h, url))
        return variants

    @staticmethod
    def _pick_best(variants: list):
        """挑最高清：先比像素数(宽*高)，再比带宽。"""
        return max(variants, key=lambda v: (v[1] * v[2], v[0]))

    def _resolve_best_playlist(self, video: Video):
        """返回 (最终媒体播放列表 url, 文本)。

        若为 master 列表，自动挑选**最高清**变体（避免"取第一个"选到低码率）。
        站点当前只有单级 index.m3u8（实测 720p），此时原样返回。
        """
        url = video.m3u8_url
        text = self.client.get_text(url, is_cdn=True)
        variants = self._parse_master_variants(text, url)
        if variants:
            bw, w, h, vurl = self._pick_best(variants)
            logger.get_logger().info(
                "发现 %d 个清晰度变体，选择最高清：%sx%s (bw=%s) -> %s",
                len(variants), w, h, bw, vurl)
            url = vurl
            text = self.client.get_text(url, is_cdn=True)
        else:
            logger.get_logger().info("播放列表为单级媒体列表（无多码率变体），直接使用：%s", url)
        return url, text

    def _fetch_key(self, key_url: str) -> bytes:
        return self.client.get_bytes(key_url, is_cdn=True)

    # ---------- 解密单分片 ----------
    @staticmethod
    def _decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
        if key is None:
            return data
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        dec = cipher.decryptor()
        return dec.update(data) + dec.finalize()

    # ---------- python 模式：下载分片 + 合并 ----------
    def _download_python(self, video: Video, out_mp4: str,
                         progress=None, max_segments: int = 0, stop_event=None):
        """下载全部分片（并发）→ 解密 → concat 转封装到 out_mp4。

        全程周期性检查 ``stop_event``；用户点"停止"后会在当前分片收尾后立即中止，
        不会把整部片子下完（之前的中断缺陷正是出在这里）。
        """
        tmpdir = tempfile.mkdtemp(prefix="tx_%s_" % video.vid)
        try:
            # 解析播放列表：若为 master，自动选**最高清**变体
            media_url, m3u8_text = self._resolve_best_playlist(video)
            parsed = self._parse_playlist(m3u8_text, media_url)

            # 解析 key（把 key url 换成 bytes；同一 key 只抓一次）
            key_cache = {}
            segments = []
            for seg_url, key_info in parsed:
                if key_info is None:
                    segments.append((seg_url, None, None))
                else:
                    key_url, iv = key_info
                    if isinstance(key_url, bytes):
                        key_bytes = key_url
                    else:
                        if key_url not in key_cache:
                            key_cache[key_url] = self._fetch_key(key_url)
                        key_bytes = key_cache[key_url]
                    if iv is None:
                        iv = b"\x00" * 16
                    segments.append((seg_url, key_bytes, iv))

            total = len(segments)
            if max_segments and max_segments < total:
                segments = segments[:max_segments]
                total = len(segments)
            if progress:
                _emit_progress(progress, 0, total, "download",
                               "开始下载分片：%s（%d）" % (
                                   sanitize_filename(video.title), total))

            # 并发下载+解密，按序落盘
            seg_files = {}
            errors = {}

            def worker(idx_seg):
                idx, (seg_url, key, iv) = idx_seg
                # 已开始下载的分片仍跑完，但还没排上的立即让出，避免空转
                if stop_event and stop_event.is_set():
                    return idx, None, "cancelled"
                try:
                    data = self.client.get_bytes(seg_url, is_cdn=True)
                    dec = self._decrypt(data, key, iv)
                    return idx, dec, None
                except Exception as e:
                    return idx, None, str(e)

            downloaded = 0
            last_b, last_t = 0.0, time.time()
            # with 管理：退出时 wait=True，最多等当前在途的 ≤workers 个分片收尾
            # （几秒，而不是等整部片子下完）——这正是"停止"能及时生效的关键。
            with ThreadPoolExecutor(max_workers=config.SEGMENT_WORKERS) as ex:
                futures = [ex.submit(worker, (i, s)) for i, s in enumerate(segments)]
                done = 0
                for fut in as_completed(futures):
                    idx, dec, err = fut.result()
                    if err == "cancelled":
                        errors[idx] = err
                    elif err:
                        errors[idx] = err
                    else:
                        p = os.path.join(tmpdir, "seg_%05d.ts" % idx)
                        with open(p, "wb") as f:
                            f.write(dec)
                        seg_files[idx] = p
                        downloaded += len(dec)
                    done += 1
                    now = time.time()
                    # 速度 / ETA（滑动窗口，≥0.4s 或每段都刷新，避免抖动）
                    speed = ((downloaded - last_b) / (now - last_t)
                             if (now - last_t) > 0 else 0.0)
                    if now - last_t >= 0.4 or done == total:
                        last_b, last_t = downloaded, now
                        frac = done / total if total else 0
                        bytes_total = (downloaded / frac) if frac > 0.001 else 0
                        eta = ((bytes_total - downloaded) / speed
                               if speed > 0 else None)
                        _emit_progress(progress, done, total, "download",
                                       "分片 %d/%d" % (done, total),
                                       speed=speed, bytes_done=downloaded,
                                       bytes_total=bytes_total, eta=eta)
                    # 用户中止：取消尚未开始的线程，跳出循环（在途分片收尾后即停）
                    if stop_event and stop_event.is_set():
                        try:
                            ex.shutdown(cancel_futures=True)
                        except Exception:
                            pass
                        raise DownloadCancelled("用户中止下载")

            if errors:
                # 部分失败：若失败过多则放弃；"cancelled" 不算致命错误
                real_errs = {k: v for k, v in errors.items() if v != "cancelled"}
                failed = len(real_errs)
                if failed > max(5, total // 20):
                    raise RuntimeError("分片失败过多：%d/%d，例如 %s" %
                                       (failed, total, list(real_errs.values())[0]))
                if "cancelled" in errors.values():
                    raise DownloadCancelled("用户中止下载")
                # 否则用空 TS 占位，避免整体失败（极少情况）
                for idx in errors:
                    seg_files[idx] = None

            # 顺序拼接（concat demuxer）
            list_path = os.path.join(tmpdir, "list.txt")
            with open(list_path, "w", encoding="utf-8") as lf:
                for i in range(total):
                    p = seg_files.get(i)
                    if p and os.path.isfile(p):
                        lf.write("file '%s'\n" % os.path.abspath(p).replace("\\", "/"))
            if stop_event and stop_event.is_set():
                raise DownloadCancelled("用户中止下载")
            if progress:
                _emit_progress(progress, total, total, "merge", "合并为 mp4 …")
            r = run_ffmpeg([
                "-f", "concat", "-safe", "0", "-i", list_path,
                "-c", "copy", "-y", out_mp4,
            ], timeout=1800)
            if r.returncode != 0 or not os.path.isfile(out_mp4):
                raise RuntimeError("ffmpeg 合并失败: " + (r.stderr.decode("utf-8", "ignore")[:500]))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ---------- 主入口 ----------
    def download_video(self, video: Video, output_dir: str,
                      progress=None, get_cover: bool = True,
                      max_segments: int = 0, stop_event=None) -> dict:
        """下载单个视频（先本地临时目录，成功后搬运到 output_dir）。

        ## 目录结构（用户定）
            {下载目录}/{模特文件夹}/{标题文件夹}/{标题}.mp4
                                              {标题}.nfo
                                              {标题}-poster.jpg
        ``output_dir`` 传入的是**模特目录**（``{下载目录}/{模特}``），
        标题子目录由本函数创建 —— 这样调用方（GUI / CLI）不用重复拼路径，
        而跨目录去重所需的「下载根」也正好是 ``dirname(output_dir)``。

        返回 ``{ok, skipped, path, poster}``。

        ``stop_event``：传入 threading.Event，下载过程中会周期性检查，
        置位即抛出 DownloadCancelled（用于"停止"按钮即时中止）。
        """
        stop = stop_event or self.stop_event
        safe_title = sanitize_filename(video.title)
        # 标题子目录：{模特目录}/{标题}
        title_dir = os.path.normpath(os.path.join(output_dir, safe_title))
        target_mp4 = os.path.normpath(os.path.join(title_dir, safe_title + ".mp4"))
        _db_start(video.vid, video.actor, video.title, target_mp4)

        # 任何阶段开始前先响应一次停止信号（点了停止就不再往下走）
        if stop and stop.is_set():
            raise DownloadCancelled("用户中止下载")

        # 幂等 ①：目标文件已存在 → 跳过（不覆盖）
        if os.path.isfile(target_mp4) and os.path.getsize(target_mp4) > 0:
            _db_skip(video.vid)
            if progress:
                progress(0, 0, "skip", "已存在: %s" % safe_title)
            return {"ok": True, "skipped": True, "path": target_mp4}

        # 幂等 ②：**其它已知目录**下已经下载过（本机先下、后续转移服务器 /
        #   NAS 两边盘符不同的场景：目录结构一致、内容一致，只是根路径不同）
        #   → 同样跳过，避免重复下载。
        # 把当前输出根也作为附加查找根：服务器上 NAS 常映射成本机盘符，
        # 这条路不需要扫描、毫秒级命中。
        try:
            hit = existing.get_index().lookup(
                video, extra_dirs=[os.path.dirname(output_dir)])
        except Exception as e:
            hit = None
            logger.get_logger().warning("查询已下载索引失败（按未命中处理）：%s", e)
        if hit and os.path.normpath(hit) != os.path.normpath(target_mp4):
            _db_skip(video.vid)
            if progress:
                progress(0, 0, "skip", "其它目录已存在: %s" % hit)
            logger.get_logger().info("跳过已下载：%s（发现于 %s）", video.title, hit)
            if config.COPY_EXISTING:
                os.makedirs(title_dir, exist_ok=True)
                mv = transfer.move_to_target(hit, target_mp4)
                if mv.get("success"):
                    logger.get_logger().info("已从旧位置搬移到：%s", mv.get("target"))
                    if progress:
                        progress(0, 0, "skip", "已从旧位置搬移: %s" % os.path.basename(hit))
                    return {"ok": True, "skipped": True,
                            "path": mv.get("target") or target_mp4,
                            "moved_from": hit}
            return {"ok": True, "skipped": True, "path": hit,
                    "existing_elsewhere": True}

        os.makedirs(title_dir, exist_ok=True)
        # ① 第一站：本地临时文件（下载目录可能位于 NAS，直接写会慢且易中断）
        temp_mp4 = transfer.temp_file_path(video.vid, "mp4")
        temp_poster = transfer.temp_file_path(video.vid + "_poster", "jpg")
        transfer.safe_remove(temp_mp4)
        transfer.safe_remove(temp_poster)

        try:
            if stop and stop.is_set():
                raise DownloadCancelled("用户中止下载")
            if self.mode == "ffmpeg" and not config.CDN_USE_PROXY:
                self._download_ffmpeg_native(video, temp_mp4, progress, stop_event=stop)
            else:
                try:
                    self._download_python(video, temp_mp4, progress, max_segments,
                                          stop_event=stop)
                except DownloadCancelled:
                    raise
                except Exception as primary_err:
                    # ② 兜底：自制下载器失败 → yt-dlp（代理同样透传）
                    if not ytdlp_engine.available():
                        raise
                    logger.get_logger().warning(
                        "主下载器失败，降级 yt-dlp 兜底：%s", primary_err)
                    transfer.safe_remove(temp_mp4)
                    fb = ytdlp_engine.download(
                        url=video.page_url,
                        output_file=temp_mp4,
                        proxy=self.client.proxy_url(),
                        progress=progress,
                        stop_event=stop,
                    )
                    if not fb.get("ok"):
                        raise RuntimeError(
                            "主下载器失败（%s），yt-dlp 兜底也失败（%s）"
                            % (primary_err, fb.get("error")))

            # ③ 校验临时文件
            if not os.path.isfile(temp_mp4) or os.path.getsize(temp_mp4) == 0:
                raise RuntimeError("下载结果为空或不存在：%s" % temp_mp4)

            # ④ 搬运到下载目录（同盘 rename / 跨盘 copy+delete）
            if progress:
                progress(1, 1, "move", "搬运到下载目录…")
            mv = transfer.move_to_target(temp_mp4, target_mp4)
            if not mv.get("success"):
                raise RuntimeError("搬运到下载目录失败：%s" % mv.get("error"))
            final_mp4 = mv.get("target") or target_mp4
            if mv.get("degraded"):
                logger.get_logger().warning("目标文件名因兼容性被降级：%s",
                                            os.path.basename(final_mp4))

            # ⑤ 封面（先落临时目录，再搬运；与 mp4 同在标题目录）
            poster = None
            if get_cover and video.cover_url:
                try:
                    cov = self.client.get_bytes(video.cover_url, is_cdn=True)
                    with open(temp_poster, "wb") as f:
                        f.write(cov)
                    t_poster = os.path.normpath(
                        os.path.join(title_dir, safe_title + "-poster.jpg"))
                    pv = transfer.move_to_target(temp_poster, t_poster)
                    if pv.get("success"):
                        poster = pv.get("target") or t_poster
                except Exception as e:
                    if progress:
                        progress(0, 0, "warn", "封面下载失败：%s" % e)

            # ⑥ 搬运成功才写 NFO
            nfo_path = nfo_mod.write_video_nfo(video, final_mp4, poster)

            _db_done(video.vid, final_mp4, poster, nfo_path,
                     os.path.getsize(final_mp4), video.runtime_minutes * 60)
            # ⑦ ⭐ 立即写进「已下载索引」：统计/去重/下载记录立刻就是对的，
            #    不用等下一次全盘扫描（实测扫描前「已下 N」会偏小）。
            try:
                existing.record_downloaded(
                    video, final_mp4,
                    size_bytes=os.path.getsize(final_mp4),
                    nfo_path=nfo_path, poster_path=poster or "",
                    duration_sec=video.runtime_minutes * 60)
            except Exception as e:
                logger.get_logger().warning("写入已下载索引失败（不影响下载）：%s", e)
            if progress:
                progress(1, 1, "done", "完成：%s" % os.path.basename(final_mp4))
            return {"ok": True, "skipped": False, "path": final_mp4, "poster": poster}
        except Exception as e:
            _db_fail(video.vid, e)
            raise
        finally:
            # 无论成败都不留临时文件
            transfer.safe_remove(temp_mp4)
            transfer.safe_remove(temp_poster)

    def _download_ffmpeg_native(self, video: Video, out_mp4: str, progress=None,
                                 stop_event=None):
        """ffmpeg 原生下载（CDN 直连时更快），直接输出到 out_mp4。

        用 Popen 启动以便：① 用户中止时立即 kill 进程；② 轮询输出文件大小
        推算下载速度与已下载字节（ffmpeg 原生不回传结构化进度）。
        """
        # 同样先解析出**最高清**的媒体播放列表再交给 ffmpeg
        media_url, _ = self._resolve_best_playlist(video)
        if stop_event and stop_event.is_set():
            raise DownloadCancelled("用户中止下载")
        if progress:
            _emit_progress(progress, 0, 0, "download",
                           "ffmpeg 原生下载：%s" % video.title)
        cmd = [
            config.FFMPEG,
            "-headers", "Referer: %s\r\nUser-Agent: %s\r\n" % (config.REFERER, config.UA),
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", media_url,
            "-c", "copy", "-y", out_mp4,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        t0 = time.time()
        last_b, last_t = 0.0, t0
        try:
            while True:
                if stop_event and stop_event.is_set():
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise DownloadCancelled("用户中止下载")
                if proc.poll() is not None:
                    break
                try:
                    sz = os.path.getsize(out_mp4) if os.path.isfile(out_mp4) else 0
                except OSError:
                    sz = 0
                now = time.time()
                speed = ((sz - last_b) / (now - last_t)
                         if (now - last_t) > 0 else 0.0)
                if now - last_t >= 0.5 or sz > last_b:
                    last_b, last_t = sz, now
                    _emit_progress(progress, 0, 0, "download",
                                   "ffmpeg 下载中",
                                   speed=speed, bytes_done=sz, bytes_total=0, eta=None)
                time.sleep(0.3)
        finally:
            # 确保进程退出，回收句柄
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        out, err = proc.communicate()
        if proc.returncode != 0 or not os.path.isfile(out_mp4):
            raise RuntimeError("ffmpeg 原生下载失败: " + (err.decode("utf-8", "ignore")[:500]))

    def close(self):
        self.client.close()
