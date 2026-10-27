# -*- coding: utf-8 -*-
"""用户设置持久化（输出目录 / 临时目录 / 代理 / 并发 / 模式）。

为什么需要：GUI 里选好的下载目录原先只存在于内存，重启就退回默认值
（桌面 = 项目/downloads，服务器 = E:\\TangXinVlog\\downloads）。
这里把它落成 JSON，启动时自动回填 config。

存储位置：``{RUNTIME_ROOT}/data/tangxin_settings.json``
（与数据库同目录；打包后 = exe 同级的 data/ 目录，不会落进临时解压目录）。
老版本遗留在根目录的设置文件会自动迁移过来（见 layout.py）。
"""
import json
import os
import threading

from . import config

_lock = threading.Lock()

_KEYS = ("output_base", "temp_dir", "proxy", "workers", "mode",
         "cdn_use_proxy", "get_cover", "known_dirs", "copy_existing",
         "origin", "cdn")


def get_settings_path() -> str:
    env = os.environ.get("TX_SETTINGS")
    if env:
        return env
    try:
        from . import layout
        return layout.data_file("tangxin_settings.json")
    except Exception:
        return os.path.join(config.DATA_DIR, "tangxin_settings.json")


def defaults() -> dict:
    """当前 config 里的值即默认设置。"""
    return {
        "output_base": config.OUTPUT_BASE,
        "temp_dir": config.TEMP_DIR,
        "proxy": config.PROXY,
        "workers": config.SEGMENT_WORKERS,
        "mode": config.DOWNLOAD_MODE,
        "cdn_use_proxy": config.CDN_USE_PROXY,
        "get_cover": True,
        # 已知下载根目录（跨根目录去重，见 existing.py）
        "known_dirs": list(getattr(config, "KNOWN_DIRS", []) or []),
        "copy_existing": bool(getattr(config, "COPY_EXISTING", False)),
        # 站点主域 / CDN（由发布页同步写入，见 publish_page.py）
        "origin": config.ORIGIN,
        "cdn": config.CDN,
    }


def load(apply_to_config: bool = True) -> dict:
    """读取设置文件；缺失/损坏时回落到默认值，且**不会**抛异常。"""
    data = defaults()
    path = get_settings_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k in _KEYS:
                    if k in saved and saved[k] not in (None, ""):
                        data[k] = saved[k]
    except Exception:
        # 设置文件损坏不应阻断启动 —— 用默认值继续
        return defaults()

    if apply_to_config:
        apply(data)
    return data


def apply(data: dict) -> None:
    """把设置写回 config（运行期生效）。"""
    if not data:
        return
    if data.get("output_base"):
        config.OUTPUT_BASE = data["output_base"]
    if data.get("temp_dir"):
        config.TEMP_DIR = data["temp_dir"]
    if data.get("proxy"):
        config.PROXY = data["proxy"]
    if data.get("workers"):
        try:
            config.SEGMENT_WORKERS = int(data["workers"])
        except (TypeError, ValueError):
            pass
    if data.get("mode") in ("python", "ffmpeg"):
        config.DOWNLOAD_MODE = data["mode"]
    if "cdn_use_proxy" in data:
        config.CDN_USE_PROXY = bool(data["cdn_use_proxy"])
    if "get_cover" in data:
        config.GET_COVER = bool(data["get_cover"])
    # 已下载识别（跨根目录去重）：只写 config。
    # ⚠️ 这里**不要**去 configure 索引单例：get_index() 会自动把 OUTPUT_BASE
    #    也并进去，两边列表不一致会触发"配置变更→清库重扫"，导致每次启动
    #    都把整个 NAS 重扫一遍（真机踩到）。索引配置统一走
    #    existing.apply_known_dirs()，由 GUI/CLI 在用户真正改动时调用。
    if "known_dirs" in data:
        config.KNOWN_DIRS = [d for d in (data.get("known_dirs") or []) if d]
    if "copy_existing" in data:
        config.COPY_EXISTING = bool(data["copy_existing"])
    # 站点主域 / CDN：必须走 set_*（REFERER 是 ORIGIN 的派生值，
    # 直接赋 ORIGIN 会让请求头带旧域名的 Referer → 站点侧可能 403）
    if data.get("origin") and data["origin"] != config.ORIGIN:
        config.set_origin(data["origin"])
    if data.get("cdn") and data["cdn"] != config.CDN:
        config.set_cdn(data["cdn"])


def save(**overrides) -> str:
    """把当前 config 的值（可被 overrides 覆盖）写入设置文件，返回路径。"""
    data = {
        "output_base": config.OUTPUT_BASE,
        "temp_dir": config.TEMP_DIR,
        "proxy": config.PROXY,
        "workers": config.SEGMENT_WORKERS,
        "mode": config.DOWNLOAD_MODE,
        "cdn_use_proxy": config.CDN_USE_PROXY,
        "get_cover": getattr(config, "GET_COVER", True),
        "known_dirs": list(getattr(config, "KNOWN_DIRS", []) or []),
        "copy_existing": bool(getattr(config, "COPY_EXISTING", False)),
        # 站点主域 / CDN（由发布页同步写入，见 publish_page.py）
        "origin": config.ORIGIN,
        "cdn": config.CDN,
    }
    # 列表类字段允许显式传空列表清空
    for k in ("known_dirs",):
        if k in overrides and overrides[k] == []:
            data[k] = []
    data.update({k: v for k, v in overrides.items() if v not in (None, "")})

    path = get_settings_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    return path
