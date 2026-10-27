# -*- coding: utf-8 -*-
"""应用设置：单一 JSON 文件（DATA/app/settings.json），支持点号路径读写。

设计原则：
  * 缺省值集中在 ``DEFAULTS``，任何模块读取都不会因键缺失而崩。
  * 保存走「读-改-写」，不覆盖用户手改的其他键。
  * 代理默认 socks5h://127.0.0.1:10808 —— 与糖心/多站点原有习惯一致。
"""
from __future__ import annotations

import copy
import json
import threading
from typing import Any, Dict

from . import paths

DEFAULTS: Dict[str, Any] = {
    "theme": "dark",
    "proxy": {
        "enabled": True,
        "url": "socks5h://127.0.0.1:10808",
    },
    "nav": {
        # 导航页（发布页）：糖心的「回家的路」，需要代理才能读
        "url": "https://daohang.pro/app.js",
        "updated_at": 0,
    },
    "tangxin": {
        "output_base": str(paths.data_dir("tangxin", "downloads")),
        # 按需求：导航页用代理读，站点默认直连（科学线路可直连）
        "proxy_enabled": False,
        "workers": 8,
        # 并发下载的任务数（同时下载几部作品）；分片并发见 workers
        "parallel": 3,
        "skip_existing": True,
        "route_url": "",
        "route_label": "",
        "cdn": "",
        "known_dirs": [],
    },
    "huangguo": {
        "download_dir": str(paths.data_dir("huangguo", "downloads")),
        "concurrency": 3,
        "site_id": "huangguoai",
        "sync_download": "",     # 移动硬盘目标（留空则自动识别）
        "sync_server": "",       # 服务器/备份盘目标
    },
    "jiuse": {
        "nav_url": "https://dizhi222.pages.dev/",
        "download_dir": str(paths.data_dir("jiuse", "downloads")),
        # 九色全程直连（按需求：不挂代理）
        "direct_only": True,
        "check_limit": 8,
        "timeout": 8,
    },
    "sites": {
        # 多站点（通用站点解析）——数据统一落在 DATA/multisite
        "download_dir": str(paths.data_dir("multisite", "downloads")),
        # 实测（2026-10）：这批站点直连 ConnectTimeout，走 socks5h 代理 200。
        # 所以默认开代理；个别站可在此关掉。
        "proxy_enabled": True,
        "pages": 2,
        "timeout": 30,
    },
    "madouqu": {
        # 麻豆磁力（独立模块）——数据统一落在 DATA/madouqu
        "base_url": "https://madouqu.sbs",
        "proxy_enabled": True,
        "timeout": 20,
        "max_pages": 5,
        "download_dir": str(paths.data_dir("madouqu", "downloads")),
    },
    "yttiktok": {
        "download_dir": str(paths.data_dir("yttiktok", "downloads")),
        "format": "best",
        "audio_only": False,
        "write_thumbnail": False,
        "parallel": 2,
    },
    "ui": {
        "font_size": 10,
        "compact": False,
    },
    "window": {"width": 1440, "height": 900, "last_page": "home"},
}


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    _lock = threading.Lock()
    _instance: "Settings | None" = None

    def __new__(cls) -> "Settings":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._data = copy.deepcopy(DEFAULTS)
            cls._instance._load()
        return cls._instance

    # ---------------------------------------------------------------- 存取
    def _path(self):
        return paths.app_file("settings.json")

    def _load(self) -> None:
        try:
            p = self._path()
            if p.exists():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = _deep_merge(DEFAULTS, raw)
        except Exception:
            self._data = copy.deepcopy(DEFAULTS)
        self._migrate()

    def _migrate(self) -> None:
        """把历史版本里写死的旧路径改到新位置。

        合并多站点模块时 ``DATA/sites`` 已改名为 ``DATA/multisite``，
        ``DATA/sites/madouqu`` 独立成 ``DATA/madouqu``；老 settings.json 里
        残留的路径会让「保存目录」指向一个已经不存在的文件夹，必须纠正。
        """
        legacy = ("/sites/downloads", "\\sites\\downloads")
        cur = str(self._data.get("sites", {}).get("download_dir", ""))
        if any(x in cur.replace("\\", "/") for x in ("/sites/downloads",)) or \
                any(x in cur for x in legacy):
            self._data.setdefault("sites", {})["download_dir"] = str(
                paths.data_dir("multisite", "downloads"))
        mq = str(self._data.get("madouqu", {}).get("download_dir", ""))
        if "sites/madouqu" in mq.replace("\\", "/") or "sites\\madouqu" in mq:
            self._data.setdefault("madouqu", {})["download_dir"] = str(
                paths.data_dir("madouqu", "downloads"))

    def save(self) -> str:
        with self._lock:
            p = self._path()
            p.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            return str(p)

    def all(self) -> Dict[str, Any]:
        return copy.deepcopy(self._data)

    # ---------------------------------------------------------------- 点号读写
    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        cur = self._data
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value

    def update(self, patch: Dict[str, Any]) -> None:
        self._data = _deep_merge(self._data, patch)

    # ---------------------------------------------------------------- 便捷项
    @property
    def proxy_url(self) -> str:
        return (self.get("proxy.url", "") or "").strip()

    def proxies(self, enabled: bool | None = None) -> Dict[str, str] | None:
        """给 requests 用的 proxies 字典；未启用代理返回 None。"""
        use = self.get("proxy.enabled", True) if enabled is None else enabled
        url = self.proxy_url
        if not use or not url:
            return None
        return {"http": url, "https": url}


def settings() -> Settings:
    return Settings()
