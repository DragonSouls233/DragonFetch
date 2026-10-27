# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
数据库自动备份管理器
支持循环备份机制，保留最近N个备份版本
"""
import os
import shutil
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)


class DatabaseBackupManager:
    """数据库备份管理器
    
    功能特点：
    1. 自动循环备份（默认保留5份）
    2. 支持手动和自动触发备份
    3. 记录备份历史和时间戳
    4. 支持恢复到指定备份版本
    """
    
    def __init__(self, db_path: str, backup_dir: str = None, max_backups: int = 5):
        """
        初始化备份管理器
        
        Args:
            db_path: 数据库文件路径
            backup_dir: 备份目录（默认为数据库同目录下的backups文件夹）
            max_backups: 最大备份数量（默认5）
        """
        self.db_path = Path(db_path)
        self.max_backups = max_backups
        
        # 设置备份目录
        if backup_dir:
            self.backup_dir = Path(backup_dir)
        else:
            # 默认放在数据库同级的 backups 文件夹
            self.backup_dir = self.db_path.parent / 'db_backups'
        
        # 确保备份目录存在
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        
        # 备份索引文件（记录当前应该写入哪个编号）
        self.index_file = self.backup_dir / 'backup_index.txt'
        
        logger.info(f"数据库备份管理器初始化完成")
        logger.info(f"  - 数据库: {self.db_path}")
        logger.info(f"  - 备份目录: {self.backup_dir}")
        logger.info(f"  - 最大备份数: {max_backups}")
    
    def _get_current_backup_index(self) -> int:
        """获取当前备份索引（1-based）"""
        try:
            if self.index_file.exists():
                with open(self.index_file, 'r') as f:
                    index = int(f.read().strip())
                    return (index % self.max_backups) + 1
        except Exception as e:
            logger.warning(f"读取备份索引失败: {e}")
        
        return 1
    
    def _save_current_index(self, index: int):
        """保存当前备份索引"""
        try:
            with open(self.index_file, 'w') as f:
                f.write(str(index))
        except Exception as e:
            logger.error(f"保存备份索引失败: {e}")
    
    def create_backup(self, reason: str = "manual") -> Dict[str, any]:
        """
        创建一个备份
        
        Args:
            reason: 备份原因（如 "add_model", "detect_all", "manual"）
            
        Returns:
            备份信息字典 {
                'success': bool,
                'backup_file': str,
                'backup_number': int,
                'size_mb': float,
                'timestamp': str,
                'reason': str,
                'error': str (如果失败)
            }
        """
        result = {
            'success': False,
            'backup_file': None,
            'backup_number': 0,
            'size_mb': 0,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'reason': reason,
            'error': None
        }
        
        try:
            # 检查源数据库是否存在
            if not self.db_path.exists():
                result['error'] = f"数据库文件不存在: {self.db_path}"
                logger.error(result['error'])
                return result
            
            # 获取当前备份编号（循环使用）
            backup_num = self._get_current_backup_index()
            
            # 构建备份文件名
            timestamp_str = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_filename = f'backup_{backup_num}_{timestamp_str}.db'
            backup_path = self.backup_dir / backup_filename
            
            # 删除旧的同一编号的备份文件（如果有）
            old_backups = list(self.backup_dir.glob(f'backup_{backup_num}_*.db'))
            for old_backup in old_backups:
                try:
                    old_backup.unlink()
                    logger.debug(f"删除旧备份: {old_backup.name}")
                except Exception as e:
                    logger.warning(f"删除旧备份失败: {e}")
            
            # 执行复制操作
            shutil.copy2(str(self.db_path), str(backup_path))
            
            # 获取文件大小
            size_bytes = backup_path.stat().st_size
            size_mb = round(size_bytes / (1024 * 1024), 2)
            
            # 更新索引到下一个位置
            self._save_current_index(backup_num)
            
            # 填写返回结果
            result.update({
                'success': True,
                'backup_file': str(backup_path),
                'backup_number': backup_num,
                'size_mb': size_mb
            })
            
            logger.info(f"✅ 数据库备份成功 #{backup_num}")
            logger.info(f"   - 文件: {backup_filename}")
            logger.info(f"   - 大小: {size_mb} MB")
            logger.info(f"   - 原因: {reason}")
            
            # 写入备份日志
            self._write_backup_log(result)
            
            return result
            
        except Exception as e:
            result['error'] = str(e)
            logger.error(f"❌ 数据库备份失败: {e}")
            return result
    
    def _write_backup_log(self, backup_info: Dict):
        """写入备份日志"""
        log_file = self.backup_dir / 'backup_history.log'
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                log_line = (
                    f"[{backup_info['timestamp']}] "
                    f"#{backup_info['backup_number']} | "
                    f"{backup_info['size_mb']} MB | "
                    f"{backup_info['reason']} | "
                    f"{Path(backup_info['backup_file']).name}\n"
                )
                f.write(log_line)
        except Exception as e:
            logger.warning(f"写入备份日志失败: {e}")
    
    def get_backup_list(self) -> List[Dict]:
        """
        获取所有备份列表
        
        Returns:
            备份信息列表 [{number, filename, size_mb, created_time}, ...]
        """
        backups = []
        
        try:
            backup_files = sorted(
                self.backup_dir.glob('backup_*.db'),
                key=lambda x: x.stat().st_mtime,
                reverse=True
            )
            
            for backup_file in backup_files:
                stat = backup_file.stat()
                
                # 解析备份编号
                parts = backup_file.stem.split('_')
                backup_num = int(parts[1]) if len(parts) >= 2 else 0
                
                backups.append({
                    'number': backup_num,
                    'filename': backup_file.name,
                    'path': str(backup_file),
                    'size_mb': round(stat.st_size / (1024 * 1024), 2),
                    'created_time': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                    'size_bytes': stat.st_size
                })
                
        except Exception as e:
            logger.error(f"获取备份列表失败: {e}")
        
        return backups
    
    def get_latest_backup(self) -> Optional[Dict]:
        """获取最新的备份信息"""
        backups = self.get_backup_list()
        return backups[0] if backups else None
    
    def restore_backup(self, backup_filename: str, create_restore_point: bool = True) -> bool:
        """
        从备份恢复数据库
        
        Args:
            backup_filename: 备份文件名
            create_restore_point: 是否在恢复前创建当前状态的备份点
            
        Returns:
            是否成功
        """
        try:
            backup_path = self.backup_dir / backup_filename
            
            if not backup_path.exists():
                logger.error(f"备份文件不存在: {backup_filename}")
                return False
            
            # 恢复前先备份当前状态（可选）
            if create_restore_point and self.db_path.exists():
                logger.info("恢复前创建当前状态备份...")
                self.create_backup(reason="pre_restore")
            
            # 执行恢复
            shutil.copy2(str(backup_path), str(self.db_path))
            
            logger.info(f"✅ 数据库恢复成功: {backup_filename}")
            return True
            
        except Exception as e:
            logger.error(f"❌ 数据库恢复失败: {e}")
            return False
    
    def cleanup_old_backups(self):
        """清理超出最大数量的旧备份（按时间排序）"""
        try:
            backups = self.get_backup_list()
            
            if len(backups) > self.max_backups:
                # 删除最旧的备份
                for i in range(len(backups) - self.max_backups):
                    old_backup = backups[-(i+1)]
                    try:
                        Path(old_backup['path']).unlink()
                        logger.info(f"清理旧备份: {old_backup['filename']}")
                    except Exception as e:
                        logger.warning(f"清理失败: {e}")
                        
        except Exception as e:
            logger.error(f"清理旧备份失败: {e}")
    
    def get_backup_stats(self) -> Dict:
        """获取备份统计信息"""
        backups = self.get_backup_list()
        
        total_size = sum(b['size_bytes'] for b in backups)
        
        return {
            'total_count': len(backups),
            'total_size_mb': round(total_size / (1024 * 1024), 2),
            'max_backups': self.max_backups,
            'latest_backup': backups[0] if backups else None,
            'oldest_backup': backups[-1] if backups else None,
            'backup_dir': str(self.backup_dir)
        }


# 全局单例实例
_backup_manager_instance: Optional[DatabaseBackupManager] = None


def get_backup_manager() -> DatabaseBackupManager:
    """获取全局备份管理器单例"""
    global _backup_manager_instance
    
    if _backup_manager_instance is None:
        from multisite.core.utils.path_utils import get_data_path, ensure_data_dir
        ensure_data_dir('db')
        db_path = os.path.join(get_data_path('db'), 'models.db')
        _backup_manager_instance = DatabaseBackupManager(db_path)
    
    return _backup_manager_instance


def auto_backup(reason: str = "auto") -> Dict[str, any]:
    """
    便捷函数：执行自动备份
    
    Args:
        reason: 备份原因
        
    Returns:
        备份结果
    """
    manager = get_backup_manager()
    return manager.create_backup(reason=reason)


if __name__ == '__main__':
    # 测试代码
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    print("=" * 60)
    print("数据库备份管理器测试")
    print("=" * 60)
    
    # 创建测试用的备份管理器
    test_db = Path("test_db/test_models.db")
    test_db.parent.mkdir(exist_ok=True)
    
    # 创建测试数据库
    import sqlite3
    conn = sqlite3.connect(str(test_db))
    conn.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY, data TEXT)")
    conn.execute("INSERT INTO test VALUES (1, 'test data')")
    conn.commit()
    conn.close()
    
    manager = DatabaseBackupManager(str(test_db), max_backups=5)
    
    # 测试多次备份
    for i in range(7):
        print(f"\n--- 第 {i+1} 次备份 ---")
        result = manager.create_backup(reason=f"test_{i+1}")
        print(f"结果: {result}")
    
    # 显示备份列表
    print("\n" + "=" * 60)
    print("备份列表:")
    print("=" * 60)
    backups = manager.get_backup_list()
    for b in backups:
        print(f"  #{b['number']} | {b['filename']} | {b['size_mb']} MB | {b['created_time']}")
    
    # 统计信息
    stats = manager.get_backup_stats()
    print(f"\n统计: {stats['total_count']} 个备份, 总大小 {stats['total_size_mb']} MB")
    
    # 清理测试文件
    import shutil
    if test_db.parent.exists():
        shutil.rmtree(test_db.parent)
    
    print("\n✅ 测试完成！")