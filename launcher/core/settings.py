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

# 基准窗口尺寸：1080P。布局（侧栏 + 8 列站点表格 + 右侧内置浏览器）按这个宽度
# 留的余量，改这里就能整体换基准。
WINDOW_DEFAULT_W = 1920
WINDOW_DEFAULT_H = 1080
WINDOW_LAYOUT_TAG = "1920x1080"

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
    "navsync": {
        # 统一地址同步中心（糖心 / 黄果 / 麻豆 / 九色）
        "auto_on_start": True,        # 每次打开软件都同步一次并做直连校验
        "timeout": 8,                 # 单个地址的直连探测超时
        "deadline": 60,               # 一次同步的总预算，超时的模块如实标「超时」
        "use_proxy_for_nav": True,    # 读发布页/地址页允许用代理（站点本身仍按校验结果）
        "parallel": True,
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
        # 地址同步中心测出来的结果：镜像能直连就不挂代理
        "proxy_enabled": False,
    },
    "jiuse": {
        "nav_url": "https://dizhi222.pages.dev/",
        "download_dir": str(paths.data_dir("jiuse", "downloads")),
        # 九色优先直连（按需求：不挂代理）。地址同步中心校验后会自动改写：
        # 直连能开 → direct_only=True；只能代理 → False 并由页面带上代理
        "direct_only": True,
        "proxy_enabled": False,
        "active_url": "",
        "check_limit": 8,
        "timeout": 8,
    },
    "sites": {
        # 站点是「一域名一模块」，下载目录各自落在 DATA/sites/<key>/downloads，
        # 所以这里没有统一的 download_dir。这里只放公共参数。
        # 实测（2026-10）：这批站点直连 ConnectTimeout，走 socks5h 代理 200。
        "proxy_enabled": True,
        "pages": 2,
        "timeout": 30,
    },
    "madouqu": {
        # 麻豆磁力（独立模块）——数据统一落在 DATA/madouqu
        # 实测 2026-10-28：madouqu.com 直连 200（1.3s）；.sbs/.club/.net/.org 全挂，
        # .cc 代理 200 但正文只有 472B 空壳。默认用 .com。
        "base_url": "https://madouqu.com",
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
    # 窗口按 1080P（1920×1080）设计：内置浏览器、8 列站点表格、详情页左右分栏
    # 都按这个宽度留的余量。layout 是「布局版本」标记，换基准尺寸时改它，
    # 老配置里记着的旧尺寸会被一次性重置（见 _migrate），之后用户自己拖的尺寸
    # 仍然照旧记住。
    "window": {"width": WINDOW_DEFAULT_W, "height": WINDOW_DEFAULT_H,
               "layout": WINDOW_LAYOUT_TAG, "last_page": "home"},
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
        """清理历史版本残留的失效键。

        站点改成「一域名一模块」后，下载目录各自落在 ``DATA/sites/<key>``，
        旧的统一键 ``sites.download_dir``（曾指向 ``DATA/sites|multisite``）
        如果留着，会让界面显示一个并不使用的目录，必须清掉。
        """
        cur = str(self._data.get("sites", {}).get("download_dir", ""))
        if cur:
            # 站点改为各自目录后，旧的统一下载目录键已无意义，清掉避免误导
            self._data.get("sites", {}).pop("download_dir", None)
        mq = str(self._data.get("madouqu", {}).get("download_dir", ""))
        if "sites/madouqu" in mq.replace("\\", "/") or "sites\\madouqu" in mq:
            self._data.setdefault("madouqu", {})["download_dir"] = str(
                paths.data_dir("madouqu", "downloads"))

        # 窗口基准从 1440×900 提到 1080P：旧配置里存着 1440×900（或用户早先拖出
        # 来的小尺寸），只改 DEFAULTS 不生效，因为读出来的是存盘值。按 layout 标记
        # 做一次性重置；重置后用户自己再拖尺寸仍会被 closeEvent 记住。
        win = self._data.setdefault("window", {})
        if str(win.get("layout", "")) != WINDOW_LAYOUT_TAG:
            win["width"] = WINDOW_DEFAULT_W
            win["height"] = WINDOW_DEFAULT_H
            win["layout"] = WINDOW_LAYOUT_TAG

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
