# -*- coding: utf-8 -*-
"""PyInstaller 运行时钩子：在 GUI 起来之前把环境准备好。

* 关闭 Qt 的高 DPI 旧缩放行为，避免 125% 缩放下字体发虚；
* 让 Qt 不写注册表、不依赖系统主题；
* 把 exe 所在目录加入 sys.path（后端 modules 以源码形式放在旁边）。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

if getattr(sys, "frozen", False):
    root = Path(sys.executable).resolve().parent
    os.environ.setdefault("DF_ROOT", str(root))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
