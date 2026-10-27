# -*- coding: utf-8 -*-
"""全局代理配置读取（后端版）。

从原 ``modules/gui/tabs/porn_tab/utils.py`` 抽出：原实现放在 GUI 目录里，
但 ``download_queue`` / ``nfo_writer`` 等纯后端模块都要用它。前端重构后
GUI 目录已删除，因此把这段逻辑下沉到 core，保持后端自洽。

读取顺序（与原实现一致）：
  1. ``app_config.json`` 的 ``network`` 段（proxy_enabled / proxy_type / host / port）
  2. ``app_config.json`` 的 ``proxy`` 段（enabled / type / host / port）
  3. ``download_config.json`` 的 ``proxy``（字符串或 dict）
  4. 环境变量 ``HTTPS_PROXY`` / ``HTTP_PROXY``
返回 ``scheme://host:port`` 字符串；未启用代理时返回空字符串。
"""
import json
import logging
import os

logger = logging.getLogger(__name__)


def _read_json(path: str):
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("读取配置失败 %s: %s", path, e)
    return None


def get_global_proxy_config() -> str:
    """返回全局代理 URL（未启用返回 ""）。"""
    try:
        from multisite.core.utils.path_utils import get_config_path
        config_file = get_config_path("app_config.json")
    except Exception:
        config_file = ""

    data = _read_json(config_file) or {}

    network = data.get("network") or {}
    if network.get("proxy_enabled", False):
        p_type = network.get("proxy_type", "socks5")
        p_host = network.get("proxy_host", "127.0.0.1")
        p_port = network.get("proxy_port", 10808)
        return f"{p_type}://{p_host}:{p_port}"

    proxy = data.get("proxy") or {}
    if isinstance(proxy, dict) and proxy.get("enabled", False):
        p_type = proxy.get("type", "socks5")
        p_host = proxy.get("host", "127.0.0.1")
        p_port = proxy.get("port", "10808")
        return f"{p_type}://{p_host}:{p_port}"

    try:
        from multisite.core.utils.path_utils import get_config_path
        dl_file = get_config_path("download_config.json")
    except Exception:
        dl_file = ""
    dl = _read_json(dl_file) or {}
    dl_proxy = dl.get("proxy", "")
    if isinstance(dl_proxy, dict):
        if dl_proxy.get("enabled", False):
            return dl_proxy.get("https") or dl_proxy.get("http") or ""
    elif dl_proxy:
        return str(dl_proxy)

    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""


# 兼容旧调用名（原 GUI 里是私有函数名）
_get_global_proxy_config = get_global_proxy_config
