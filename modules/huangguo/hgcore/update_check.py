# -*- coding: utf-8 -*-
"""更新监测：发现「已下载的剧」在平台上新增了集数，并给出待补下载的集号。

设计要点（与需求一一对应）:
  1. 只检查**本地有下载记录**的剧 —— 否则每轮要抓全站几千部详情，请求爆炸。
  2. 基线是 dramas.episode_count（平台总集数），不是「数文件个数」——
     文件可能被删/损坏，DB 才是唯一权威。
  3. **防抖**：一次抓取异常（站点改版 / 临时 404 / 网络抖动）不能让用户误以为更新。
     新集数必须**连续两次检测结果一致**才采信（confirm=True 时）。
     手动点击「检测更新」可 confirm=False 立即采信。
  4. 增量识别：新增集 = {1..新集数} 中**不在 episodes(status=done)** 的集号。
     所以「A 剧原本 3 集 → 变 4 集」时，若 1-3 已下载，只会得到 {4}；
     若其中 2 集缺失，则会一并补上，符合「只下没下完的」。
  5. **只检查「短剧 / 漫剧」**：换脸 / 魔改是单集作品（无「更新集数」概念），
     由 category_rules 的归属记忆识别后直接跳过，不抓详情、不计入统计。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, List, Optional
import json
import time

from . import applog, category_rules, library_db, notify, site_api, sites

_PEND_KEY = "upd_pend:{site}:{did}"
# 「有更新」TAB 的待补清单（持久化在 meta 表，跨重启保留）
_PEND_LIST_KEY = "updates_pending"
# 是否已按本地记录回填过一次（一次性迁移标记）
_PEND_SEED_KEY = "updates_pending_seeded"


@dataclass
class UpdateInfo:
    """一处更新（某剧在平台上新增了集数）。"""
    site_id: str
    did: str
    title: str
    base_count: int
    new_count: int
    new_eps: List[int] = field(default_factory=list)
    cover: str = ""

    @property
    def site_name(self) -> str:
        s = sites.get_site(self.site_id)
        return s["name"] if s else self.site_id

    @property
    def added(self) -> int:
        return max(0, self.new_count - self.base_count)

    @property
    def text(self) -> str:
        eps = "、".join(str(e) for e in self.new_eps[:8])
        more = "" if len(self.new_eps) <= 8 else f" 等 {len(self.new_eps)} 集"
        return (f"《{self.title}》原 {self.base_count} 集 → 现 {self.new_count} 集，"
                f"待补下载：{eps}{more}")


@dataclass
class CheckResult:
    """一次检测的完整结果（供 UI 展示进度与统计）。"""
    updates: List[UpdateInfo] = field(default_factory=list)
    checked: int = 0
    failed: int = 0
    pending: int = 0        # 首次发现、等下一轮确认的
    site_id: Optional[str] = None

    @property
    def total_new_eps(self) -> int:
        return sum(len(u.new_eps) for u in self.updates)


def _baseline(row: dict) -> int:
    """取该剧的集数基线（优先平台总集数，退化到已跟踪的最大集号）。"""
    return int(row.get("platform_eps") or 0) or int(row.get("total_eps") or 0)


def is_monitored(row: dict) -> bool:
    """该剧是否属于「需要检查更新」的类别。

    只检查短剧 / 漫剧；换脸 / 魔改 是单集作品，无「新增集数」概念，直接排除。
    归属由 category_rules 记忆判定（按 站点+did、站点+标题 双索引）。
    """
    cat = category_rules.of(row.get("site_id") or "", row.get("did"), row.get("title"))
    return not category_rules.is_single_ep(cat)


def _fetch_count(site_id: str, did) -> int:
    """抓一次详情，返回平台集数（失败/解析不出返回 0）。"""
    return _fetch_detail(site_id, did)[0]


def _fetch_detail(site_id: str, did):
    """抓一次详情，返回 (可下载集数, 可下载集号列表)。

    口径说明：
      - huangguoai 的详情页把「未更新」的集也画进网格（无 href 的死按钮），
        parser_detail 已只返回**已更新**的集，这里的 episode_count 就是
        「已更新集数」，不会再把「共 8 集·已更新 1 集」误判成 8 集。
      - huangguome 的分集多为金币/会员解锁，免费集才算「可下载」，否则
        检测出的「待补集」永远取不到地址（付费墙），徒增噪声。
    """
    det = site_api.load_detail_for(site_id, did)
    free = det.get("free_eps")
    if free:
        eps = [int(e) for e in free]
        return (max(eps) if eps else 0), eps
    eps = [int(e) for e in (det.get("episodes") or [])]
    count = int(det.get("episode_count") or 0)
    if count <= 0:
        count = max(eps) if eps else 0
    if not eps and count > 0:
        eps = list(range(1, count + 1))
    return count, eps


# ---------------------------------------------------------------- 下架（404）标记

_STALE_AFTER = 3          # 连续 404 达到该次数即列入忽略名单


def _fail_key(site_id: str, did) -> str:
    return f"upd_fail:{site_id}:{did}"


def _stale_key(site_id: str, did) -> str:
    return f"upd_stale:{site_id}:{did}"


def is_stale(site_id: str, did) -> bool:
    """该剧是否已被标记为「疑已下架」，不再参与更新检测。"""
    try:
        return bool(library_db.get_kv(_stale_key(site_id, str(did))))
    except Exception:                            # noqa: BLE001
        return False


def _note_failure(row: dict, exc: Exception, log) -> None:
    """抓取失败时累计次数；连续 404 到阈值就列入忽略名单（只记一次 info）。"""
    if "404" not in str(exc):
        return
    site_id, did = row["site_id"], str(row["did"])
    try:
        n = int(library_db.get_kv(_fail_key(site_id, did)) or 0) + 1
        library_db.set_kv(_fail_key(site_id, did), str(n))
        if n >= _STALE_AFTER and not library_db.get_kv(_stale_key(site_id, did)):
            library_db.set_kv(_stale_key(site_id, did), "1")
            log.info("更新检测：《%s》连续 %d 次 404（疑已下架），已列入忽略名单",
                     row.get("title") or did, n)
    except Exception:                            # noqa: BLE001
        pass


def _clear_failure(row: dict) -> None:
    """抓取成功后清零失败计数。"""
    try:
        key = _fail_key(row["site_id"], str(row["did"]))
        if library_db.get_kv(key):
            library_db.set_kv(key, "0")
    except Exception:                            # noqa: BLE001
        pass


def check_updates(site_id: Optional[str] = None,
                  max_workers: int = 4,
                  confirm: bool = True,
                  timeout: int = 20,
                  on_progress: Optional[Callable[[int, int, str], None]] = None,
                  ) -> CheckResult:
    """检测已下载剧的新增集。

    site_id    None = 检测所有站点
    confirm    True = 需连续两次一致才采信（默认，用于定时/启动检测）
               False = 立即采信（用于用户手动点击）
    on_progress(done, total, title) 进度回调
    """
    result = CheckResult(site_id=site_id)
    try:
        rows = library_db.list_downloaded_dramas(site_id=site_id, include_zero=False)
    except Exception as exc:                     # noqa: BLE001
        applog.get_logger().bug("更新检测：读取下载记录失败：%s", exc)
        return result

    # 只检查有基线的剧（基线为 0 说明详情从未成功解析过，先跳过）
    # 且只检查短剧 / 漫剧（换脸 / 魔改 为单集作品，无更新概念，排除）
    # 且跳过「孤儿记录」（episodes 有、dramas 无）：无详情可抓，检查必然 404
    # 且跳过「疑已下架」（连续 404 达阈值）：不再每轮刷失败日志
    targets = [r for r in rows
               if _baseline(r) > 0 and str(r["did"])
               and r.get("has_meta", 1) and is_monitored(r)
               and not is_stale(r["site_id"], r["did"])]
    total = len(targets)
    result.checked = total
    if not total:
        return result

    log = applog.get_logger()
    log.info("更新检测开始：%d 部剧（站点=%s，确认模式=%s）",
             total, site_id or "全部", "连续两次" if confirm else "立即")

    def _work(row: dict):
        count, eps = _fetch_detail(row["site_id"], row["did"])
        return row, count, eps

    done = 0
    with ThreadPoolExecutor(max_workers=max(2, max_workers)) as ex:
        futures = {ex.submit(_work, r): r for r in targets}
        for fut in as_completed(futures):
            row = futures[fut]
            done += 1
            try:
                row, new_count, avail_eps = fut.result()
            except Exception as exc:             # noqa: BLE001
                result.failed += 1
                _note_failure(row, exc, log)
                log.debug("更新检测失败 %s/%s: %s",
                          row["site_id"], row["did"], exc)
                if on_progress:
                    on_progress(done, total, row.get("title") or "")
                continue

            _clear_failure(row)
            if on_progress:
                on_progress(done, total, row.get("title") or "")

            _evaluate(row, new_count, avail_eps, confirm, result, log)

    if result.updates:
        log.info("更新检测完成：发现 %d 部有新增，共 %d 集待补",
                 len(result.updates), result.total_new_eps)
    else:
        log.info("更新检测完成：无新增（检查 %d 部，失败 %d，待确认 %d）",
                 result.checked, result.failed, result.pending)

    # 并入「有更新」TAB 的待补清单（持久化，跨重启保留）
    try:
        n = save_pending(result)
        log.info("待补清单现有 %d 部剧", n)
    except Exception as exc:                     # noqa: BLE001
        log.bug("更新待补清单写入异常：%s", exc)
    return result


def _evaluate(row: dict, new_count: int, avail_eps: List[int], confirm: bool,
              result: CheckResult, log) -> None:
    """把单部剧的检测结果与基线比对，必要时记入 result.updates。"""
    site_id = row["site_id"]
    did = str(row["did"])
    title = row.get("title") or did
    base = _baseline(row)
    pend_key = _PEND_KEY.format(site=site_id, did=did)

    # 抓不到有效集数（页面异常/网络问题）：**不动基线**，也不能当作「无更新」
    if new_count <= 0:
        return

    if new_count <= base:
        # 恢复正常：清掉可能残留的待确认记录
        if library_db.get_kv(pend_key):
            library_db.set_kv(pend_key, "")
        return

    # 防抖：连续两次一致才采信
    if confirm:
        prev = library_db.get_kv(pend_key) or ""
        if prev != str(new_count):
            library_db.set_kv(pend_key, str(new_count))
            result.pending += 1
            log.debug("更新检测：%s 集数 %d→%d，等下一轮确认", title, base, new_count)
            return

    # 确认更新：算出真正缺失的集（只取**确实存在**的集号，不臆造 ep）
    try:
        done_eps = set(library_db.known_done_eps(site_id, did).keys())
    except Exception:                            # noqa: BLE001
        done_eps = set()
    have = [int(e) for e in (avail_eps or [])]
    if have:
        new_eps = [e for e in have if e not in done_eps]
    else:
        new_eps = [e for e in range(1, new_count + 1) if e not in done_eps]
    if not new_eps:
        # 平台集数涨了但都已下载：视为无待补，只更新基线
        library_db.set_episode_count(site_id, did, new_count)
        library_db.set_kv(pend_key, "")
        return

    # 写入新基线，清掉待确认标记
    library_db.set_episode_count(site_id, did, new_count)
    library_db.set_kv(pend_key, "")

    result.updates.append(UpdateInfo(
        site_id=site_id, did=did, title=title,
        base_count=base, new_count=new_count, new_eps=new_eps,
        cover=row.get("cover_url") or "",
    ))


# ---------------------------------------------------------------- 待补清单（「有更新」TAB）

def _load_pending_raw() -> List[dict]:
    """读取原始待补清单（JSON 存于 meta 表）。"""
    raw = library_db.get_kv(_PEND_LIST_KEY) or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("site_id") and d.get("did")]


def _save_pending_raw(items: List[dict]) -> None:
    try:
        library_db.set_kv(_PEND_LIST_KEY,
                          json.dumps(items, ensure_ascii=False, separators=(",", ":")))
    except Exception as exc:                     # noqa: BLE001
        applog.bug("写待补清单失败：%s", exc)


def _still_missing(site_id: str, did, eps) -> List[int]:
    """筛掉已下载完成的集，返回仍待补的集号。"""
    try:
        done = set(library_db.known_done_eps(site_id, did).keys())
    except Exception:                            # noqa: BLE001
        done = set()
    out = []
    for e in (eps or []):
        try:
            n = int(e)
        except (TypeError, ValueError):
            continue
        if n not in done:
            out.append(n)
    return out


def save_pending(result: "CheckResult") -> int:
    """把本轮检测到的更新并入「待补清单」并持久化（供「有更新」TAB 展示）。

    - 本轮命中的剧：写入 / 刷新记录
    - 已有记录：仅保留其中仍待补的集；已全部下完的自动移除
    返回清理后清单里剩余的剧数。
    """
    items = {f'{i.get("site_id")}:{i.get("did")}': dict(i) for i in _load_pending_raw()}
    for u in result.updates:
        items[f"{u.site_id}:{u.did}"] = {
            "site_id": u.site_id,
            "did": str(u.did),
            "title": u.title,
            "cover": getattr(u, "cover", "") or "",
            "new_eps": [int(e) for e in (u.new_eps or [])],
            "base_count": int(u.base_count or 0),
            "new_count": int(u.new_count or 0),
            "at": int(time.time()),
        }
    out = []
    for it in items.values():
        miss = _still_missing(it.get("site_id", ""), it.get("did"), it.get("new_eps"))
        if miss:
            it["new_eps"] = miss
            out.append(it)
    _save_pending_raw(out)
    return len(out)


def pending_updates() -> List[dict]:
    """读取待补清单（读取时再剔除已下完的项，保证 TAB 状态实时准确）。

    首次读取时按本地下载记录回填一次：更新检测「采信」后会把平台集数
    写成新基线，之后不再重复上报；因此本 TAB 上线前已确认过的更新不会
    自动出现，需要按「已下 < 平台集数」回填，保证一打开就有内容。
    """
    raw = _load_pending_raw()
    if not library_db.get_kv(_PEND_SEED_KEY):
        try:
            seeded = _seed_from_db()
        except Exception as exc:                 # noqa: BLE001
            applog.bug("回填待补清单失败：%s", exc)
            seeded = []
        if seeded:
            known = {f'{i.get("site_id")}:{i.get("did")}' for i in raw}
            raw = raw + [i for i in seeded
                         if f'{i.get("site_id")}:{i.get("did")}' not in known]
            _save_pending_raw(raw)
        try:
            library_db.set_kv(_PEND_SEED_KEY, "1")
        except Exception:                        # noqa: BLE001
            pass

    out = []
    for it in raw:
        miss = _still_missing(it.get("site_id", ""), it.get("did"), it.get("new_eps"))
        if miss:
            if miss != it.get("new_eps"):
                it = dict(it)
                it["new_eps"] = miss
            out.append(it)
    if len(out) != len(raw):
        _save_pending_raw(out)
    return out


def _seed_from_db() -> List[dict]:
    """按本地下载记录回填待补清单（仅短剧 / 漫剧；跳过孤儿与单集类）。"""
    out: List[dict] = []
    rows = library_db.list_downloaded_dramas(include_zero=False)
    for r in rows:
        if not r.get("has_meta"):
            continue
        done = int(r.get("done_eps") or 0)
        total = int(r.get("platform_eps") or 0)
        if done <= 0 or total <= done:
            continue
        did = str(r.get("did"))
        cat = category_rules.of(r.get("site_id") or "", did, r.get("title"))
        if category_rules.is_single_ep(cat):
            continue
        try:
            done_eps = set(library_db.known_done_eps(r.get("site_id"), did).keys())
        except Exception:                        # noqa: BLE001
            done_eps = set()
        miss = [e for e in range(1, total + 1) if e not in done_eps]
        if not miss:
            continue
        out.append({
            "site_id": r.get("site_id"),
            "did": did,
            "title": r.get("title") or did,
            "cover": r.get("cover_url") or "",
            "new_eps": miss,
            "base_count": done,
            "new_count": total,
            "at": int(time.time()),
        })
    return out


def pending_count() -> int:
    return len(pending_updates())


def notify_updates(result: CheckResult) -> None:
    """把检测结果通过 notify 层推送出去（桌面通知 + Webhook）。"""
    if not result.updates:
        return
    lines = "\n".join(u.text for u in result.updates[:5])
    if len(result.updates) > 5:
        lines += f"\n…等共 {len(result.updates)} 部剧"
    notify.notify_async(
        f"发现 {len(result.updates)} 部剧更新",
        f"共 {result.total_new_eps} 集待补下载\n{lines}",
        kind=notify.KIND_UPDATE,
        payload={"updates": [
            {"site_id": u.site_id, "did": u.did, "eps": u.new_eps}
            for u in result.updates
        ]},
    )


def run_check_and_notify(site_id: Optional[str] = None,
                         confirm: bool = True,
                         on_progress=None) -> CheckResult:
    """检测 + 推送（供定时任务 / 启动延迟调用）。"""
    res = check_updates(site_id=site_id, confirm=confirm, on_progress=on_progress)
    notify_updates(res)
    return res
