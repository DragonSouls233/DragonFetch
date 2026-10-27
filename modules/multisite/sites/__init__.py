# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""多站点接入层

把"只能下 PornHub"扩展成"能下 13 个站点"，同时保证 **PornHub 原有链路逐字节不变**。

模块划分
--------
- :mod:`modules.sites.registry`     站点注册表与 URL 识别（纯标准库，零依赖）
- :mod:`modules.sites.base`         数据模型 ``ResolvedVideo`` / ``SiteListItem``
- :mod:`modules.sites.service`      门面 ``SiteService``：下载链路唯一介入点
- :mod:`modules.sites.ytdlp_adapter`  通用站点：用 yt-dlp 展开列表
- :mod:`modules.sites.eaf_adapter`    兜底站点：用 ``unofficial-api-for-*`` 解析直链

三条路径
--------
============  ==========================  ==================================
站点          处理方式                     yt-dlp 有无 extractor
============  ==========================  ==================================
PornHub       **原有链路（本层不介入）**   有
其余 9 站     现有 yt-dlp 下载链路即可     有
porntrex      先用 unofficial-api 解析直链  **无**
xfreehd       先用 unofficial-api 解析直链  **无**
thumbzilla    先用 unofficial-api 解析直链  **无**
============  ==========================  ==================================

本包的 ``__init__`` 刻意保持**极轻**（不导入任何子模块），
理由同 ``modules.core.net``：``download_manager`` 会被测试以 importlib 单独加载，
重量级 ``__init__`` 会连带拖入无关依赖。请显式导入子模块::

    from multisite.sites.service import get_site_service
    from multisite.sites.registry import detect_site
"""
