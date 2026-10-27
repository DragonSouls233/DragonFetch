# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
定时任务管理器
用于管理系统的定时任务，如每天自动检测模特更新

⭐ 2026-09-22 重写（修复「定时任务单日重复跑两整轮」）
=====================================================

**故障现象**（服务端 09-22 实测）：759 个唯一模特 × **恰好 2 次** = 1518 次检测，
4.4 小时做了 2 倍工作（抓取量翻倍 / 本地目录重复扫描 / 写库翻倍），
且与下载抢同一个 socks5 代理。

**日志铁证**：
```
02:31:25,601  定时任务执行完成        ← 上一轮刚"完成"
02:31:25,601  执行定时任务            ← 同一毫秒下一轮就"执行"
```
`添加定时任务` 出现 **4 次**。

**原实现的三处根因**：
1. ``add_daily_task`` **无条件 append** → 同名任务被注册 4 次，同一秒命中 4 次
2. ``_scheduler_loop`` 用 **精确到秒**（``now.second == task_time.second``）匹配触发，
   且**没有任何「今天已跑过」的记录**。而 ``check_all_models`` 是**真异步**
   （每个模特一次 ``await``），所以一轮 2.2 小时的检测期间事件循环**始终在轮询** ——
   只要目标秒被观察到第二次（``asyncio.sleep(1)`` 不保证 1 秒精度、
   事件循环繁忙时同一次 tick 可落到同一秒），就会再起一轮。
3. ``create_task`` 之后**不跟踪**，无从知道上一轮是否还在跑 → 无防重入。

**修法**（参考 ``sickchill/oldbeard/scheduler.py:27,82,93`` 的 ``lastRun`` 时间戳模型）：
- 幂等注册：按任务名去重，重复注册**替换**定义而非追加
- **当天已执行标记**：``_DAILY_LAST_RUN``（模块级，跨 ``Scheduler`` 实例存活）
  → 语义上保证「**每个任务每天最多执行一次**」，不依赖任何时间匹配的精确性
- 触发条件改为「目标时刻已到 **且** 在宽限窗口内 **且** 今天没跑过」
  （宽限窗口用于覆盖「进程恰好在目标时刻重启」的场景）
- 防重入：同名任务在跑时不重复起
- ``stop()`` 退出前给在跑任务收尾机会，避免 event loop 关闭时任务被**静默销毁**
- 修掉一个**潜伏 bug**：``_run`` 的 ``finally`` 原来关的是 ``self.loop``，
  而 ``update_scheduler_config`` 会 ``stop()`` → ``start()`` 换新 loop，
  导致**旧线程关掉了新 loop**
"""
import asyncio
import logging
from datetime import date, datetime, time
from threading import Lock
from threading import Thread
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: ⭐ 当天已执行记录 —— 模块级，**跨 Scheduler 实例存活**。
#: 键 = 任务名（稳定标识，不含时间），值 = 最近一次执行的日期。
#: 为什么要模块级：``download_manager.update_scheduler_config()`` 会
#: ``scheduler.stop()`` → ``tasks.clear()`` → ``add_daily_task()`` → ``start()``，
#: 若标记只存在实例的任务字典里，换配置后当天就会再跑一遍。
_DAILY_LAST_RUN: Dict[str, date] = {}
_DAILY_LOCK = Lock()


class Scheduler:
    """
    定时任务管理器

    功能：
    1. 支持设置每天固定时间执行的任务
    2. 支持异步任务执行
    3. 线程安全的任务管理
    4. ⭐ 每个任务**每天最多执行一次**（当天已执行标记 + 防重入）
    """

    #: 错过计划时刻后仍允许补跑的宽限窗口（秒）。
    #: 用途：进程恰好在目标时刻前后重启时仍能补上当天这一次。
    #: 设 0 表示严格模式（只认目标秒，不接受补跑）。
    DEFAULT_GRACE_SECONDS: int = 300

    #: ``stop()`` 后等待在跑任务收尾的上限（秒）。超时则取消（会记 WARNING）。
    DEFAULT_SHUTDOWN_WAIT: int = 15

    def __init__(self, grace_seconds: Optional[int] = None,
                 shutdown_wait: Optional[int] = None):
        """初始化定时任务管理器

        Args:
            grace_seconds: 补跑宽限窗口（秒）；None 用 DEFAULT_GRACE_SECONDS
            shutdown_wait: 停止时等待在跑任务收尾的上限（秒）
        """
        self.tasks: List[Dict] = []
        self.running = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[Thread] = None

        self.grace_seconds = (self.DEFAULT_GRACE_SECONDS
                              if grace_seconds is None else int(grace_seconds))
        self.shutdown_wait = (self.DEFAULT_SHUTDOWN_WAIT
                              if shutdown_wait is None else int(shutdown_wait))

        #: 在跑的协程：task_id -> asyncio.Task（仅事件循环线程读写）
        self._in_flight: Dict[str, asyncio.Task] = {}
        #: 已就「错过窗口」告警过的 (task_id, date)，避免刷日志
        self._missed_warned: set = set()

    # ==================== 注册 / 注销 ====================

    @staticmethod
    def _task_id(task: Callable) -> str:
        """任务稳定标识 —— 只用任务名，**不含时间**。

        不含时间是有意为之：``update_scheduler_config`` 改时间后，
        当天已执行的标记仍然有效 → 不会因为「改了时间」而当天再跑一遍。
        """
        return getattr(task, '__name__', None) or getattr(task, '__qualname__', str(task))

    def add_daily_task(self, hour: int, minute: int, second: int,
                       task: Callable, *args, **kwargs) -> str:
        """添加每天固定时间执行的任务（**幂等**）

        ⭐ 与原实现的关键差别：同名任务**只保留一份定义**。
        重复调用（``_init_scheduler`` 每次启动都会调一次）不会再累积出
        4 个相同条目、导致同一秒触发 4 次。

        Args:
            hour: 小时 (0-23)
            minute: 分钟 (0-59)
            second: 秒 (0-59)
            task: 要执行的任务函数
            *args: 任务函数的位置参数
            **kwargs: 任务函数的关键字参数

        Returns:
            str: 任务标识（= 任务名）
        """
        task_id = self._task_id(task)
        new_def = {
            'time': time(hour, minute, second),
            'task': task,
            'args': args,
            'kwargs': kwargs,
        }

        for i, existing in enumerate(self.tasks):
            if existing.get('id') == task_id:
                self.tasks[i] = {'id': task_id, **new_def}
                logger.info(
                    f"定时任务已存在，更新定义（不重复注册）: "
                    f"每天 {hour:02d}:{minute:02d}:{second:02d} 执行 {task_id}")
                return task_id

        self.tasks.append({'id': task_id, **new_def})
        logger.info(
            f"添加定时任务: 每天 {hour:02d}:{minute:02d}:{second:02d} 执行 {task_id}")
        return task_id

    def remove_daily_task(self, task: Callable) -> bool:
        """移除某个每天执行的任务（同时清掉它的当天已执行标记）。"""
        task_id = self._task_id(task) if callable(task) else str(task)
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t.get('id') != task_id]
        removed = len(self.tasks) != before
        with _DAILY_LOCK:
            _DAILY_LAST_RUN.pop(task_id, None)
        self._missed_warned = {x for x in self._missed_warned if x[0] != task_id}
        if removed:
            logger.info(f"已移除定时任务: {task_id}")
        return removed

    def list_tasks(self) -> List[Dict]:
        """列出已注册任务（供诊断 / GUI 展示）。"""
        out = []
        for t in self.tasks:
            tid = t.get('id')
            with _DAILY_LOCK:
                last = _DAILY_LAST_RUN.get(tid)
            out.append({
                'id': tid,
                'time': t['time'].strftime('%H:%M:%S'),
                'last_run_date': last.isoformat() if last else None,
                'ran_today': last == date.today(),
                'in_flight': t.get('id') in self._in_flight,
            })
        return out

    # ==================== 当天已执行标记 ====================

    def has_run_today(self, task: Callable) -> bool:
        """该任务今天是否已执行过。"""
        task_id = self._task_id(task) if callable(task) else str(task)
        with _DAILY_LOCK:
            return _DAILY_LAST_RUN.get(task_id) == date.today()

    def clear_daily_mark(self, task: Optional[Callable] = None) -> None:
        """清除「当天已执行」标记。

        Args:
            task: 为 None 时清除全部任务的标记

        用途：手动重跑（例如定时检测失败后想当天再来一次）。
        """
        with _DAILY_LOCK:
            if task is None:
                _DAILY_LAST_RUN.clear()
            else:
                task_id = self._task_id(task) if callable(task) else str(task)
                _DAILY_LAST_RUN.pop(task_id, None)
        logger.info("已清除定时任务当天执行标记"
                    + ("（全部）" if task is None else f"（{self._task_id(task)}）"))

    def _mark_run_today(self, task_id: str) -> None:
        with _DAILY_LOCK:
            _DAILY_LAST_RUN[task_id] = date.today()

    def _rollback_run_today(self, task_id: str) -> None:
        """仅在「提交都没成功」时回滚当天标记，让下次轮询还能重试。

        ⚠️ 区别于「任务执行失败」：后者**不回滚**，避免失败任务被反复重试
        （sickchill 的 lastRun 也是执行前记账）。
        """
        with _DAILY_LOCK:
            if _DAILY_LAST_RUN.get(task_id) == date.today():
                _DAILY_LAST_RUN.pop(task_id, None)

    # ==================== 启动 / 停止 ====================

    def start(self) -> None:
        """启动定时任务管理器"""
        if self.running:
            logger.warning("定时任务管理器已经在运行")
            return

        self.running = True
        self.thread = Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info(
            f"定时任务管理器已启动（补跑宽限 {self.grace_seconds}s，"
            f"已注册 {len(self.tasks)} 个任务）")

    def stop(self) -> None:
        """停止定时任务管理器（**优雅停止**）

        与原实现的差别：
        - 原实现只 ``running=False`` + ``join(5)``；``_scheduler_loop`` 一退出，
          ``_run`` 的 ``finally`` 立刻 ``loop.close()``，
          **在跑的检测协程被直接销毁**（半途中断、状态不完整）。
        - 现在 ``_scheduler_loop`` 退出前会 ``await`` 在跑任务收尾
          （上限 ``shutdown_wait`` 秒），超时才取消并记 WARNING。
        """
        if not self.running and not (self.thread and self.thread.is_alive()):
            logger.info("定时任务管理器未在运行")
            return

        self.running = False
        if self.thread:
            self.thread.join(timeout=self.shutdown_wait + 5)
            if self.thread.is_alive():
                logger.warning(
                    f"定时任务线程在 {self.shutdown_wait + 5}s 内未退出"
                    f"（可能有长任务仍在收尾）")
        logger.info("定时任务管理器已停止")

    def trigger_now(self, task: Optional[Callable] = None) -> bool:
        """立即手动触发任务（跳过当天已执行标记，但仍受防重入保护）。

        线程安全：可从任意线程调用。

        Args:
            task: 为 None 时触发所有已注册任务

        Returns:
            bool: 是否成功提交
        """
        if not self.loop or not self.running:
            logger.warning("定时任务管理器未运行，无法手动触发")
            return False

        targets = [t for t in self.tasks
                   if task is None or t.get('id') == self._task_id(task)]
        if not targets:
            logger.warning("没有匹配的定时任务可触发")
            return False

        def _submit():
            for t in targets:
                tid = t.get('id')
                if tid in self._in_flight:
                    logger.warning(f"定时任务仍在执行中，跳过手动触发: {tid}")
                    continue
                self._spawn(t)

        try:
            self.loop.call_soon_threadsafe(_submit)
            return True
        except Exception:
            logger.debug("手动触发定时任务失败", exc_info=True)
            return False

    # ==================== 内部实现 ====================

    def _run(self) -> None:
        """运行定时任务循环（独立线程内的专用事件循环）"""
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._scheduler_loop())
        except Exception as e:
            logger.error(f"定时任务管理器错误: {e}")
        finally:
            # ⭐ 只关自己创建的这个 loop，不去动 self.loop。
            #    原实现无条件 `self.loop.close()`：而 update_scheduler_config 会
            #    stop() 之后立刻 start() 装上**新的** loop，
            #    于是旧线程的 finally 把新 loop 关掉了。
            try:
                loop.close()
            except Exception:
                logger.debug("关闭定时任务事件循环失败", exc_info=True)
            if self.loop is loop:
                self.loop = None

    async def _scheduler_loop(self) -> None:
        """定时任务循环：每秒轮询一次，由「当天已执行标记」保证不重复"""
        while self.running:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"定时任务轮询出错: {e}")
                logger.debug("定时任务轮询异常详情", exc_info=True)

            # 每秒检查一次（只作为循环节拍，**不参与时间匹配**）
            await asyncio.sleep(1)

        await self._drain_in_flight()

    def _tick(self, now_dt: Optional[datetime] = None) -> List[str]:
        """轮询一次：返回本次实际触发的 task_id 列表（便于测试断言）。

        Args:
            now_dt: 注入当前时间（仅测试用；生产走 ``datetime.now()``）
        """
        now_dt = now_dt or datetime.now()
        fired: List[str] = []

        # 顺手清理非今日的「错过窗口」告警记录，避免长期运行后无限增长
        if len(self._missed_warned) > 64:
            today = now_dt.date()
            self._missed_warned = {x for x in self._missed_warned if x[1] == today}

        for task_info in list(self.tasks):
            due, reason = self._is_due(task_info, now_dt)
            if not due:
                continue

            task_id = task_info.get('id')
            # ⭐ 防重入：同名任务还在跑就不再起一个
            running_task = self._in_flight.get(task_id)
            if running_task is not None and not running_task.done():
                logger.warning(f"定时任务仍在执行中，跳过本次触发: {task_id}")
                continue

            # ⭐ 先记账再执行（sickchill scheduler.py:93 同款顺序）——
            #    这样即使任务失败也不会当天反复重试。
            self._mark_run_today(task_id)
            atask = self._spawn(task_info)
            if atask is None:
                # ⭐ 连"提交"都没成功（例如事件循环还没就绪）属于**调度器自身故障**，
                #    不是任务失败 → 必须回滚当天标记，否则这一天就被白白烧掉了。
                self._rollback_run_today(task_id)
                logger.warning(f"定时任务提交失败，已回滚当天标记以便下次轮询重试: {task_id}")
                continue
            fired.append(task_id)

        return fired

    def _spawn(self, task_info: Dict) -> Optional[asyncio.Task]:
        """创建并跟踪一个任务协程。"""
        task_id = task_info.get('id')
        try:
            atask = self.loop.create_task(self._execute_task(task_info))
        except Exception as e:
            logger.error(f"提交定时任务失败: {task_id}, 错误: {e}")
            return None
        self._in_flight[task_id] = atask
        atask.add_done_callback(
            lambda _fut, _tid=task_id: self._in_flight.pop(_tid, None))
        return atask

    def _is_due(self, task_info: Dict, now_dt: datetime) -> Tuple[bool, str]:
        """判断任务此刻是否应当触发。

        触发条件（三者同时满足）：
        1. **今天还没执行过**（``_DAILY_LAST_RUN``）
        2. 目标时刻已到（``now >= 今天的目标时刻``）
        3. 距目标时刻不超过宽限窗口（``grace_seconds``，0 = 不允许补跑）

        Returns:
            (是否触发, 原因标签)
        """
        task_id = task_info.get('id')

        if self.has_run_today(task_id):
            return False, 'already-ran-today'

        target = datetime.combine(now_dt.date(), task_info['time'])
        if now_dt < target:
            return False, 'not-yet'

        overdue = (now_dt - target).total_seconds()
        if self.grace_seconds and overdue > self.grace_seconds:
            # 已经错过窗口（例如进程在目标时刻没在跑）——每天只告警一次
            key = (task_id, now_dt.date())
            if key not in self._missed_warned:
                self._missed_warned.add(key)
                logger.warning(
                    f"已错过今日定时窗口: {task_id} 计划 {task_info['time'].strftime('%H:%M:%S')}，"
                    f"当前已晚 {overdue / 60:.1f} 分钟（超过宽限 {self.grace_seconds}s）"
                    f"，今天不再触发")
            return False, 'missed-window'

        if self.grace_seconds == 0 and overdue > 1:
            # 严格模式：只认目标秒（容 1 秒轮询误差）
            return False, 'strict-skip'

        return True, 'due'

    async def _drain_in_flight(self) -> None:
        """循环退出前，给在跑任务一个收尾机会。

        必要性：``_run`` 在 ``run_until_complete`` 返回后会 ``loop.close()``，
        若此时仍有 pending 协程，它们会被**静默销毁**
        （表现为 "Task was destroyed but it is pending!" 或状态写一半）。
        """
        pending = [t for t in self._in_flight.values() if not t.done()]
        if not pending:
            return

        logger.info(
            f"定时任务管理器停止中，等待 {len(pending)} 个在跑任务收尾"
            f"（最多 {self.shutdown_wait}s）...")
        _done, still_pending = await asyncio.wait(
            pending, timeout=self.shutdown_wait)

        for t in still_pending:
            logger.warning("定时任务在停止宽限期内未结束，取消之（状态可能不完整）")
            t.cancel()
        if still_pending:
            await asyncio.wait(still_pending, timeout=5)
        logger.info("在跑定时任务收尾完成")

    async def _execute_task(self, task_info: Dict) -> None:
        """执行任务"""
        task = task_info['task']
        args = task_info['args']
        kwargs = task_info['kwargs']
        task_id = task_info.get('id')

        logger.info(f"执行定时任务: {task_id}")
        started = datetime.now()

        try:
            if asyncio.iscoroutinefunction(task):
                await task(*args, **kwargs)
            else:
                task(*args, **kwargs)

            elapsed = (datetime.now() - started).total_seconds()
            logger.info(
                f"定时任务执行完成: {task_id}（耗时 {elapsed:.1f}s）")
        except asyncio.CancelledError:
            logger.warning(f"定时任务被取消: {task_id}")
            raise
        except Exception as e:
            elapsed = (datetime.now() - started).total_seconds()
            logger.error(
                f"定时任务执行失败: {task_id}, 错误: {e}（耗时 {elapsed:.1f}s）")
            logger.debug("定时任务异常详情", exc_info=True)


# 全局定时任务管理器实例
global_scheduler = Scheduler()


def get_scheduler() -> Scheduler:
    """
    获取全局定时任务管理器实例

    Returns:
        Scheduler: 定时任务管理器实例
    """
    return global_scheduler
