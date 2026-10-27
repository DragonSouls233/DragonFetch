# -*- coding: utf-8 -*-
"""任务队列：并发调度多个分集下载。

用标准库 threading 实现，不依赖 Qt，便于单独测试。
UI 层通过回调接收进度更新。
"""

import queue
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import (applog, category_rules, config, library_db, library_scan,
               nfo_writer, site_api, sites)
from .downloader import run_download, cancel as cancel_download
from .models import Drama, DownloadTask, TaskStatus


def site_label(site_id: str) -> str:
    """站点展示名（用于日志，站点缺失时退回 id）。"""
    s = sites.get_site(site_id)
    return s["name"] if s else (site_id or "未知站点")


def describe_fetch_error(exc: Exception, site_id: str = "") -> str:
    """把取址阶段的异常转成一句可读原因。

    旧版让 ConnectionError 直接冒泡到 _run_one 的外层 except，于是每失败一集
    就往日志里刷一整段 traceback（用户看到的就是满屏 404 堆栈）。这里提炼成
    一句话写进 task.error 与日志文件，既能定位又不淹没日志。
    """
    text = str(exc or "").strip()
    label = site_label(site_id)
    low = text.lower()
    if "404" in text:
        return f"《{label}》上没有这部剧（404），可能已下架或不属于该站点"
    if "403" in text:
        return "被站点拒绝访问（403）"
    if "超时" in text or "timed out" in low:
        return "网络超时，稍后重试"
    return text[:200] or exc.__class__.__name__


class TaskQueue:
    """并发下载队列。

    用法:
        q = TaskQueue(concurrency=2)
        q.on_task_update = fn      # (task) -> None
        q.on_log          = fn      # (str)  -> None
        q.on_finished     = fn      # ()     -> None
        q.add_many(did, title, [1,2,3])
        q.start()
    """

    def __init__(self, concurrency: int = None):
        self.concurrency = concurrency or config.DEFAULT_CONCURRENCY
        self._queue: "queue.Queue[DownloadTask]" = queue.Queue()
        self._tasks: List[DownloadTask] = []
        self._threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False

        # 已在队列里等待（尚未被 worker 取走）的任务 id。
        # 用途：start() 会被多个入口反复调用（入队新任务、切站恢复、点开始）。
        # 没有它就无法区分「还没入队」和「已在队列里排队」，前者漏入队会永久
        # 停在待下载，后者重复入队会被两个线程同时下载同一集。
        self._queued: set = set()
        # 正在被 worker 处理的任务数（含取址/下载/合并全过程）。
        # 队列空 + _active==0 才是真的「全部结束」，否则 worker 会提前退出。
        self._active = 0
        # 运行代次：每次从「停止」进入「运行」时 +1，用于作废上一轮的收尾判定。
        self._gen = 0

        # 元数据归纳：did -> Drama；did -> {ep: 输出路径}
        self.write_nfo: bool = True
        self._dramas: Dict[int, Drama] = {}
        self._ensuring: set = set()      # 正在加载详情的 did，避免并发重复
        self._done: Dict[int, Dict[int, str]] = {}
        self._eps_of: Dict[int, List[int]] = {}   # did -> 全部集号（预计算，免每集扫描）
        self._eps_seen: Dict[int, set] = {}       # did -> 集号集合（入队去重用）
        self._libjson_at: Dict[int, float] = {}   # did -> 上次写 library.json 的时间

        # 回调
        self.on_task_update: Optional[Callable[[DownloadTask], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None
        self.on_finished: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------ 任务管理

    @property
    def tasks(self) -> List[DownloadTask]:
        return list(self._tasks)

    def add(self, did: int, ep: int, title: str, category: str = "") -> DownloadTask:
        """添加一个任务（尚未入队执行）。

        站点在**入队这一刻固化**进任务：之后无论用户怎么切换站点，这个任务都只会
        请求它自己所属站点的后端与域名（旧版任务不带站点，取址时读全局站点，
        切站后剩余任务全部打到错误域名上）。

        category 为空时查一次归属记忆：搜索 / 更新检测等入口拿不到分类，
        但只要这部剧曾在「换脸 / 魔改」TAB 下出现过，仍会自动归到对应文件夹。
        记忆也没有则再查剧库（dramas.category，浏览/下载时随剧入库的权威来源）。
        """
        if not category:
            category = category_rules.of(config.ACTIVE_SITE_ID, did=did, title=title)
        if not category:
            try:
                db_cat = library_db.get_category(config.ACTIVE_SITE_ID, did)
                if db_cat:
                    category = db_cat
            except Exception:                      # noqa: BLE001
                pass
        # 最后兜底：剧名含「换脸 / 魔改」字样（搜索 / 上新等入口下载时常见），
        # 直接据标题归类，避免漏进 huangguoai 站点根目录。
        if not category:
            try:
                category = category_rules.infer_category_from_title(title)
            except Exception:                      # noqa: BLE001
                pass
        task = DownloadTask(did=did, ep=ep, drama_title=title,
                            site_id=config.ACTIVE_SITE_ID, category=category)
        with self._lock:
            self._tasks.append(task)
            self._index_eps([task])
        return task

    def _index_eps(self, tasks: List[DownloadTask]) -> None:
        """维护 did -> 集号列表 的索引（调用方需已持锁）。

        旧实现每完成一集都用 `[t.ep for t in self._tasks if t.did == did]` 现算，
        任务数上万时每次都要扫全表；这里改成入队时一次性建好，完成时 O(1) 取用。
        去重走集合（而不是 `in list`），避免单剧集数多时退化成 O(E²)。
        """
        for t in tasks:
            seen = self._eps_seen.setdefault(t.did, set())
            if t.ep in seen:
                continue
            seen.add(t.ep)
            self._eps_of.setdefault(t.did, []).append(t.ep)

    def add_many(self, did: int, title: str, eps: List[int],
                 category: str = "") -> List[DownloadTask]:
        """批量添加；命中本地自检缓存的完整集直接标记为 DONE。

        注意：这里只用 peek_done（纯内存缓存，绝不 ffprobe）。未命中的集
        保持 PENDING，由下载线程在真正下载前校验并跳过完整文件，
        避免在 UI 线程对整部剧跑 ffprobe 造成界面卡死。

        **队列运行中追加的任务会立即入队**：旧版这里只往 _tasks 里塞，
        随后 _start_queue() 因 `_running` 为真直接 return —— 于是「下载途中
        再点某部剧下载 / 点补下更新」新增的任务永远不会被任何线程取走，
        界面表现为永远停在「待下载」（用户感知就是「卡住）」。
        """
        tasks = []
        for ep in eps:
            task = self.add(did, ep, title, category)
            # 分类目录不同，自检路径必须带上分类，否则换脸/魔改会重复下载
            if library_scan.peek_done(title, ep, category=task.category):
                task.status = TaskStatus.DONE
                task.percent = 100.0
                task.output_path = str(
                    library_scan.video_path(title, ep, category=task.category)
                )
            tasks.append(task)
        self._enqueue_if_running(tasks)
        return tasks

    def add_tasks(self, tasks: List[DownloadTask]) -> None:
        """追加一批已构造好的任务（批量流程使用）。

        若队列正在运行，新任务会**立即入队并被空闲线程接管**；
        否则仅在 _tasks 中暂存，等待下一次 start() 统一入队。
        """
        with self._lock:
            self._tasks.extend(tasks)
            self._index_eps(tasks)
        self._enqueue_if_running(tasks)

    def clear(self) -> None:
        """清空任务列表（仅在运行停止后调用）。"""
        with self._lock:
            self._tasks.clear()
            self._queued.clear()
            self._eps_of.clear()
            self._eps_seen.clear()
            self._libjson_at.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def replace_tasks(self, tasks: List[DownloadTask]) -> None:
        """用给定任务整体替换任务列表（「清空已完成」后重建）。

        不要绕过本方法直接改 _tasks：集号索引与「已入队」标记都建立在任务
        列表之上，外部直接赋值会让索引失效（元数据归纳退化为全表扫描）。
        """
        self.clear()
        with self._lock:
            self._tasks = list(tasks)
            self._index_eps(self._tasks)

    # ------------------------------------------------------------ 执行控制

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """启动/续跑队列（可反复调用，幂等）。

        三个入口都会调它：① 新任务入队后 ② 切回站点恢复 ③ 用户点「开始下载」。
        旧版开头就是 `if self._running: return`，于是**运行中新增的任务永远
        不会被调度**（界面永远停在「待下载」，用户感知即「卡住」）；而且首次
        只按当时的队列长度开线程（`min(concurrency, qsize)`），先下 1 部再追加
        100 部时全程只有 1 路在跑。现在：运行中调用只做「补入队 + 补线程」，
        非运行状态才做完整重启（连 FAILED 一起重排，保留重试语义）。
        """
        with self._lock:
            was_running = self._running
            self._running = True
            if not was_running:
                self._gen += 1              # 新的一轮：让上一轮的收尾判定作废
            gen = self._gen
        self._stop_event.clear()

        self._enqueue_pending(include_failed=not was_running)
        self._ensure_threads()

        if not was_running:
            # 守护线程：所有任务结束后触发 on_finished
            threading.Thread(target=self._wait_all, args=(gen,),
                             daemon=True).start()

    def _ensure_threads(self) -> None:
        """把存活的工作线程补足到 concurrency 条。

        线程数**不按当时的队列长度裁剪**：队列此刻可能只有 1 个任务，但用户
        随时会追加几百个；线程一旦按 qsize 缩水，追加的任务就只能挤在 1~2 路里
        慢慢跑（实测峰值并发只有 1，而设定是 4）。
        """
        with self._lock:
            if not self._running:
                return
            self._threads = [t for t in self._threads if t.is_alive()]
            need = self.concurrency - len(self._threads)
            for _ in range(max(0, need)):
                th = threading.Thread(target=self._worker, daemon=True)
                th.start()
                self._threads.append(th)

    def _enqueue(self, tasks: List[DownloadTask],
                 require_running: bool = True) -> int:
        """把任务放入待执行队列；同一任务绝不重复入队。

        「是否还在运行」的判定与入队在同一把锁内完成，避免与 _wait_all 的
        「判定跑完」交错：否则可能刚判完就入队，任务留在队列里无人消费。
        queue.Queue 无界，put 不会阻塞，持锁入队是安全的。
        """
        with self._lock:
            if require_running and not self._running:
                return 0
            todo = []
            for t in tasks:
                if t.status != TaskStatus.PENDING or id(t) in self._queued:
                    continue
                self._queued.add(id(t))
                todo.append(t)
            for t in todo:
                self._queue.put(t)
        return len(todo)

    def _enqueue_if_running(self, tasks: List[DownloadTask]) -> None:
        """队列正在运行就立即入队（并补齐线程）；否则留给下一次 start()。"""
        if tasks and self._enqueue(tasks):
            self._ensure_threads()

    def _enqueue_pending(self, include_failed: bool) -> None:
        """把 _tasks 里可执行的任务排进队列。

        只排**当前站点**的任务：跨站任务此刻执行会拿当前站点的目录与后端去
        处理别的站点的剧（文件落到错误目录）。它们保持原状，等用户切回对应
        站点时再继续。
        """
        cur = config.ACTIVE_SITE_ID
        wanted = ({TaskStatus.PENDING, TaskStatus.FAILED} if include_failed
                  else {TaskStatus.PENDING})
        with self._lock:
            targets = [t for t in self._tasks
                       if t.status in wanted
                       and (not t.site_id or t.site_id == cur)]
        # 重试语义：旧 _enqueue 只接受 PENDING，FAILED 会被永远跳过（永不重跑）。
        # 现在「全新启动」时把 FAILED 复位为 PENDING，连同可下载任务一起重新入队，
        # 落实 start() 注释里的「保留重试语义」。暂停→恢复（include_failed=False）
        # 时不动 FAILED，避免打断用户「先暂停、稍后重试」的意图。
        if include_failed:
            for t in targets:
                if t.status == TaskStatus.FAILED:
                    t.status = TaskStatus.PENDING
        self._enqueue(targets, require_running=False)

    def _drained(self) -> bool:
        """队列是否真的跑完：既没有排队等待的，也没有正在处理的。"""
        with self._lock:
            return self._active == 0 and self._queue.empty()

    def stop(self) -> None:
        """请求停止（已入队但未开始的任务不再执行）。"""
        self._stop_event.set()
        for t in self._tasks:
            if t.status == TaskStatus.PENDING:
                t.status = TaskStatus.CANCELLED

    def pause(self) -> None:
        """暂停调度：不再取新任务，但**保留 PENDING**（可再次 start 继续）。

        与 stop() 的区别：stop() 把未开始的任务置为 CANCELLED（放弃），
        而切换站点只是临时中断——任务应当保留，切回原站点后继续即可。
        队列里已排队的任务也会被保留（_queued 记录让 start() 不会重复入队）。
        """
        self._stop_event.set()

    def has_pending(self) -> bool:
        """是否还有未完成的任务（用于切站点前提示用户）。"""
        return any(t.status in (TaskStatus.PENDING, TaskStatus.FETCHING,
                                TaskStatus.DOWNLOADING, TaskStatus.MERGING)
                   for t in self._tasks)

    def cancel(self, task: DownloadTask) -> None:
        """取消单个任务。"""
        if task.status in (TaskStatus.DOWNLOADING, TaskStatus.FETCHING, TaskStatus.MERGING):
            cancel_download(task)
            self._notify(task)

    def cancel_all(self) -> None:
        """取消所有进行中的任务。"""
        self.stop()
        for t in self._tasks:
            if t.status in (TaskStatus.DOWNLOADING, TaskStatus.FETCHING, TaskStatus.MERGING):
                cancel_download(t)
                self._notify(t)

    # ------------------------------------------------------------ 内部

    def _worker(self) -> None:
        """工作线程主循环。

        不因「队列暂时取空」就立刻退出：批量下载的下一批任务可能在几百毫秒后
        才入队，线程一退出就没人接了。改为阻塞式取任务，取空且当前没有任何
        在途任务（_active==0）才退出；之后若有新任务入队，_ensure_threads()
        会把线程重新补上。
        """
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._drained():
                    return
                continue

            with self._lock:
                self._active += 1
                self._queued.discard(id(task))
            try:
                if task.status == TaskStatus.CANCELLED:
                    continue
                self._run_one(task)
            finally:
                with self._lock:
                    self._active -= 1
                self._queue.task_done()

    def _run_one(self, task: DownloadTask) -> None:
        """执行单个任务：取地址 → 下载 → 归纳元数据（并写库）。

        站点一律取自**任务自身**（入队时固化）。若任务站点与当前站点不一致则
        不执行：播放地址、下载目录、NFO、封面缓存都是按"当前站点"组织的，
        跨站执行会把文件写进错误站点的目录。此时保留 PENDING（不写库、不改状态），
        等用户切回对应站点后点「开始」即可继续。
        """
        site = task.site_id or config.ACTIVE_SITE_ID
        try:
            if self._stop_event.is_set():
                return

            if task.site_id and task.site_id != config.ACTIVE_SITE_ID:
                self._log(
                    f"[{task.name}] 跳过：任务属于「{site_label(task.site_id)}」，"
                    f"切回该站点后点「开始」即可继续"
                )
                return

            self._ensure_tvshow(task)

            # 下载前自检：本地已有完整文件则直接跳过（不发网络请求）
            # 该检查在工作线程执行，ffprobe 有缓存，不会拖慢界面。
            out_path = library_scan.video_path(
                task.drama_title, task.ep, category=task.category
            )
            if library_scan.validate_video(out_path):
                task.status = TaskStatus.DONE
                task.percent = 100.0
                task.output_path = str(out_path)
                self._record_done(task)
                self._notify(task)
                self._log(f"[{task.name}] 已存在，跳过")
                return

            # 登记「进行中」：集状态 + 下载流水
            library_db.set_episode_status(site, task.did, task.ep, library_db.EP_RUNNING)
            task._dl_id = library_db.log_download(
                site, task.did, task.ep, task.drama_title, library_db.EP_RUNNING
            )

            self._log(f"[{task.name}] 获取播放地址...")
            task.status = TaskStatus.FETCHING
            self._notify(task)

            # 按**任务自带站点**取址（不再读全局站点）；连接类异常转成一句
            # 可读原因，避免每失败一集就刷一整段 traceback。
            try:
                m3u8 = site_api.get_play_url_for(site, task.did, task.ep)
                task.error = ""
            except Exception as exc:                 # noqa: BLE001
                m3u8 = ""
                task.error = describe_fetch_error(exc, site)

            if not m3u8:
                task.status = TaskStatus.FAILED
                task.error = task.error or "未能获取播放地址（付费墙拦截或站点改版）"
                applog.bug("取址失败: site=%s did=%s ep=%s err=%s",
                           site, task.did, task.ep, task.error)
                self._mark_failed(task, site)
                self._notify(task)
                self._log(f"[{task.name}] 取址失败: {task.error}")
                return

            self._log(f"[{task.name}] 开始下载")
            ok = run_download(
                task,
                m3u8,
                on_progress=self._notify,
                on_log=self._log,
            )
            # 失败重试一次：播放地址的 auth_key 时效很短，排队 / 并发耗时后常已过期，
            # 直接复用旧地址会一直 403（旧版即表现为大量「退出码 1」偶发失败）。
            # 这里重新取址再试一次，能显著降低这类假失败。
            if not ok and task.status != TaskStatus.CANCELLED:
                first_err = task.error or ""
                self._log(f"[{task.name}] 失败({first_err})，重取播放地址重试一次…")
                try:
                    retry_url = site_api.get_play_url_for(site, task.did, task.ep)
                except Exception:                    # noqa: BLE001
                    retry_url = ""
                if retry_url:
                    task.error = ""
                    ok = run_download(
                        task, retry_url,
                        on_progress=self._notify, on_log=self._log,
                    )
                if not ok and not task.error:
                    task.error = first_err
            # 用户取消：标记为「已取消」，绝不能再写成失败（否则取消的任务会被
            # 记录成「下载失败」，误导「有更新」TAB 与下载记录，且无法与真实失败区分）。
            if task.status == TaskStatus.CANCELLED:
                self._mark_cancelled(task, site)
                self._notify(task)
                self._log(f"[{task.name}] 已取消")
                return
            if ok:
                self._record_done(task)
            else:
                applog.error("下载失败: site=%s did=%s ep=%s err=%s",
                             site, task.did, task.ep, task.error or "")
                self._mark_failed(task, site)
            self._log(f"[{task.name}] {'完成' if ok else '失败: ' + task.error}")

        except Exception as exc:                 # noqa: BLE001
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            applog.exception("下载任务异常: site=%s did=%s ep=%s",
                             site, task.did, task.ep)
            self._mark_failed(task, site)
            self._notify(task)
            self._log(f"[{task.name}] 异常: {exc}")

    def _mark_failed(self, task: DownloadTask, site: str) -> None:
        """记录失败：集状态 + 下载流水（写库失败不影响下载流程）。"""
        try:
            library_db.set_episode_status(
                site, task.did, task.ep, library_db.EP_FAILED,
                error=task.error or "",
            )
            dl_id = getattr(task, "_dl_id", None)
            if dl_id:
                library_db.update_download(
                    dl_id, library_db.EP_FAILED, error=task.error or ""
                )
        except Exception:                        # noqa: BLE001
            pass

    def _mark_cancelled(self, task: DownloadTask, site: str) -> None:
        """记录取消：集状态 + 下载流水（与「失败」区分，避免误导更新检测）。"""
        try:
            library_db.set_episode_status(
                site, task.did, task.ep, library_db.EP_CANCELLED,
                error="用户已取消",
            )
            dl_id = getattr(task, "_dl_id", None)
            if dl_id:
                library_db.update_download(dl_id, library_db.EP_CANCELLED)
        except Exception:                        # noqa: BLE001
            pass

    # ------------------------------------------------------------ 元数据归纳

    def _ensure_tvshow(self, task: DownloadTask) -> None:
        """首次遇到该剧时加载详情，生成 tvshow.nfo / poster.jpg。

        同一部剧的多个分集可能由不同工作线程并发进入，用锁 + _ensuring
        集合保证详情只加载一次（避免重复请求与重复写文件）。
        """
        if not self.write_nfo or task.did in self._dramas:
            return
        with self._lock:
            if task.did in self._dramas or task.did in self._ensuring:
                return
            self._ensuring.add(task.did)
        try:
            # 详情按**任务自带站点**抓取（并发安全，不读全局站点）
            sid = task.site_id or config.ACTIVE_SITE_ID
            # 批量准备阶段已经抓过详情页：直接复用，避免在这里二次请求。
            # 这部剧的首个分集若被某个下载线程堵在详情请求上，其余分集也会
            # 排队等锁，批量入队时开头就会出现「一动不动」的假死。
            detail = getattr(task, "detail", None) or \
                site_api.load_detail_for(sid, task.did)
            # 封面源：详情页 og:image 优先，失败则退回卡片封面（pic.zdmhyg.cn 那张）
            covers = []
            if detail.get("cover"):
                covers.append(detail["cover"])
            if task.cover and task.cover not in covers:
                covers.append(task.cover)

            drama = Drama(
                did=task.did,
                title=detail.get("title") or task.drama_title,
                desc=detail.get("desc", ""),
                tags=detail.get("tags", []),
                cover=covers[0] if covers else "",
                category=task.category,
            )
            if drama.title:
                task.drama_title = drama.title
            self._dramas[task.did] = drama

            # 单集类（换脸/魔改）按电影刮削写 movie.nfo；多集剧写 tvshow.nfo
            nfo_name = "tvshow.nfo"
            if category_rules.is_single_ep(task.category):
                nfo_writer.write_movie_nfo(drama)
                nfo_name = "movie.nfo"
            else:
                nfo_writer.write_tvshow_nfo(drama, detail.get("episode_count", 0))

            # 封面：优先用本地已缓存封面（DATA/covers，与应用内网格同源，稳定可靠），
            # 再回退「详情页封面 / 卡片封面」的实时抓取 + 解密。这样即便实时抓取偶发
            # 失败（站点限流 / 解密 race），已下载的剧也大概率能从缓存拿到海报，
            # 避免「tvshow.nfo 有了却没有刮削封面」的情况。
            cover_bytes = None
            try:
                cover_bytes = nfo_writer.cached_cover_bytes(sid, task.did)
            except Exception:                        # noqa: BLE001
                cover_bytes = None
            if not cover_bytes and covers:
                from .http_client import fetch_bytes
                from .media_crypto import decrypt_cover
                for cu in covers[:2]:                 # 最多试 2 个源，别无限等
                    raw = fetch_bytes(cu, timeout=config.META_COVER_TIMEOUT)
                    if raw:
                        p = decrypt_cover(raw)
                        if p:
                            cover_bytes = p
                            break
            if cover_bytes:
                nfo_writer.save_poster(drama, cover_bytes)
                self._log(f"[{drama.title}] 元数据已归纳: {nfo_name} / poster.jpg")
            else:
                self._log(f"[{drama.title}] 封面获取失败，已跳过 poster.jpg（不影响视频）")
        except Exception as exc:                 # noqa: BLE001 - 元数据失败不阻塞下载
            self._log(f"[{task.name}] 元数据归纳失败(不影响下载): {exc}")
        finally:
            with self._lock:
                self._ensuring.discard(task.did)

    def _record_done(self, task: DownloadTask) -> None:
        """记录单集完成：写数据库 + 集 NFO + 刷新 library.json。"""
        # 1) 数据库（与 NFO 开关解耦，始终记录）
        try:
            site = task.site_id or config.ACTIVE_SITE_ID
            size = 0
            if task.output_path:
                try:
                    size = Path(task.output_path).stat().st_size
                except OSError:
                    size = 0
            library_db.set_episode_status(
                site, task.did, task.ep, library_db.EP_DONE,
                file_path=task.output_path or "", size_bytes=size,
            )
            dl_id = getattr(task, "_dl_id", None)
            if dl_id:
                library_db.update_download(dl_id, library_db.EP_DONE, bytes_done=size)
        except Exception:                        # noqa: BLE001
            pass

        # 2) NFO / library.json
        if not self.write_nfo:
            return
        drama = self._dramas.get(task.did)
        if drama is None:
            return

        # 单集类（换脸/魔改）文件名为 <剧名>.mp4，没有 S01E01 集号，
        # 再写 {S01E01}.nfo 会与视频对不上（Kodi 无法配对），直接跳过。
        if not category_rules.is_single_ep(task.category):
            nfo_writer.write_episode_nfo(task)

        done_map = self._done.setdefault(task.did, {})
        if task.output_path:
            done_map[task.ep] = task.output_path

        # 集号列表在入队时已建好索引，这里 O(1) 取用（旧版每集都扫全表）。
        # 索引可能缺失（例如外部直接改写 _tasks 的场景），此时按需补建一次，
        # 保证「全部下完」的判定始终基于**整部剧**的集数而不是已完成的那几集。
        eps = self._eps_of.get(task.did)
        if eps is None:
            eps = sorted({t.ep for t in self._tasks if t.did == task.did})
            with self._lock:
                self._eps_of[task.did] = list(eps)
                self._eps_seen[task.did] = set(eps)
        self._maybe_update_library_json(drama, sorted(eps), done_map, task.did)

    # 同一部剧的 library.json 最小重写间隔（秒）
    _LIBJSON_MIN_INTERVAL = 1.5

    def _maybe_update_library_json(self, drama: Drama, total_eps: List[int],
                                   done_map: Dict[int, str], did: int) -> None:
        """节流写 library.json。

        旧实现「每完成一集就重写一次整部剧清单」：一季 100 集 = 100 次 JSON
        序列化 + 100 次落盘，批量下几百部剧时是相当可观的无谓 IO 与卡顿。
        现在同一部剧最快 1.5 秒写一次，全部下完时再补一次收尾写——清单每次
        都带最新 done_map，内容不丢。
        """
        now = time.monotonic()
        last = self._libjson_at.get(did, 0.0)
        finished = bool(total_eps) and all(ep in done_map for ep in total_eps)
        if not finished and now - last < self._LIBJSON_MIN_INTERVAL:
            return
        self._libjson_at[did] = now
        nfo_writer.update_library_json(drama, total_eps, done_map)

    def _wait_all(self, gen: int) -> None:
        """等待所有工作线程结束，触发完成回调。

        只 join 一轮是不够的：等待期间用户可能又追加了任务并补了新线程，
        旧的一轮 join 返回时会误判「跑完了」。因此循环 join，直到队列真的
        排空为止。gen 为本轮的运行代次——若期间又开启了新一轮，本轮直接
        作废（否则会把新一轮的「刚开始」误报成「已结束」）。
        """
        while True:
            with self._lock:
                threads = list(self._threads)
            for th in threads:
                th.join()
            if self._stop_event.is_set():
                break
            # 「判定跑完」与「置 _running=False」在同一次持锁内完成，
            # 否则并发的 add_tasks 可能恰好在判定之后入队，任务无人消费。
            with self._lock:
                if self._gen != gen:
                    return
                if self._active == 0 and self._queue.empty():
                    self._running = False
                    self._threads = []
                    break
            self._ensure_threads()

        if self.on_finished:
            try:
                self.on_finished()
            except Exception:                    # noqa: BLE001 - 回调异常不能影响队列
                pass

    def _notify(self, task: DownloadTask) -> None:
        cb = self.on_task_update
        if cb is None:
            return
        try:
            cb(task)
        except Exception:                        # noqa: BLE001
            # UI 回调绝不允许影响下载线程：一旦回调抛错（如程序退出时桥接对象被
            # 销毁触发 '_Bridge' does not have a signal），若直接冒泡会杀掉整个
            # worker 线程，导致后续任务无人消费、下载卡住。这里吞掉异常，保证
            # 下载继续。注意：此处被吞掉的异常不影响「任务本身」的成功/失败判定。
            pass

    def _log(self, msg: str) -> None:
        cb = self.on_log
        if cb is None:
            return
        try:
            cb(msg)
        except Exception:                        # noqa: BLE001
            pass

    # ------------------------------------------------------------ 统计

    def stats(self) -> dict:
        """统计各状态任务数。"""
        s = {st: 0 for st in TaskStatus}
        for t in self._tasks:
            s[t.status] += 1
        return {
            "total": len(self._tasks),
            "done": s[TaskStatus.DONE],
            "failed": s[TaskStatus.FAILED],
            "running": s[TaskStatus.DOWNLOADING] + s[TaskStatus.FETCHING] + s[TaskStatus.MERGING],
            "pending": s[TaskStatus.PENDING],
            "cancelled": s[TaskStatus.CANCELLED],
        }
