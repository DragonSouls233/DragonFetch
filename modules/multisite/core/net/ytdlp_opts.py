# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""yt-dlp **库模式** 参数校正

为什么需要它
------------
``impersonate`` 这个选项在 **命令行** 和 **Python 库** 两种用法下接受的类型不同：

===============  ==========================  ================================
用法             接受的取值                   说明
===============  ==========================  ================================
命令行           字符串（``--impersonate chrome``）  由 CLI 的 argparse 归一化
Python 库 API     ``ImpersonateTarget`` 实例    **字符串和 True 都不行**
===============  ==========================  ================================

实测（yt-dlp 2026.06.09）：

.. code-block:: text

    yt_dlp.YoutubeDL({'impersonate': True})                -> AssertionError
    yt_dlp.YoutubeDL({'impersonate': 'chrome'})            -> AssertionError
    yt_dlp.YoutubeDL({'impersonate': ImpersonateTarget('chrome')})  -> OK

原因在 ``YoutubeDL.__init__``：

.. code-block:: python

    if impersonate_target := self.params.get('impersonate'):
        if not self._impersonate_target_available(impersonate_target):
            raise YoutubeDLError(...)

而 ``_impersonate_target_available`` 内部是
``assert isinstance(target, ImpersonateTarget)`` —— 传字符串/True 直接断言失败。
同文件里的 ``_parse_impersonate_targets()``（能把字符串转成对象）**在该版本中
从未被 ``__init__`` 调用**，属于死代码；归一化只发生在 CLI 入口。

为什么这个 bug 特别难查
----------------------
``AssertionError`` 的 ``str()`` 是**空字符串**。于是日志里只会看到

.. code-block:: text

    yt-dlp 库模式下载失败: <url> -

后面什么都没有。本项目 2026-09-19 的日志里出现过大量「错误输出为空」的记录，
这类空消息异常就是同一个指纹。

本模块提供 :func:`apply_impersonate`，把这件事收敛成一处：
**先转成对象再写入**，转不了就干脆不写（降级为不使用指纹），
绝不写入一个会当场炸掉的值。
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["resolve_impersonate", "apply_impersonate", "is_target_available",
           "describe_target"]

# 本进程内对"目标是否可用"的判定缓存，避免每次下载都起一个 YoutubeDL 去试
_AVAILABILITY_CACHE: Dict[str, bool] = {}


def resolve_impersonate(target: Any = "chrome") -> Optional[Any]:
    """把任意形态的 ``impersonate`` 取值转成库模式接受的 ``ImpersonateTarget``。

    Args:
        target: 可以是 ``ImpersonateTarget``、字符串（``"chrome"``）、
            ``True``/``""``（表示"任选一个可用目标"）、``None``。

    Returns:
        ``ImpersonateTarget`` 实例；yt-dlp 不可用或取值非法时返回 ``None``
        （调用方据此降级，不要抛异常）。
    """
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
    except Exception as exc:  # pragma: no cover - yt-dlp 未安装
        logger.debug("yt-dlp 不可用，无法解析 impersonate: %s", exc)
        return None

    if target is None or target is False:
        return None

    if isinstance(target, ImpersonateTarget):
        return target

    if target is True or target == "":
        try:
            return ImpersonateTarget()
        except Exception:
            return None

    try:
        return ImpersonateTarget.from_str(str(target))
    except Exception as exc:
        logger.warning("impersonate 取值无法解析: %r (%s)", target, exc)
        return None


def apply_impersonate(opts: Dict[str, Any], target: Any = "chrome") -> bool:
    """把 ``impersonate`` 以**安全形态**写进 ``ydl_opts``。

    这是本模块唯一推荐的使用方式——直接写 ``opts['impersonate'] = 'chrome'``
    会让 ``YoutubeDL()`` 构造阶段抛空消息断言。

    Args:
        opts: 传给 ``yt_dlp.YoutubeDL`` 的参数字典（原地修改）。
        target: 同 :func:`resolve_impersonate`。

    Returns:
        ``True`` 表示已写入；``False`` 表示目标不可用、**未写入**
        （调用方会失去 TLS 指纹伪装能力，但至少不会崩）。
    """
    resolved = resolve_impersonate(target)
    if resolved is None:
        opts.pop("impersonate", None)
        logger.warning(
            "未能启用 TLS 指纹伪装（impersonate=%r）：yt-dlp/curl_cffi 不可用或取值非法。"
            "缺少指纹可能导致部分站点返回 410/403。", target)
        return False

    # ⚠️ 光能构造出 ImpersonateTarget 还不够：``from_str`` 只做字符串切分，
    # 随便传个对象都会"成功"造出一个 client 名荒谬的目标，然后到
    # YoutubeDL 里才炸。必须在写入前真正验一次可用性（结果带缓存，不重复开销）。
    if not is_target_available(target):
        opts.pop("impersonate", None)
        logger.warning(
            "TLS 指纹目标 %r 不可用（缺少 curl_cffi 或对应指纹数据），本次不使用指纹伪装。"
            "部分站点可能因此返回 410/403。", target)
        return False

    opts["impersonate"] = resolved
    return True


def is_target_available(target: Any = "chrome") -> bool:
    """真起一个 ``YoutubeDL`` 验证目标是否可用（需要 curl_cffi + 对应指纹数据）。

    比只看能否构造出 ``ImpersonateTarget`` 更严格：构造对象不依赖 curl_cffi，
    而真正发包时要。
    """
    key = str(target)
    if key in _AVAILABILITY_CACHE:
        return _AVAILABILITY_CACHE[key]

    resolved = resolve_impersonate(target)
    if resolved is None:
        _AVAILABILITY_CACHE[key] = False
        return False

    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                               "impersonate": resolved}):
            pass
        _AVAILABILITY_CACHE[key] = True
    except Exception as exc:
        logger.info("impersonate 目标 %r 不可用: %s", target, exc)
        _AVAILABILITY_CACHE[key] = False
    return _AVAILABILITY_CACHE[key]


def describe_target(target: Any = "chrome") -> Dict[str, Any]:
    """返回诊断信息，供验证脚本打印（不参与业务逻辑）。"""
    resolved = resolve_impersonate(target)
    return {
        "input": repr(target),
        "resolved_type": type(resolved).__name__ if resolved is not None else None,
        "resolved": str(resolved) if resolved is not None else None,
        "available": is_target_available(target),
    }
