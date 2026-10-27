# -*- coding: utf-8 -*-
"""持久化设置：站点地址、代理、并发等保存到本地 JSON，可随时改、可保存。

配置文件位置: <ROOT>/.hg_config.json
ROOT = G:\\黄果  （即 hg-downloader 的上两级目录）

本模块只依赖标准库，避免与 config 形成循环导入。
"""

import json
import os
from pathlib import Path

# 配置落到数据目录（默认 <模块>/DATA，前端用 DF_DATA 指向 <ROOT>/DATA/huangguo）
APP_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = Path(os.environ.get("DF_DATA") or (APP_DIR / "DATA"))
CONFIG_PATH = Path(os.environ.get("DF_CONFIG") or (_DATA_DIR / ".hg_config.json"))
ROOT_DIR = Path(os.environ.get("DF_ROOT") or APP_DIR)

DEFAULTS = {
    "site_id": "huangguoai",  # 上次生效的站点（区分两个黄果站点）
    "base_url": "https://huangguoai.com",
    "proxy_url": "",          # 空 = 直连；格式 http://host:port 或 socks5://host:port
    "concurrency": 3,         # 同时下载的剧集数
    "fragments": 12,          # m3u8 分片并发数

    # 下载加速
    "rate_limit": 0,          # 限速 KB/s，0 = 不限速
    "temp_dir": "",           # 暂存目录（SSD，合并在此完成），空 = 默认 <DATA>/tmp

    # 下载目录（空 = 默认 <项目根>/downloads）。改这里即可转移媒体库位置。
    "download_dir": "",
    # 是否把单集放进 "Season 01/" 子目录（Kodi/Emby/Jellyfin 标准结构）
    "season_folder": False,

    # 更新监测
    "update_check_enabled": True,     # 启动后自动检测已下载剧的新增集
    "update_check_interval_h": 6,     # 自动检测间隔（小时）
    "update_check_startup_delay": 30, # 启动后首次检测延迟（秒）

    # 通知
    "notify_desktop": True,           # 系统托盘通知
    "notify_webhook": "",             # 可选：POST JSON 到该 URL（企业微信/飞书/钉钉机器人）

    # 外盘同步（把下载目录里「近期更新」的剧集镜像到其它盘符）
    "sync_download_dir": "",          # 下载盘（备份盘）：增量镜像目标，空 = 不同步
    "sync_server_dir": "",            # 服务器运行盘：增量镜像目标，空 = 不同步
    "sync_fast_index": True,          # True = 用下载索引定位待同步文件（快）；False = 全盘扫描

    # 备用域名（镜像）：主域被墙时自动挑一个可达的；site_mirrors 为用户手动添加的列表
    "auto_pick_mirror": True,        # True = 启动/下载前自动选可达域名，跳过被屏蔽
    "auto_refresh_mirror": True,     # True = 现有域名全不可达时，自动读官方发布页更新到最新
    "site_mirrors": {},              # {site_id: [url, ...]} 用户添加的备用域名
}


def load() -> dict:
    """读取设置；文件不存在或损坏时返回默认。"""
    d = dict(DEFAULTS)
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                d.update({k: data[k] for k in DEFAULTS if k in data})
    except Exception:                 # noqa: BLE001 - 损坏配置回退默认
        pass
    return d


def save(d: dict) -> None:
    """写入设置到本地文件（与已有内容合并，避免丢失未提交的键）。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged = load()
    for k in DEFAULTS:
        if k in d:
            merged[k] = d[k]
    CONFIG_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
