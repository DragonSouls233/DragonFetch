# -*- coding: utf-8 -*-
"""设置页：代理、目录、并发、外观、系统信息与维护动作。

所有值写入 ``DATA/app/settings.json``；主题切换即时生效（无需重启）。
"""
from __future__ import annotations

import os
import shutil
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ...core import backend, paths
from ...core import version as V
from ...core.settings import settings
from ...core.tasks import CENTER, Task
from .. import theme
from ..widgets import Card, HLine, open_path
from .base import Page


def _dir_row(line: QLineEdit, parent: QWidget) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(line, 1)
    btn = QPushButton("选择…")
    btn.setObjectName("Ghost")

    def pick() -> None:
        d = QFileDialog.getExistingDirectory(
            parent, "选择目录", line.text() or str(paths.ROOT))
        if d:
            line.setText(d)

    btn.clicked.connect(pick)
    lay.addWidget(btn)
    return w


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit != "GB" else f"{n / 1024:.1f} GB"
        n /= 1024
    return "-"


def _dir_size(p) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


class SettingsPage(Page):
    key = "settings"
    title = "设置"
    subtitle = "代理只用于读取导航页/发布页；站点与下载默认直连，可单独开启"

    def build(self) -> None:
        s = settings()

        grid = QGridLayout()
        grid.setSpacing(14)

        # ---------------- 代理
        proxy_card = Card()
        pl = QVBoxLayout(proxy_card)
        pl.setContentsMargins(16, 14, 16, 14)
        pl.setSpacing(8)
        head = QHBoxLayout()
        t = QLabel("代理（读导航页 / 发布页）")
        t.setObjectName("H2")
        head.addWidget(t)
        head.addStretch(1)
        self.chk_proxy = QCheckBox("启用代理")
        self.chk_proxy.setChecked(bool(s.get("proxy.enabled", True)))
        head.addWidget(self.chk_proxy)
        pl.addLayout(head)
        pl.addWidget(HLine())
        row = QHBoxLayout()
        self.ed_proxy = QLineEdit(s.get("proxy.url", ""))
        self.ed_proxy.setPlaceholderText("socks5h://127.0.0.1:10808")
        row.addWidget(self.ed_proxy, 1)
        self.btn_test = QPushButton("测试连通性")
        self.btn_test.clicked.connect(self.test_proxy)
        row.addWidget(self.btn_test)
        pl.addLayout(row)
        self.lbl_proxy_result = QLabel(
            "提示：导航页（daohang.pro）境内通常需要代理；糖心的科学线路一般可直连。")
        self.lbl_proxy_result.setObjectName("Muted")
        self.lbl_proxy_result.setWordWrap(True)
        pl.addWidget(self.lbl_proxy_result)
        grid.addWidget(proxy_card, 0, 0)

        # ---------------- 性能 / 外观
        perf = Card()
        rl = QVBoxLayout(perf)
        rl.setContentsMargins(16, 14, 16, 14)
        rl.setSpacing(8)
        t2 = QLabel("性能与外观")
        t2.setObjectName("H2")
        rl.addWidget(t2)
        rl.addWidget(HLine())
        form2 = QFormLayout()
        form2.setSpacing(8)
        self.sp_tx_seg = QSpinBox()
        self.sp_tx_seg.setRange(1, 32)
        self.sp_tx_seg.setValue(int(s.get("tangxin.workers", 8)))
        form2.addRow("糖心单部内分片并发", self.sp_tx_seg)
        self.sp_tx_par = QSpinBox()
        self.sp_tx_par.setRange(1, 8)
        self.sp_tx_par.setValue(int(s.get("tangxin.parallel", 3)))
        form2.addRow("糖心同时下载部数", self.sp_tx_par)
        self.sp_hg_conc = QSpinBox()
        self.sp_hg_conc.setRange(1, 16)
        self.sp_hg_conc.setValue(int(s.get("huangguo.concurrency", 3)))
        form2.addRow("黄果同时下载集数", self.sp_hg_conc)
        self.sp_yt_par = QSpinBox()
        self.sp_yt_par.setRange(1, 6)
        self.sp_yt_par.setValue(int(s.get("yttiktok.parallel", 2)))
        form2.addRow("映取同时下载条数", self.sp_yt_par)
        self.cmb_theme = QComboBox()
        self.cmb_theme.addItems(["深色", "浅色"])
        self.cmb_theme.setCurrentIndex(0 if s.get("theme", "dark") == "dark" else 1)
        form2.addRow("主题", self.cmb_theme)
        self.sp_font = QSpinBox()
        self.sp_font.setRange(8, 14)
        self.sp_font.setValue(int(s.get("ui.font_size", 10)))
        form2.addRow("界面字号（重启生效）", self.sp_font)
        rl.addLayout(form2)
        grid.addWidget(perf, 0, 1)

        # ---------------- 目录
        dirs_card = Card()
        dl = QVBoxLayout(dirs_card)
        dl.setContentsMargins(16, 14, 16, 14)
        dl.setSpacing(8)
        t3 = QLabel("下载目录")
        t3.setObjectName("H2")
        dl.addWidget(t3)
        dl.addWidget(HLine())
        form = QFormLayout()
        form.setSpacing(8)
        self.ed_tx = QLineEdit(s.get("tangxin.output_base", ""))
        form.addRow("糖心输出", _dir_row(self.ed_tx, self))
        self.ed_hg = QLineEdit(s.get("huangguo.download_dir", ""))
        form.addRow("黄果下载", _dir_row(self.ed_hg, self))
        self.ed_yt = QLineEdit(s.get("yttiktok.download_dir", ""))
        form.addRow("映取下载", _dir_row(self.ed_yt, self))
        self.ed_js = QLineEdit(s.get("jiuse.download_dir", ""))
        form.addRow("九色下载", _dir_row(self.ed_js, self))
        self.ed_mq = QLineEdit(s.get("madouqu.download_dir", ""))
        form.addRow("麻豆下载", _dir_row(self.ed_mq, self))
        dl.addLayout(form)
        grid.addWidget(dirs_card, 1, 0)

        # ---------------- 系统信息
        info = Card()
        il = QVBoxLayout(info)
        il.setContentsMargins(16, 14, 16, 14)
        il.setSpacing(8)
        head2 = QHBoxLayout()
        t4 = QLabel("系统信息")
        t4.setObjectName("H2")
        head2.addWidget(t4)
        head2.addStretch(1)
        btn_copy = QPushButton("复制诊断信息")
        btn_copy.setObjectName("Ghost")
        btn_copy.clicked.connect(self._copy_info)
        head2.addWidget(btn_copy)
        il.addLayout(head2)
        il.addWidget(HLine())
        self.lbl_info = QLabel("读取中…")
        self.lbl_info.setObjectName("Sub")
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        il.addWidget(self.lbl_info)
        row3 = QHBoxLayout()
        # 运行日志放在这里：页面里不再让日志占位置，需要看时再开独立窗口，
        # 且窗口是非模态的——开着日志照样能继续操作主界面。
        btn_runlog = QPushButton("运行日志")
        btn_runlog.setObjectName("Primary")
        btn_runlog.clicked.connect(self.open_log_window)
        row3.addWidget(btn_runlog)
        for text, target in (("DATA 目录", paths.DATA), ("日志目录", paths.LOGS),
                             ("TEMP 目录", paths.TEMP), ("安装目录", paths.ROOT)):
            b = QPushButton(text)
            b.setObjectName("Ghost")
            b.clicked.connect(lambda _c=False, p=target: open_path(str(p)))
            row3.addWidget(b)
        il.addLayout(row3)
        grid.addWidget(info, 1, 1)

        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.body.addLayout(grid)

        # ---------------- 操作
        actions = QHBoxLayout()
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setObjectName("Primary")
        self.btn_save.clicked.connect(self.save)
        btn_clean = QPushButton("清理 TEMP 缓存")
        btn_clean.setObjectName("Ghost")
        btn_clean.clicked.connect(self.clean_temp)
        btn_ver = QPushButton(f"v{V.APP_VERSION}")
        btn_ver.setObjectName("Ghost")
        btn_ver.clicked.connect(
            lambda: self.notify(f"{V.APP_TITLE} · build {V.APP_BUILD}", "info"))
        actions.addWidget(self.btn_save)
        actions.addWidget(btn_clean)
        actions.addStretch(1)
        actions.addWidget(btn_ver)
        self.body.addLayout(actions)
        self.body.addStretch(1)

    # -------------------------------------------------------------- 生命周期
    def on_show(self) -> None:
        self._refresh_info()

    def _refresh_info(self) -> None:
        d = paths.describe()
        dep = []
        for mod in ("PyQt6", "requests", "bs4", "lxml", "yaml", "cryptography",
                    "curl_cffi"):
            try:
                __import__(mod)
                dep.append(mod)
            except Exception:
                pass
        data_size = _dir_size(paths.DATA)
        temp_size = _dir_size(paths.TEMP)
        lines = [
            f"版本　　{V.APP_TITLE}　build {V.APP_BUILD}",
            f"运行模式{'打包版' if d['frozen'] else '源码版'}　Python {d['python']}",
            f"安装目录　{d['root']}",
            f"可执行　　{d['executable']}",
            f"数据占用　DATA {_human(data_size)}　TEMP {_human(temp_size)}",
            f"yt-dlp　　{backend.cached_ytdlp_version('检测中…')}",
            f"依赖　　　{', '.join(dep) or '（无）'}",
        ]
        self.lbl_info.setText("\n".join(lines))
        self._info_text = "\n".join(lines)
        # 版本号若还是占位，后台查一次再回填（主线程绝不再同步 spawn 子进程）。
        if not backend.cached_ytdlp_version(""):
            self._refresh_ytdlp_async()

    _ytdlp_task = None

    def _refresh_ytdlp_async(self) -> None:
        if self._ytdlp_task is not None and self._ytdlp_task.isRunning():
            return
        t = Task(backend.ytdlp_version)
        t.done.connect(lambda _v: self._refresh_info())
        t.failed.connect(lambda _tb: None)
        self._ytdlp_task = t
        CENTER.start(t)

    def _copy_info(self) -> None:
        QApplication.clipboard().setText(getattr(self, "_info_text", ""))
        self.notify("诊断信息已复制", "ok")

    # -------------------------------------------------------------- 保存
    def save(self) -> None:
        s = settings()
        s.update({
            "proxy": {"enabled": self.chk_proxy.isChecked(),
                      "url": self.ed_proxy.text().strip()},
            "tangxin": {
                "output_base": self.ed_tx.text().strip(),
                "workers": int(self.sp_tx_seg.value()),
                "parallel": int(self.sp_tx_par.value()),
            },
            "huangguo": {
                "download_dir": self.ed_hg.text().strip(),
                "concurrency": int(self.sp_hg_conc.value()),
            },
            "yttiktok": {
                "download_dir": self.ed_yt.text().strip(),
                "parallel": int(self.sp_yt_par.value()),
            },
            "jiuse": {"download_dir": self.ed_js.text().strip()},
            "madouqu": {"download_dir": self.ed_mq.text().strip()},
            "theme": "dark" if self.cmb_theme.currentIndex() == 0 else "light",
            "ui": {"font_size": int(self.sp_font.value())},
        })
        path = s.save()
        for d in (self.ed_tx.text(), self.ed_hg.text(), self.ed_yt.text(),
                  self.ed_js.text(), self.ed_mq.text()):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        # 主题即时生效
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.build_qss(dark=(s.get("theme") != "light")))
        win = self.window()
        if hasattr(win, "_refresh_side_status"):
            win._refresh_side_status()
        self.notify(f"已保存：{paths.rel(path)}", "ok")
        self._refresh_info()

    def open_log_window(self) -> None:
        """打开运行日志（非模态独立窗口，开着也能继续操作主界面）。"""
        win = self.window()
        if win is not None and win is not self and hasattr(win, "_open_log"):
            win._open_log()
            return
        # 兜底：页面单独运行时自己开一个
        try:
            from ..shell import LogDialog

            if getattr(self, "_log_win", None) is None:
                self._log_win = LogDialog(None)
            self._log_win.show()
            self._log_win.raise_()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"日志窗口打不开：{exc}", "err")

    def clean_temp(self) -> None:
        n = 0
        cutoff = time.time() - 7 * 86400
        for root, _dirs, files in os.walk(paths.TEMP):
            for f in files:
                p = os.path.join(root, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                        n += 1
                except OSError:
                    pass
        self.notify(f"已清理 {n} 个 7 天前的临时文件", "ok" if n else "info")
        self._refresh_info()

    # -------------------------------------------------------------- 代理
    def test_proxy(self) -> None:
        url = self.ed_proxy.text().strip()
        use = self.chk_proxy.isChecked()
        self.btn_test.setEnabled(False)
        self.lbl_proxy_result.setText("测试中…")

        def _run():
            import requests
            import urllib3
            urllib3.disable_warnings()
            proxies = {"http": url, "https": url} if (use and url) else None
            r = requests.get("https://daohang.pro/app.js", proxies=proxies,
                             timeout=15, verify=False,
                             headers={"User-Agent": "Mozilla/5.0"})
            body = r.content.decode("utf-8", "replace")
            ok = r.status_code == 200 and "name:" in body
            return ok, (f"HTTP {r.status_code} · {len(body)} 字节 · "
                        + ("链接表可读" if ok else "未取到链接表"))

        task = Task(_run)
        task.done.connect(self._proxy_done)
        task.failed.connect(lambda tb: (self.btn_test.setEnabled(True),
                                        self.lbl_proxy_result.setText(
                                            "失败：" + tb.splitlines()[-1][:140])))
        CENTER.start(task)

    def _proxy_done(self, result) -> None:
        self.btn_test.setEnabled(True)
        ok, text = result
        self.lbl_proxy_result.setText(("✓ " if ok else "⚠ ") + text)
        self.notify(text, "ok" if ok else "warn")
