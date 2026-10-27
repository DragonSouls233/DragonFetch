# -*- coding: utf-8 -*-
"""站点模块注册表：一个域名一个模块，彼此独立。

新增站点 = 新建 ``modules/sites/<key>/__init__.py``（里面一个 ``SITE``），
再在本文件加一行 import；不需要改任何其他站点。
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .base import SiteDB, SiteModule
from . import xvideos as _xvideos
from . import xnxx as _xnxx
from . import eporner as _eporner
from . import xhamster as _xhamster
from . import spankbang as _spankbang
from . import youporn as _youporn
from . import beeg as _beeg
from . import redtube as _redtube
from . import tube8 as _tube8
from . import porntrex as _porntrex
from . import xfreehd as _xfreehd
from . import thumbzilla as _thumbzilla

# 界面里的展示顺序（综合片库 → 需要站点包解析的站点）
ORDER = (
    "xvideos",
    "xnxx",
    "eporner",
    "xhamster",
    "spankbang",
    "youporn",
    "beeg",
    "redtube",
    "tube8",
    "porntrex",
    "xfreehd",
    "thumbzilla",
)

MODULES: Dict[str, SiteModule] = {
    "xvideos": _xvideos.SITE,
    "xnxx": _xnxx.SITE,
    "eporner": _eporner.SITE,
    "xhamster": _xhamster.SITE,
    "spankbang": _spankbang.SITE,
    "youporn": _youporn.SITE,
    "beeg": _beeg.SITE,
    "redtube": _redtube.SITE,
    "tube8": _tube8.SITE,
    "porntrex": _porntrex.SITE,
    "xfreehd": _xfreehd.SITE,
    "thumbzilla": _thumbzilla.SITE,
}


__all__ = ["MODULES", "ORDER", "SiteModule", "SiteDB", "get", "all", "keys"]


def get(key: str) -> Optional[SiteModule]:
    """按 key 取站点模块；不存在返回 None。"""
    return MODULES.get((key or "").strip())


def all() -> List[SiteModule]:
    """按界面顺序返回全部站点模块。"""
    return [MODULES[k] for k in ORDER if k in MODULES]


def keys() -> List[str]:
    return [m.key for m in all()]
