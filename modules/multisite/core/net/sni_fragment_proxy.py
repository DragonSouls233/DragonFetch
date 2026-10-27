# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""SNI 分片本地代理（Lite 模式）

解决什么问题
------------
2026-09-20 雪崩暴露了一个结构性弱点：项目只有**一条**抗封锁路径
——切换 v2ray 节点。于是"连不上"这件事只有一个解法：换节点 → 重启 Xray →
链路中断几秒。切换本身又成了雪崩的放大器。

本模块提供**第二条正交路径**：不换节点，而是在本地把 TLS ClientHello 里的
SNI 域名**切成两段、分两次写入**（中间隔十几毫秒），使单个 TCP 段里不再包含
完整域名，从而绕过基于 SNI 明文匹配的 DPI 阻断。

拓扑（现有 v2ray 逻辑零改动）::

    yt-dlp
      └─► socks5://127.0.0.1:10809   ← 本模块（SNI 分片）
             └─► socks5://127.0.0.1:10808   ← 现有 Xray，原样保留
                    └─► 目标站

设计参考
--------
思路来自 EchterAlsFake/Porn_Fetch 的 Lite 模式（``tls_client_hello.py`` 解析
ClientHello 定位 SNI 偏移 + ``sni_fragment_proxy_lite.py`` 做 loopback 分片代理）。
其 Strict 模式走 PyDivert/WinDivert/eBPF 驱动，需要管理员/root 权限且
Windows ARM64 不支持，本模块**不采用**，只做用户态 Lite 模式。

许可说明
--------
本项目自 2026-09-20 起改用 **GPL-3.0-or-later**（原因见 README「为什么是
GPL-3.0」），与 Porn_Fetch 的 GPL-3.0-or-later 许可**互相兼容**，
授权冲突已不存在。本文件仍为**独立编写**：未复制其任何代码，
仅借鉴"定位 SNI 偏移后分两次写入"的设计思想；
TLS 解析、SOCKS5 服务端、中继与限流逻辑均自行实现。

有效性边界（必须知道）
----------------------
- 只对**明文 SNI** 的 DPI 有效。对 TLS 1.3 + ECH（加密 ClientHello）
  或基于 IP/流量特征的封锁无效。
- 分片代理只能作用于 **TCP 首包的写入方式**，不改变加密内容，
  因此不会破坏 TLS 校验（服务端按流重组，与分段无关）。
- 需要真实环境验证：本模块在无 DPI 环境下**无法自证有效**，
  只能验证"ClientHello 解析正确、确实分成两段发出、数据完整无损"。
  穿透效果必须在实际被阻站点上验证。
"""

import argparse
import asyncio
import ipaddress
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = [
    'SniRange',
    'find_sni_range',
    'tls_record_total_len',
    'FragmentConfig',
    'SniFragmentProxy',
    'start_sni_fragment_proxy',
]

# TLS 常量
_TLS_CONTENT_HANDSHAKE = 0x16
_TLS_HANDSHAKE_CLIENT_HELLO = 0x01
_EXT_SERVER_NAME = 0x0000
_SNI_NAME_TYPE_HOST = 0x00

# SOCKS5 常量
_SOCKS_VER = 0x05
_SOCKS_AUTH_NONE = 0x00
_SOCKS_AUTH_USERPASS = 0x02
_SOCKS_AUTH_NO_ACCEPTABLE = 0xFF
_SOCKS_CMD_CONNECT = 0x01
_ATYP_IPV4 = 0x01
_ATYP_DOMAIN = 0x03
_ATYP_IPV6 = 0x04


class SniRange(tuple):
    """SNI 主机名在缓冲区中的 ``(start, end)`` 字节区间。"""

    @property
    def start(self) -> int:
        return self[0]

    @property
    def end(self) -> int:
        return self[1]

    @property
    def length(self) -> int:
        return self[1] - self[0]

    def midpoint(self) -> int:
        """区间中点——分片点选这里，使两段都不含完整域名。

        取中点而不是首字节：即便 DPI 只检查第一个网络包，第一段也只含域名的
        前半截（例如 ``www.por``），匹配不到完整域名。
        """
        return (self[0] + self[1]) // 2

    def __repr__(self) -> str:  # pragma: no cover
        return f'SniRange({self[0]}, {self[1]})'


# ---------------------------------------------------------------------------
# TLS ClientHello 解析
# ---------------------------------------------------------------------------

def tls_record_total_len(buf: bytes) -> Optional[int]:
    """返回首个 TLS record 声明的总长度（含 5 字节头）。

    用于判断"ClientHello 是否还没收全"：单个 record 最大 16KB + 5 字节头，
    实际 ClientHello 很少超过 4KB，但 SNI 较长的场景（多域名、大扩展）
    确实可能跨 record。
    """
    if len(buf) < 5:
        return None
    if buf[0] != _TLS_CONTENT_HANDSHAKE:
        return None
    length = struct.unpack('>H', buf[3:5])[0]
    return 5 + length


def find_sni_range(buf: bytes, max_scan: int = 65536) -> Optional[SniRange]:
    """在 TCP 流起始缓冲区中定位 SNI 主机名的字节区间。

    Args:
        buf: 从连接起点开始的字节流（可以只收到一部分，本函数会自行判断
            是否解析完整并返回 ``None``）
        max_scan: 最多解析的字节数，防御性上限，避免恶意超长 ClientHello
            导致解析开销失控

    Returns:
        :class:`SniRange` 或 ``None``（不是 TLS / 未收全 / 无 SNI 扩展）
    """
    data = buf[:max_scan]

    # ---- TLS record 层 ----
    if len(data) < 5 or data[0] != _TLS_CONTENT_HANDSHAKE:
        return None
    rec_len = struct.unpack('>H', data[3:5])[0]
    rec_end = 5 + rec_len
    # record 未收全
    if rec_len <= 0 or len(data) < rec_end:
        return None

    hs = data[5:rec_end]

    # ---- Handshake 层：ClientHello ----
    if len(hs) < 4 or hs[0] != _TLS_HANDSHAKE_CLIENT_HELLO:
        return None
    hs_len = int.from_bytes(hs[1:4], 'big')
    if hs_len <= 0 or len(hs) < 4 + hs_len:
        return None

    # 这里开始用**绝对偏移**（相对原始 buf）来定位，便于直接拿去切片
    base = 5  # ClientHello 体在 buf 中的起始
    p = base + 4  # 跳过 handshake 头

    def need(n: int) -> bool:
        return p + n <= base + 4 + hs_len and p + n <= len(data)

    # client_version(2) + random(32)
    if not need(2 + 32):
        return None
    p += 2 + 32

    # session_id
    if not need(1):
        return None
    sid_len = data[p]
    p += 1
    if not need(sid_len):
        return None
    p += sid_len

    # cipher_suites
    if not need(2):
        return None
    cs_len = struct.unpack('>H', data[p:p + 2])[0]
    p += 2
    if not need(cs_len):
        return None
    p += cs_len

    # compression_methods
    if not need(1):
        return None
    comp_len = data[p]
    p += 1
    if not need(comp_len):
        return None
    p += comp_len

    # ---- extensions（可选，TLS 1.0 之前没有）----
    if not need(2):
        return None
    ext_total = struct.unpack('>H', data[p:p + 2])[0]
    p += 2
    ext_end = p + ext_total
    if not need(ext_total):
        return None

    while p + 4 <= ext_end:
        ext_type = struct.unpack('>H', data[p:p + 2])[0]
        ext_len = struct.unpack('>H', data[p + 2:p + 4])[0]
        p += 4
        if p + ext_len > ext_end:
            return None

        if ext_type == _EXT_SERVER_NAME:
            # server_name_list: list_len(2) + [ type(1) + name_len(2) + name ]
            q = p
            if q + 2 > p + ext_len:
                return None
            list_len = struct.unpack('>H', data[q:q + 2])[0]
            q += 2
            list_end = min(q + list_len, p + ext_len)
            while q + 3 <= list_end:
                name_type = data[q]
                name_len = struct.unpack('>H', data[q + 1:q + 3])[0]
                q += 3
                if q + name_len > list_end:
                    return None
                if name_type == _SNI_NAME_TYPE_HOST and name_len > 0:
                    return SniRange((q, q + name_len))
                q += name_len
            return None

        p += ext_len

    return None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class FragmentConfig:
    """分片代理配置。"""

    #: 监听地址
    listen_host: str = '127.0.0.1'
    listen_port: int = 10809
    #: 上游代理，形如 ``socks5://127.0.0.1:10808``；None 表示直连目标
    upstream: Optional[str] = None
    #: 两段写入之间的间隔（秒）。经验值 10~20ms：太小会被网卡合并，
    #: 太大则明显拖慢握手。
    fragment_delay: float = 0.012
    #: 找不到 SNI 时的兜底分片点（字节）。设为 None 则完全不分片。
    fallback_split: Optional[int] = 2
    #: 解析首批数据的最长等待（秒）
    handshake_timeout: float = 5.0
    #: 单次读取块大小
    chunk_size: int = 32768
    #: ClientHello 解析上限（防御性）
    max_client_hello: int = 65536
    #: 建立上游连接的超时
    connect_timeout: float = 10.0
    #: 日志中打印真实域名（排查用）；False 时只打印长度
    log_hostname: bool = False


# ---------------------------------------------------------------------------
# 地址编码
# ---------------------------------------------------------------------------

def _encode_socks_addr(host: str, port: int) -> bytes:
    """把目标地址编码成 SOCKS5 ATYP + ADDR + PORT 形式。"""
    try:
        ip = ipaddress.ip_address(host)
        if ip.version == 4:
            return bytes([_ATYP_IPV4]) + ip.packed + struct.pack('>H', port)
        return bytes([_ATYP_IPV6]) + ip.packed + struct.pack('>H', port)
    except ValueError:
        hb = host.encode('idna') if host.isascii() else host.encode('utf-8')
        if len(hb) > 255:
            raise ValueError(f'域名过长: {host}')
        return bytes([_ATYP_DOMAIN, len(hb)]) + hb + struct.pack('>H', port)


async def _open_direct(host: str, port: int, cfg: FragmentConfig):
    return await asyncio.wait_for(asyncio.open_connection(host, port),
                                  timeout=cfg.connect_timeout)


async def _open_via_socks5(host: str, port: int, upstream: str,
                           cfg: FragmentConfig):
    """经上游 SOCKS5 建立到 (host, port) 的连接，返回 (reader, writer)。"""
    u = urlparse(upstream if '://' in upstream else 'socks5://' + upstream)
    uh, up = u.hostname, u.port or 1080
    if not uh:
        raise ValueError(f'无法解析上游代理: {upstream}')

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(uh, up), timeout=cfg.connect_timeout)

    try:
        want_userpass = bool(u.username)
        if want_userpass:
            writer.write(bytes([_SOCKS_VER, 2, _SOCKS_AUTH_NONE, _SOCKS_AUTH_USERPASS]))
        else:
            writer.write(bytes([_SOCKS_VER, 1, _SOCKS_AUTH_NONE]))
        await writer.drain()

        ver, method = await asyncio.wait_for(reader.readexactly(2), cfg.connect_timeout)
        if ver != _SOCKS_VER:
            raise ConnectionError(f'上游代理协议版本异常: {ver}')
        if method == _SOCKS_AUTH_USERPASS:
            ub = (u.username or '').encode()
            pb = (u.password or '').encode()
            writer.write(bytes([0x01, len(ub)]) + ub + bytes([len(pb)]) + pb)
            await writer.drain()
            _, status = await asyncio.wait_for(reader.readexactly(2), cfg.connect_timeout)
            if status != 0:
                raise ConnectionError('上游代理用户名/密码认证失败')
        elif method != _SOCKS_AUTH_NONE:
            raise ConnectionError(f'上游代理不支持的认证方式: {method}')

        writer.write(bytes([_SOCKS_VER, _SOCKS_CMD_CONNECT, 0x00])
                     + _encode_socks_addr(host, port))
        await writer.drain()

        head = await asyncio.wait_for(reader.readexactly(4), cfg.connect_timeout)
        if head[1] != 0x00:
            raise ConnectionError(f'上游代理 CONNECT 失败, REP={head[1]}')
        atyp = head[3]
        if atyp == _ATYP_IPV4:
            await reader.readexactly(4)
        elif atyp == _ATYP_DOMAIN:
            n = (await reader.readexactly(1))[0]
            await reader.readexactly(n)
        elif atyp == _ATYP_IPV6:
            await reader.readexactly(16)
        else:
            raise ConnectionError(f'上游代理返回未知 ATYP: {atyp}')
        await reader.readexactly(2)  # BND.PORT
        return reader, writer
    except Exception:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            logger.debug("_encode_socks_addr 忽略异常", exc_info=True)
        raise


# ---------------------------------------------------------------------------
# 分片写入
# ---------------------------------------------------------------------------

async def _read_first_payload(reader: asyncio.StreamReader,
                              cfg: FragmentConfig) -> bytes:
    """读取连接起始的字节流，直到能确定 SNI 位置或达到上限。

    不能假设 ClientHello 一定在第一个 TCP 包里：跨 record、TLS 1.3 大扩展、
    或客户端把 ClientHello 与早期数据一起发时都会跨包。所以这里按
    "record 声明长度" 循环补齐，而不是读一次就分片。
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + cfg.handshake_timeout
    buf = b''

    while len(buf) < cfg.max_client_hello:
        remain = deadline - loop.time()
        if remain <= 0:
            break
        try:
            chunk = await asyncio.wait_for(reader.read(cfg.chunk_size), remain)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk

        # 已经不是 TLS 了，别再等
        if len(buf) >= 1 and buf[0] != _TLS_CONTENT_HANDSHAKE:
            break
        # 拿到 SNI 就够了
        if find_sni_range(buf) is not None:
            break
        # record 还没收全，继续读
        total = tls_record_total_len(buf)
        if total is not None and len(buf) < total:
            continue
        # 已经收全但仍没找到 SNI（无 SNI 扩展 / 非 ClientHello），收工
        if total is not None and len(buf) >= total:
            break
    return buf


async def _write_fragmented(writer: asyncio.StreamWriter,
                            payload: bytes,
                            cfg: FragmentConfig) -> Tuple[int, Optional[SniRange], str]:
    """把首包按 SNI 中点切成两段分两次写入。

    Returns:
        ``(分片点, SNI 区间, 模式)``，模式取值 ``'sni'`` / ``'fallback'`` / ``'none'``
    """
    if not payload:
        return 0, None, 'none'

    rng = find_sni_range(payload)
    if rng is not None and rng.length >= 2:
        split = rng.midpoint()
        if logger.isEnabledFor(logging.DEBUG):
            host = '<hidden>'
            if cfg.log_hostname:
                host = payload[rng.start:rng.end].decode('ascii', 'replace')
            logger.debug(f'SNI 分片: {host} 区间=({rng.start},{rng.end}) 切点={split}')
        mode = 'sni'
    elif cfg.fallback_split and len(payload) > cfg.fallback_split:
        split = cfg.fallback_split
        mode = 'fallback'
        logger.debug(f'未找到 SNI，按兜底偏移分片: {split}')
    else:
        writer.write(payload)
        await writer.drain()
        return len(payload), None, 'none'

    writer.write(payload[:split])
    await writer.drain()
    if cfg.fragment_delay > 0:
        await asyncio.sleep(cfg.fragment_delay)
    writer.write(payload[split:])
    await writer.drain()
    return split, rng, mode


# ---------------------------------------------------------------------------
# SOCKS5 服务端
# ---------------------------------------------------------------------------

class SniFragmentProxy:
    """本地 SOCKS5 分片代理。"""

    def __init__(self, config: Optional[FragmentConfig] = None):
        self.cfg = config or FragmentConfig()
        self._server: Optional[asyncio.AbstractServer] = None
        self._tasks: set = set()
        self.stats: Dict[str, int] = {
            'connections': 0,
            'fragmented': 0,
            'fallback_fragmented': 0,
            'not_fragmented': 0,
            'errors': 0,
            'bytes_up': 0,
            'bytes_down': 0,
        }
        self.last_sni: Optional[str] = None

    # ---- 生命周期 ----
    async def start(self) -> 'SniFragmentProxy':
        self._server = await asyncio.start_server(
            self._on_client, self.cfg.listen_host, self.cfg.listen_port)
        addrs = ', '.join(str(s.getsockname()) for s in self._server.sockets)
        logger.info(f'SNI 分片代理已启动: {addrs}'
                    f'{" -> " + self.cfg.upstream if self.cfg.upstream else " (直连)"}')
        return self

    async def serve_forever(self):
        if self._server is None:
            await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self):
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                logger.debug("__init__ 忽略异常", exc_info=True)
            self._server = None
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    # ---- 连接处理 ----
    def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        task = asyncio.ensure_future(self._handle_client(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        self.stats['connections'] += 1
        up_writer: Optional[asyncio.StreamWriter] = None
        try:
            target = await self._socks5_greeting_and_request(reader, writer)
            if target is None:
                return
            host, port = target

            if self.cfg.upstream:
                up_reader, up_writer = await _open_via_socks5(host, port, self.cfg.upstream, self.cfg)
            else:
                up_reader, up_writer = await _open_direct(host, port, self.cfg)

            writer.write(bytes([_SOCKS_VER, 0x00, 0x00, _ATYP_IPV4, 0, 0, 0, 0, 0, 0]))
            await writer.drain()

            await self._relay(reader, writer, up_reader, up_writer)
        except (asyncio.IncompleteReadError, ConnectionResetError,
                BrokenPipeError, asyncio.TimeoutError, ConnectionError) as e:
            logger.debug(f'连接结束: {type(e).__name__}: {e}')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.stats['errors'] += 1
            logger.warning(f'处理连接出错: {type(e).__name__}: {e}')
        finally:
            for w in (up_writer, writer):
                if w is None:
                    continue
                try:
                    w.close()
                except Exception:
                    logger.debug("_on_client 忽略异常", exc_info=True)
                try:
                    await w.wait_closed()
                except Exception:
                    logger.debug("_on_client 忽略异常", exc_info=True)

    async def _socks5_greeting_and_request(self, reader, writer):
        """完成 SOCKS5 握手，返回 (host, port)；失败返回 None 并已回包。"""
        ver, nmethods = await asyncio.wait_for(reader.readexactly(2), self.cfg.handshake_timeout)
        if ver != _SOCKS_VER:
            return None
        methods = await asyncio.wait_for(reader.readexactly(nmethods), self.cfg.handshake_timeout)
        if _SOCKS_AUTH_NONE not in methods:
            writer.write(bytes([_SOCKS_VER, _SOCKS_AUTH_NO_ACCEPTABLE]))
            await writer.drain()
            return None
        writer.write(bytes([_SOCKS_VER, _SOCKS_AUTH_NONE]))
        await writer.drain()

        head = await asyncio.wait_for(reader.readexactly(4), self.cfg.handshake_timeout)
        _ver, cmd, _rsv, atyp = head
        if cmd != _SOCKS_CMD_CONNECT:
            writer.write(bytes([_SOCKS_VER, 0x07, 0x00, _ATYP_IPV4, 0, 0, 0, 0, 0, 0]))
            await writer.drain()
            return None

        if atyp == _ATYP_IPV4:
            host = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == _ATYP_IPV6:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        elif atyp == _ATYP_DOMAIN:
            n = (await reader.readexactly(1))[0]
            raw = await reader.readexactly(n)
            try:
                host = raw.decode('idna') if raw.isascii() else raw.decode('utf-8')
            except Exception:
                host = raw.decode('utf-8', 'replace')
        else:
            writer.write(bytes([_SOCKS_VER, 0x08, 0x00, _ATYP_IPV4, 0, 0, 0, 0, 0, 0]))
            await writer.drain()
            return None

        port = struct.unpack('>H', await reader.readexactly(2))[0]
        return host, port

    async def _relay(self, c_reader, c_writer, s_reader, s_writer):
        """双向中继；客户端首包做分片。"""

        async def client_to_server():
            first = await _read_first_payload(c_reader, self.cfg)
            if first:
                split, rng, mode = await _write_fragmented(s_writer, first, self.cfg)
                self.stats['bytes_up'] += len(first)
                if mode == 'sni':
                    self.stats['fragmented'] += 1
                    if self.cfg.log_hostname:
                        self.last_sni = first[rng.start:rng.end].decode('ascii', 'replace')
                    else:
                        self.last_sni = f'<{rng.length} bytes>'
                elif mode == 'fallback':
                    self.stats['fallback_fragmented'] += 1
                    logger.debug(f'非 TLS/无 SNI 流量，按兜底偏移分片（切点={split}）')
                else:
                    self.stats['not_fragmented'] += 1
            while True:
                data = await c_reader.read(self.cfg.chunk_size)
                if not data:
                    break
                self.stats['bytes_up'] += len(data)
                s_writer.write(data)
                await s_writer.drain()

        async def server_to_client():
            while True:
                data = await s_reader.read(self.cfg.chunk_size)
                if not data:
                    break
                self.stats['bytes_down'] += len(data)
                c_writer.write(data)
                await c_writer.drain()

        t1 = asyncio.ensure_future(client_to_server())
        t2 = asyncio.ensure_future(server_to_client())
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc:
                raise exc


# ---------------------------------------------------------------------------
# 便捷入口 / 自测
# ---------------------------------------------------------------------------

async def start_sni_fragment_proxy(listen: str = '127.0.0.1:10809',
                                   upstream: Optional[str] = None,
                                   **kwargs) -> SniFragmentProxy:
    """解析 ``host:port`` 形式的监听地址并启动代理。"""
    host, _, port_s = listen.rpartition(':')
    cfg = FragmentConfig(
        listen_host=host or '127.0.0.1',
        listen_port=int(port_s),
        upstream=upstream,
        **kwargs,
    )
    return await SniFragmentProxy(cfg).start()


def _main(argv=None):
    p = argparse.ArgumentParser(
        prog='python -m modules.core.net.sni_fragment_proxy',
        description='SNI 分片本地代理（Lite 模式）')
    p.add_argument('--listen', default='127.0.0.1:10809', help='监听地址，默认 127.0.0.1:10809')
    p.add_argument('--upstream', default=None,
                   help='上游代理，例如 socks5://127.0.0.1:10808；不填则直连目标')
    p.add_argument('--delay', type=float, default=0.012, help='分片间隔秒数，默认 0.012')
    p.add_argument('--no-fallback', action='store_true', help='找不到 SNI 时不分片')
    p.add_argument('--log-hostname', action='store_true', help='日志中打印真实域名')
    p.add_argument('-v', '--verbose', action='store_true', help='调试日志')
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
    )

    cfg = FragmentConfig(
        listen_host=args.listen.rpartition(':')[0] or '127.0.0.1',
        listen_port=int(args.listen.rpartition(':')[2]),
        upstream=args.upstream,
        fragment_delay=args.delay,
        fallback_split=None if args.no_fallback else 2,
        log_hostname=args.log_hostname,
    )
    proxy = SniFragmentProxy(cfg)
    try:
        asyncio.run(proxy.serve_forever())
    except KeyboardInterrupt:
        logger.info(f'已停止。统计: {proxy.stats}')


if __name__ == '__main__':  # pragma: no cover
    _main()
