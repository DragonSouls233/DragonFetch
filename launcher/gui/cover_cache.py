# -*- coding: utf-8 -*-
"""列表封面加载：内存 LRU + 磁盘缓存 + 可选解密，解码/缩放全在后台线程。

为什么单独一个模块：黄果的封面是**加密图**（``media_crypto.decrypt_cover``），
不能像普通图片那样直接 ``QPixmap(url)`` 或交给 QNetworkAccessManager，
否则拿到的是一堆解不开的字节 —— 表现就是「封面图一个都没有」。
另外列表动辄几十上百张封面，必须两级缓存 + 后台解码，否则界面会卡住。

用法::

    cache = CoverCache(Path("DATA/huangguo/covers/huangguoai"),
                       decode=ns.media_crypto.decrypt_cover,
                       fetch=lambda u: ns.http_client.fetch_bytes(u, timeout=15))
    cache.request(did, cover_url, self._on_cover)   # 回调在主线程

三级顺序：内存 LRU（零 IO）→ 磁盘（<site>/<did>.jpg，已解密原图）→ 网络。
同一 key 的并发请求会合并成一次下载，其余请求排队等同一份结果。
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, Qt, pyqtSignal
from PyQt6.QtGui import QImage

__all__ = ["CoverCache"]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _safe_name(key: object) -> str:
    """把 did/slug 变成安全文件名（长度也砍一刀，避免超出路径限制）。"""
    s = re.sub(r"[^\w\-.]", "_", str(key)).strip("_")
    return (s or "x")[:80]


def _default_fetch(url: str, timeout: float = 15.0) -> Optional[bytes]:
    try:
        import requests

        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": _UA, "Referer": url})
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:                       # noqa: BLE001 - 单张失败不影响列表
        pass
    return None


class _Signals(QObject):
    done = pyqtSignal(object, object)       # (key, QImage | None)


class _CoverTask(QRunnable):
    """单个封面：取字节 → 解密 → 解码缩放。全部在工作线程完成。"""

    def __init__(self, owner: "CoverCache", key: object, url: str):
        super().__init__()
        self.owner = owner
        self.key = key
        self.url = url
        self.signals = _Signals()

    def run(self) -> None:                  # noqa: D401
        img = None
        try:
            img = self.owner._load_sync(self.key, self.url)
        except Exception:                   # noqa: BLE001
            img = None
        finally:
            try:
                self.signals.done.emit(self.key, img)
            except RuntimeError:            # 界面已销毁
                pass


class CoverCache(QObject):
    """一个下载站点一个实例（缓存目录里按站点分目录，天然隔离）。"""

    def __init__(self, cache_dir: Path, *, decode: Optional[Callable[[bytes], Optional[bytes]]] = None,
                 fetch: Optional[Callable[[str], Optional[bytes]]] = None,
                 size: tuple[int, int] = (160, 224), max_mem: int = 1200,
                 max_disk: int = 6000, timeout: float = 15.0, parent=None):
        super().__init__(parent)
        self.dir = Path(cache_dir)
        self.size = (int(size[0]), int(size[1]))
        self.max_mem = max(32, int(max_mem))
        # 磁盘缓存上限：之前只有内存 LRU，磁盘侧只增不减，长期运行会无限膨胀。
        # 0 表示不限制（保留旧行为）；>0 时超过则按文件 mtime 淘汰最旧的。
        self.max_disk = max(0, int(max_disk))
        self.timeout = timeout
        self._decode = decode
        self._fetch = fetch or _default_fetch
        self._disk_pruned_at = 0.0

        self._mem: "OrderedDict[object, QImage]" = OrderedDict()
        self._lock = threading.Lock()
        self._pending: dict[object, list[Callable]] = {}

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(6)

        self.hits_mem = 0
        self.hits_disk = 0
        self.misses = 0

    # ------------------------------------------------------------------ 对外

    def request(self, key: object, url: str, on_loaded: Callable[[object, QImage], None]) -> None:
        """请求一张封面；``on_loaded(key, QImage)`` 在主线程回调（失败传 None）。

        key 用 did 这类跨次稳定、且站点内唯一的值（磁盘缓存靠它命名）。
        """
        hit = self._mem_get(key)
        if hit is not None:
            self.hits_mem += 1
            on_loaded(key, hit)
            return
        if not (url or "").strip():
            on_loaded(key, None)
            return

        with self._lock:
            waiters = self._pending.get(key)
            if waiters is not None:          # 已在下载，挂队等同一份结果
                waiters.append(on_loaded)
                return
            self._pending[key] = [on_loaded]

        task = _CoverTask(self, key, url)
        task.signals.done.connect(self._on_task_done, Qt.ConnectionType.QueuedConnection)
        self._pool.start(task)

    def has_memory(self, key: object) -> bool:
        return self._mem_get(key) is not None

    def warm(self, pairs, workers: int = 6, on_progress=None) -> dict:
        """批量把封面**提前下载**到磁盘（只写盘，不解码、不占内存）。

        用户诉求："封面可以提前下载到数据库同目录下面进行保存"。
        列表里几百张封面如果都等滚动到眼前才抓，翻页就是一片灰块；
        这里一次并发抓完写进缓存目录，下次打开全部秒开（走磁盘命中）。

        pairs: ``[(key, url), ...]``；已在磁盘上的直接跳过。
        返回 ``{"ok": n, "skip": m, "fail": k}``。
        """
        from concurrent.futures import ThreadPoolExecutor

        pairs = [(k, (u or "").strip()) for k, u in (pairs or []) if (u or "").strip()]
        total = len(pairs)
        done = [0]
        stat = {"ok": 0, "skip": 0, "fail": 0}
        if not total:
            return stat

        def _one(k: object, url: str) -> None:
            try:
                if self._read_disk(k) is not None:
                    stat["skip"] += 1
                else:
                    raw = self._fetch(url)
                    plain = raw
                    if raw and self._decode is not None:
                        try:
                            got = self._decode(raw)
                        except Exception:               # noqa: BLE001
                            got = None
                        if got:
                            plain = got
                    if plain and _looks_like_image(plain):
                        self._write_disk(k, plain)
                        stat["ok"] += 1
                    else:
                        stat["fail"] += 1
            except Exception:                           # noqa: BLE001
                stat["fail"] += 1
            finally:
                done[0] += 1
                if on_progress is not None:
                    try:
                        on_progress(done[0], total)
                    except Exception:                   # noqa: BLE001
                        pass

        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            list(ex.map(lambda p: _one(p[0], p[1]), pairs))
        return stat

    def clear_memory(self) -> None:
        with self._lock:
            self._mem.clear()

    def stats(self) -> dict:
        with self._lock:
            mem = len(self._mem)
        return {"mem": mem, "dir": str(self.dir),
                "mem_hits": self.hits_mem, "disk_hits": self.hits_disk,
                "misses": self.misses}

    def disk_path(self, key: object) -> Path:
        return self.dir / f"{_safe_name(key)}.jpg"

    # ------------------------------------------------------------ 工作线程侧

    def _load_sync(self, key: object, url: str) -> Optional[QImage]:
        # 1) 磁盘缓存（已是解密后的原图）
        data = self._read_disk(key)
        if data is not None:
            self.hits_disk += 1
            return self._to_image(data)

        # 2) 网络 + 解密
        raw = self._fetch(url) if callable(self._fetch) else _default_fetch(url, self.timeout)
        if not raw:
            self.misses += 1
            return None
        plain = raw
        if self._decode is not None:
            got = None
            try:
                got = self._decode(raw)
            except Exception:               # noqa: BLE001
                got = None
            if got:
                plain = got
            elif not _looks_like_image(raw):
                # 解密失败、原图也不是图片 —— 真拿不到，别把垃圾写进磁盘缓存
                self.misses += 1
                return None
        if not _looks_like_image(plain):
            self.misses += 1
            return None
        self._write_disk(key, plain)
        return self._to_image(plain)

    def _read_disk(self, key: object) -> Optional[bytes]:
        p = self.disk_path(key)
        try:
            if p.is_file() and p.stat().st_size > 0:
                return p.read_bytes()
        except OSError:
            pass
        return None

    def _write_disk(self, key: object, data: bytes) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.disk_path(key).with_suffix(".jpg.tmp")
            tmp.write_bytes(data)
            tmp.replace(self.disk_path(key))
        except OSError:
            pass
        self._maybe_prune_disk()

    def disk_count(self) -> int:
        try:
            return sum(1 for p in self.dir.glob("*.jpg") if p.is_file())
        except OSError:
            return 0

    def _maybe_prune_disk(self) -> None:
        """节流式磁盘清理：最多每 30 秒检查一次，超过 max_disk 则按 mtime 淘汰。"""
        if self.max_disk <= 0:
            return
        import time as _time

        now = _time.time()
        if now - self._disk_pruned_at < 30.0:
            return
        self._disk_pruned_at = now
        try:
            files = [p for p in self.dir.glob("*.jpg") if p.is_file()]
        except OSError:
            return
        if len(files) <= self.max_disk:
            return
        # 最旧的排前面（mtime 升序），删到比上限少 10%
        files.sort(key=lambda p: p.stat().st_mtime)
        keep = max(int(self.max_disk * 0.9), 1)
        for p in files[: len(files) - keep]:
            try:
                p.unlink()
            except OSError:
                pass

    def _to_image(self, data: Optional[bytes]) -> Optional[QImage]:
        if not data:
            return None
        img = QImage()
        if not img.loadFromData(data) or img.isNull():
            return None
        w, h = self.size
        if img.width() > w or img.height() > h:
            img = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        return img

    # -------------------------------------------------------------- 主线程侧

    def _on_task_done(self, key: object, img) -> None:
        with self._lock:
            waiters = self._pending.pop(key, [])
        if img is not None and not img.isNull():
            self._mem_put(key, img)
        for cb in waiters:
            try:
                cb(key, img)
            except RuntimeError:            # 控件已销毁
                pass
            except Exception:               # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 内存 LRU

    def _mem_get(self, key: object) -> Optional[QImage]:
        with self._lock:
            img = self._mem.get(key)
            if img is not None:
                self._mem.move_to_end(key)
            return img

    def _mem_put(self, key: object, img: QImage) -> None:
        with self._lock:
            self._mem[key] = img
            self._mem.move_to_end(key)
            while len(self._mem) > self.max_mem:
                self._mem.popitem(last=False)


def _looks_like_image(data: bytes) -> bool:
    """按文件头判断是不是能解码的图片（JPEG/PNG/GIF/BMP/WEBP）。"""
    if not data or len(data) < 12:
        return False
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:2] == b"BM":
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
