# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
临时下载管理器
支持先下载到临时目录，完成后移动到目标目录
"""
import os
import sys
import errno
import shutil
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Optional, Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class TempDownloadManager:
    """临时下载管理器"""
    
    def __init__(self, temp_dir: str = None):
        """
        初始化管理器
        
        Args:
            temp_dir: 临时目录路径，默认为软件目录下的temp/downloads
        """
        if temp_dir is None:
            # 获取软件根目录
            if getattr(sys, 'frozen', False):
                # 打包后
                app_dir = os.path.dirname(sys.executable)
            else:
                # 开发环境 - 向上找到项目根目录
                current_dir = os.path.dirname(os.path.abspath(__file__))
                # 从 modules/core/utils 向上3级到项目根目录
                app_dir = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
            
            temp_dir = os.path.join(app_dir, 'temp', 'downloads')
        
        self.temp_dir = temp_dir
        self._db_lock: threading.Lock = threading.Lock()  # 数据库访问锁
        self._ensure_temp_dir()
        self._init_database()

        # 回收上次进程异常退出（被杀 / 崩溃 / 断电）留下的在途记录。
        # 体检 C1：download_status 里积了 61 条 status='downloading'，
        # 横跨 2026-05 ~ 09-22、progress 全 0 —— 没有任何回收机制。
        #
        # ⚠️ 每进程只做一次：``core/download_manager.py`` 会在每个视频下载时
        # 重新实例化本类，不设闸门就会对着两万多行表反复全表扫。
        if not TempDownloadManager._auto_recover_done:
            try:
                recovered = self.recover_stale_downloads()
                TempDownloadManager._auto_recover_done = True
                if recovered:
                    logger.info(f"启动维护：回收 {recovered} 条僵尸 downloading 记录")
            except Exception:
                # 不置闸门 —— 本次失败，下次实例化再试
                logger.debug("回收僵尸下载记录失败（不影响启动）", exc_info=True)

    # 进程内只自动回收一次（见 __init__ 说明）
    _auto_recover_done = False

    def _ensure_temp_dir(self):
        """确保临时目录存在"""
        os.makedirs(self.temp_dir, exist_ok=True)
        logger.info(f"临时下载目录: {self.temp_dir}")
    
    def _get_db_path(self) -> str:
        """获取数据库路径"""
        from multisite.core.utils.path_utils import get_data_path
        return get_data_path('db', 'download_cache.db')
    
    def _init_database(self):
        """初始化下载数据库"""
        db_path = self._get_db_path()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 创建下载状态表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS download_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_url TEXT UNIQUE,
                video_title TEXT,
                model_id INTEGER,
                model_name TEXT,
                target_dir TEXT,
                temp_file TEXT,
                status TEXT DEFAULT 'pending',
                progress REAL DEFAULT 0,
                file_size INTEGER DEFAULT 0,
                error_msg TEXT,
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()

    # 在途记录超过这个时长仍未收尾，即视为僵尸（正常单次下载最多几小时）
    STALE_DOWNLOADING_HOURS = 24

    @staticmethod
    def _parse_db_timestamp(value):
        """解析库里两种时间写法：``datetime.now().isoformat()``（带 T 与微秒）
        与 SQLite ``CURRENT_TIMESTAMP``（空格分隔、无微秒）。

        直接拿字符串比较是**错的** —— ``'2026-09-22T00:00:00'`` 与
        ``'2026-09-22 00:00:00'`` 的 'T'(0x54) > ' '(0x20)，混排时会全部判反。
        """
        if not value:
            return None
        text = str(value).strip().replace('T', ' ')
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(text[:26], fmt)
            except ValueError:
                continue
        return None

    def recover_stale_downloads(self, max_age_hours: int = None) -> int:
        """把长期停留在 ``downloading`` 的记录回收为 ``failed``，返回回收条数。

        为什么需要：``download_status.status`` 只有写入、没有回收。进程被杀 /
        崩溃 / 断电后那条记录永远停在 ``downloading``，后果有两个：
          1. ``smart_cleanup`` 把它当"活跃下载"⇒ 对应的 ``.part`` 残留永不清理；
          2. GUI / 统计把它算作"正在下载"⇒ 掩盖真实队列状态。

        判据用 ``started_at``（缺失则 ``created_at``），阈值默认 24 小时 ——
        远大于任何单次真实下载耗时，因此**不会误杀在跑的下载**。
        幂等，可重复调用。
        """
        hours = self.STALE_DOWNLOADING_HOURS if max_age_hours is None else max_age_hours
        now = datetime.now()
        recovered = 0

        with self._db_lock:
            db_path = self._get_db_path()
            conn = sqlite3.connect(db_path, timeout=30)
            cursor = conn.cursor()

            try:
                cursor.execute('PRAGMA busy_timeout=30000')
                cursor.execute('''
                    SELECT id, started_at, created_at FROM download_status
                    WHERE status = 'downloading'
                ''')
                rows = cursor.fetchall()
            except sqlite3.Error as e:
                conn.close()
                logger.warning(f"回收僵尸下载记录失败: {e}")
                return 0

            stale_ids = []
            for row_id, started_at, created_at in rows:
                ts = self._parse_db_timestamp(started_at) or self._parse_db_timestamp(created_at)
                if ts is None:
                    # 时间戳缺失/无法解析 —— 这种记录不可能还在跑，一并回收
                    stale_ids.append(row_id)
                    continue
                if (now - ts).total_seconds() > hours * 3600:
                    stale_ids.append(row_id)

            if stale_ids:
                marker = f'[自动回收] 在途超过 {hours} 小时未收尾'
                for row_id in stale_ids:
                    cursor.execute('''
                        UPDATE download_status
                        SET status = 'failed',
                            error_msg = CASE
                                WHEN error_msg IS NULL OR error_msg = '' THEN ?
                                ELSE error_msg
                            END,
                            completed_at = ?
                        WHERE id = ? AND status = 'downloading'
                    ''', (marker, now.isoformat(), row_id))
                conn.commit()
                recovered = len(stale_ids)
                logger.info(f"已回收 {recovered} 条僵尸 downloading 记录（>{hours}h）")

            conn.close()

        return recovered

    def get_temp_file_path(self, video_url: str, model_id: int = None) -> str:
        """
        获取临时文件路径
        
        Args:
            video_url: 视频URL
            model_id: 模特ID
            
        Returns:
            临时文件的完整路径
        """
        # 使用URL的hash作为文件名基础
        import hashlib
        url_hash = hashlib.md5(video_url.encode()).hexdigest()[:12]
        
        # 生成文件名
        if model_id:
            filename = f"{model_id}_{url_hash}.mp4"
        else:
            filename = f"{url_hash}.mp4"
        
        return os.path.join(self.temp_dir, filename)
    
    def start_download(self, video_url: str, video_title: str, 
                       model_id: int, model_name: str, 
                       target_dir: str) -> str:
        """
        开始下载，返回临时文件路径
        
        Args:
            video_url: 视频URL
            video_title: 视频标题
            model_id: 模特ID
            model_name: 模特名
            target_dir: 目标目录
            
        Returns:
            临时文件路径
        """
        temp_file = self.get_temp_file_path(video_url, model_id)
        
        # 记录下载状态
        with self._db_lock:
            db_path = self._get_db_path()
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT OR REPLACE INTO download_status 
                (video_url, video_title, model_id, model_name, target_dir, temp_file, status, started_at)
                VALUES (?, ?, ?, ?, ?, ?, 'downloading', ?)
            ''', (video_url, video_title, model_id, model_name, target_dir, temp_file, 
                  datetime.now().isoformat()))
            
            conn.commit()
            conn.close()
        
        logger.info(f"开始下载: {video_title} -> {temp_file}")
        return temp_file
    
    def update_progress(self, video_url: str, progress: float, file_size: int = 0):
        """更新下载进度"""
        with self._db_lock:
            db_path = self._get_db_path()
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE download_status 
                SET progress = ?, file_size = ?
                WHERE video_url = ?
            ''', (progress, file_size, video_url))
            
            conn.commit()
            conn.close()
    
    def complete_download(self, video_url: str) -> Dict[str, Any]:
        """
        下载完成，移动文件到目标目录
        
        Args:
            video_url: 视频URL
            
        Returns:
            结果字典 {success, target_file, error}
        """
        video_title = None
        model_id = None
        model_name = None
        target_dir = None
        temp_file = None
        
        # 第一步：获取下载信息
        with self._db_lock:
            db_path = self._get_db_path()
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT video_title, model_id, model_name, target_dir, temp_file
                FROM download_status WHERE video_url = ?
            ''', (video_url,))
            
            row = cursor.fetchone()
            if not row:
                conn.close()
                return {'success': False, 'error': '下载记录不存在'}
            
            video_title, model_id, model_name, target_dir, temp_file = row
            conn.close()
        
        # 第二步：检查临时文件是否存在（yt-dlp可能会修改文件名）
        actual_temp_file = None
        if os.path.exists(temp_file):
            actual_temp_file = temp_file
        else:
            # yt-dlp可能添加了.fxxx扩展名或修改了文件名
            # 查找临时目录中所有匹配的文件
            import glob
            temp_dir = os.path.dirname(temp_file)
            temp_basename = os.path.basename(temp_file)
            
            # 移除扩展名，用于匹配
            if '.' in temp_basename:
                base_name = temp_basename.rsplit('.', 1)[0]
            else:
                base_name = temp_basename
            
            # 查找所有匹配的文件（包括.part文件）
            patterns = [
                os.path.join(temp_dir, f"{base_name}*.mp4"),
                os.path.join(temp_dir, f"{base_name}*.webm"),
                os.path.join(temp_dir, f"{base_name}*.mkv"),
                os.path.join(temp_dir, f"{base_name}*.flv"),
                os.path.join(temp_dir, f"{base_name}*.avi"),
                os.path.join(temp_dir, f"{base_name}*.part")  # 包括未完成的.part文件
            ]
            
            all_matches = []
            for pattern in patterns:
                all_matches.extend(glob.glob(pattern))
            
            if all_matches:
                # 使用最新的文件（按修改时间）
                actual_temp_file = max(all_matches, key=os.path.getmtime)
                logger.info(f"找到实际下载文件: {actual_temp_file} (原始: {temp_file})")
            else:
                # 尝试查找所有视频文件
                all_videos = []
                for ext in ['*.mp4', '*.webm', '*.mkv', '*.flv', '*.avi', '*.part']:
                    all_videos.extend(glob.glob(os.path.join(temp_dir, ext)))
                
                if all_videos:
                    # 使用最新的文件
                    actual_temp_file = max(all_videos, key=os.path.getmtime)
                    logger.warning(f"使用最新的视频文件: {actual_temp_file}")
        
        if not actual_temp_file:
            return {'success': False, 'error': f'临时文件不存在: {temp_file}'}
        
        # 如果文件还是.part格式，尝试使用它（可能是yt-dlp下载的单文件）
        if actual_temp_file.endswith('.part') or '.part-Frag' in actual_temp_file:
            logger.warning(f"文件仍是.part格式，尝试使用: {actual_temp_file}")
            # 检查文件大小是否合理
            try:
                size = os.path.getsize(actual_temp_file)
                if size < 1024 * 1024:  # 小于1MB，可能不完整
                    logger.error(f"文件太小，可能下载未完成: {actual_temp_file} ({size} bytes)")
                    return {'success': False, 'error': f'文件太小，可能下载未完成: {actual_temp_file}'}
                logger.info(f"使用.part文件，大小: {size} bytes")
            except Exception as e:
                logger.error(f"检查文件大小失败: {e}")
                return {'success': False, 'error': f'检查文件大小失败: {e}'}
        
        # 确保目标目录存在
        os.makedirs(target_dir, exist_ok=True)
        
        # 生成目标文件名（使用正确的命名格式）
        target_file = self._generate_target_filename(
            video_title=video_title,
            model_name=model_name,
            video_url=video_url,
            target_dir=target_dir
        )
        
        # 处理文件名冲突 - 如果文件已存在则直接跳过
        if os.path.exists(target_file):
            logger.info(f"目标文件已存在，跳过下载: {target_file}")
            
            # 清理临时文件
            try:
                os.remove(actual_temp_file)
                logger.info(f"清理临时文件: {actual_temp_file}")
            except Exception as e:
                logger.warning(f"清理临时文件失败: {actual_temp_file}, error: {e}")
            
            # 更新数据库状态
            with self._db_lock:
                db_path = self._get_db_path()
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                
                cursor.execute('''
                    UPDATE download_status
                    SET status = 'skipped', completed_at = ?
                    WHERE video_url = ?
                ''', (datetime.now().isoformat(), video_url))
                conn.commit()
                conn.close()
            
            return {'success': True, 'target_file': target_file, 'skipped': True}
        
        # 第五步：移动文件
        result = self._move_file(actual_temp_file, target_file)
        
        if not result['success']:
            return result
        
        # 第六步：立即清理临时文件（双重保险）
        self.cleanup_single_temp(actual_temp_file)
        
        # 第七步：更新数据库状态
        with self._db_lock:
            db_path = self._get_db_path()
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                UPDATE download_status
                SET status = 'completed', completed_at = ?
                WHERE video_url = ?
            ''', (datetime.now().isoformat(), video_url))
            conn.commit()
            conn.close()
        
        # 以**实际落盘路径**为准回报（[Errno 22] 降级重试后文件名会变）
        moved_to = result.get('target') or target_file
        if result.get('degraded'):
            logger.warning(f"⚠️ 目标文件名因兼容性被降级: {os.path.basename(moved_to)}")
        logger.info(f"文件移动成功: {actual_temp_file} -> {moved_to}")
        return {'success': True, 'target_file': moved_to,
                'degraded': bool(result.get('degraded'))}
    
    def _generate_target_filename(self, video_title: str, model_name: str, 
                                   video_url: str, target_dir: str) -> str:
        """
        生成目标文件名，使用正确的命名格式
        
        格式: [模特名] 标题 (视频ID).mp4
        
        Args:
            video_title: 视频标题
            model_name: 模特名
            video_url: 视频URL
            target_dir: 目标目录
            
        Returns:
            目标文件完整路径
        """
        # 导入FileNameFormatter
        from multisite.core.utils.filename_formatter import FileNameFormatter
        
        # 提取视频ID
        video_id = FileNameFormatter.extract_video_id_from_url(video_url)
        
        # 使用模特名或默认值
        artist = model_name if model_name else "Unknown"
        
        # 生成文件名
        filename = FileNameFormatter.format_video_filename_with_ext(
            artist=artist,
            title=video_title,
            video_id=video_id,
            ext="mp4"
        )
        
        # ⚠️ 2026-09-22：normpath 压掉重复分隔符。
        # 现场 error.log 里出现过 `N:/日本/[Channel] BabyYurin\\[BabyYurin] …`
        # 这种双反斜杠目标路径（dir 拼接 + join 各留一个），虽然 Windows 通常
        # 会容错，但目标若是 SMB 盘就是额外的不确定性，没必要留着。
        return os.path.normpath(os.path.join(target_dir, filename))
    
    def _move_file(self, source: str, target: str) -> Dict[str, Any]:
        """
        移动文件，支持跨盘符
        
        Args:
            source: 源文件路径
            target: 目标文件路径
            
        Returns:
            结果字典
        """
        # 检查是否同盘符
        source_drive = os.path.splitdrive(source)[0]
        target_drive = os.path.splitdrive(target)[0]
        
        same_drive = source_drive == target_drive
        
        logger.info(f"移动文件: {source} -> {target}")
        logger.info(f"源盘符: {source_drive}, 目标盘符: {target_drive}, 同盘符: {same_drive}")
        
        try:
            self._do_move(source, target, same_drive)
            return {'success': True, 'target': target}

        except OSError as e:
            logger.error(f"移动文件失败: {e}")

            # ⚠️ 2026-09-22 体检 D2：服务端单日 132 条
            # `[Errno 22] Invalid argument`，全在 SMB 盘 N: 下、标题为
            # 「超长日文 + emoji」的 JAVDB 视频上。已排除长度（实测 176 字符）、
            # 非法字符、控制字符、日志编码等假设 ⇒ 真因未定。
            # 这里做两件事：① 把可判定的因素打全，下次复现直接定位；
            # ② 用更保守的文件名重试一次 —— 下载的流量已经花掉了，
            #    因为命名问题把成品丢掉是最亏的结局。
            if getattr(e, 'errno', None) == errno.EINVAL:
                self._diagnose_invalid_path(target)
                degraded = self._degrade_target_name(target)
                if degraded and degraded != target:
                    try:
                        logger.warning(f"尝试降级文件名重试: {os.path.basename(degraded)}")
                        self._do_move(
                            source, degraded,
                            os.path.splitdrive(degraded)[0] == os.path.splitdrive(source)[0])
                        logger.info(f"✅ 降级文件名后移动成功: {degraded}")
                        return {'success': True, 'target': degraded, 'degraded': True}
                    except OSError as e2:
                        logger.error(f"降级重试仍失败: {e2}")

            return {'success': False, 'error': str(e)}

        except Exception as e:
            logger.error(f"移动文件失败: {e}")
            return {'success': False, 'error': str(e)}

    def _do_move(self, source: str, target: str, same_drive: bool) -> None:
        """真正的搬运动作（同盘 rename / 跨盘 copy+delete），失败时向上抛 OSError。"""
        if same_drive:
            # 同盘符，使用rename（瞬间完成）
            os.rename(source, target)
            logger.info("使用rename移动（同盘符）")
        else:
            # 跨盘符，使用copy+delete
            logger.info("使用copy移动（跨盘符）...")

            file_size = os.path.getsize(source)
            buffer_size = 1024 * 1024  # 1MB缓冲

            with open(source, 'rb') as f_src:
                with open(target, 'wb') as f_dst:
                    while True:
                        chunk = f_src.read(buffer_size)
                        if not chunk:
                            break
                        f_dst.write(chunk)

            # 删除源文件
            os.remove(source)
            logger.info(f"跨盘符移动完成: {file_size / 1024 / 1024:.2f} MB")

    @staticmethod
    def _diagnose_invalid_path(path: str) -> None:
        """[Errno 22] 的结构化诊断，把可判定因素一次性打全。

        2026-09-22 已排除：路径长度（实测 176 < 260）、``<>:"|*?`` 非法字符、
        控制字符、日志字节编码。剩下的怀疑对象是「非 BMP 字符（emoji）」与
        「SMB 目标盘」的组合。下次复现时这几项会直接出现在日志里。
        """
        try:
            parent, name = os.path.split(path)
            stem, _ext = os.path.splitext(name)
            diag = {
                'path_len': len(path),
                'name_len': len(name),
                # Windows 按 UTF-16 码元计长：emoji / 非 BMP 字符各算 2 个
                'name_utf16_units': len(name.encode('utf-16-le')) // 2,
                'non_bmp_chars': sum(1 for c in stem if ord(c) > 0xFFFF),
                'illegal_chars': [c for c in '<>:"|*?' if c in name],
                'ctrl_chars': sum(1 for c in stem if ord(c) < 0x20),
                'parent_exists': os.path.isdir(parent),
                'drive': os.path.splitdrive(path)[0],
                'trailing_space_or_dot': [
                    s for s in path.replace('\\', '/').split('/') if s and s[-1] in ' .'
                ],
            }
            logger.error(f"路径诊断: {diag}")
        except Exception:
            logger.debug("路径诊断本身失败", exc_info=True)

    # 降级文件名的 UTF-16 码元上限（保守，远低于 255）
    DEGRADED_NAME_LIMIT = 120

    @classmethod
    def _degrade_target_name(cls, path: str) -> str:
        """为 [Errno 22] 的路径生成更保守的替代名（同目录、保留扩展名）。

        规则：剥掉非 BMP 字符（emoji —— 在 UTF-16 里占 2 个码元，也是各类
        文件系统 / 协议最容易出差异的一类）、剥掉非法字符，再把 stem 压到
        ``DEGRADED_NAME_LIMIT`` 个码元以内。目录不动，语义上仍是同一个视频。
        返回空串表示无法生成（调用方放弃重试）。
        """
        try:
            parent, name = os.path.split(path)
            stem, ext = os.path.splitext(name)

            cleaned = ''.join(c for c in stem if ord(c) <= 0xFFFF)
            cleaned = ''.join(c for c in cleaned if c not in '<>:"|*?/\\')
            cleaned = cleaned.strip().rstrip('. ')

            while len(cleaned) > cls.DEGRADED_NAME_LIMIT:
                cleaned = cleaned[:-1]
            cleaned = cleaned.rstrip('. ').strip()

            if not cleaned:
                cleaned = 'video'

            return os.path.join(parent, cleaned + ext)
        except Exception:
            logger.debug("生成降级文件名失败", exc_info=True)
            return ''
    
    def _safe_filename(self, filename: str) -> str:
        """生成安全的文件名"""
        # 移除非法字符
        illegal_chars = '<>:"/\\|?*'
        for char in illegal_chars:
            filename = filename.replace(char, '')
        
        # 限制长度
        if len(filename) > 200:
            filename = filename[:200]
        
        return filename.strip()
    
    def _resolve_filename_conflict(self, filepath: str) -> str:
        """解决文件名冲突"""
        if not os.path.exists(filepath):
            return filepath
        
        # 添加序号
        base, ext = os.path.splitext(filepath)
        counter = 1
        
        while os.path.exists(f"{base}_{counter}{ext}"):
            counter += 1
        
        return f"{base}_{counter}{ext}"
    
    def fail_download(self, video_url: str, error_msg: str):
        """标记下载失败"""
        with self._db_lock:
            db_path = self._get_db_path()
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            cursor.execute('SELECT temp_file FROM download_status WHERE video_url = ?', (video_url,))
            row = cursor.fetchone()

            cursor.execute('''
                UPDATE download_status
                SET status = 'failed', error_msg = ?
                WHERE video_url = ?
            ''', (error_msg, video_url))

            conn.commit()
            conn.close()

        if row and os.path.exists(row[0]):
            try:
                os.remove(row[0])
                logger.info(f"清理失败下载的临时文件: {row[0]}")
            except Exception as e:
                logger.warning(f"删除临时文件失败: {row[0]}, 错误: {e}")
    
    def smart_cleanup(self, max_size_mb: int = 2048, max_age_minutes: int = 120) -> dict:
        """
        智能清理临时文件
        
        策略：
        1. 先清理孤儿文件（无数据库记录的残留文件）
        2. 清理超时的文件（只清理已完成/失败的文件）
        3. 如果总大小超过阈值，从最旧的已完成/失败记录开始清理
        4. 确保只清理已完成下载的文件，避免删除正在下载过程中的临时文件
        
        Args:
            max_size_mb: 最大允许的TEMP目录大小(MB)，超过后开始清理
            max_age_minutes: 文件最大保留时间(分钟)，超过直接删除
            
        Returns:
            清理统计 {orphan_cleaned, size_cleaned, freed_mb, total_mb}
        """
        import time
        result = {
            'orphan_cleaned': 0,
            'size_cleaned': 0,
            'freed_bytes': 0,
            'total_before_mb': 0,
            'total_after_mb': 0
        }
        
        current_time = time.time()
        max_age_seconds = max_age_minutes * 60
        
        # 获取当前目录大小
        total_size = self.get_temp_dir_size()
        result['total_before_mb'] = round(total_size / (1024 * 1024), 2)
        max_size_bytes = max_size_mb * 1024 * 1024
        
        logger.info(f"临时文件清理开始: 当前大小={result['total_before_mb']}MB, 最大允许={max_size_mb}MB, 超时时间={max_age_minutes}分钟")
        
        # 获取所有有数据库记录的活跃临时文件
        active_temp_files = set()
        active_base_names = set()  # 基础文件名（用于匹配分片文件）
        active_full_paths = set()  # 完整路径，用于更准确的匹配
        
        try:
            db_path = self._get_db_path()
            conn = sqlite3.connect(db_path, timeout=10)
            cursor = conn.cursor()
            
            # 获取所有pending/downloading状态的文件的temp_file
            cursor.execute('''
                SELECT temp_file FROM download_status 
                WHERE status IN ('pending', 'downloading') AND temp_file IS NOT NULL
            ''')
            for row in cursor.fetchall():
                if row[0]:
                    temp_file = row[0]
                    active_full_paths.add(temp_file)
                    
                    # 也添加去掉扩展名的基础路径，用于匹配 .part 等文件
                    temp_file_no_ext = temp_file
                    if '.' in os.path.basename(temp_file):
                        temp_file_no_ext = os.path.dirname(temp_file) + '\\' + os.path.basename(temp_file).rsplit('.', 1)[0]
                    active_full_paths.add(temp_file_no_ext)
                    
                    basename = os.path.basename(temp_file)
                    active_temp_files.add(basename)
                    
                    # 提取基础名（去掉扩展名）用于匹配分片文件
                    base_name = basename.rsplit('.', 1)[0] if '.' in basename else basename
                    active_base_names.add(base_name)
            
            conn.close()
        except Exception as e:
            logger.warning(f"获取活跃文件列表失败: {e}")
        
        logger.info(f"活跃文件统计: {len(active_full_paths)}个完整路径, {len(active_base_names)}个基础名")
        if active_base_names:
            logger.debug(f"活跃文件基础名: {list(active_base_names)[:5]}")  # 只显示前5个
        
        # 第一步：清理孤儿文件（无记录的残留文件）和分片文件
        if os.path.exists(self.temp_dir):
            # 先收集需要删除的分片文件（按基础名分组）
            frag_files_to_delete = []  # (filepath, file_size, reason)
            
            for filename in os.listdir(self.temp_dir):
                filepath = os.path.join(self.temp_dir, filename)
                
                if not os.path.isfile(filepath):
                    continue
                
                # 检查是否是活跃下载的文件
                is_active_file = filepath in active_full_paths or filename in active_temp_files
                
                # 检查是否是活跃文件的基础名（用于处理 .part 等分片文件）
                # 如果文件名以活跃文件的基础名开头，也应该视为活跃文件
                filename_base = filename
                if '.' in filename:
                    filename_base = filename.rsplit('.', 1)[0]  # 去掉扩展名
                    # 如果去掉扩展名后还有扩展名，继续去掉
                    if '.' in filename_base:
                        filename_base = filename_base.rsplit('.', 1)[0]
                
                is_active_base = filename_base in active_base_names
                
                # 检查文件年龄
                try:
                    file_age = current_time - os.path.getmtime(filepath)
                    is_old = file_age > max_age_seconds
                except Exception:
                    is_old = True  # 如果无法获取文件时间，视为旧文件
                
                # 如果文件是活跃文件的基础名（去掉扩展名后匹配），则视为活跃文件
                if is_active_base:
                    is_active_file = True
                
                # === 处理分片文件 (.part-FragXXX, .part, .ytdl, etc) ===
                is_frag_file = '.part-Frag' in filename or filename.endswith('.part') or filename.endswith('.ytdl') or '.frag' in filename.lower()
                
                if is_frag_file:
                    # 提取分片文件的基础名 - 更全面的匹配
                    frag_base = filename
                    # 处理各种分片格式
                    if '.part-Frag' in filename:
                        # 格式: xxx.mp4.part-Frag300 -> 基础名: xxx
                        frag_base = filename.split('.part-Frag')[0]
                        # 进一步去掉可能的扩展名
                        if '.' in frag_base:
                            frag_base = frag_base.rsplit('.', 1)[0]
                    elif filename.endswith('.part'):
                        # 格式: xxx.mp4.part -> 基础名: xxx
                        frag_base = filename.rsplit('.part', 1)[0]
                        # 进一步去掉可能的扩展名
                        if '.' in frag_base:
                            frag_base = frag_base.rsplit('.', 1)[0]
                    elif filename.endswith('.ytdl'):
                        # 格式: xxx.mp4.ytdl -> 基础名: xxx
                        frag_base = filename.rsplit('.ytdl', 1)[0]
                        # 进一步去掉可能的扩展名
                        if '.' in frag_base:
                            frag_base = frag_base.rsplit('.', 1)[0]
                    elif '.frag' in filename.lower():
                        # 其他格式，移除扩展名
                        if '.' in frag_base:
                            frag_base = frag_base.rsplit('.', 1)[0]
                            # 再移除一层
                            if '.' in frag_base:
                                frag_base = frag_base.rsplit('.', 1)[0]
                    
                    # 检查这个分片是否属于活跃下载
                    is_active_frag = frag_base in active_base_names
                    
                    # 如果基础名匹配不上，尝试检查去掉扩展名后是否匹配
                    if not is_active_frag and '.' in frag_base:
                        potential_base = frag_base.rsplit('.', 1)[0]
                        is_active_frag = potential_base in active_base_names
                    
                    # ===== 关键修复：只有非活跃的分片才删除，正在下载的分片绝对保留 =====
                    if not is_active_frag:
                        try:
                            file_size = os.path.getsize(filepath)
                            frag_files_to_delete.append((filepath, file_size, '孤儿分片'))
                            logger.debug(f"标记删除分片: {filename} (基础名: {frag_base})")
                        except Exception as e:
                            logger.warning(f"获取分片文件大小失败: {filepath}, 错误: {e}")
                    else:
                        logger.debug(f"保留活跃分片: {filename} (基础名: {frag_base})")
                    continue
                
                # === 处理普通文件 (.mp4 等) ===
                
                # 检查是否是孤儿文件（不在活跃列表中）
                is_orphan = not is_active_file
                
                # ===== 关键修复：只有孤儿文件才删除，活跃文件（正在下载）永远保留 =====
                if is_orphan:
                    try:
                        file_size = os.path.getsize(filepath)
                        # 使用安全删除
                        self._safe_remove_file(filepath, "孤儿文件")
                        
                        # 只有成功删除才计数
                        if not os.path.exists(filepath):
                            result['freed_bytes'] += file_size
                            result['orphan_cleaned'] += 1
                            logger.info(f"清理孤儿临时文件: {filename} ({file_size / (1024*1024):.2f} MB)")
                            
                    except Exception as e:
                        logger.error(f"清理失败: {filename} - {e}")
            
            # 批量删除收集到的分片文件 - 使用安全删除
            for filepath, file_size, reason in frag_files_to_delete:
                # 使用安全删除
                self._safe_remove_file(filepath, reason)
                # 只有成功删除才计数
                if not os.path.exists(filepath):
                    result['freed_bytes'] += file_size
                    result['orphan_cleaned'] += 1
                    logger.info(f"清理{reason}: {os.path.basename(filepath)} ({file_size / (1024*1024):.2f} MB)")
        
        # 第二步：如果还是太大，清理已完成的记录对应的文件
        total_size = self.get_temp_dir_size()
        if total_size > max_size_bytes:
            logger.warning(f"TEMP目录仍过大 ({total_size/(1024*1024):.1f}MB > {max_size_mb}MB)，继续清理...")
            
            try:
                db_path = self._get_db_path()
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                
                # 获取所有completed/skipped/failed状态的最旧记录
                cursor.execute('''
                    SELECT temp_file, completed_at FROM download_status 
                    WHERE status IN ('completed', 'skipped', 'failed') 
                    AND temp_file IS NOT NULL
                    ORDER BY completed_at ASC
                ''')
                
                for row in cursor.fetchall():
                    temp_file = row[0]
                    
                    # 确保文件存在且不是活跃下载的文件
                    if temp_file and os.path.exists(temp_file) and temp_file not in active_full_paths:
                        try:
                            file_size = os.path.getsize(temp_file)
                            # 使用安全删除
                            self._safe_remove_file(temp_file, "已完成记录的临时文件")
                            
                            # 只有成功删除才计数
                            if not os.path.exists(temp_file):
                                result['freed_bytes'] += file_size
                                result['size_cleaned'] += 1
                                logger.info(f"强制清理已完成记录的临时文件: {os.path.basename(temp_file)} ({file_size/(1024*1024):.2f} MB)")
                            
                            # 同时清理关联的分片文件
                            self._cleanup_frag_files(temp_file)
                        except Exception as e:
                            logger.error(f"清理失败: {temp_file} - {e}")
                    
                    # 再次检查大小
                    total_size = self.get_temp_dir_size()
                    if total_size < max_size_bytes * 0.7:  # 清理到70%以下
                        break
                
                conn.close()
            except Exception as e:
                logger.error(f"按大小清理失败: {e}")
        
        # 最终统计
        final_size = self.get_temp_dir_size()
        result['total_after_mb'] = round(final_size / (1024 * 1024), 2)
        result['freed_mb'] = round(result['freed_bytes'] / (1024 * 1024), 2)
        
        if result['orphan_cleaned'] > 0 or result['size_cleaned'] > 0:
            logger.info(
                f"TEMP清理完成: 孤儿{result['orphan_cleaned']}个 + "
                f"超时/强制{result['size_cleaned']}个 = "
                f"释放 {result['freed_mb']} MB "
                f"(剩余 {result['total_after_mb']} MB)"
            )
        
        return result
    
    def cleanup_single_temp(self, temp_file_path: str):
        """
        清理单个临时文件及其关联的分片文件（下载完成后立即调用）
        
        Args:
            temp_file_path: 临时文件路径
        """
        if not temp_file_path:
            return
        
        # 先清理分片文件（通常先释放）
        self._cleanup_frag_files(temp_file_path)
        
        # 清理主文件 - 重试机制处理Windows文件锁定
        if os.path.exists(temp_file_path):
            self._safe_remove_file(temp_file_path, "主临时文件")
    
    def update_download_progress(self, video_url: str, temp_file: str = None, progress: float = None):
        """
        更新下载进度，刷新文件时间戳防止被误删（心跳机制）
        
        Args:
            video_url: 视频URL
            temp_file: 临时文件路径（可选）
            progress: 下载进度（可选）
        """
        try:
            import time
            
            # 更新数据库记录，延长超时
            with self._db_lock:
                db_path = self._get_db_path()
                conn = sqlite3.connect(db_path, timeout=10)
                cursor = conn.cursor()
                
                # 更新completed_at字段为当前时间
                cursor.execute('''
                    UPDATE download_status 
                    SET completed_at = ?
                    WHERE video_url = ?
                ''', (datetime.now().isoformat(), video_url))
                
                conn.commit()
                conn.close()
            
            # Touch临时文件，更新文件时间戳
            if temp_file and os.path.exists(temp_file):
                try:
                    os.utime(temp_file, (time.time(), time.time()))
                except Exception as e:
                    logger.debug(f"Touch主文件失败: {e}")
            
            # Touch相关的分片文件
            temp_dir = self.temp_dir
            if temp_file and os.path.exists(temp_dir):
                base_name = os.path.basename(temp_file)
                if '.' in base_name:
                    base_no_ext = base_name.rsplit('.', 1)[0]
                else:
                    base_no_ext = base_name
                
                for filename in os.listdir(temp_dir):
                    if base_no_ext in filename:
                        filepath = os.path.join(temp_dir, filename)
                        try:
                            os.utime(filepath, (time.time(), time.time()))
                        except Exception as e:
                            logger.debug(f"Touch分片文件失败: {e}")
                            
            logger.debug(f"更新下载心跳: {video_url}")
        except Exception as e:
            logger.debug(f"更新下载心跳失败: {e}")
    
    def _cleanup_frag_files(self, base_file_path: str):
        """
        清理指定基础文件的所有分片文件
        
        Args:
            base_file_path: 基础文件路径 (如 xxx.mp4)
        """
        import glob
        import time
        
        if not base_file_path:
            return
            
        temp_dir = os.path.dirname(base_file_path)
        base_name = os.path.basename(base_file_path)
        
        # 提取基础名（去掉扩展名）
        if '.' in base_name:
            name_without_ext = base_name.rsplit('.', 1)[0]
        else:
            name_without_ext = base_name
        
        # 匹配所有分片文件: name.part-Frag* 和 *.part
        frag_patterns = [
            os.path.join(temp_dir, f"{base_name}.part-Frag*"),
            os.path.join(temp_dir, f"{base_name}.part*"),
        ]
        
        frag_files = []
        for pattern in frag_patterns:
            frag_files.extend(glob.glob(pattern))
        
        for frag_file in frag_files:
            self._safe_remove_file(frag_file, "分片文件")
    
    def _safe_remove_file(self, file_path: str, file_type: str = "文件", max_retries: int = 5, retry_delay: float = 0.5):
        """
        安全删除文件，包含重试机制和Windows文件锁定处理
        
        Args:
            file_path: 文件路径
            file_type: 文件类型描述（用于日志）
            max_retries: 最大重试次数
            retry_delay: 每次重试间隔(秒)
        """
        import time
        
        if not os.path.exists(file_path):
            return
        
        for attempt in range(1, max_retries + 1):
            try:
                size = os.path.getsize(file_path)
                os.remove(file_path)
                
                if attempt == 1:
                    logger.debug(f"清理{file_type}: {os.path.basename(file_path)} ({size/(1024*1024):.2f} MB)")
                else:
                    logger.info(f"清理{file_type}成功(第{attempt}次): {os.path.basename(file_path)}")
                
                return
            except PermissionError as e:
                if "另一个程序正在使用此文件" in str(e) or "being used by another process" in str(e):
                    if attempt < max_retries:
                        logger.debug(f"文件被占用，等待重试({attempt}/{max_retries}): {os.path.basename(file_path)}")
                        time.sleep(retry_delay)
                    else:
                        logger.warning(f"清理{file_type}失败(文件被锁定): {os.path.basename(file_path)} - {e}")
                else:
                    logger.warning(f"清理{file_type}失败(权限): {os.path.basename(file_path)} - {e}")
                    return
            except Exception as e:
                if attempt < max_retries:
                    logger.debug(f"清理{file_type}失败，等待重试({attempt}/{max_retries}): {os.path.basename(file_path)} - {e}")
                    time.sleep(retry_delay)
                else:
                    logger.warning(f"清理{file_type}失败(最终): {os.path.basename(file_path)} - {e}")
                    return
    
    def get_pending_downloads(self) -> list:
        """获取未完成的下载"""
        db_path = self._get_db_path()
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT video_url, video_title, model_id, model_name, target_dir, temp_file, progress
            FROM download_status 
            WHERE status IN ('pending', 'downloading')
            ORDER BY created_at
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        return [{
            'video_url': row[0],
            'video_title': row[1],
            'model_id': row[2],
            'model_name': row[3],
            'target_dir': row[4],
            'temp_file': row[5],
            'progress': row[6]
        } for row in rows]
    
    def cleanup_temp_files(self, max_age_hours: int = 24):
        """清理过期的临时文件"""
        import time
        
        cleaned = 0
        current_time = time.time()
        max_age_seconds = max_age_hours * 3600
        
        for filename in os.listdir(self.temp_dir):
            filepath = os.path.join(self.temp_dir, filename)
            
            if os.path.isfile(filepath):
                file_age = current_time - os.path.getmtime(filepath)
                
                if file_age > max_age_seconds:
                    try:
                        os.remove(filepath)
                        cleaned += 1
                        logger.info(f"清理过期临时文件: {filename}")
                    except Exception as e:
                        logger.error(f"清理失败: {filename} - {e}")
        
        logger.info(f"清理了 {cleaned} 个过期临时文件")
        return cleaned
    
    def get_temp_dir_size(self) -> int:
        """获取临时目录大小（字节）"""
        total_size = 0
        
        for filename in os.listdir(self.temp_dir):
            filepath = os.path.join(self.temp_dir, filename)
            if os.path.isfile(filepath):
                total_size += os.path.getsize(filepath)
        
        return total_size
