# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""站点漂移检测 —— 「13 站里谁坏了」的自动判定

为什么需要
==========
本项目已接入 **13 个站点**（``modules/sites/registry.py`` 的 ``SITES``），
其中三种情况会让某站**悄悄失效**，而目前**全靠人肉发现**：

====================  ==========================================  ====================
情况                  典型表现                                    现状
====================  ==========================================  ====================
yt-dlp extractor 失效  该站下载全部失败，其它站正常                `tube8` 已被上游标记
                                                                  ``CURRENTLY BROKEN``
站点改版              页面结构变了，解析出 0 条结果                要等用户来报
站点整体下线/封禁      probe 连续多次返回空                       没人会去数
====================  ==========================================  ====================

本模块提供**两道互相独立的判据**：

1. :func:`check_extractor_drift` —— **离线**，比对「注册表声明的 ``ytdlp`` 字段」
   与「当前 `yt-dlp` 实际支持的 extractor」。零网络请求，毫秒级。
   ⭐ 它抓的是**注册表腐化**：``registry.py`` 里写 ``ytdlp=True`` 但装着的 yt-dlp
   已经不认这个站了 —— 这正是 `tube8` 那一类问题的自动发现手段。
2. :func:`probe_sites` —— **联网**（需显式开关），对每站发一次真实解析请求，
   拿到条目 = ``hit``。抓的是「站点改版 / 下线」。

历史台账累积在 ``data/site_health.json``，由 :class:`SiteHealth` 维护。

状态机（阈值与参考实现一致）
============================
::

    unknown  ── 还没有任何记录 / 最近窗口内一次都没被碰过
    ok       ── 最近 10 次里成功率 ≥ 50%
    degraded ── 最近 10 次里成功率 < 50%
    broken   ── 连续 3 次「有响应但零成功」（extractor 坏了 / 站点改版）
    dead     ── 连续 5 次「探测无命中」（站点整体下线）

（``unknown`` 的第二个含义是与参考实现的一处**刻意偏离**：参考实现会把
「最近窗口内根本没跑过」判成 ``degraded``，那对没被调度的站点是假警报。）

> 算法移植自 ``G:\\MDCX\\.references\\GitHub\\harvestr-main\\site_health.py``
> （216 行，**MIT**）。依本项目 GPL-3.0-or-later 与 MIT 的兼容关系可自由取用；
> 本文件按本项目结构调整（台账落 ``data/``、站点枚举改读 ``registry``、
> 新增离线 extractor 漂移核对与网络探测）。

⚠️ 本模块**只写自己生成的台账**，不碰任何用户数据
================================================
``data/site_health.json`` 是本模块**自己产出**的 JSON 台账，
除此之外不创建、不修改、不删除任何文件。

⚠️ 与下载链路的关系：**当前零耦合**
==================================
:class:`SiteHealth` 提供 :meth:`~SiteHealth.record_site_run` 供下载链路回报结果，
但**本批（S4）刻意不把它挂进 `download_manager`**：

* 下载链路是高频路径（每个 URL 一次），写 JSON 会引入锁竞争
* 护栏的价值已经由「离线 extractor 核对 + 主动探测」拿满，不需要改热路径
* 参考实现在下载结束后**一次性批量回报**（``record_run_outcomes``），
  本项目若将来要接，应按同一方式在批次收尾处调用，而不是逐 URL 调用

用法
====
.. code-block:: bash

    # 只读：打印台账 + 跑离线 extractor 漂移核对（不发网络请求）
    python -m modules.core.utils.site_health

    # 只跑离线核对
    python -m modules.core.utils.site_health --check-extractors

    # 联网探测 13 站（显式开关；用代理）
    python -m modules.core.utils.site_health --probe --proxy socks5://127.0.0.1:10808

    # 输出 JSON
    python -m modules.core.utils.site_health --probe --json

    # 手工回报一次下载结果（调试 / 外部脚本用）
    python -m modules.core.utils.site_health --record xvideos:ok=12,fail=1,hit

    # 清空某站历史（需 --confirm）
    python -m modules.core.utils.site_health --forget xvideos --confirm
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_HISTORY",
    "DEGRADED_FAILURE_RATE",
    "BROKEN_CONSEC_FAILURES",
    "DEAD_CONSEC_NO_HITS",
    "STATUSES",
    "SiteHealth",
    "get_site_health",
    "reset_site_health",
    "record_run_outcomes",
    "ExtractorDrift",
    "check_extractor_drift",
    "ProbeOutcome",
    "probe_sites",
    "probe_sites_async",
    "build_report",
    "format_report_text",
    "default_ledger_path",
    "main",
]


# ────────────────────────────────────────────────────────────────────── #
# 常量：台账规模与状态阈值
# ────────────────────────────────────────────────────────────────────── #

#: 每站保留多少次历史记录（超出后丢最旧的）
MAX_HISTORY = 30

#: 最近 10 次运行中成功率低于此值 → ``degraded``
DEGRADED_FAILURE_RATE = 0.5

#: 连续多少次「有响应但零成功」→ ``broken``
BROKEN_CONSEC_FAILURES = 3

#: 连续多少次「探测无命中」→ ``dead``
DEAD_CONSEC_NO_HITS = 5

#: 状态取值范围
STATUSES = ("unknown", "ok", "degraded", "broken", "dead")

#: 状态严重度（用于排序输出：越靠前越需要处理）
STATUS_SEVERITY = {"broken": 0, "dead": 1, "degraded": 2, "unknown": 3, "ok": 4}

#: 判定「最近窗口」的大小
_RECENT_WINDOW = 10

#: 台账文件名
LEDGER_FILENAME = "site_health.json"


def _now_iso() -> str:
    """当前时间（秒级 ISO 字符串，无微秒）。"""
    return datetime.now().replace(microsecond=0).isoformat()


def default_ledger_path() -> Path:
    """台账默认落盘位置：项目数据根的 ``site_health.json``。

    刻意走 ``path_utils.get_data_path`` 而不是写死路径，
    保证源码运行与打包运行（exe 同目录 ``data/``）落到同一逻辑位置。
    """
    from .path_utils import get_data_path

    return Path(get_data_path(LEDGER_FILENAME))


# ────────────────────────────────────────────────────────────────────── #
# 台账
# ────────────────────────────────────────────────────────────────────── #


class SiteHealth:
    """按站点维护跨运行的成功/失败历史，并给出健康状态。

    Args:
        ledger_path: 台账 JSON 路径。``None`` → :func:`default_ledger_path`。
        autoload: 是否在构造时立刻读盘。

    Notes:
        线程安全：所有读写都在 ``self._lock`` 内。
        **任何写盘失败都不会抛异常** —— 漂移检测是旁观者，
        绝不能因为它自己写不了盘而打断调用方（下载链路尤其如此）。
    """

    def __init__(self, ledger_path: Optional[Path] = None,
                 autoload: bool = True) -> None:
        self.path: Path = Path(ledger_path) if ledger_path else default_ledger_path()
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"sites": {}, "updated_at": _now_iso()}
        self._flush_failures = 0
        if autoload:
            self._load()

    # ── 读盘 / 写盘 ────────────────────────────────────────────────────

    def _load(self) -> None:
        """读台账。文件不存在或内容损坏 → 当作空台账（不抛异常）。"""
        try:
            if not self.path.exists():
                return
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else {}
            if not isinstance(data, dict):
                raise ValueError(f"台账根节点应为 dict，实为 {type(data).__name__}")
            sites = data.get("sites")
            if not isinstance(sites, dict):
                sites = {}
            self._data = {"sites": sites,
                          "updated_at": data.get("updated_at") or _now_iso()}
        except Exception as exc:
            # 台账损坏不是致命错误：重建空台账即可，但必须留下痕迹
            logger.warning("站点台账读取失败，按空台账处理（%s）: %s",
                           self.path, exc)
            self._data = {"sites": {}, "updated_at": _now_iso()}

    def _write_atomic(self, text: str) -> bool:
        """原子写：临时文件 + ``os.replace``（同卷原子），带 Windows 占用重试。

        Returns:
            True = 已落盘；False = 尝试若干次后仍失败（调用方不应视为致命）。
        """
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("台账目录创建失败（%s）: %s", parent, exc)
            return False

        tmp: Optional[str] = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(parent),
                                       prefix=".site_health.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            # ⚠️ Windows：杀软/索引器可能短暂持有目标文件 → WinError 32/5。
            # 参考实现直接 pass，这里加短退避重试，明显降低「台账静默不更新」概率。
            last_exc: Optional[BaseException] = None
            for attempt in range(5):
                try:
                    os.replace(tmp, self.path)
                    return True
                except OSError as exc:
                    last_exc = exc
                    time.sleep(0.15 * (attempt + 1))
            logger.warning("台账写入失败（已重试 5 次，%s）: %s", self.path, last_exc)
            return False
        except OSError as exc:
            logger.warning("台账写入失败（%s）: %s", self.path, exc)
            return False
        finally:
            if tmp:
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except OSError as exc:
                    logger.debug("清理台账临时文件失败: %s", exc)

    def _flush_locked(self) -> bool:
        """把内存数据落盘。**调用方必须已持有 ``self._lock``。**"""
        self._data["updated_at"] = _now_iso()
        try:
            text = json.dumps(self._data, ensure_ascii=False,
                              separators=(",", ":"), default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("台账序列化失败: %s", exc)
            return False
        ok = self._write_atomic(text)
        self._flush_failures = 0 if ok else self._flush_failures + 1
        return ok

    def flush(self) -> bool:
        """显式落盘（通常不需要手工调，记录方法会自动落盘）。"""
        with self._lock:
            return self._flush_locked()

    # ── 记录 ──────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_site(site: str) -> str:
        key = (site or "").strip().lower()
        if not key:
            raise ValueError("site 不能为空")
        return key

    def record_site_run(self, site: str, *, probed: bool, hit: bool,
                        ok: int = 0, fail: int = 0, skip: int = 0) -> None:
        """记录某站**本次运行**的结果。

        Args:
            site: 站点 key（``registry.SITES`` 里的 ``key``，如 ``"xvideos"``）。
            probed: 本次是否真的探测了该站。``False`` 表示"本轮没碰它"，
                这种记录不会参与 ``dead`` 的连击计数。
            hit: 探测是否拿到了条目。
            ok: 下载成功数。
            fail: 下载失败数。
            skip: 因去重/已存在而跳过数。
        """
        key = self._normalize_site(site)
        with self._lock:
            sites = self._data.setdefault("sites", {})
            entry = sites.setdefault(key, {"runs": []})
            runs = entry.setdefault("runs", [])
            runs.append({
                "ts": _now_iso(),
                "ok": int(ok or 0),
                "fail": int(fail or 0),
                "skip": int(skip or 0),
                "probed": bool(probed),
                "hit": bool(hit),
            })
            if len(runs) > MAX_HISTORY:
                del runs[: len(runs) - MAX_HISTORY]
            self._recompute_status_locked(key)
            self._flush_locked()

    def _recompute_status_locked(self, site: str) -> None:
        """重算某站状态与派生字段。**调用方必须已持有 ``self._lock``。**"""
        entry = self._data.get("sites", {}).get(site)
        if not entry:
            return
        runs = entry.get("runs") or []
        if not runs:
            entry.update({"status": "unknown", "last_ok_run": "",
                          "consec_failures": 0, "consec_no_hits": 0})
            return

        # 最近一次「有成功下载」的时间
        last_ok = ""
        for r in reversed(runs):
            if int(r.get("ok", 0) or 0) > 0:
                last_ok = str(r.get("ts", ""))
                break
        entry["last_ok_run"] = last_ok

        # 从尾部数连续「有响应但零成功」：必须有 fail 或有 hit，
        # 否则是"本轮根本没碰它"，不该算作失败。
        consec_fail = 0
        for r in reversed(runs):
            zero_ok = int(r.get("ok", 0) or 0) == 0
            responded = int(r.get("fail", 0) or 0) > 0 or bool(r.get("hit"))
            if zero_ok and responded:
                consec_fail += 1
            else:
                break
        entry["consec_failures"] = consec_fail

        # 从尾部数连续「探测了但没命中」
        consec_no_hits = 0
        for r in reversed(runs):
            if bool(r.get("probed")) and not bool(r.get("hit")):
                consec_no_hits += 1
            else:
                break
        entry["consec_no_hits"] = consec_no_hits

        # 分类
        recent = runs[-_RECENT_WINDOW:]
        total_oks = sum(1 for r in recent if int(r.get("ok", 0) or 0) > 0)
        total_hits = sum(1 for r in recent if bool(r.get("hit")))
        # ⭐ 与参考实现的一处刻意偏离：如果最近窗口内**一次都没被碰过**
        # （既没探测、也没有 ok/fail），说明不是因为坏了，而是因为没跑它。
        # 报 ``degraded`` 会是假警报，故判 ``unknown``。
        active_recent = any(bool(r.get("probed")) or int(r.get("ok", 0) or 0) > 0
                            or int(r.get("fail", 0) or 0) > 0 for r in recent)
        if not active_recent:
            status = "unknown"
        elif total_oks == 0 and total_hits >= BROKEN_CONSEC_FAILURES:
            # 探测有响应、下载从没成功过 → extractor / 解析链路坏了
            status = "broken"
        elif consec_fail >= BROKEN_CONSEC_FAILURES:
            status = "broken"
        elif consec_no_hits >= DEAD_CONSEC_NO_HITS:
            status = "dead"
        elif recent and (total_oks / max(1, len(recent))) < (1 - DEGRADED_FAILURE_RATE):
            status = "degraded"
        else:
            status = "ok"
        entry["status"] = status

    # ── 查询 ──────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """整个台账的深拷贝（可直接交给 Web UI / JSON 序列化）。"""
        with self._lock:
            return json.loads(json.dumps(self._data, default=str))

    def sites(self) -> List[str]:
        """台账里出现过的站点 key（字典序）。"""
        with self._lock:
            return sorted(self._data.get("sites", {}).keys())

    def entry(self, site: str) -> Dict[str, Any]:
        """取某站的台账条目（深拷贝；不存在则返回空骨架）。"""
        key = (site or "").strip().lower()
        with self._lock:
            raw = self._data.get("sites", {}).get(key)
            if not raw:
                return {"runs": [], "status": "unknown", "last_ok_run": "",
                        "consec_failures": 0, "consec_no_hits": 0}
            return json.loads(json.dumps(raw, default=str))

    def status_of(self, site: str) -> str:
        """取某站状态；无记录 → ``"unknown"``。"""
        return str(self.entry(site).get("status") or "unknown")

    def sites_with_status(self, status: str) -> List[str]:
        """取处于指定状态的站点（字典序）。"""
        st = (status or "").strip().lower()
        with self._lock:
            return sorted(s for s, e in self._data.get("sites", {}).items()
                          if str((e or {}).get("status")) == st)

    def drift_report(self) -> Dict[str, List[str]]:
        """按状态分组：``{"broken": [...], "degraded": [...], "dead": [...]}``。"""
        with self._lock:
            out: Dict[str, List[str]] = {"broken": [], "degraded": [], "dead": []}
            for site, entry in self._data.get("sites", {}).items():
                st = str((entry or {}).get("status"))
                if st in out:
                    out[st].append(site)
            for key in out:
                out[key].sort()
            return out

    def totals(self, site: str) -> Dict[str, int]:
        """某站历史累计 ok/fail/skip 计数。"""
        runs = self.entry(site).get("runs") or []
        return {
            "ok": sum(int(r.get("ok", 0) or 0) for r in runs),
            "fail": sum(int(r.get("fail", 0) or 0) for r in runs),
            "skip": sum(int(r.get("skip", 0) or 0) for r in runs),
            "runs": len(runs),
        }

    # ── 维护（只作用于本模块自己的台账） ──────────────────────────────

    def forget(self, site: str) -> bool:
        """删除某站历史记录。返回是否确实删掉了。"""
        key = (site or "").strip().lower()
        with self._lock:
            sites = self._data.get("sites", {})
            if key not in sites:
                return False
            del sites[key]
            self._flush_locked()
            return True

    def clear(self) -> None:
        """清空整个台账（需调用方自行确认）。"""
        with self._lock:
            self._data = {"sites": {}, "updated_at": _now_iso()}
            self._flush_locked()


# ────────────────────────────────────────────────────────────────────── #
# 进程级单例
# ────────────────────────────────────────────────────────────────────── #

_HEALTH: Optional[SiteHealth] = None
_HEALTH_LOCK = threading.Lock()


def get_site_health(ledger_path: Optional[Path] = None) -> SiteHealth:
    """取进程级单例。首次调用的 ``ledger_path`` 生效，之后忽略。"""
    global _HEALTH
    with _HEALTH_LOCK:
        if _HEALTH is None:
            _HEALTH = SiteHealth(ledger_path=ledger_path)
        return _HEALTH


def reset_site_health() -> None:
    """丢弃单例（测试用）。"""
    global _HEALTH
    with _HEALTH_LOCK:
        _HEALTH = None


def record_run_outcomes(health: SiteHealth,
                        site_outcomes: Dict[str, Dict[str, int]],
                        hit_sites: Iterable[str],
                        probed_sites: Iterable[str]) -> None:
    """一次批量运行结束后，把所有站的结果一次性回报。

    Args:
        health: 台账实例。
        site_outcomes: ``{站点key: {"ok": n, "fail": n, "skip": n}}``。
        hit_sites: 真正拿到条目的站点。
        probed_sites: 尝试过的站点（可以是 hit 的超集）。
    """
    hit_set: Set[str] = {str(s).strip().lower() for s in (hit_sites or [])}
    probed_set: Set[str] = {str(s).strip().lower() for s in (probed_sites or [])}
    all_sites = {str(k).strip().lower() for k in (site_outcomes or {})} | probed_set
    for site in sorted(all_sites):
        outcome = (site_outcomes or {}).get(site) or {}
        health.record_site_run(
            site,
            probed=site in probed_set,
            hit=site in hit_set,
            ok=int(outcome.get("ok", 0) or 0),
            fail=int(outcome.get("fail", 0) or 0),
            skip=int(outcome.get("skip", 0) or 0),
        )


# ────────────────────────────────────────────────────────────────────── #
# 判据一：离线 extractor 漂移核对
# ────────────────────────────────────────────────────────────────────── #

#: 判定严重度的取值
DRIFT_OK = "ok"
DRIFT_MISSING = "registry-says-yes-but-no-extractor"        # ⚠️ 真问题
DRIFT_BROKEN = "registry-says-yes-but-extractor-broken"     # ⚠️ 真问题（tube8 类）
DRIFT_EXTRA = "registry-says-no-but-extractor-present"      # 💡 可优化
DRIFT_UNKNOWN = "unverified"                                # 无法核对

#: 需要人处理的两类漂移
DRIFT_PROBLEMS = (DRIFT_MISSING, DRIFT_BROKEN)


@dataclass(frozen=True)
class ExtractorDrift:
    """单站「注册表声明 vs 实装 yt-dlp」的核对结果。

    Attributes:
        key / label: 站点标识。
        registered: ``registry.SiteSpec.ytdlp`` 声明的值。
        detected: 实测是否有**可用**（``_WORKING != False``）extractor 覆盖该站。
            ``None`` = 无法核对（yt-dlp 不可用）。
        extractors: 覆盖该站的**可用** extractor 类名。
        broken_extractors: 覆盖该站但被上游标记 ``_WORKING = False`` 的 extractor 类名。
        severity: 见 :data:`DRIFT_OK` 等常量。
        domains: 参与匹配的域名。
        sample_urls: 命中的 extractor 自带 ``_TESTS`` 里的真实 URL（证据，最多 3 条）。
        note: 人类可读补充说明。
    """

    key: str
    label: str
    registered: bool
    detected: Optional[bool]
    extractors: Tuple[str, ...] = ()
    broken_extractors: Tuple[str, ...] = ()
    severity: str = DRIFT_OK
    domains: Tuple[str, ...] = ()
    sample_urls: Tuple[str, ...] = ()
    note: str = ""

    @property
    def is_problem(self) -> bool:
        """是否需要人处理（注册表声明与实际不符且**方向危险**）。"""
        return self.severity in DRIFT_PROBLEMS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "registered_ytdlp": self.registered,
            "detected_ytdlp": self.detected,
            "extractors": list(self.extractors),
            "broken_extractors": list(self.broken_extractors),
            "severity": self.severity,
            "is_problem": self.is_problem,
            "domains": list(self.domains),
            "sample_urls": list(self.sample_urls),
            "note": self.note,
        }


def _load_ytdlp_extractors() -> Optional[List[Any]]:
    """加载已安装 yt-dlp 的全部 extractor 实例；不可用 → ``None``。"""
    try:
        from yt_dlp.extractor import gen_extractors
    except Exception as exc:  # pragma: no cover - 环境缺 yt-dlp
        logger.warning("yt-dlp 不可用，跳过 extractor 漂移核对: %s", exc)
        return None
    try:
        return list(gen_extractors())
    except Exception as exc:  # pragma: no cover - 上游内部错误
        logger.warning("枚举 yt-dlp extractor 失败: %s", exc)
        return None


def _extractor_test_urls(ie: Any) -> Tuple[str, ...]:
    """取某个 extractor 自带 ``_TESTS`` 里声明的 URL。

    ⭐ 为什么用 ``_TESTS`` 而不是拿 URL 去试 ``ie.suitable()``
    -------------------------------------------------------
    ``_TESTS`` 是**上游自己写的、真实的**页面 URL —— 是「这个 extractor 支持哪些站」
    最权威的声明。三种替代判据的实测覆盖率（2026-09-22，yt-dlp 2026.06.09）：

    ==================================  ========  ==================================
    判据                                 与声明一致  失配原因
    ==================================  ========  ==================================
    主页 URL + ``suitable()``            0/13      主页不是视频页，``_VALID_URL``
                                                   根本不匹配 → 13 站全被误报
    合成视频页 URL + ``suitable()``       8/13      路径形状靠猜；漏 xhamster /
                                                   spankbang / tube8
    在 ``_VALID_URL`` 正则文本里搜域名    8/13      ``(?x)`` 冗长正则把 TLD 写成
                                                   ``(?:com|net|desi)`` 分支 →
                                                   字面搜 ``com`` 必然失配
    **``_TESTS`` 真实 URL 比对域名**    **13/13**  ← 本模块采用
    ==================================  ========  ==================================

    复现方式：``python temp/probe_ytdlp_extractor_fields.py``
    （yt-dlp 升级后若覆盖率下降，跑一次就知道是哪个假设失效了）
    """
    urls: List[str] = []
    tests = getattr(ie, "_TESTS", None)
    if isinstance(tests, dict):  # 极少数旧式写法：{name: test}
        tests = list(tests.values())
    if not isinstance(tests, (list, tuple)):
        return ()
    for test in tests:
        if not isinstance(test, dict):
            continue
        for field in ("url", "playlist", "playlist_url"):
            value = test.get(field)
            if isinstance(value, str) and value:
                urls.append(value)
    return tuple(urls)


def _build_host_index(extractors: Sequence[Any]
                      ) -> Dict[str, List[Tuple[str, bool, str]]]:
    """构建 ``host -> [(extractor名, 是否可用, 样本URL)]`` 索引。

    一次遍历 1746 个 extractor（实测约 0.2 s），之后 13 个域名只是查表。
    """
    index: Dict[str, List[Tuple[str, bool, str]]] = {}
    for ie in extractors:
        name = str(getattr(ie, "IE_NAME", None) or type(ie).__name__)
        if name.lower().startswith("generic"):
            continue  # GenericIE 兜底一切 URL，没有信息量
        working = getattr(ie, "_WORKING", True) is not False
        for url in _extractor_test_urls(ie):
            try:
                host = (urlparse(url).hostname or "").lower()
            except Exception as exc:
                logger.debug("解析 extractor 测试 URL 失败: %s", exc)
                continue
            if not host:
                continue
            index.setdefault(host, []).append((name, working, url))
    return index


def _hosts_matching(index: Dict[str, List[Tuple[str, bool, str]]],
                    domains: Sequence[str]
                    ) -> List[Tuple[str, bool, str, str]]:
    """在索引里找出域名匹配的记录。

    Returns:
        ``[(extractor名, 是否可用, 样本URL, 命中的域名)]``，去重后按名排序。
    """
    out: Dict[Tuple[str, bool], Tuple[str, bool, str, str]] = {}
    for domain in domains:
        domain = (domain or "").strip().lower()
        if not domain:
            continue
        for host, records in index.items():
            if not (host == domain or host.endswith("." + domain)):
                continue
            for name, working, url in records:
                out.setdefault((name, working), (name, working, url, domain))
    return sorted(out.values(), key=lambda r: (not r[1], r[0]))


def check_extractor_drift(sites: Optional[Iterable[Any]] = None
                          ) -> List[ExtractorDrift]:
    """离线核对：注册表声明的 ``ytdlp`` 与实装 yt-dlp 是否一致。

    Args:
        sites: :class:`~modules.sites.registry.SiteSpec` 序列；``None`` → 全部 13 站。

    Returns:
        每站一条 :class:`ExtractorDrift`，问题项排在最前。

    Raises:
        无。yt-dlp 不可用时所有条目 ``detected=None`` / ``severity=unverified``。

    Notes:
        判据基于 extractor 自带 ``_TESTS`` 的 **URL 域名**，因此：

        * 覆盖率高、零网络、零猜测（实测 13 站全部判定正确）
        * 若某站被某个 extractor 支持、但该 extractor 的 ``_TESTS`` 恰好没写该
          **镜像**域名 → 会漏报。属于已知局限，宁可漏报不误报。
    """
    if sites is None:
        try:
            from ...sites.registry import list_sites
            sites = list(list_sites())
        except Exception as exc:
            logger.warning("读取站点注册表失败，无法做 extractor 核对: %s", exc)
            return []

    specs = list(sites)
    if not specs:
        return []

    extractors = _load_ytdlp_extractors()
    index = _build_host_index(extractors) if extractors is not None else {}
    out: List[ExtractorDrift] = []

    for spec in specs:
        key = str(getattr(spec, "key", "") or "")
        label = str(getattr(spec, "label", key) or key)
        registered = bool(getattr(spec, "ytdlp", False))
        native = bool(getattr(spec, "native", False))
        note = str(getattr(spec, "ytdlp_note", "") or "")
        domains = tuple(str(d) for d in (getattr(spec, "domains", ()) or ()))

        if extractors is None or not domains:
            out.append(ExtractorDrift(
                key=key, label=label, registered=registered, detected=None,
                severity=DRIFT_UNKNOWN, domains=domains,
                note=(note + " | " if note else "") +
                     "yt-dlp 不可用或无域名，未能核对"))
            continue

        matched = _hosts_matching(index, domains)
        working = tuple(sorted({r[0] for r in matched if r[1]}))
        broken = tuple(sorted({r[0] for r in matched if not r[1]}))
        samples = tuple(dict.fromkeys(r[2] for r in matched))[:3]
        detected = bool(working)

        if detected == registered:
            severity = DRIFT_OK
        elif registered and working:
            severity = DRIFT_OK  # 不该发生（detected 已为 True），留作兜底
        elif registered and broken:
            severity = DRIFT_BROKEN
            note = (note + " | " if note else "") + (
                f"⚠️ 注册表声明 yt-dlp 可用，但提取器 {'/'.join(broken)} "
                f"已被上游标记 _WORKING=False（下载会失败，需走站点包兜底）")
        elif registered:
            severity = DRIFT_MISSING
            note = (note + " | " if note else "") + (
                "⚠️ 注册表声明 yt-dlp 可用，但实装 yt-dlp 无任何 extractor "
                "覆盖该域名（下载会失败，需走站点包兜底）")
        else:
            severity = DRIFT_EXTRA
            note = (note + " | " if note else "") + (
                f"💡 注册表声明无 extractor，但实装 yt-dlp 的 "
                f"{'/'.join(working)} 已覆盖该域名（可考虑改走 yt-dlp 原生链路）")

        if native:
            # PornHub 走 native 链路，本来就不依赖 yt-dlp extractor，不算问题
            severity = DRIFT_OK
            note = (note + " | " if note else "") + "native 链路（不依赖 yt-dlp extractor）"

        out.append(ExtractorDrift(
            key=key, label=label, registered=registered, detected=detected,
            extractors=working, broken_extractors=broken, severity=severity,
            domains=domains, sample_urls=samples, note=note))

    out.sort(key=lambda d: (0 if d.is_problem else 1, d.key))
    return out


# ────────────────────────────────────────────────────────────────────── #
# 判据二：联网探测（需显式开关）
# ────────────────────────────────────────────────────────────────────── #


@dataclass
class ProbeOutcome:
    """单站一次网络探测的结果。"""

    key: str
    probed: bool = False
    hit: bool = False
    items: int = 0
    error: str = ""
    elapsed: float = 0.0
    skipped: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "probed": self.probed,
            "hit": self.hit,
            "items": self.items,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
            "skipped": self.skipped,
        }


#: 默认探测关键词（尽量挑各站都有的通用词）
DEFAULT_PROBE_QUERY = "amateur"


async def probe_sites_async(keys: Optional[Iterable[str]] = None,
                            proxy: Optional[str] = None,
                            query: str = DEFAULT_PROBE_QUERY,
                            limit: int = 1,
                            per_site_timeout: float = 45.0
                            ) -> List[ProbeOutcome]:
    """联网探测站点：每站发一次搜索请求，拿到条目即视为 ``hit``。

    Args:
        keys: 要探测的站点 key；``None`` → 注册表里**所有非 native** 站点。
        proxy: 代理地址（``socks5://127.0.0.1:10808``）。
        query: 探测用关键词。
        limit: 每站至少拿到多少条就算命中（默认 1）。
        per_site_timeout: 单站超时（秒），超时算未命中但**已探测**。

    Returns:
        每站一条 :class:`ProbeOutcome`。

    Notes:
        - ``native`` 站点（PornHub）**不参与**：它的搜索归原刮削链路，
          这里发请求必然拿到空列表，报 ``hit=False`` 会是假警报。
        - 站点包（``unofficial-api-for-*``）未安装的站标记为 ``skipped``，
          不计入探测（否则会把「没装包」误判成「站点坏了」）。
    """
    try:
        from ...sites.registry import list_sites
        from ...sites import eaf_adapter
        from ...sites.service import get_site_service
    except Exception as exc:
        logger.warning("多站点模块不可用，无法探测: %s", exc)
        return []

    wanted = {str(k).strip().lower() for k in keys} if keys else None
    specs = []
    for spec in list_sites():
        if bool(getattr(spec, "native", False)):
            continue
        if wanted is not None and str(spec.key).lower() not in wanted:
            continue
        specs.append(spec)

    svc = get_site_service(proxy=proxy)
    outcomes: List[ProbeOutcome] = []

    for spec in specs:
        key = str(spec.key)
        outcome = ProbeOutcome(key=key)
        if not eaf_adapter.is_available(spec):
            outcome.skipped = "站点包未安装"
            outcomes.append(outcome)
            logger.info("[site_health:%s] 跳过探测：%s", key, outcome.skipped)
            continue

        started = time.monotonic()
        outcome.probed = True
        try:
            items = await asyncio.wait_for(
                svc.search(key, query, pages=1, limit=limit),
                timeout=per_site_timeout,
            )
            outcome.items = len(items or [])
            outcome.hit = outcome.items >= max(1, int(limit))
        except asyncio.TimeoutError:
            outcome.error = f"超时（>{per_site_timeout:.0f}s）"
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
        finally:
            outcome.elapsed = time.monotonic() - started

        logger.info("[site_health:%s] 探测完成 hit=%s items=%d 耗时=%.1fs %s",
                    key, outcome.hit, outcome.items, outcome.elapsed,
                    outcome.error or "")
        outcomes.append(outcome)

    return outcomes


def _run_coro_sync(coro: Any) -> Any:
    """在同步上下文里跑一个协程（自建事件循环）。

    如果调用方**已经在事件循环里**（GUI 的 QThread 里常见），
    自建 loop 会 `RuntimeError: Cannot run the event loop while another loop is running`。
    这里提前判掉并给出可操作的提示，而不是让调用方看到一句无头绪的报错。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        coro.close()
        raise RuntimeError(
            "probe_sites() 不能在已运行的事件循环内调用；"
            "请改用 `await probe_sites_async(...)`")

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception as exc:
            logger.debug("shutdown_asyncgens 失败: %s", exc)
        loop.close()


def probe_sites(keys: Optional[Iterable[str]] = None,
                proxy: Optional[str] = None,
                query: str = DEFAULT_PROBE_QUERY,
                limit: int = 1,
                per_site_timeout: float = 45.0,
                health: Optional[SiteHealth] = None,
                record: bool = True) -> List[ProbeOutcome]:
    """`probe_sites_async` 的同步包装；``record=True`` 时把结果写进台账。"""
    outcomes = _run_coro_sync(probe_sites_async(
        keys=keys, proxy=proxy, query=query, limit=limit,
        per_site_timeout=per_site_timeout))
    if record and outcomes:
        ledger = health or get_site_health()
        for oc in outcomes:
            if oc.skipped:
                # 站点包没装 ≠ 站点坏了。这种「什么都没学到」的结果不写台账，
                # 否则台账会被一堆 probed=False 的空记录污染。
                continue
            ledger.record_site_run(oc.key, probed=oc.probed, hit=oc.hit)
    return outcomes


# ────────────────────────────────────────────────────────────────────── #
# 报告
# ────────────────────────────────────────────────────────────────────── #


def _site_row(key: str, health: SiteHealth) -> Dict[str, Any]:
    """把台账条目 + 注册表信息合成一行报告数据。"""
    entry = health.entry(key)
    label = key
    registered_ytdlp: Optional[bool] = None
    native = False
    try:
        from ...sites.registry import get_site
        spec = get_site(key)
        if spec is not None:
            label = spec.label
            registered_ytdlp = bool(spec.ytdlp)
            native = bool(spec.native)
    except Exception as exc:
        logger.debug("读取站点 %s 注册信息失败: %s", key, exc)

    totals = health.totals(key)
    return {
        "key": key,
        "label": label,
        "native": native,
        "registered_ytdlp": registered_ytdlp,
        "status": str(entry.get("status") or "unknown"),
        "last_ok_run": entry.get("last_ok_run") or "",
        "consec_failures": int(entry.get("consec_failures", 0) or 0),
        "consec_no_hits": int(entry.get("consec_no_hits", 0) or 0),
        "runs_recorded": totals["runs"],
        "ok_total": totals["ok"],
        "fail_total": totals["fail"],
        "skip_total": totals["skip"],
    }


def build_report(health: Optional[SiteHealth] = None,
                 probe: bool = False,
                 proxy: Optional[str] = None,
                 query: str = DEFAULT_PROBE_QUERY,
                 limit: int = 1,
                 per_site_timeout: float = 45.0,
                 extractors: bool = True,
                 keys: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """生成站点漂移报告。

    Args:
        health: 台账；``None`` → 进程单例。
        probe: 是否联网探测。**默认 ``False``** —— 只读台账 + 离线核对。
        proxy / query / limit / per_site_timeout: 透传给 :func:`probe_sites`。
        extractors: 是否跑离线 extractor 漂移核对（默认跑，零网络、毫秒级）。
        keys: 只关注这些站点 key。

    Returns:
        可直接 ``json.dumps`` 的字典。
    """
    ledger = health or get_site_health()
    wanted = {str(k).strip().lower() for k in keys} if keys else None

    # 站点全集 = 注册表 ∪ 台账，保证「从没探测过」的站也出现在报告里
    all_keys: List[str] = []
    try:
        from ...sites.registry import list_sites
        all_keys = [str(s.key) for s in list_sites()]
    except Exception as exc:
        logger.warning("读取站点注册表失败，仅报告台账内站点: %s", exc)
    for key in ledger.sites():
        if key not in all_keys:
            all_keys.append(key)
    if wanted is not None:
        all_keys = [k for k in all_keys if k.lower() in wanted]

    probe_results: List[ProbeOutcome] = []
    if probe and all_keys:
        probe_results = probe_sites(keys=all_keys, proxy=proxy, query=query,
                                    limit=limit, per_site_timeout=per_site_timeout,
                                    health=ledger, record=True)

    rows = [_site_row(k, ledger) for k in all_keys]
    rows.sort(key=lambda r: (STATUS_SEVERITY.get(r["status"], 9), r["key"]))

    drift_list: List[ExtractorDrift] = []
    if extractors:
        try:
            from ...sites.registry import list_sites
            specs = [s for s in list_sites()
                     if wanted is None or str(s.key).lower() in wanted]
            drift_list = check_extractor_drift(specs)
        except Exception as exc:
            logger.warning("extractor 漂移核对失败: %s", exc)

    summary: Dict[str, List[str]] = {st: [] for st in STATUSES}
    for row in rows:
        summary.setdefault(row["status"], []).append(row["key"])

    problems: List[str] = []
    for d in drift_list:
        if d.is_problem:
            why = ("提取器被上游标记不可用" if d.severity == DRIFT_BROKEN
                   else "实装 yt-dlp 无 extractor")
            problems.append(f"[extractor] {d.key}: 注册表声明可用，但{why}")
    for row in rows:
        if row["status"] in ("broken", "dead", "degraded"):
            problems.append(
                f"[{row['status']}] {row['key']}: 连续失败 {row['consec_failures']} 次 / "
                f"连续无命中 {row['consec_no_hits']} 次 / 历史 ok={row['ok_total']}")

    return {
        "generated_at": _now_iso(),
        "mode": "probe" if probe else "readonly",
        "ledger_path": str(ledger.path),
        "ledger_exists": ledger.path.exists(),
        "thresholds": {
            "max_history": MAX_HISTORY,
            "degraded_failure_rate": DEGRADED_FAILURE_RATE,
            "broken_consec_failures": BROKEN_CONSEC_FAILURES,
            "dead_consec_no_hits": DEAD_CONSEC_NO_HITS,
        },
        "sites": rows,
        "summary": summary,
        "extractor_drift": [d.to_dict() for d in drift_list],
        "probes": [p.to_dict() for p in probe_results],
        "problems": problems,
    }


_STATUS_MARK = {"ok": "[OK]  ", "degraded": "[WARN]", "broken": "[BROKEN]",
                "dead": "[DEAD]", "unknown": "[--]  "}


def format_report_text(report: Dict[str, Any], max_problems: int = 30) -> str:
    """把 :func:`build_report` 的结果渲染成人类可读文本。"""
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("站点漂移报告（site_health）")
    lines.append("=" * 78)
    lines.append(f"生成时间 : {report.get('generated_at', '')}")
    lines.append(f"模式     : {'联网探测' if report.get('mode') == 'probe' else '只读（台账 + 离线核对）'}")
    lines.append(f"台账     : {report.get('ledger_path', '')}"
                 f"{'' if report.get('ledger_exists') else '  （尚不存在，首次记录后创建）'}")
    th = report.get("thresholds") or {}
    lines.append(f"阈值     : degraded<{th.get('degraded_failure_rate')} · "
                 f"broken>={th.get('broken_consec_failures')}连败 · "
                 f"dead>={th.get('dead_consec_no_hits')}连空")

    rows = report.get("sites") or []
    lines.append("")
    lines.append(f"── 站点状态（共 {len(rows)} 站）" + " " + "─" * 40)
    lines.append(f"{'状态':<9}{'站点':<13}{'名称':<12}{'连续失败':>8}{'连续空':>7}"
                 f"{'历史ok':>7}{'历史fail':>9}{'记录数':>7}  最近成功")
    for row in rows:
        mark = _STATUS_MARK.get(row["status"], row["status"])
        lines.append(
            f"{mark:<9}{row['key']:<13}{row['label'][:10]:<12}"
            f"{row['consec_failures']:>8}{row['consec_no_hits']:>7}"
            f"{row['ok_total']:>7}{row['fail_total']:>9}{row['runs_recorded']:>7}"
            f"  {row['last_ok_run'] or '—'}")

    drift = report.get("extractor_drift") or []
    if drift:
        bad = [d for d in drift if d.get("is_problem")]
        extra = [d for d in drift if d.get("severity") == DRIFT_EXTRA]
        lines.append("")
        lines.append(f"── yt-dlp extractor 离线核对（{len(drift)} 站："
                     f"⚠️{len(bad)} 处漂移 / 💡{len(extra)} 处可优化）" + " " + "─" * 12)
        for d in drift:
            if d.get("is_problem"):
                exts = "/".join(d.get("broken_extractors") or d.get("extractors") or []) or "—"
                lines.append(f"  ⚠️  {d['key']:<13} {exts:<22} {d['note']}")
            elif d.get("severity") == DRIFT_EXTRA:
                lines.append(f"  💡  {d['key']:<13} "
                             f"{'/'.join(d.get('extractors') or []):<22} {d['note']}")
        if not bad:
            lines.append("  ✅ 没有「声明可用实则不可用」的站点")

    probes = report.get("probes") or []
    if probes:
        lines.append("")
        lines.append("── 本轮联网探测" + " " + "─" * 52)
        for p in probes:
            if p.get("skipped"):
                lines.append(f"  [SKIP] {p['key']:<13} {p['skipped']}")
            elif p.get("hit"):
                lines.append(f"  [HIT]  {p['key']:<13} {p['items']} 条  "
                             f"{p['elapsed']}s")
            else:
                lines.append(f"  [MISS] {p['key']:<13} {p['elapsed']}s  "
                             f"{p['error'] or '返回 0 条'}")

    problems = report.get("problems") or []
    lines.append("")
    lines.append(f"── 需要处理（{len(problems)} 项）" + " " * 30)
    if not problems:
        lines.append("  ✅ 没有发现异常")
    else:
        for item in problems[:max_problems]:
            lines.append(f"  • {item}")
        if len(problems) > max_problems:
            lines.append(f"  … 另有 {len(problems) - max_problems} 项未显示")

    lines.append("")
    lines.append("说明：本模块只读写自己生成的台账，不修改、不删除任何用户数据。")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────── #
# CLI
# ────────────────────────────────────────────────────────────────────── #


def _parse_record_spec(spec: str) -> Tuple[str, Dict[str, Any]]:
    """解析 ``--record`` 的参数：``key:ok=12,fail=1,hit,probed``。"""
    if ":" not in spec:
        raise ValueError(f"格式应为 key:ok=N,fail=N,skip=N,hit,probed，收到 {spec!r}")
    key, _, rest = spec.partition(":")
    key = key.strip().lower()
    if not key:
        raise ValueError("站点 key 不能为空")
    fields: Dict[str, Any] = {"ok": 0, "fail": 0, "skip": 0,
                              "hit": False, "probed": True}
    for chunk in rest.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, _, value = chunk.partition("=")
            name = name.strip().lower()
            if name not in ("ok", "fail", "skip"):
                raise ValueError(f"未知计数字段 {name!r}（只支持 ok/fail/skip）")
            fields[name] = int(value.strip() or 0)
        elif chunk.lower() in ("hit", "probed", "no-hit", "nohit"):
            lowered = chunk.lower()
            if lowered == "hit":
                fields["hit"] = True
            elif lowered in ("no-hit", "nohit"):
                fields["hit"] = False
            else:
                fields["probed"] = True
        else:
            raise ValueError(f"无法解析片段 {chunk!r}")
    return key, fields


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="python -m modules.core.utils.site_health",
        description="站点漂移检测：读台账、离线核对 yt-dlp extractor、可选联网探测")
    parser.add_argument("--ledger", default="", help="台账 JSON 路径（默认 data/site_health.json）")
    parser.add_argument("--check-extractors", action="store_true",
                        help="只跑离线 extractor 漂移核对（默认报告里也会带，"
                             "此开关表示「只跑这个」）")
    parser.add_argument("--no-extractors", action="store_true",
                        help="不做离线 extractor 核对")
    parser.add_argument("--probe", action="store_true",
                        help="联网探测各站（会发真实请求；默认关闭）")
    parser.add_argument("--proxy", default="", help="探测用代理，如 socks5://127.0.0.1:10808")
    parser.add_argument("--query", default=DEFAULT_PROBE_QUERY, help="探测关键词")
    parser.add_argument("--limit", type=int, default=1, help="每站至少几条才算命中")
    parser.add_argument("--per-site-timeout", type=float, default=45.0,
                        help="单站探测超时（秒）")
    parser.add_argument("--only", default="", help="只关注这些站点 key（逗号分隔）")
    parser.add_argument("--record", default="", help="手工回报一次结果：key:ok=1,fail=0,hit")
    parser.add_argument("--forget", default="", help="清除某站历史（需 --confirm）")
    parser.add_argument("--clear", action="store_true", help="清空整个台账（需 --confirm）")
    parser.add_argument("--confirm", action="store_true", help="确认执行 --forget / --clear")
    parser.add_argument("--show", type=int, default=30, help="文本报告最多打印多少条问题")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--verbose", action="store_true", help="DEBUG 日志")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    ledger_path = Path(args.ledger) if args.ledger else None
    health = SiteHealth(ledger_path=ledger_path)

    # ── 台账维护动作（需 --confirm） ──────────────────────────────────
    if args.forget or args.clear:
        if not args.confirm:
            print("⚠️ --forget / --clear 需要同时给出 --confirm 才会真正修改台账。")
            return 2
        if args.clear:
            health.clear()
            print(f"已清空台账: {health.path}")
        else:
            ok = health.forget(args.forget)
            print(f"{'已删除' if ok else '未找到'}站点历史: {args.forget}")
        if not (args.json or args.record):
            return 0

    # ── 手工回报 ──────────────────────────────────────────────────────
    if args.record:
        try:
            key, fields = _parse_record_spec(args.record)
        except ValueError as exc:
            print(f"❌ --record 参数错误: {exc}")
            return 2
        health.record_site_run(key, **fields)
        print(f"已记录 {key}: {fields} → 状态 {health.status_of(key)}")
        print(f"台账: {health.path}")
        return 0

    # ── 只跑离线核对 ──────────────────────────────────────────────────
    if args.check_extractors and not args.probe:
        drift = check_extractor_drift()
        if args.json:
            print(json.dumps([d.to_dict() for d in drift],
                             ensure_ascii=False, indent=2))
        else:
            bad = [d for d in drift if d.is_problem]
            extra = [d for d in drift if d.severity == DRIFT_EXTRA]
            unknown = [d for d in drift if d.severity == DRIFT_UNKNOWN]
            print(f"yt-dlp extractor 离线核对：{len(drift)} 站 · "
                  f"⚠️ 漂移 {len(bad)} · 💡 可优化 {len(extra)} · "
                  f"未核对 {len(unknown)}")
            print("-" * 96)
            print(f"  {'站点':<13}{'声明':<7}{'实测':<7}{'结论':<10}extractor")
            for d in drift:
                if d.is_problem:
                    flag = "⚠️ 漂移"
                elif d.severity == DRIFT_EXTRA:
                    flag = "💡 可优化"
                elif d.severity == DRIFT_UNKNOWN:
                    flag = "未能核对"
                else:
                    flag = "一致"
                exts = "/".join(d.extractors or d.broken_extractors) or "—"
                print(f"  {d.key:<13}{str(d.registered):<7}{str(d.detected):<7}"
                      f"{flag:<10}{exts}")
                if d.is_problem:
                    print(f"      ↳ {d.note}")
        return 0

    # ── 常规报告 ──────────────────────────────────────────────────────
    keys = [k.strip() for k in args.only.split(",") if k.strip()] or None
    report = build_report(
        health=health,
        probe=bool(args.probe),
        proxy=args.proxy or None,
        query=args.query,
        limit=args.limit,
        per_site_timeout=args.per_site_timeout,
        extractors=not args.no_extractors,
        keys=keys,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report_text(report, max_problems=args.show))
    return 0


if __name__ == "__main__":
    sys.exit(main())
