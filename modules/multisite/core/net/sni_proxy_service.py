# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""SNI 分片代理的后台服务封装

主程序（GUI / Web 后台）是同步+多线程的，而 :mod:`sni_fragment_proxy`
基于 asyncio。本模块负责把那个 asyncio 服务器放进一条独立后台线程里跑，
并提供 启动 / 停止 / 状态查询 的同步接口。

配置（``config/app_config.json``）::

    "network": {
      "sni_fragment": {
        "enabled": false,              // 默认关闭，需实测通过后再打开
        "listen_port": 10809,
        "upstream": "socks5://127.0.0.1:10808",   // 一般指向现有 Xray
        "fragment_delay": 0.012,
        "fallback_split": 2,
        "log_hostname": false
      }
    }

为什么默认关闭
--------------
SNI 分片只对"明文 SNI 的 DPI 阻断"有效，对 IP 封锁、TLS1.3+ECH 无效。
而且它**无法在无阻断的环境里自证有效**（本地只能验证机制正确性）。
所以默认关闭，需要用户在真实被阻断的站点上验证后再开启——
开启方式：把 ``enabled`` 改成 ``true``，或直接运行
``启动SNI分片代理.bat`` 用独立的本地端口做对照实验。
"""

import json
import logging
import os
import socket
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    'SniProxyService',
    'get_service',
    'ensure_started_from_config',
    'load_sni_fragment_config',
    'DEFAULT_CONFIG',
]

DEFAULT_CONFIG: Dict = {
    'enabled': False,
    'listen_host': '127.0.0.1',
    'listen_port': 10809,
    'upstream': 'socks5://127.0.0.1:10808',
    'fragment_delay': 0.012,
    'fallback_split': 2,
    'handshake_timeout': 5.0,
    'log_hostname': False,
}

_service: Optional['SniProxyService'] = None
_service_lock = threading.Lock()


def _project_config_path() -> str:
    """定位 ``config/app_config.json``。

    刻意不走 ``modules.core.utils.path_utils``：那条链路会连带导入
    yaml / 配置单例，让本模块在轻量测试环境里不可用。
    """
    here = os.path.dirname(os.path.abspath(__file__))          # modules/core/net
    root = os.path.dirname(os.path.dirname(os.path.dirname(here)))  # 项目根
    return os.path.join(root, 'config', 'app_config.json')


def load_sni_fragment_config(config_path: Optional[str] = None) -> Dict:
    """从 ``app_config.json`` 读取 ``network.sni_fragment``，与默认值合并。"""
    cfg = dict(DEFAULT_CONFIG)
    path = config_path or _project_config_path()
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            user = (data.get('network', {}) or {}).get('sni_fragment', {}) or {}
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    except Exception as e:
        logger.warning(f'读取 SNI 分片配置失败，使用默认值: {e}')
    return cfg


class SniProxyService:
    """在后台线程中运行 SNI 分片代理。"""

    def __init__(self, config: Optional[Dict] = None):
        self.cfg = dict(DEFAULT_CONFIG)
        if config:
            self.cfg.update({k: v for k, v in config.items() if k in DEFAULT_CONFIG})
        self._thread: Optional[threading.Thread] = None
        self._loop = None
        self._proxy = None
        self._started = threading.Event()
        self._start_error: Optional[str] = None

    # ---- 派生信息 ----
    @property
    def local_proxy_url(self) -> str:
        return f"socks5://{self.cfg['listen_host']}:{self.cfg['listen_port']}"

    @property
    def running(self) -> bool:
        if self._thread is None or not self._thread.is_alive():
            return False
        port = int(self.cfg['listen_port'])
        try:
            with socket.create_connection((self.cfg['listen_host'], port), timeout=0.5):
                return True
        except Exception:
            return False

    def status(self) -> Dict:
        st = {
            'running': self.running,
            'local_proxy': self.local_proxy_url,
            'upstream': self.cfg.get('upstream'),
            'enabled_in_config': bool(self.cfg.get('enabled')),
            'listen_port': self.cfg.get('listen_port'),
            'start_error': self._start_error,
        }
        if self._proxy is not None:
            st['stats'] = dict(self._proxy.stats)
            st['last_sni'] = self._proxy.last_sni
        return st

    # ---- 生命周期 ----
    def start(self, wait_ready: float = 5.0) -> bool:
        """启动后台代理；幂等。"""
        if self.running:
            return True

        self._started.clear()
        self._start_error = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name='sni-fragment-proxy')
        self._thread.start()

        deadline = time.time() + max(0.0, wait_ready)
        while time.time() < deadline:
            if self._started.is_set():
                break
            time.sleep(0.05)

        if not self._started.is_set():
            self._start_error = self._start_error or '启动超时'
            logger.error(f"SNI 分片代理启动失败: {self._start_error}")
            return False
        if self._start_error:
            return False

        # 再确认端口真的在听
        for _ in range(20):
            if self.running:
                logger.info(f"SNI 分片代理已就绪: {self.local_proxy_url} "
                            f"-> {self.cfg.get('upstream') or '直连'}")
                return True
            time.sleep(0.05)
        self._start_error = '端口未就绪'
        return False

    def _run(self):
        import asyncio
        from .sni_fragment_proxy import FragmentConfig, SniFragmentProxy

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            fcfg = FragmentConfig(
                listen_host=self.cfg['listen_host'],
                listen_port=int(self.cfg['listen_port']),
                upstream=self.cfg.get('upstream') or None,
                fragment_delay=float(self.cfg.get('fragment_delay', 0.012)),
                fallback_split=self.cfg.get('fallback_split', 2),
                handshake_timeout=float(self.cfg.get('handshake_timeout', 5.0)),
                log_hostname=bool(self.cfg.get('log_hostname', False)),
            )
            self._proxy = SniFragmentProxy(fcfg)
            loop.run_until_complete(self._proxy.start())
            self._started.set()
            loop.run_forever()
        except Exception as e:
            self._start_error = f'{type(e).__name__}: {e}'
            self._started.set()
            logger.error(f"SNI 分片代理运行异常: {self._start_error}")
        finally:
            try:
                if self._proxy is not None:
                    loop.run_until_complete(self._proxy.stop())
            except Exception:
                logger.debug("_run 忽略异常", exc_info=True)
            try:
                loop.close()
            except Exception:
                logger.debug("_run 忽略异常", exc_info=True)
            self._loop = None

    def stop(self, timeout: float = 3.0) -> bool:
        """停止后台代理。"""
        if self._loop is None or self._thread is None:
            return True
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            logger.debug("stop 忽略异常", exc_info=True)
        self._thread.join(timeout=timeout)
        self._thread = None
        self._proxy = None
        return not self.running


def get_service(config: Optional[Dict] = None) -> SniProxyService:
    """获取全局单例服务。"""
    global _service
    with _service_lock:
        if _service is None:
            _service = SniProxyService(config)
        elif config:
            _service.cfg.update({k: v for k, v in config.items() if k in DEFAULT_CONFIG})
        return _service


def ensure_started_from_config(config_path: Optional[str] = None) -> Optional[str]:
    """按配置决定是否启动，返回应当使用的本地代理 URL。

    Returns:
        ``enabled`` 为真且启动成功时返回 ``socks5://127.0.0.1:<port>``，
        否则返回 ``None``（调用方应继续使用原有代理）。
    """
    cfg = load_sni_fragment_config(config_path)
    if not cfg.get('enabled'):
        return None
    svc = get_service(cfg)
    return svc.local_proxy_url if svc.start() else None
