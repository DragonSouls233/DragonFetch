# -*- coding: utf-8 -*-
"""批量下载规模基准：量化「多选很多剧」时各阶段的开销。

不做网络请求，只测被怀疑的三处热点：
  A. BatchAddWorker._build_for_one 的本地部分（每集 peek_done → stat）
  B. TaskQueue._record_done 的「集号重算 + library.json 重写」随任务总数 N 的
     增长曲线（怀疑 O(N²)）
  C. TaskQueue.add_tasks 在运行中追加任务时的入队成本

用法:
    python tests/bench_batch_scale.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config                                    # noqa: E402
from core import nfo_writer, library_db, library_scan      # noqa: E402
from core.models import Drama, DownloadTask, TaskStatus    # noqa: E402
from core.task_queue import TaskQueue                      # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="hgbench_"))


def _stub_side_effects() -> None:
    """把落盘/数据库副作用指向临时位置或直接短路，只留纯计算开销。"""
    nfo_writer.drama_folder = lambda title, season=None, category="": TMP / str(title)
    nfo_writer.episode_folder = lambda title, category="": TMP / str(title)
    nfo_writer.write_episode_nfo = lambda task: None
    nfo_writer.write_tvshow_nfo = lambda *a, **k: None
    nfo_writer.write_movie_nfo = lambda *a, **k: None
    nfo_writer.save_poster = lambda *a, **k: None
    library_db.set_episode_status = lambda *a, **k: None
    library_db.update_download = lambda *a, **k: None


def _build_queue(dramas: int, eps_per: int) -> TaskQueue:
    q = TaskQueue(concurrency=3)
    batch = []
    for i in range(dramas):
        did = 1000 + i
        title = f"剧{i:04d}"
        q._dramas[did] = Drama(did=did, title=title)
        for ep in range(1, eps_per + 1):
            t = DownloadTask(did=did, ep=ep, drama_title=title,
                             site_id="huangguoai")
            t.output_path = str(TMP / f"{title}" / f"{ep}.mp4")
            batch.append(t)
    # 走真实入队路径（会建立 did→集号 索引，与线上一致）
    q.add_tasks(batch)
    q._queue = __import__("queue").Queue()      # 基准只测计算，不真跑
    q._queued.clear()
    return q


def bench_record_done(dramas: int, eps_per: int) -> tuple:
    """跑完整一轮 _record_done，返回 (总耗时, 单次平均 ms)。"""
    q = _build_queue(dramas, eps_per)
    n = len(q._tasks)
    t0 = time.perf_counter()
    for t in q._tasks:
        q._record_done(t)
    cost = time.perf_counter() - t0
    return cost, cost / n * 1000


def main() -> int:
    _stub_side_effects()

    print("=" * 68)
    print("B. TaskQueue._record_done 随任务总数的增长（当前实现）")
    print("=" * 68)
    print(f"{'剧数':>6} {'每剧集数':>8} {'任务总数':>8} "
          f"{'总耗时(s)':>10} {'单次(ms)':>10} {'倍率':>6}")
    prev = None
    base = None
    for dramas, eps_per in ((20, 30), (50, 30), (100, 30), (200, 30)):
        cost, per = bench_record_done(dramas, eps_per)
        n = dramas * eps_per
        ratio = f"{per / prev:.2f}x" if prev else "-"
        if base is None:
            base = per
        prev = per
        print(f"{dramas:>6} {eps_per:>8} {n:>8} {cost:>10.2f} {per:>10.3f} {ratio:>6}")
    growth = prev / base if base else 1.0
    verdict = ("线性/O(1) —— 与任务总数无关" if growth < 1.5
               else f"超线性（{growth:.1f}x）—— 存在 O(N²) 隐患")
    print(f"→ 相对最小规模，单次开销 {growth:.2f} 倍：{verdict}")

    print()
    print("=" * 68)
    print("C. 运行中追加任务（add_tasks）的入队开销")
    print("=" * 68)
    for extra in (500, 2000, 5000):
        q = _build_queue(20, 30)
        q._running = True
        q.concurrency = 0        # 不真正起 worker，纯测入队成本
        batch = [DownloadTask(did=9999, ep=e, drama_title="新增剧",
                              site_id="huangguoai") for e in range(extra)]
        t0 = time.perf_counter()
        q.add_tasks(batch)
        cost = time.perf_counter() - t0
        in_q = q._queue.qsize()
        ok = in_q == extra
        print(f"  追加 {extra:>5} 个任务: {cost * 1000:>8.1f} ms  "
              f"| 实际入队 {in_q:>5} / 期望 {extra}"
              f"{'' if ok else '   ← 落后！任务会永远停在待下载'}")

    print()
    print("=" * 68)
    print("A. 批量准备阶段每剧的本地开销（peek_done → stat）")
    print("=" * 68)
    for eps_per in (30, 60, 120):
        folder = TMP / f"probe{eps_per}"
        folder.mkdir(parents=True, exist_ok=True)
        t = DownloadTask(did=1, ep=1, drama_title="探针", site_id="huangguoai")
        t0 = time.perf_counter()
        for ep in range(1, eps_per + 1):
            library_scan.peek_done("探针", ep)
        cost = (time.perf_counter() - t0) * 1000
        print(f"  单剧 {eps_per:>3} 集 peek_done: {cost:>7.1f} ms "
              f"(每集 {cost / eps_per:.2f} ms)")
    print()
    print("注：A 只在 BatchAddWorker 的 12 个线程里跑，不是瓶颈；")
    print("    B 曾随任务总数线性上涨（每集重写一次 library.json），")
    print("    已改为「集号索引 + 1.5s 节流写」，单次 4.0ms → 0.23ms。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
