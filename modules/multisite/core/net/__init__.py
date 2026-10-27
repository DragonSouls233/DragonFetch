# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""网络层增强模块

集中放置与"下载失败治理"相关的网络组件：

- ``error_classifier``  下载错误的语义分类（决定该不该换代理 / 该不该重试）
- ``proxy_probe``       代理健康探测（换节点前的最后一道确认）
- ``sni_fragment_proxy``  SNI 分片本地代理（绕过 SNI 阻断的第二条正交路径）
- ``sni_proxy_service``  SNI 分片代理的后台线程生命周期封装（读配置、启停）

本包的 ``__init__`` 刻意保持轻量：**不做任何子模块的导入**。
因为 ``modules.core.download_manager`` 会依赖本包，而 ``download_manager``
被大量测试以 importlib 方式单独加载，重量级 ``__init__``（例如
``modules.core.utils.__init__`` 会连带导入 yaml / 配置文件）会把不相关
依赖拖进来，导致测试环境无谓地失败。
子模块请按需显式导入，例如::

    from multisite.core.net.error_classifier import classify_error
"""

__all__ = [
    'error_classifier',
    'proxy_probe',
    'sni_fragment_proxy',
    'sni_proxy_service',
]
