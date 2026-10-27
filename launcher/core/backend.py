# -*- coding: utf-8 -*-
"""后端门面：在同一进程内安全加载 modules/ 下的四个后端。

为什么要「门面」而不是直接 import：
  * 各后端原本是独立工程，内部用 ``from core import ...`` / ``from modules import ...``
    这类**顶级包名**，两个工程同时导入会互相覆盖。合并时已把它们改名成唯一
    命名空间（``hgcore`` / ``psp``），这里统一注入 sys.path。
  * 糖心/黄果的目录、代理、下载路径由前端设置决定，必须在**首次 import 之前**
    写进环境变量，否则模块级常量会固化旧值。

所有函数都是惰性 + 线程安全；导入失败抛 ``BackendError``（带可读原因）。
"""
from __future__ import annotations

import importlib
import os
import sys
import threading
from types import SimpleNamespace

from . import paths
from .settings import settings


class BackendError(RuntimeError):
    """后端不可用（缺依赖、路径不对等），消息可直接展示给用户。"""


_LOCK = threading.RLock()
_CACHE: dict[str, SimpleNamespace] = {}
_prepared = False


def prepare() -> None:
    """注入环境变量与 sys.path（幂等）。"""
    global _prepared
    with _LOCK:
        if _prepared:
            return
        s = settings()
        paths.ensure_dirs()

        tx_data = paths.data_dir("tangxin")
        os.environ.update({
            "TX_ROOT": str(paths.ROOT),
            "TX_DATA": str(tx_data),
            "TX_TEMP": str(paths.temp_dir("tangxin")),
            "TX_BIN": str(paths.BIN),
            "TX_OUTPUT": str(s.get("tangxin.output_base") or (tx_data / "downloads")),
            "TX_SETTINGS": str(tx_data / "tangxin_settings.json"),
            "TX_PROXY": s.proxy_url if s.get("tangxin.proxy_enabled", True) else "",
            "DF_ROOT": str(paths.ROOT),
            "DF_BIN": str(paths.BIN),
            "DF_DATA_ROOT": str(paths.DATA),
            "DF_DATA": str(paths.data_dir("huangguo")),
            "DF_DOWNLOAD": str(s.get("huangguo.download_dir")
                               or paths.data_dir("huangguo", "downloads")),
        })

        for p in (paths.MODULES, paths.MODULES / "huangguo",
                  paths.MODULES / "multisite", paths.MODULES / "madouqu",
                  paths.MODULES / "yttiktok", paths.MODULES / "jiuse"):
            sp = str(p)
            if sp not in sys.path:
                sys.path.insert(0, sp)
        if not paths.MODULES.is_dir():
            raise BackendError(
                f"未找到后端代码目录：{paths.MODULES}\n"
                "打包版请把 modules 文件夹放在 exe 同级目录。")
        _prepared = True


def _imp(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        raise BackendError(f"无法加载后端模块 {name}：{exc}") from exc


# ------------------------------------------------------------------ 糖心
def tangxin() -> SimpleNamespace:
    prepare()
    with _LOCK:
        if "tangxin" not in _CACHE:
            _CACHE["tangxin"] = SimpleNamespace(
                config=_imp("tangxin.config"),
                settings=_imp("tangxin.settings"),
                db=_imp("tangxin.db"),
                models=_imp("tangxin.models"),
                crawler=_imp("tangxin.crawler"),
                downloader=_imp("tangxin.downloader"),
                existing=_imp("tangxin.existing"),
                ytdlp_engine=_imp("tangxin.ytdlp_engine"),
                layout=_imp("tangxin.layout"),
            )
    ns = _CACHE["tangxin"]
    apply_tangxin_settings(ns)
    return ns


def apply_tangxin_settings(ns: SimpleNamespace | None = None) -> SimpleNamespace:
    """把前端设置同步到糖心后端（线路/代理/输出目录/并发）。"""
    ns = ns or tangxin()
    s = settings()
    try:
        ns.settings.load(apply_to_config=True)
    except Exception:
        pass
    try:
        route = (s.get("tangxin.route_url") or "").strip()
        if not route:
            # 没有手选线路时，优先用导航页解析出的「科学线路」（实测可直连）
            try:
                from . import nav as _nav
                for e in _nav.find_entries("tangxin"):
                    route = e.get("science_base_url") or e.get("base_url") or ""
                    if route:
                        break
            except Exception:
                route = ""
        if route:
            ns.config.set_origin(route)
        cdn = (s.get("tangxin.cdn") or "").strip()
        if cdn:
            ns.config.set_cdn(cdn)
        ns.config.PROXY = s.proxy_url if s.get("tangxin.proxy_enabled", True) else ""
        out = (s.get("tangxin.output_base") or "").strip()
        if out:
            ns.config.OUTPUT_BASE = out
        ns.config.SEGMENT_WORKERS = int(s.get("tangxin.workers") or
                                        getattr(ns.config, "SEGMENT_WORKERS", 8))
    except Exception:
        pass
    return ns


# ------------------------------------------------------------------ 黄果
def huangguo() -> SimpleNamespace:
    prepare()
    with _LOCK:
        if "huangguo" not in _CACHE:
            _CACHE["huangguo"] = SimpleNamespace(
                config=_imp("hgcore.config"),
                settings=_imp("hgcore.settings"),
                library_db=_imp("hgcore.library_db"),
                site_api=_imp("hgcore.site_api"),
                models=_imp("hgcore.models"),
                task_queue=_imp("hgcore.task_queue"),
                mirror_refresh=_imp("hgcore.mirror_refresh"),
                sites=_imp("hgcore.sites"),
                nfo_writer=_imp("hgcore.nfo_writer"),
                applog=_imp("hgcore.applog"),
                sync_mgr=_imp("hgcore.sync_mgr"),
                update_check=_imp("hgcore.update_check"),
                library_scan=_imp("hgcore.library_scan"),
                media_move=_imp("hgcore.media_move"),
                site_db=_imp("hgcore.site_db"),
                site_discovery=_imp("hgcore.site_discovery"),
                downloader=_imp("hgcore.downloader"),
            )
    ns = _CACHE["huangguo"]
    apply_huangguo_settings(ns)
    return ns


def apply_huangguo_settings(ns: SimpleNamespace | None = None) -> SimpleNamespace:
    ns = ns or huangguo()
    s = settings()
    try:
        data = ns.settings.load()
        data["proxy_url"] = s.proxy_url if s.get("proxy.enabled", True) else ""
        data["concurrency"] = int(s.get("huangguo.concurrency") or data.get("concurrency") or 3)
        dl = (s.get("huangguo.download_dir") or "").strip()
        if dl:
            data["download_dir"] = dl
        sid = (s.get("huangguo.site_id") or "").strip()
        if sid:
            data["site_id"] = sid
        ns.config.apply_settings(data)
        ns.config.ensure_dirs()
        # 首次运行 / 空库时把表结构建好，否则查询会报 "no such table: dramas"
        ns.library_db.init_db()
    except Exception:
        pass
    return ns


def huangguo_site_meta() -> dict:
    """当前生效站点的展示信息（站点名 / 域名 / 代理状态）。"""
    ns = huangguo()
    cfg = ns.config
    site = getattr(cfg, "ACTIVE_SITE", {}) or {}
    return {
        "site_id": getattr(cfg, "ACTIVE_SITE_ID", "") or site.get("id", ""),
        "site_name": site.get("name", "") or site.get("id", ""),
        "base_url": getattr(cfg, "BASE_URL", ""),
        "mirrors": list(site.get("mirrors") or [])[:6],
    }


def huangguo_sync_targets() -> list:
    """外盘同步目标：优先取设置，其次自动识别移动硬盘。"""
    s = settings()
    out = []
    for role, key in (("download", "huangguo.sync_download"),
                      ("server", "huangguo.sync_server")):
        p = (s.get(key) or "").strip()
        if p:
            out.append({"role": role, "path": p})
    if not out:
        try:
            auto = huangguo().sync_mgr.detect_removable_target()
            if auto:
                out.append({"role": "download", "path": auto})
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ 多站点
def multisite() -> SimpleNamespace:
    """多站点（原 psp 精简版）：只保留通用站点解析，JAVDB / Madouqu 已拆分。"""
    prepare()
    with _LOCK:
        if "multisite" not in _CACHE:
            _CACHE["multisite"] = SimpleNamespace(
                registry=_imp("multisite.sites.registry"),
                service=_imp("multisite.sites.service"),
            )
    return _CACHE["multisite"]


def site_service():
    """取多站点解析服务单例（代理跟随设置，直连站点可显式置空）。"""
    s = settings()
    proxy = s.proxy_url if s.get("multisite.proxy_enabled", False) else ""
    return multisite().service.get_site_service(proxy=proxy or None)


def site_list() -> list:
    """给下拉框用的站点列表：``[{"key","label","homepage","ytdlp","native"}]``。

    剔除 ``native`` 站点（PornHub 归 PornSimilarityPlatform，本项目不展示）。
    """
    out = []
    for spec in multisite().registry.list_sites() or []:
        if getattr(spec, "native", False):
            continue
        out.append({
            "key": getattr(spec, "key", ""),
            "label": getattr(spec, "label", ""),
            "homepage": getattr(spec, "homepage", ""),
            "ytdlp": bool(getattr(spec, "ytdlp", False)),
        })
    return out


# ------------------------------------------------------------------ 麻豆
def madouqu() -> SimpleNamespace:
    """麻豆（磁力爬虫）：独立模块，自带 JSON 库。"""
    prepare()
    with _LOCK:
        if "madouqu" not in _CACHE:
            _CACHE["madouqu"] = SimpleNamespace(
                crawler=_imp("madouqu.core.crawler"),
                db=_imp("madouqu.core.database"),
                models=_imp("madouqu.core.models"),
            )
    return _CACHE["madouqu"]


def madouqu_db():
    """麻豆的库实例（物理目录 DATA/madouqu）。"""
    ns = madouqu()
    return ns.db.MadouquDatabase(db_dir=str(paths.data_dir("madouqu")))


def madouqu_crawler(use_selenium: bool = False):
    """构建麻豆爬虫：默认纯 requests（不依赖 selenium / chromedriver）。"""
    ns = madouqu()
    s = settings()
    cfg = {
        "base_url": (s.get("madouqu.base_url") or "").strip() or "https://madouqu.sbs",
        "use_selenium": bool(use_selenium),
        "timeout": int(s.get("madouqu.timeout") or 20),
        "max_retries": 2,
    }
    proxy = s.proxy_url if s.get("madouqu.proxy_enabled", True) else ""
    crawler = ns.crawler.MadouquCrawler(cfg)
    if proxy:
        try:
            crawler.session.proxies.update({"http": proxy, "https": proxy})
        except Exception:
            pass
    return crawler


# ------------------------------------------------------------------ 九色
def jiuse() -> SimpleNamespace:
    """九色：地址页（pages.dev）直连解析 + 站点解析 + 独立库。"""
    prepare()
    with _LOCK:
        if "jiuse" not in _CACHE:
            _CACHE["jiuse"] = SimpleNamespace(
                nav=_imp("jiuse.nav"),
                client=_imp("jiuse.client"),
                site=_imp("jiuse.site"),
                db=_imp("jiuse.db"),
            )
    return _CACHE["jiuse"]


def jiuse_nav_url() -> str:
    from .settings import settings as _s
    return (_s().get("jiuse.nav_url") or "").strip() or "https://dizhi222.pages.dev/"


# ------------------------------------------------------------------ 映取 / yt-dlp
def ytdlp() -> SimpleNamespace:
    prepare()
    with _LOCK:
        if "ytdlp" not in _CACHE:
            _CACHE["ytdlp"] = SimpleNamespace(
                service=_imp("yttiktok.core.ytdlp_service"),
                models=_imp("yttiktok.core.models"),
            )
    return _CACHE["ytdlp"]


def warm_up() -> list:
    """后台预热：把后端先导入好，避免首次切页时卡 2~4 秒。"""
    done = []
    for name in ("tangxin", "huangguo", "multisite", "madouqu", "jiuse", "ytdlp"):
        try:
            globals()[name]()
            done.append(name)
        except Exception:
            pass
    return done


def ytdlp_version() -> str:
    import subprocess
    exe = paths.bin_path("yt-dlp.exe")
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=10)
        return (out.stdout or "").strip() or "未知"
    except Exception:
        return "不可用"


# ------------------------------------------------------------------ 概览统计
def stats() -> dict:
    """首页/状态栏用的轻量统计（任何一项失败都不影响其他项）。"""
    out: dict = {}

    try:
        ns = tangxin()
        out["tangxin"] = {
            "records": ns.db.count_by_status(),
            "existing": ns.db.count_existing(),
            "origin": getattr(ns.config, "ORIGIN", ""),
        }
    except Exception as exc:  # noqa: BLE001
        out["tangxin"] = {"error": str(exc)}

    try:
        ns = huangguo()
        out["huangguo"] = {
            "dramas": ns.library_db.count_dramas(None),
            "site": huangguo_site_meta(),
            "downloads": len(ns.library_db.recent_downloads(limit=200)),
        }
    except Exception as exc:  # noqa: BLE001
        out["huangguo"] = {"error": str(exc)}

    try:
        out["sites"] = {"count": len(site_list()), "keys": [s["key"] for s in site_list()]}
    except Exception as exc:  # noqa: BLE001
        out["sites"] = {"error": str(exc)}

    try:
        db = madouqu_db()
        st = db.get_stats()
        out["madouqu"] = {
            "models": st.get("model_count", 0),
            "videos": st.get("video_count", 0),
            "magnets": st.get("magnet_count", 0),
            "base_url": (settings().get("madouqu.base_url") or ""),
        }
    except Exception as exc:  # noqa: BLE001
        out["madouqu"] = {"error": str(exc)}

    try:
        ns = jiuse()
        st = ns.nav.current()
        out["jiuse"] = {
            "addresses": len(st.get("addresses") or []),
            "active": st.get("active", ""),
            "updated_at": st.get("updated_at", 0),
        }
    except Exception as exc:  # noqa: BLE001
        out["jiuse"] = {"error": str(exc)}

    out["ytdlp"] = ytdlp_version()
    return out
