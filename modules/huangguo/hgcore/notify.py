# -*- coding: utf-8 -*-
"""通知层：把「新增集 / 下载完成 / 出错」等事件推送给用户。

设计：core 层不依赖 Qt，通过**注册回调（sink）**解耦。
  - UI 启动时调用 notify.register_sink(...) 把系统托盘通知接进来
  - 另有内置 Webhook sink（读设置里的 notify_webhook），可直接推到
    企业微信 / 飞书 / 钉钉 / Bark / ntfy 等，无需 UI

用法:
    from hgcore import notify
    notify.notify("发现更新", "《XXX》新增 2 集", kind="update",
                  payload={"site_id": "huangguoai", "did": 117, "eps": [37, 38]})
"""

import json
import threading
import time
import urllib.request
from typing import Callable, Dict, List, Optional

# sink 签名: fn(title: str, message: str, kind: str, payload: dict) -> None
_sinks: List[Callable] = []
_lock = threading.Lock()

# 事件类型（用于 UI 过滤与图标选择）
KIND_INFO = "info"
KIND_UPDATE = "update"        # 发现新增集
KIND_DONE = "done"            # 下载完成
KIND_ERROR = "error"          # 出错


def register_sink(fn: Callable) -> None:
    """注册一个通知出口（重复注册同一函数会被忽略）。"""
    with _lock:
        if fn not in _sinks:
            _sinks.append(fn)


def unregister_sink(fn: Callable) -> None:
    with _lock:
        if fn in _sinks:
            _sinks.remove(fn)


def notify(title: str, message: str = "", kind: str = KIND_INFO,
           payload: Optional[Dict] = None, webhook: bool = True) -> None:
    """同步分发通知到所有出口（各 sink 需自行保证不阻塞，见 notify_async）。"""
    payload = payload or {}
    with _lock:
        sinks = list(_sinks)
    for fn in sinks:
        try:
            fn(title, message, kind, payload)
        except Exception:                    # noqa: BLE001 - 单个 sink 失败不影响其他
            pass
    if webhook:
        _post_webhook(title, message, kind)


def notify_async(title: str, message: str = "", kind: str = KIND_INFO,
                 payload: Optional[Dict] = None) -> None:
    """后台线程分发（Webhook 有网络 IO，避免阻塞调用方/UI 线程）。"""
    threading.Thread(
        target=notify, args=(title, message, kind, payload),
        daemon=True, name="notify",
    ).start()


# ---------------------------------------------------------------- Webhook

def _post_webhook(title: str, message: str, kind: str) -> None:
    """把通知 POST 到设置里配置的 Webhook（若已配置）。

    发送 JSON: {"msgtype":"text","text":{"content":"..."}} —— 兼容
    企业微信 / 钉钉 / 飞书 群机器人最简格式；同时附带通用字段便于自建服务解析。
    """
    try:
        from . import settings
        url = (settings.load().get("notify_webhook") or "").strip()
    except Exception:                        # noqa: BLE001
        return
    if not url or not url.startswith(("http://", "https://")):
        return

    content = f"{title}\n{message}".strip()
    body = {
        "msgtype": "text",
        "text": {"content": content},
        # 通用字段（自建服务可用）
        "title": title, "message": message, "kind": kind,
        "ts": int(time.time()),
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:                        # noqa: BLE001 - 推送失败静默
        pass
