# -*- coding: utf-8 -*-
"""HTTP 客户端：九色模块默认**全程直连**（不读系统/环境代理）。

为什么不用 requests 的默认行为：
  * requests 会读取 ``HTTP_PROXY`` 等环境变量，用户机器上挂了代理时会“偷偷”走代理，
    与「九色优先直连」的冲突 → 统一 ``trust_env = False``；
  * pages.dev 走 Cloudflare，纯 requests 的 TLS 指纹容易被 reset。
    因此按顺序尝试：requests → curl_cffi(impersonate chrome) → urllib。
    任何一个通道成功就返回，并把通道名写进结果，便于界面显示。

``proxy`` 参数（可选）只给「地址页/站点在本机确实不通」时兜底用：
地址同步中心会先直连，失败且用户允许时才带 proxy 再试一次，并把结果里的
``direct`` 标记传给上层，由上层决定这个模块要不要走代理。
"""
from __future__ import annotations

import ssl
import urllib.request
from typing import Dict, Optional, Tuple

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}


def _proxy_map(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    p = (proxy or "").strip()
    return {"http": p, "https": p} if p else None


class HttpStatusError(RuntimeError):
    """服务器明确回了 4xx/5xx：换通道、换 TLS 校验都不会变，别再重试。"""


def _via_requests(url: str, timeout: float, allow_insecure: bool,
                  proxy: Optional[str] = None) -> str:
    import requests
    import urllib3

    if allow_insecure:
        urllib3.disable_warnings()
    s = requests.Session()
    s.trust_env = False                      # 关键：不走环境代理
    r = s.get(url, headers=HEADERS, timeout=timeout,
              proxies=_proxy_map(proxy),
              verify=not allow_insecure, allow_redirects=True)
    if r.status_code >= 400:
        raise HttpStatusError(f"HTTP {r.status_code}")
    return r.text


def _via_curl_cffi(url: str, timeout: float, allow_insecure: bool,
                   proxy: Optional[str] = None) -> str:
    from curl_cffi import requests as creq

    r = creq.get(url, headers=HEADERS, impersonate="chrome",
                 timeout=timeout, allow_redirects=True,
                 proxies=_proxy_map(proxy),
                 verify=not allow_insecure)
    if r.status_code >= 400:
        raise HttpStatusError(f"HTTP {r.status_code}")
    return r.text


def _via_urllib(url: str, timeout: float, allow_insecure: bool,
                proxy: Optional[str] = None) -> str:
    ctx = ssl.create_default_context()
    if allow_insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # 显式代理处理器：不给 proxy 就传空表，确保是直连
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(_proxy_map(proxy) or {}),
        urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, headers=HEADERS)
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", "replace")


CHANNELS = (("requests", _via_requests),
            ("curl_cffi", _via_curl_cffi),
            ("urllib", _via_urllib))

# 网络层失败（DNS/超时/连接重置）——换 TLS 校验方式重试也没意义，直接跳出
_NET_HINTS = ("timeout", "timed out", "connecttimeout", "readtimeout",
              "connectionerror", "connection aborted", "connection reset",
              "getaddrinfo", "name or service", "remote end closed",
              "remote disconnected", "failed to perform", "proxyerror")

# 「这条路根本不通」的硬失败：超时 / DNS 失败。换下一个通道也是同样的等待，
# 所以直接放弃整个通道序列；而 reset / TLS 指纹被拦是**通道相关**的，
# 必须继续换 curl_cffi 试（Cloudflare 常见）。
_HARD_HINTS = ("timeout", "timed out", "getaddrinfo", "name or service",
               "proxyerror")


def _is_net_error(exc: Exception) -> bool:
    msg = f"{type(exc).__name__} {exc}".lower()
    return any(k in msg for k in _NET_HINTS)


def _is_hard_error(exc: Exception) -> bool:
    msg = f"{type(exc).__name__} {exc}".lower()
    return any(k in msg for k in _HARD_HINTS)


def fetch(url: str, timeout: float = 20.0,
          proxy: Optional[str] = None) -> Tuple[str, str]:
    """抓取页面 → ``(html, 通道名)``；全部失败抛 RuntimeError（含各通道原因）。

    两轮：先正常校验 TLS，再 ``noverify`` 兜一次。命中「硬失败」（超时/DNS）
    就不再换通道空等——一个被墙的域名因此从 30s+ 降到 1 个超时；
    而 reset / TLS 拦截属通道相关，仍会继续换 curl_cffi 试（Cloudflare 常见）。
    """
    errors = []
    hard = False
    for insecure in (False, True):
        for name, fn in CHANNELS:
            try:
                html = fn(url, timeout, insecure, proxy)
                if html and len(html) > 64:
                    tag = name + ("/noverify" if insecure else "")
                    return html, (tag + "/proxy" if proxy else tag)
                errors.append(f"{name}: 返回内容过短({len(html or '')})")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__} {str(exc)[:80]}")
                if isinstance(exc, HttpStatusError):
                    # 403/404 这类是站点明确答复，换通道也是同一个答案
                    hard = True
                    break
                if _is_hard_error(exc):
                    hard = True
                    break
        if hard:
            break
    mode = "代理通道" if proxy else "直连通道"
    raise RuntimeError(f"所有{mode}都失败 → " + " | ".join(errors[:4]))


def check(url: str, timeout: float = 8.0,
          proxy: Optional[str] = None) -> dict:
    """轻量探测：只取页面头部，返回 ok/http/耗时/通道。"""
    import time

    t0 = time.time()
    try:
        html, via = fetch(url, timeout=timeout, proxy=proxy)
        return {"url": url, "ok": True, "http": 200,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "via": via, "size": len(html), "direct": not proxy}
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "ok": False, "http": 0,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "via": "", "error": str(exc)[:200], "size": 0,
                "direct": not proxy}
