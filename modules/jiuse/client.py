# -*- coding: utf-8 -*-
"""HTTP 客户端：九色模块**全程直连**（不读系统/环境代理）。

为什么不用 requests 的默认行为：
  * requests 会读取 ``HTTP_PROXY`` 等环境变量，用户机器上挂了代理时会“偷偷”走代理，
    与「九色全程直连」的要求冲突 → 统一 ``trust_env = False``。
  * pages.dev 走 Cloudflare，纯 requests 的 TLS 指纹容易被 reset。
    因此按顺序尝试：requests → curl_cffi(impersonate chrome) → urllib。
    任何一个通道成功就返回，并把通道名写进结果，便于界面显示。
"""
from __future__ import annotations

import ssl
import urllib.request
from typing import Tuple

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}


def _via_requests(url: str, timeout: float, allow_insecure: bool) -> str:
    import requests
    import urllib3

    if allow_insecure:
        urllib3.disable_warnings()
    s = requests.Session()
    s.trust_env = False                      # 关键：不走环境代理
    r = s.get(url, headers=HEADERS, timeout=timeout,
              verify=not allow_insecure, allow_redirects=True)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.text


def _via_curl_cffi(url: str, timeout: float, allow_insecure: bool) -> str:
    from curl_cffi import requests as creq

    r = creq.get(url, headers=HEADERS, impersonate="chrome",
                 timeout=timeout, allow_redirects=True,
                 verify=not allow_insecure)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.text


def _via_urllib(url: str, timeout: float, allow_insecure: bool) -> str:
    ctx = ssl.create_default_context()
    if allow_insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    # 显式空代理处理器：确保直连
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, headers=HEADERS)
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", "replace")


CHANNELS = (("requests", _via_requests),
            ("curl_cffi", _via_curl_cffi),
            ("urllib", _via_urllib))


def fetch(url: str, timeout: float = 20.0) -> Tuple[str, str]:
    """抓取页面 → ``(html, 通道名)``；全部失败抛 RuntimeError（含各通道原因）。"""
    errors = []
    for insecure in (False, True):
        for name, fn in CHANNELS:
            try:
                html = fn(url, timeout, insecure)
                if html and len(html) > 64:
                    return html, name + ("/noverify" if insecure else "")
                errors.append(f"{name}: 返回内容过短({len(html or '')})")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__} {str(exc)[:80]}")
    raise RuntimeError("所有直连通道都失败 → " + " | ".join(errors[:4]))


def check(url: str, timeout: float = 8.0) -> dict:
    """轻量探测：只取页面头部，返回 ok/http/耗时/通道。"""
    import time

    t0 = time.time()
    try:
        html, via = fetch(url, timeout=timeout)
        return {"url": url, "ok": True, "http": 200,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "via": via, "size": len(html)}
    except Exception as exc:  # noqa: BLE001
        return {"url": url, "ok": False, "http": 0,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "via": "", "error": str(exc)[:200], "size": 0}
