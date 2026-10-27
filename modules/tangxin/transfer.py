# -*- coding: utf-8 -*-
"""临时下载目录 + 搬运（"先下载在本地，再拷贝到下载目录"）。

与主平台 ``modules/core/utils/temp_download_manager.py`` 流程一致：

    下载 → 落在**本地临时目录** temp/downloads
         → 成功后搬运到**用户选择的下载目录** {下载目录}/{演员}/{标题}.mp4
         → 搬运成功才写 NFO / 更新下载记录

为什么这么绕：
  - 下载目录常常指向 NAS/SMB 网络盘。让 ffmpeg 直接把 concat 结果写进网络盘，
    一来慢，二来网络抖动会让整段下载功亏一篑，三来失败会在目标目录留下残片。
  - 先落本地盘，下载过程不受网络盘影响；搬运只是一次数百 MB 的文件拷贝，
    失败可重试，且失败时目标目录依然干净。

搬运规则（对齐主平台）：
  - 同盘符 → ``os.rename``（原子、瞬时）
  - 跨盘符 → ``shutil.copy2`` + ``os.remove``（保留时间戳）
  - 撞 ``[Errno 22] Invalid argument``（主平台在 SMB + 超长/emoji 标题上踩过）
    → 先打结构化诊断，再用**降级文件名**（剥非 BMP 字符 / 非法字符 / 限 120 码元）
    重试一次 —— 流量已经花了，不该因为命名把成品丢掉。
"""
import errno
import os
import shutil
import time
from datetime import datetime

from . import config, logger

# 降级文件名的 UTF-16 码元上限（保守，远低于 255）
DEGRADED_NAME_LIMIT = 120

# 临时文件超过这个时长还没搬走，视为上次崩溃的残留
STALE_TEMP_HOURS = 24


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def get_temp_dir() -> str:
    """本地临时下载目录（确保存在）。"""
    return ensure_dir(config.TEMP_DIR)


def temp_file_path(vid: str, ext: str = "mp4") -> str:
    """生成本地临时文件路径：{temp_dir}/{vid}.{ext}。"""
    return os.path.join(get_temp_dir(), "%s.%s" % (vid, ext.lstrip(".")))


# ---------- 诊断与降级（沿用主平台在 SMB 上踩坑后沉淀的经验）----------
def _diagnose_invalid_path(path: str) -> None:
    """:class:`[Errno 22]` 的结构化诊断 —— 把可判定因素一次性打全。"""
    try:
        parent, name = os.path.split(path)
        stem, _ext = os.path.splitext(name)
        logger.get_logger().error("路径诊断: %s", {
            "path_len": len(path),
            "name_len": len(name),
            # Windows 按 UTF-16 码元计长：emoji / 非 BMP 字符各算 2 个
            "name_utf16_units": len(name.encode("utf-16-le")) // 2,
            "non_bmp_chars": sum(1 for c in stem if ord(c) > 0xFFFF),
            "illegal_chars": [c for c in '<>:"|*?' if c in name],
            "ctrl_chars": sum(1 for c in stem if ord(c) < 0x20),
            "parent_exists": os.path.isdir(parent),
            "drive": os.path.splitdrive(path)[0],
        })
    except Exception:
        pass


def _degrade_target_name(path: str) -> str:
    """为 :class:`[Errno 22]` 生成更保守的替代名（同目录、保留扩展名）。

    剥掉非 BMP 字符（emoji）、非法字符，再把 stem 压到 ``DEGRADED_NAME_LIMIT``。
    目录不动，语义上仍是同一个视频。返回空串表示无法生成。
    """
    try:
        parent, name = os.path.split(path)
        stem, ext = os.path.splitext(name)
        cleaned = "".join(c for c in stem if ord(c) <= 0xFFFF)
        cleaned = "".join(c for c in cleaned if c not in '<>:"|*?/\\')
        cleaned = cleaned.strip().rstrip(". ")
        if len(cleaned) > DEGRADED_NAME_LIMIT:
            cleaned = cleaned[:DEGRADED_NAME_LIMIT]
        cleaned = cleaned.rstrip(". ").strip()
        if not cleaned:
            cleaned = "video"
        return os.path.join(parent, cleaned + ext)
    except Exception:
        return ""


def _same_drive(src: str, dst: str) -> bool:
    return os.path.splitdrive(src)[0] == os.path.splitdrive(dst)[0]


def _do_move(src: str, dst: str) -> None:
    """真正的搬运动作；失败时向上抛 OSError。"""
    if _same_drive(src, dst):
        os.rename(src, dst)          # 同盘：原子、瞬时
    else:
        # 跨盘（含本地→SMB）：copy2 保留时间戳，再删源
        with open(src, "rb") as fr, open(dst, "wb") as fw:
            shutil.copyfileobj(fr, fw, 1024 * 1024)
        shutil.copystat(src, dst)
        os.remove(src)


def move_to_target(src: str, target: str) -> dict:
    """把临时文件搬到目标路径。

    返回 ``{success, target, degraded, skipped, error}``。
    目标已存在时返回 ``skipped=True``（幂等，不覆盖已有文件）。
    """
    log = logger.get_logger()
    if not os.path.isfile(src):
        return {"success": False, "error": "临时文件不存在: %s" % src}

    target = os.path.normpath(target)
    if os.path.exists(target):
        log.info("目标文件已存在，跳过搬运: %s", target)
        return {"success": True, "target": target, "skipped": True}

    ensure_dir(os.path.dirname(target))
    try:
        log.info("搬运文件: %s -> %s", os.path.basename(src), target)
        _do_move(src, target)
        return {"success": True, "target": target}
    except OSError as e:
        log.error("搬运文件失败: %s", e)
        if getattr(e, "errno", None) == errno.EINVAL:
            _diagnose_invalid_path(target)
            degraded = _degrade_target_name(target)
            if degraded and degraded != target:
                try:
                    log.warning("尝试降级文件名重试: %s", os.path.basename(degraded))
                    _do_move(src, degraded)
                    log.info("降级文件名后搬运成功: %s", degraded)
                    return {"success": True, "target": degraded, "degraded": True}
                except OSError as e2:
                    log.error("降级重试仍失败: %s", e2)
        return {"success": False, "error": str(e)}
    except Exception as e:
        log.error("搬运文件失败: %s", e)
        return {"success": False, "error": str(e)}


def safe_remove(path: str, max_retries: int = 5, retry_delay: float = 0.5) -> bool:
    """安全删除（Windows 文件锁常见）：重试若干次，返回是否删掉。"""
    if not path or not os.path.exists(path):
        return True
    for attempt in range(1, max_retries + 1):
        try:
            os.remove(path)
            return True
        except PermissionError:
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                logger.get_logger().warning("删除临时文件失败(被占用): %s", path)
                return False
        except Exception as e:
            logger.get_logger().warning("删除临时文件失败: %s - %s", path, e)
            return False
    return False


def cleanup_stale(max_age_hours: int = STALE_TEMP_HOURS) -> int:
    """清理上次崩溃/被杀留下的过期临时文件，返回清理数量。"""
    temp_dir = get_temp_dir()
    now = time.time()
    removed = 0
    try:
        for name in os.listdir(temp_dir):
            p = os.path.join(temp_dir, name)
            if not os.path.isfile(p):
                continue
            try:
                age = now - os.path.getmtime(p)
            except Exception:
                age = max_age_hours * 3600 + 1
            if age > max_age_hours * 3600:
                if safe_remove(p):
                    removed += 1
    except Exception as e:
        logger.get_logger().warning("清理过期临时文件失败: %s", e)
    if removed:
        logger.get_logger().info("清理过期临时文件 %d 个（>%sh）", removed, max_age_hours)
    return removed
