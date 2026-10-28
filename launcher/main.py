# -*- coding: utf-8 -*-
"""DragonFetch 入口：一个进程、一个窗口，装载全部模块页面。

源码运行：``python -m launcher.main``（或双击 启动.bat）
打包运行：``DragonFetch.exe``（需要同级的 modules / bin / DATA 目录）
"""
from __future__ import annotations

import sys
import time
import traceback


def _icon_path():
    """图标：源码在 <ROOT>/assets，打包后在解包目录 assets。"""
    from pathlib import Path
    from launcher.core import paths

    cands = [paths.ROOT / "assets" / "icon.ico"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(Path(meipass) / "assets" / "icon.ico")
    for p in cands:
        try:
            if p.exists():
                return str(p)
        except OSError:
            continue
    return ""


def _install_excepthook() -> None:
    """槽函数里的未捕获异常：写日志 + 提示，**不要**让 Qt 直接 abort 整个程序。

    PyQt6 在 ``sys.excepthook`` 为默认实现时，会把 Python 槽异常当致命错误
    （进程直接退出）。桌面应用不该因为一个统计卡算错数就闪退，所以这里接管：
    完整堆栈落 ``logs/ui-error.log``，界面只弹一条提示，其余功能照常可用。
    """
    from launcher.core import paths
    from launcher.core.logbus import LOG

    def _hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            p = paths.LOGS / "ui-error.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{text}")
        except Exception:
            pass
        try:
            LOG.error(f"界面异常（已拦截，程序继续）：{text.strip().splitlines()[-1]}")
        except Exception:
            print(text, file=sys.stderr)

    sys.excepthook = _hook


def _fatal(message: str) -> int:
    """无 GUI 时的兜底报错（打包漏文件最常见）。"""
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "DragonFetch 启动失败", message)
    except Exception:
        print("DragonFetch 启动失败：\n" + message, file=sys.stderr)
    return 2


def _selftest() -> int:
    """无界面自检：验证打包后的路径 / 后端 / bin 是否齐全（打包验证用）。

    用法：``DragonFetch.exe --selftest``
    结果写入 ``logs/selftest.txt``（无控制台时看这个文件）。
    """
    from launcher.core import backend, paths
    from launcher.core import version as V

    paths.ensure_dirs()
    lines = [f"{V.APP_TITLE} build {V.APP_BUILD} 自检"]
    ok = True
    d = paths.describe()
    lines.append(f"  模式      : {'打包版' if d['frozen'] else '源码版'}")
    lines.append(f"  ROOT      : {d['root']}")
    lines.append(f"  modules   : {d['modules']}  {'OK' if paths.MODULES.is_dir() else '缺失'}")
    lines.append(f"  bin       : {d['bin']}  {'OK' if paths.BIN.is_dir() else '缺失'}")

    for name in ("tangxin", "huangguo", "multisite", "madouqu", "jiuse", "ytdlp"):
        try:
            getattr(backend, name)()
            lines.append(f"  backend {name:9s}: OK")
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"  backend {name:9s}: FAIL {type(exc).__name__}: {exc}")

    try:
        mods = backend.site_list()
        lines.append(f"  站点模块  : {len(mods)} 个（"
                     + "/".join(m["key"] for m in mods[:4]) + " …）")
        if not mods:
            ok = False
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(f"  站点模块  : FAIL {type(exc).__name__}: {exc}")

    try:
        ver = backend.ytdlp_version()
        lines.append(f"  yt-dlp    : {ver}")
        ok = ok and ver not in ("不可用", "未知", "")
    except Exception as exc:  # noqa: BLE001
        ok = False
        lines.append(f"  yt-dlp    : FAIL {exc}")

    for name in ("ffmpeg.exe", "ffprobe.exe"):
        p = paths.BIN / name
        lines.append(f"  {name:10s}: {'OK' if p.exists() else '缺失'}")

    try:
        from launcher.core import dbstore
        lines.append(f"  本地库    : {len(dbstore.stores())} 个")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  本地库    : FAIL {exc}")

    # 地址同步状态（离线读取上次结果，不联网，避免自检被网络拖住）
    try:
        from launcher.core import navsync
        rows = navsync.summary()
        if not navsync.state().get("at"):
            # 全新数据目录还没同步过，属于正常状态，不算自检失败
            lines.append("  地址同步  : 尚未执行（首次启动会自动同步一次）")
            rows = []
        else:
            ok_n = sum(1 for r in rows if r["ok"])
            direct_n = sum(1 for r in rows if r["ok"] and r["direct"])
            lines.append(f"  地址同步  : {ok_n}/{len(rows)} 可用 · 直连 {direct_n}"
                         f"（{navsync.last_run_text().split('· ')[-1]}）")
        for r in rows:
            lines.append(f"    · {r['label']:10s} {(r['url'] or '未同步')[:44]:46s}"
                         + ("直连" if r["ok"] and r["direct"]
                            else ("需代理" if r["ok"] else "失败")))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  地址同步  : FAIL {exc}")

    lines.append("结果      : " + ("通过" if ok else "失败"))
    text = "\n".join(lines)
    out = paths.LOGS / "selftest.txt"
    try:
        out.write_text(text, encoding="utf-8")
    except OSError:
        pass
    try:
        print(text)
    except Exception:
        pass
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv or "--self-test" in sys.argv:
        return _selftest()
    if "--version" in sys.argv:
        from launcher.core import version as _v
        print(_v.VERSION_TEXT)
        return 0

    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtGui import QFont, QIcon

    from launcher.core import paths
    from launcher.core import version as V
    from launcher.core.settings import settings
    from launcher.gui import theme

    # 源码运行时把仓库根放进 sys.path；打包版模块已在 bundle 内，无需处理。
    if not paths.FROZEN:
        root = str(paths.ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)

    paths.ensure_dirs()
    _install_excepthook()

    if not paths.MODULES.is_dir():
        return _fatal(
            f"未找到后端代码目录：\n{paths.MODULES}\n\n"
            "如果是绿色版/打包版，请保持 exe 与 modules、bin、DATA 同级。")

    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName(V.APP_NAME)
    app.setApplicationDisplayName(V.APP_NAME)
    app.setApplicationVersion(V.APP_VERSION)
    app.setStyle("Fusion")

    s = settings()
    size = int(s.get("ui.font_size", 10) or 10)
    app.setFont(QFont("Microsoft YaHei UI", size))
    app.setStyleSheet(theme.build_qss(dark=(s.get("theme", "dark") != "light")))
    ico = _icon_path()
    if ico:
        app.setWindowIcon(QIcon(ico))

    from launcher.gui.shell import MainWindow
    win = MainWindow()
    if ico:
        win.setWindowIcon(QIcon(ico))
    win.show()

    # 后台预热后端（不阻塞界面）
    from PyQt6.QtCore import QTimer
    from launcher.core import backend as _backend
    from launcher.core.tasks import CENTER, Task
    QTimer.singleShot(300, lambda: CENTER.start(Task(_backend.warm_up)))

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
