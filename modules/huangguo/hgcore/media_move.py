# -*- coding: utf-8 -*-
"""媒体目录迁移：把已下载的媒体库整体搬到新目录，并同步数据库。

使用场景
--------
用户在设置里把「下载目录」从 A 改到 B。此时磁盘上的旧文件仍在 A，
数据库 episodes.file_path 也指向 A。若不处理，会出现：
  - 断点续传失效（在新目录找不到已下载分集 → 重新下载）
  - 下载记录界面点「打开文件」打不开

本模块提供两种策略：
  MOVE  —— 移动文件到新目录（推荐，省空间；跨盘自动降级为复制+删除）
  COPY  —— 复制文件到新目录（旧目录保留，最安全，但占双份空间）

两种情况都会调用 library_db.rewrite_file_paths() 把 DB 里的路径前缀改写，
保证迁移后数据库仍能正确定位文件。

注意：只搬运媒体与元数据文件，不搬运 DATA 目录（封面/DB/日志属于应用数据，
与媒体库位置无关）。
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import applog, config, library_db

# 策略常量
MOVE = "move"
COPY = "copy"

# 需要搬运的文件扩展名（媒体 + 元数据 + 图片）
_MEDIA_EXT = {
    ".mp4", ".mkv", ".ts", ".flv", ".avi", ".mov", ".webm", ".m4v",
    ".nfo", ".jpg", ".jpeg", ".png", ".json", ".srt", ".ass",
}


@dataclass
class MoveResult:
    """迁移结果汇总。"""

    moved: int = 0                 # 成功搬运的文件数
    skipped: int = 0               # 目标已存在 / 非媒体文件而跳过
    errors: int = 0                # 搬运失败的文件数
    bytes_moved: int = 0           # 搬运字节数
    db_rows: int = 0               # 数据库路径改写的行数
    old_root: str = ""
    new_root: str = ""
    strategy: str = MOVE
    error_samples: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.errors == 0

    def summary(self) -> str:
        verb = "移动" if self.strategy == MOVE else "复制"
        size_mb = self.bytes_moved / (1024 * 1024)
        s = (f"{verb}完成：{self.moved} 个文件"
             f"（{size_mb:.1f} MB），数据库同步 {self.db_rows} 条")
        if self.skipped:
            s += f"，跳过 {self.skipped} 个"
        if self.errors:
            s += f"，失败 {self.errors} 个"
        return s


def _iter_files(root: Path):
    """遍历目录下所有文件（容错：单文件异常不中断）。"""
    try:
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    yield p
            except OSError:
                continue
    except OSError:
        return


def _same_drive(a: Path, b: Path) -> bool:
    """判断两个路径是否在同一盘符（跨盘时 shutil.move 会退化为复制+删除）。"""
    try:
        return a.resolve().drive.lower() == b.resolve().drive.lower()
    except Exception:                       # noqa: BLE001
        return False


def migrate_download_root(new_root: str,
                          strategy: str = MOVE,
                          on_progress: Optional[Callable[[int, int, str], None]] = None,
                          rewrite_db: bool = True) -> MoveResult:
    """把当前下载根目录的内容迁移到 new_root，并改写数据库路径。

    参数
    ----
    new_root    : 目标下载根目录（绝对路径或相对项目根）
    strategy    : media_move.MOVE（移动）或 media_move.COPY（复制）
    on_progress : 进度回调 (done, total, current_name)
    rewrite_db  : 是否同步改写 episodes.file_path 前缀

    返回 MoveResult。迁移完成后会更新 config 的 DOWNLOAD_DIR（通过
    config.set_download_root）。
    """
    old_root = Path(config.DOWNLOAD_DIR)
    new_path = Path(new_root).expanduser()
    if not new_path.is_absolute():
        new_path = config.ROOT_DIR / new_path

    result = MoveResult(old_root=str(old_root), new_root=str(new_path),
                        strategy=strategy)

    if old_root.resolve() == new_path.resolve():
        # 目录没变，只改写 DB（例如用户只是修正了配置里的写法）
        if rewrite_db:
            result.db_rows = _rewrite_db(old_root, new_path)
        config.set_download_root(str(new_path))
        return result

    try:
        new_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.errors += 1
        result.error_samples.append(f"无法创建目标目录：{exc}")
        return result

    files = list(_iter_files(old_root)) if old_root.is_dir() else []
    total = len(files)

    for i, src in enumerate(files, 1):
        rel = src.relative_to(old_root)
        dst = new_path / rel
        name = rel.name
        if src.suffix.lower() not in _MEDIA_EXT:
            result.skipped += 1
            if on_progress:
                on_progress(i, total, name)
            continue
        try:
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                result.skipped += 1
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                try:
                    size = src.stat().st_size
                except OSError:
                    pass
                if strategy == COPY:
                    shutil.copy2(str(src), str(dst))
                else:
                    # 同盘用 replace 更快；跨盘交给 shutil.move（复制+删除）
                    if _same_drive(src, dst):
                        try:
                            src.replace(dst)
                        except OSError:
                            shutil.move(str(src), str(dst))
                    else:
                        shutil.move(str(src), str(dst))
                result.moved += 1
                result.bytes_moved += size
        except Exception as exc:            # noqa: BLE001
            result.errors += 1
            if len(result.error_samples) < 5:
                result.error_samples.append(f"{name}: {exc}")
        if on_progress:
            on_progress(i, total, name)

    # 移动模式下清理旧目录里的空壳
    if strategy == MOVE:
        _prune_empty(old_root)

    # 改写数据库路径前缀
    if rewrite_db:
        result.db_rows = _rewrite_db(old_root, new_path)

    # 更新运行期配置
    config.set_download_root(str(new_path))

    applog.get_logger().info(
        "媒体目录迁移 %s -> %s：%s", old_root, new_path, result.summary()
    )
    if result.errors:
        applog.get_logger().bug(
            "媒体目录迁移有 %d 个文件失败：%s",
            result.errors, "; ".join(result.error_samples),
        )
    return result


def _rewrite_db(old_root: Path, new_root: Path) -> int:
    """改写 episodes 表中的路径前缀。"""
    try:
        return library_db.rewrite_file_paths(str(old_root), str(new_root))
    except Exception as exc:                # noqa: BLE001
        applog.get_logger().bug("数据库路径改写失败：%s", exc)
        return 0


def _prune_empty(root: Path) -> None:
    """自底向上删除空目录（移动后残留的空壳）。"""
    if not root.is_dir():
        return
    try:
        dirs = sorted((p for p in root.rglob("*") if p.is_dir()),
                      key=lambda p: len(p.parts), reverse=True)
    except OSError:
        return
    for d in dirs:
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    try:
        if not any(root.iterdir()):
            root.rmdir()
    except OSError:
        pass


def preview(old_root: Optional[str] = None) -> Tuple[int, int]:
    """预览：返回 (文件数, 总字节数)，用于迁移前提示。"""
    root = Path(old_root) if old_root else Path(config.DOWNLOAD_DIR)
    if not root.is_dir():
        return 0, 0
    n = 0
    total = 0
    for p in _iter_files(root):
        if p.suffix.lower() not in _MEDIA_EXT:
            continue
        n += 1
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return n, total
