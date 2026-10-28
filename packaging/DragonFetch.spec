# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir）。

设计取舍
--------
* **onedir 而不是 onefile**：启动快、杀软误报少、模块/数据可见可修。
* **后端不进 bundle**：``modules/`` 以源码形式放在 exe 旁边，运行时由
  ``launcher.core.backend`` 注入 sys.path 后 import。好处是
  ① 动态 import 不用逐个写 hiddenimports；
  ② 用户/我们改一行后端代码不必重新打包；
  ③ 与「BIN / 代码 / DATA 分离」的目录约定一致。
* 因此 hiddenimports 只列**后端会用到的第三方库**（它们必须真的进 bundle）。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PROJ = Path(SPECPATH).parent                  # SPECPATH = packaging/，上一级 = 项目根

block_cipher = None

# ---- 后端用到的第三方依赖（必须打进 bundle）----
THIRD_PARTY = [
    "requests", "urllib3", "certifi", "charset_normalizer", "idna",
    "bs4", "lxml", "lxml.etree", "lxml._elementpath", "html5lib", "soupsieve",
    "yaml", "dateutil", "dateutil.parser",
    "cryptography",
    "Crypto", "Crypto.Cipher", "Crypto.Cipher.AES", "Crypto.Util.Padding",
    "curl_cffi", "curl_cffi.requests",
    "psutil", "aiohttp", "aiofiles", "m3u8",
    "win32api", "win32file", "win32con",
]

hidden = list(THIRD_PARTY)
for pkg in ("bs4", "lxml", "cryptography"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass
hidden = sorted(set(hidden))

datas = []
icon = PROJ / "assets" / "icon.ico"
if icon.exists():
    datas.append((str(icon), "assets"))
ver = PROJ / "packaging" / "version_info.txt"

a = Analysis(
    [str(PROJ / "launcher" / "main.py")],
    pathex=[str(PROJ)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(PROJ / "packaging" / "runtime_hook.py")],
    excludes=[
        "tkinter", "PyQt5", "PySide2", "PySide6",
        "matplotlib", "pandas", "numpy", "scipy", "IPython",
        "pytest", "unittest", "pydoc_data", "test",
        "notebook", "jupyter", "sqlalchemy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DragonFetch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(icon) if icon.exists() else None,
    version=str(ver) if ver.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DragonFetch",
)
