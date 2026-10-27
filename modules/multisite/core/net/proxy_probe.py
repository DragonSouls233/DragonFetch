# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""代理健康探测

为什么需要它
------------
2026-09-20 事故的核心机制是"**失败即换节点**"：任意一个下载失败都直接触发
``switch_to_next_node()``，而切换动作会重启 Xray、杀掉并发下载进程，
把一次网络抖动放大成全站瘫痪。

事后的止血方案是给切换加"阈值 + 冷却 + 互斥"三道闸门，但那仍然是**被动**的
——它只限制切换的频率，不判断"这次失败到底是不是代理的锅"。于是：

    连续 3 个视频因为**已删除**而失败  →  阈值达标  →  白切一次节点

本模块补上主动判断：在真正动手换节点之前，先探测**当前代理本身还通不通**。
代理正常 => 失败大概率与代理无关 => 不换节点。这道"最后确认"正是雪崩的反面。

设计参考
--------
"用 HTTP 客户端库自身的连接指标在同进程内测代理"这个思路，来自
EchterAlsFake/Porn_Fetch 的 ``proxy_tester.py``（用 curl_cffi 的 ``CurlInfo``
读取 CONNECT_TIME / STARTTRANSFER_TIME / SSL_VERIFYRESULT）。

本项目原有的 ``v2ray_controller.test_node_speed()`` 走的是另一条路——
为每个候选节点**临时拉起一个 Xray 进程**再测速，开销在秒级且会占用
SOCKS 端口，因此不可能在下载线程里高频调用。本模块是同进程测量，开销在毫秒级。

许可说明
--------
本项目自 2026-09-20 起改用 **GPL-3.0-or-later**（原因见 README「为什么是
GPL-3.0」），与 Porn_Fetch 的 GPL-3.0-or-later 许可**互相兼容**，
授权冲突已不存在。本文件仍为**独立实现**：未复制其任何代码，
仅借鉴"同进程内测量代理"的设计思想。

探测目标的取值注意
------------------
Porn_Fetch 用 ``speed.cloudflare.com`` 作测速目标。本项目**不采用**
——在国内网络环境下该域名的可达性本身就不稳定，会把"目标站不可达"
错判成"代理坏了"，等于引入新的误判源。
默认目标见 :data:`DEFAULT_PROBE_TARGETS`，可通过配置文件
``network.probe_url`` 或环境变量 ``PSP_PROBE_URL`` 覆盖。
"""

import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
import logging

logger = logging.getLogger(__name__)

__all__ = [
    'ProxyProbeResult',
    'probe_proxy',
    'tcp_alive',
    'probe_proxy_cached',
    'invalidate_cache',
    'DEFAULT_PROBE_TARGETS',
]

#: 默认探测目标：体积最小、且"能翻墙就一定通"的经典端点。
#: 刻意避开 speed.cloudflare.com（见模块 docstring 说明）。
DEFAULT_PROBE_TARGETS: Tuple[str, ...] = (
    'https://www.gstatic.com/generate_204',
    'https://www.google.com/generate_204',
    'https://cp.cloudflare.com/generate_204',
)

_ENV_PROBE_URL = 'PSP_PROBE_URL'

# 探测结果缓存：key = (proxy_url, target) -> (过期时间, 结果)
_CACHE: Dict[Tuple[str, str], Tuple[float, 'ProxyProbeResult']] = {}
_CACHE_LOCK = threading.Lock()

#: 缓存有效期（秒）。短一点，避免"代理刚死但缓存说它活着"。
DEFAULT_CACHE_TTL = 15.0


@dataclass
class ProxyProbeResult:
    """代理探测结果。"""

    proxy: str
    level: str          # 'tcp' | 'http'
    ok: bool
    latency_ms: float = -1.0
    target: str = ''
    error: str = ''
    status_code: Optional[int] = None

    @property
    def alive(self) -> bool:
        """代理端口在监听（代理进程没死）。"""
        return self.ok

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        if self.ok:
            return f"代理正常 [{self.level}] {self.latency_ms:.0f}ms target={self.target}"
        return f"代理异常 [{self.level}] {self.error} target={self.target}"


def _split_host_port(url: str) -> Tuple[Optional[str], Optional[int]]:
    """从 ``scheme://host:port`` 里取出 host / port。"""
    try:
        p = urlparse(url)
        if p.hostname:
            port = p.port
            if port is None:
                port = {'socks5': 1080, 'socks5h': 1080, 'http': 8080, 'https': 443}.get(p.scheme, 0)
            return p.hostname, int(port)
    except Exception:
        logger.debug("_split_host_port 忽略异常", exc_info=True)
    return None, None


def tcp_alive(host: str, port: int, timeout: float = 2.0) -> ProxyProbeResult:
    """L1 探测：TCP 连通性。

    只验证"端口有人在听"，不验证出口。用于快速判断代理进程是否已被杀掉 /
    正在重启——这正是 2026-09-20 事故里 Xray 被反复重启时的状态。
    """
    t0 = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            dt = (time.perf_counter() - t0) * 1000.0
        return ProxyProbeResult(
            proxy=f'{host}:{port}', level='tcp', ok=True,
            latency_ms=dt, target=f'{host}:{port}',
        )
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000.0
        return ProxyProbeResult(
            proxy=f'{host}:{port}', level='tcp', ok=False,
            latency_ms=dt, target=f'{host}:{port}',
            error=f'{type(e).__name__}: {e}',
        )


def _probe_targets(target: Optional[str]) -> List[str]:
    if target:
        return [target]
    env = os.environ.get(_ENV_PROBE_URL, '').strip()
    if env:
        return [env]
    return list(DEFAULT_PROBE_TARGETS)


def _requests_via_proxy(proxy_url: str, target: str, timeout: float,
                        prefer_remote_dns: bool = True) -> ProxyProbeResult:
    import requests  # 延迟导入：让只用 tcp_alive 的调用方不依赖 requests

    # socks5 -> socks5h：让 DNS 在代理解析，避免本地 DNS 污染导致的假失败
    req_proxy = proxy_url
    if prefer_remote_dns and req_proxy.startswith('socks5://'):
        req_proxy = 'socks5h://' + req_proxy[len('socks5://'):]

    t0 = time.perf_counter()
    try:
        r = requests.get(
            target,
            proxies={'http': req_proxy, 'https': req_proxy},
            timeout=timeout,
            stream=True,
            allow_redirects=False,
            headers={'User-Agent': 'Mozilla/5.0 (proxy-probe)'},
        )
        dt = (time.perf_counter() - t0) * 1000.0
        code = r.status_code
        try:
            r.close()
        except Exception:
            logger.debug("_requests_via_proxy 忽略异常", exc_info=True)
        # 只要拿到了 HTTP 响应，就说明"代理→目标"这条链路是通的。
        # 4xx/5xx 是目标站的态度，不是代理的锅，仍算链路可达。
        return ProxyProbeResult(
            proxy=proxy_url, level='http', ok=True,
            latency_ms=dt, target=target, status_code=code,
        )
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000.0
        return ProxyProbeResult(
            proxy=proxy_url, level='http', ok=False,
            latency_ms=dt, target=target,
            error=f'{type(e).__name__}: {e}',
        )


def probe_proxy(proxy_url: str,
                target: Optional[str] = None,
                timeout: float = 4.0,
                level: str = 'auto') -> ProxyProbeResult:
    """探测代理健康度。

    Args:
        proxy_url: 形如 ``socks5://127.0.0.1:10808``
        target: 探测目标 URL；None 时依次尝试 :data:`DEFAULT_PROBE_TARGETS`
        timeout: 单次 HTTP 探测超时（秒）。L1 只用 timeout/2。
        level: ``'tcp'`` 只做连通性；``'http'`` 做出口探测；
               ``'auto'``（默认）先 TCP，通了再 http。

    Returns:
        :class:`ProxyProbeResult`。``level='auto'`` 下最终返回的是
        **最严格的一次**结果：只有 L1、L2 都通过才算 ok。
    """
    host, port = _split_host_port(proxy_url)
    if not host or not port:
        return ProxyProbeResult(
            proxy=proxy_url, level='tcp', ok=False,
            error=f'无法解析代理地址: {proxy_url}',
        )

    if level == 'tcp':
        return tcp_alive(host, port, timeout=min(timeout, 2.0))

    l1 = tcp_alive(host, port, timeout=min(timeout, 2.0))
    if not l1.ok:
        return l1
    if level == 'tcp':
        return l1

    last: Optional[ProxyProbeResult] = None
    for t in _probe_targets(target):
        res = _requests_via_proxy(proxy_url, t, timeout)
        if res.ok:
            return res
        last = res
    return last or ProxyProbeResult(
        proxy=proxy_url, level='http', ok=False, error='无可用探测目标',
    )


def probe_proxy_cached(proxy_url: str,
                       target: Optional[str] = None,
                       timeout: float = 4.0,
                       cache_ttl: float = DEFAULT_CACHE_TTL) -> ProxyProbeResult:
    """带缓存的 :func:`probe_proxy`。

    下载线程里可能同时有多个任务判定失败，若各自都去探测会打出一串
    并发探测请求。缓存让同一代理在 ``cache_ttl`` 秒内只真正探测一次。
    """
    key = (proxy_url, target or '')
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]

    res = probe_proxy(proxy_url, target=target, timeout=timeout)

    with _CACHE_LOCK:
        _CACHE[key] = (now + max(0.0, cache_ttl), res)
    return res


def invalidate_cache(proxy_url: Optional[str] = None) -> None:
    """清空探测缓存；传 ``proxy_url`` 时只清该代理的条目。

    节点切换成功后应当调用：换了出口 IP，旧结论立即失效。
    """
    with _CACHE_LOCK:
        if proxy_url is None:
            _CACHE.clear()
        else:
            for k in [k for k in _CACHE if k[0] == proxy_url]:
                _CACHE.pop(k, None)
