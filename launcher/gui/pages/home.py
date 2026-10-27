# -*- coding: utf-8 -*-
"""总览页：一屏看清四个模块的状态与入口。"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ...core import backend, nav, paths
from ...core.tasks import CENTER, Task
from ..widgets import Badge, Card, HLine, StatCard
from .base import Page


class HomePage(Page):
    key = "home"
    title = "总览"
    subtitle = "四个后端模块已合并到同一进程，数据统一落在 DATA 目录"

    def build(self) -> None:
        self._stats_cards = {}
        refresh = QPushButton("刷新状态")
        refresh.clicked.connect(self.reload)
        self.header.add_action(refresh)

        banner = Card()
        bl = QHBoxLayout(banner)
        bl.setContentsMargins(18, 14, 18, 14)
        self.lbl_nav = QLabel("导航页：尚未读取")
        self.lbl_nav.setObjectName("Sub")
        bl.addWidget(self.lbl_nav, 1)
        btn_nav = QPushButton("刷新导航页")
        btn_nav.setObjectName("Primary")
        btn_nav.clicked.connect(self.refresh_nav)
        btn_tx = QPushButton("进入糖心")
        btn_tx.setObjectName("Ghost")
        btn_tx.clicked.connect(lambda: self.switch_page("tangxin"))
        bl.addWidget(btn_nav)
        bl.addWidget(btn_tx)
        self.body.addWidget(banner)

        grid = QGridLayout()
        grid.setSpacing(12)
        specs = [
            ("tangxin_records", "糖心 · 下载记录"),
            ("tangxin_files", "糖心 · 已下载文件"),
            ("huangguo_dramas", "黄果 · 剧库"),
            ("sites_count", "站点 · 模块数"),
        ]
        for idx, (key, label) in enumerate(specs):
            card = StatCard(label, "-", "读取中…")
            self._stats_cards[key] = card
            grid.addWidget(card, 0, idx)
        self.body.addLayout(grid)

        modules = Card()
        ml = QVBoxLayout(modules)
        ml.setContentsMargins(18, 16, 18, 16)
        ml.setSpacing(10)
        title = QLabel("模块入口")
        title.setObjectName("H2")
        ml.addWidget(title)
        ml.addWidget(HLine())
        rows = [
            ("糖心 Vlog", "演员云 → 作品库 → 直链下载；导航页用代理读，站点可直连", "tangxin"),
            ("黄果短剧", "剧库浏览 → 选集 → 并发队列下载；镜像域名由发布页刷新", "huangguo"),
            ("九色视频", "地址页取最新入口 → 线路检测 → 直连解析下载（不挂代理）", "jiuse"),
            ("站点 · 独立模块", "每个域名一个模块一个页面：解析直链 / 抓取列表 / 搜索（见左侧「站点」）",
             "site:xvideos"),
            ("麻豆磁力", "独立模块：按关键词抓 BT 磁力，可入库、导出", "madouqu"),
            ("映取 · YT/TikTok", "YouTube / TikTok / 通用站点，基于共享 yt-dlp", "yttiktok"),
            ("导航页 · 黄视频", "daohang.pro 入口表 + 黄视频站点解析下载", "nav"),
        ]
        for name, desc, key in rows:
            row = QHBoxLayout()
            row.setSpacing(12)
            lbl = QLabel(name)
            lbl.setFixedWidth(140)
            lbl.setStyleSheet("font-weight: 600;")
            sub = QLabel(desc)
            sub.setObjectName("Sub")
            btn = QPushButton("进入")
            btn.setObjectName("Ghost")
            btn.setFixedWidth(72)
            btn.clicked.connect(lambda _c=False, k=key: self.switch_page(k))
            row.addWidget(lbl)
            row.addWidget(sub, 1)
            row.addWidget(btn)
            ml.addLayout(row)
        self.body.addWidget(modules)

        self.lbl_env = QLabel("")
        self.lbl_env.setObjectName("Muted")
        self.body.addWidget(self.lbl_env)
        self.body.addStretch(1)

    # -------------------------------------------------------------- 数据
    def on_show(self) -> None:
        self.reload()

    def reload(self) -> None:
        task = Task(backend.stats)
        task.done.connect(self._apply_stats)
        task.failed.connect(lambda tb: self.notify(tb.splitlines()[-1][:160], "err"))
        CENTER.start(task)
        self._refresh_nav_label()

    def _apply_stats(self, data: dict) -> None:
        tx = data.get("tangxin") or {}
        hg = data.get("huangguo") or {}
        st = data.get("sites") or {}

        rec = tx.get("records") or {}
        rec_total = sum(int(v or 0) for v in rec.values()) if isinstance(rec, dict) else 0
        c1 = self._stats_cards["tangxin_records"]
        if tx.get("error"):
            c1.set_value("—")
            c1.set_hint(tx["error"][:40])
        else:
            c1.set_value(rec_total)
            c1.set_hint(f"完成 {rec.get('done', 0)} · 失败 {rec.get('failed', 0)}")

        c2 = self._stats_cards["tangxin_files"]
        c2.set_value(tx.get("existing", "—") if not tx.get("error") else "—")
        c2.set_hint(f"线路 {tx.get('origin', '') or '未设置'}")

        c3 = self._stats_cards["huangguo_dramas"]
        if hg.get("error"):
            c3.set_value("—")
            c3.set_hint(hg["error"][:40])
        else:
            c3.set_value(hg.get("dramas", 0))
            site = (hg.get("site") or {})
            c3.set_hint(f"站点 {site.get('site_name') or site.get('site_id') or '-'}")

        c4 = self._stats_cards["sites_count"]
        js = data.get("jiuse") or {}
        if st.get("error"):
            c4.set_value("—")
            c4.set_hint(st["error"][:40])
        else:
            c4.set_value(st.get("count", 0))
            n_js = len(js.get("addresses") or [])
            c4.set_hint(f"九色线路 {n_js} 条" if n_js else "一域名一模块")

        self.lbl_env.setText(
            f"yt-dlp {data.get('ytdlp', '-')} · 数据根 {paths.rel(paths.DATA)} · "
            f"临时 {paths.rel(paths.TEMP)}")

    def refresh_nav(self) -> None:
        task = Task(nav.refresh, want_progress=True)
        task.done.connect(self._nav_done)
        task.failed.connect(lambda tb: self.notify("导航页读取失败：" + tb.splitlines()[-1][:120], "err"))
        CENTER.start(task)

    def _nav_done(self, result: dict) -> None:
        if result.get("ok"):
            self.notify(f"导航页已更新：{len(result.get('entries') or [])} 条入口", "ok")
        else:
            self.notify(f"导航页读取失败：{result.get('error', '')[:120]}", "warn")
        self._refresh_nav_label()

    def _refresh_nav_label(self) -> None:
        payload = nav.cached_payload()
        entries = payload.get("entries") or []
        if not entries:
            self.lbl_nav.setText("导航页：尚未读取（需要代理）")
            return
        import datetime as _dt
        ts = payload.get("at") or 0
        when = _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "未知"
        tx = [e["name"] for e in entries if e.get("kind") == "tangxin"]
        hv = [e["name"] for e in entries if e.get("kind") == "huangvideo"]
        self.lbl_nav.setText(
            f"导航页：{len(entries)} 条 · 更新于 {when} · "
            f"糖心线路 {len(tx)} · 黄视频 {len(hv)}")
