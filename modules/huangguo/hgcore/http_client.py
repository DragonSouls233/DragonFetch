# -*- coding: utf-8 -*-
"""HTTP 客户端：统一 UA、超时、重试、连接池、可选代理。

性能设计:
  - 每个线程独立持有一个 requests.Session（线程本地），兼顾线程安全与连接复用
  - 复用 Session 可避免每次请求重复 TLS 握手，批量抓取详情页时收益明显
  - 支持通过环境变量 HG_PROXY 或设置文件配置代理（默认空 = 直连）
"""

import threading
import time
from typing import Optional

import urllib3
import requests
from requests.adapters import HTTPAdapter

# 镜像站点使用自签/不受信证书，访问时关闭了证书校验（verify=False）。
# 屏蔽由此产生的 InsecureRequestWarning，避免日志被刷屏、误导用户以为报错。
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from . import config


_thread_local = threading.local()


def _make_session() -> requests.Session:
    """创建一个带连接池与代理配置的 Session。"""
    s = requests.Session()
    s.headers.update(config.DEFAULT_HEADERS)

    # 连接池：批量并发抓取时复用 TCP / TLS 连接
    adapter = HTTPAdapter(pool_connections=20, pool_maxsize=40, max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)

    if config.PROXY_URL:
        s.proxies = {"http": config.PROXY_URL, "https": config.PROXY_URL}
    return s


def _session() -> requests.Session:
    """获取当前线程的 Session（线程本地，懒创建）。"""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = _make_session()
        _thread_local.session = s
    return s


def _request(url: str, referer: Optional[str], timeout: int) -> requests.Response:
    """带指数退避重试的 GET，返回 Response；连续失败抛 ConnectionError。"""
    headers = {"Referer": referer} if referer else None
    last_err = None
    for attempt in range(config.RETRY):
        try:
            resp = _session().get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as exc:                  # noqa: BLE001 - 统一收敛为重试
            last_err = exc
            if attempt < config.RETRY - 1:
                time.sleep(config.RETRY_DELAY * (attempt + 1))
    raise ConnectionError(f"请求失败 {url} -> {last_err}") from last_err


# ---------------------------------------------------------------- 模块级便捷函数

def fetch_text(url: str, referer: str = None, timeout: int = None) -> str:
    """GET 文本。timeout 为可选覆盖（毫秒级探测时传较小值）。"""
    resp = _request(url, referer, timeout or config.TIMEOUT)
    enc = resp.encoding or "utf-8"
    return resp.content.decode(enc, errors="ignore")


def fetch_bytes(url: str, referer: str = None, timeout: int = None) -> Optional[bytes]:
    """GET 二进制（用于封面）。失败返回 None，不抛异常。"""
    try:
        resp = _request(url, referer, timeout or config.TIMEOUT)
        return resp.content
    except Exception:                            # noqa: BLE001 - 封面失败可容忍
        return None


# ---------------------------------------------------------------- 兼容类（单线程场景）

class HttpClient:
    """带重试的轻量 HTTP 客户端（保持向后兼容）。"""

    def __init__(self, timeout: int = None, retry: int = None):
        self.timeout = timeout or config.TIMEOUT
        self.retry = retry or config.RETRY
        self.session = _make_session()

    def fetch_text(self, url: str, referer: str = None, encoding: str = "utf-8") -> str:
        resp = _request(url, referer, self.timeout)
        enc = resp.encoding or encoding
        return resp.content.decode(enc, errors="ignore")

    def fetch_bytes(self, url: str, referer: str = None, timeout: int = None) -> Optional[bytes]:
        try:
            resp = _request(url, referer, timeout or self.timeout)
            return resp.content
        except Exception:                        # noqa: BLE001
            return None

    def close(self) -> None:
        self.session.close()


def get_client() -> HttpClient:
    """获取默认 HttpClient（兼容旧调用）。"""
    return HttpClient()
