# -*- coding: utf-8 -*-
"""运行日志：按天分文件记录，BUG/ERROR 单独分级便于定位。

设计要点
--------
* **一天一个文件**：`DATA/logs/YYYY-MM-DD.log`，跨零点自动切新文件，
  不用重启进程。日志按时间顺序追加，便于排查"某天出过什么问题"。
* **自定义 `BUG` 级别**（35，介于 WARNING 与 ERROR 之间）：
  用来标记「不致命但明显不对」的情况，例如
  "封面解密失败"、"m3u8 地址为空"、"站点识别回退到默认"。
  这样在日志查看器里可以单独把 BUG 行标红。
* **格式**：`时间 [级别] [模块] 消息`，异常自动附 traceback。
* **控制台同步输出**：开发时直接看 stderr，打包后用户只看文件。
* **自动清理**：默认保留最近 30 天，避免日志无限增长。

用法：
    from hgcore import applog
    log = applog.get_logger()
    log.info("首页加载完成：%d 部", n)
    log.bug("封面解密失败 did=%s", did)      # 会以 BUG 级别记录
    log.error("下载失败：%s", err, exc_info=True)
"""
import logging
import sys
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from . import config


# 自定义 BUG 级别（WARNING=30, ERROR=40）
BUG_LEVEL = 35
logging.addLevelName(BUG_LEVEL, "BUG")

# 应用版本号（启动横幅与日志统一引用，避免各处写死导致不一致）
APP_VERSION = "1.11"

_DATE_FMT = "%Y-%m-%d"
_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_RETENTION_DAYS = 30
_MAX_BYTES_WARN = 20 * 1024 * 1024      # 单日超 20MB 时提醒


class DailyFileHandler(logging.Handler):
    """按「当天日期」写文件的 handler，跨零点自动切换文件。

    标准库的 TimedRotatingFileHandler 只在收到新日志时才 rollover，
    且文件名带后缀（app.log.2026-09-10），不如直接按日期命名直观。
    这里自己实现，逻辑简单且行为可预测。
    """

    def __init__(self, log_dir: Path, encoding: str = "utf-8") -> None:
        super().__init__()
        self._dir = Path(log_dir)
        self._encoding = encoding
        self._date: Optional[str] = None
        self._stream = None

    # -------------------------------------------------- 内部
    def _ensure_stream(self) -> None:
        today = datetime.now().strftime(_DATE_FMT)
        if self._stream is not None and self._date == today:
            return
        # 跨天（或首次）：关旧开新
        self._close()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._stream = open(self._dir / f"{today}.log", "a",
                                encoding=self._encoding)
            self._date = today
        except OSError:
            self._stream = None      # 落盘失败也不能让程序崩

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None

    # -------------------------------------------------- Handler 接口
    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_stream()
            if self._stream is None:
                return
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()          # 崩溃前的日志也要保住
        except Exception:                 # noqa: BLE001 - 记日志不能反过来炸程序
            self.handleError(record)

    def close(self) -> None:
        self._close()
        super().close()

    @property
    def current_path(self) -> Optional[Path]:
        """当前正在写的日志文件路径（没写过则为 None）。"""
        if self._date:
            return self._dir / f"{self._date}.log"
        return None


# ---------------------------------------------------------------- Logger

_logger: Optional[logging.Logger] = None
_file_handler: Optional[DailyFileHandler] = None
_lock = threading.Lock()
_installed = False


class _BugMixin(logging.Logger):
    """给 Logger 加一个 .bug() 便捷方法。"""

    def bug(self, msg, *args, **kwargs):      # noqa: D102
        if self.isEnabledFor(BUG_LEVEL):
            self._log(BUG_LEVEL, msg, args, **kwargs)


logging.setLoggerClass(_BugMixin)


def get_logger() -> "_BugMixin":
    """取全局 logger（首次调用时初始化）。"""
    global _logger, _file_handler
    if _logger is not None:
        return _logger

    with _lock:
        if _logger is not None:
            return _logger

        logger = logging.getLogger("hgdown")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        # 文件：按天
        _file_handler = DailyFileHandler(config.LOG_DIR)
        _file_handler.setLevel(logging.DEBUG)
        _file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-5s] [%(name)s] %(message)s",
            datefmt=_TIME_FMT,
        ))
        logger.addHandler(_file_handler)

        # 控制台：只到 INFO，避免刷屏
        try:
            console = logging.StreamHandler(sys.stderr)
            console.setLevel(logging.INFO)
            console.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)-5s] %(message)s",
                datefmt="%H:%M:%S",
            ))
            logger.addHandler(console)
        except Exception:                 # noqa: BLE001
            pass

        _logger = logger
        return logger


def current_log_path() -> Optional[Path]:
    """今天的日志文件路径。"""
    get_logger()
    return _file_handler.current_path if _file_handler else None


# ---------------------------------------------------------------- 便捷函数

def info(msg: str, *args) -> None:
    get_logger().info(msg, *args)


def warning(msg: str, *args) -> None:
    get_logger().warning(msg, *args)


def bug(msg: str, *args) -> None:
    """记录一条 BUG（不致命但明显不对）。"""
    get_logger().bug(msg, *args)


def error(msg: str, *args, exc_info: bool = False) -> None:
    get_logger().error(msg, *args, exc_info=exc_info)


def exception(msg: str, *args) -> None:
    """在 except 块里用：自动带 traceback。"""
    get_logger().error(msg, *args, exc_info=True)


def install_excepthook() -> None:
    """把未捕获异常写进日志（以 BUG 级别 + traceback）。

    同时保留默认行为（打印到 stderr），方便开发时直接看到。
    """
    global _installed
    if _installed:
        return
    _installed = True

    logger = get_logger()
    prev_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            prev_hook(exc_type, exc_value, exc_tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.bug("未捕获异常:\n%s", text.rstrip())
        prev_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def install_thread_excepthook() -> None:
    """QThread 里的未捕获异常默认会被吞掉，单独兜一层。

    调用后需在 QThread.run 外层用 `applog.guard_thread()` 包一下，
    或直接依赖下面的 `guard_thread` 装饰器。
    """
    if hasattr(threading, "excepthook"):
        def _hook(args):                  # noqa: ANN001
            logger = get_logger()
            text = "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback))
            logger.bug("线程未捕获异常:\n%s", text.rstrip())
        threading.excepthook = _hook


# ---------------------------------------------------------------- 读取（给日志查看器用）

def available_dates() -> List[str]:
    """已有日志的日期列表（倒序，最近的在最前）。"""
    if not config.LOG_DIR.is_dir():
        return []
    dates = []
    for p in config.LOG_DIR.glob("*.log"):
        if len(p.stem) == 10 and p.stem[4] == "-":       # YYYY-MM-DD
            dates.append(p.stem)
    return sorted(dates, reverse=True)


def read_log(date: str = "", max_lines: int = 4000) -> str:
    """读某天日志（date='' 表示今天），最多返回末尾 max_lines 行。"""
    date = date or datetime.now().strftime(_DATE_FMT)
    path = config.LOG_DIR / f"{date}.log"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"(读取日志失败: {exc})"
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = [f"...（仅显示最后 {max_lines} 行，共 {len(lines)} 行）"] + lines[-max_lines:]
    return "\n".join(lines)


def summarize_today() -> dict:
    """汇总今天的日志：各级别条数 + 最近的 BUG/ERROR 行。"""
    date = datetime.now().strftime(_DATE_FMT)
    path = config.LOG_DIR / f"{date}.log"
    counts = {"BUG": 0, "ERROR": 0, "WARNING": 0, "INFO": 0}
    recent_problems: List[str] = []
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                for level in ("BUG", "ERROR", "WARNING", "INFO"):
                    if f"[{level}" in line:
                        counts[level] = counts.get(level, 0) + 1
                        if level in ("BUG", "ERROR"):
                            recent_problems.append(line)
                        break
        except OSError:
            pass
    return {
        "date": date,
        "counts": counts,
        "problems": recent_problems[-20:],
        "path": str(path),
    }


def prune_old(days: int = _RETENTION_DAYS) -> int:
    """删除超过 days 天的日志，返回删除数量。"""
    if not config.LOG_DIR.is_dir():
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).strftime(_DATE_FMT)
    removed = 0
    for p in config.LOG_DIR.glob("*.log"):
        if p.stem < cutoff:                   # 日期字符串可直接字典序比较
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def startup_banner(app_version: str = APP_VERSION) -> None:
    """启动时打一段分隔性的头部，便于在日志里切分"每次运行"。"""
    log = get_logger()
    log.info("=" * 62)
    log.info("黄果短剧下载器 v%s 启动  pid=%s  站点=%s",
             app_version, _pid(), config.ACTIVE_SITE.get("name", "?"))
    log.info("BASE_URL=%s  DATA=%s", config.BASE_URL, config.DATA_DIR)
    log.info("=" * 62)
    try:
        prune_old()
    except Exception:                     # noqa: BLE001
        pass


def _pid() -> int:
    import os
    return os.getpid()


def log_timer(label: str, seconds: float, threshold: float = 1.0) -> None:
    """记录一段耗时；超过阈值按 BUG 记（说明有性能问题）。"""
    if seconds >= threshold:
        get_logger().bug("%s 耗时过长: %.2fs (阈值 %.1fs)", label, seconds, threshold)
    else:
        get_logger().debug("%s 耗时 %.3fs", label, seconds)
