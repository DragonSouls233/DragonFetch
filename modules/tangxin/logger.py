# -*- coding: utf-8 -*-
"""糖心 Vlog —— 统一日志。

同时写文件（UTF-8）与控制台（stderr）。文件位于部署目录 logs/tangxin_YYYY-MM-DD.log。
**必须在导入任何可能失败的模块之前调用 setup_logging()**，这样即便是启动期异常也能落盘，
方便在无显示环境（服务器/SSH）排查「为什么没启动」。
"""
import logging
import os
import sys
import traceback
from datetime import datetime

from . import config

_LOGGER = None
LOG_PATH = None          # 当前日志文件绝对路径（供启动器打印给用户）
LOG_DIR = None


def setup_logging(name="tangxin", log_dir=None):
    global _LOGGER, LOG_PATH, LOG_DIR
    if _LOGGER is not None:
        return _LOGGER

    if log_dir is None:
        # 统一用 config.LOG_DIR（打包后自动指向 exe 同级 logs/，避免写进临时解压目录）
        log_dir = os.environ.get("TX_LOG_DIR") or getattr(config, "LOG_DIR", None)
        if not log_dir:
            # 兜底：logger.py 在 tangxin/ 下；部署根 = 其上一级
            pkg_dir = os.path.dirname(os.path.abspath(__file__))
            log_dir = os.path.join(os.path.dirname(pkg_dir), "logs")
    LOG_DIR = log_dir
    os.makedirs(log_dir, exist_ok=True)

    date = datetime.now().strftime("%Y-%m-%d")
    LOG_PATH = os.path.join(log_dir, "%s_%s.log" % (name, date))

    logger = logging.getLogger("tangxin")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(module)s.%(funcName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    # 文件（DEBUG 全量）
    try:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        sys.stderr.write("无法创建日志文件 %s: %s\n" % (LOG_PATH, e))

    # 控制台（INFO 以上；GUI 模式下也不会刷屏，因为仅 stderr）
    try:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    except Exception:
        pass

    # 未捕获异常也写入日志（主线程有效；Qt 槽内异常见 main_window 的额外兜底）
    def _excepthook(et, ev, tb):
        logger.critical("未捕获异常：%s", "".join(traceback.format_exception(et, ev, tb)))
    sys.excepthook = _excepthook

    logger.info("日志初始化完成 -> %s", LOG_PATH)
    _LOGGER = logger
    return logger


def get_logger():
    global _LOGGER
    if _LOGGER is None:
        return setup_logging()
    return _LOGGER
