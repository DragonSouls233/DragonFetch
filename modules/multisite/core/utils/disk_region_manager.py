# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
地区-盘符映射管理器
用于管理不同地区的视频存储位置，支持按地区批量设置本地目录
"""
import os
import json
from typing import Dict, List, Optional, Set
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class DiskRegionManager:
    """地区-盘符映射管理器"""

    # 国家到地区的映射
    COUNTRY_REGION_MAP = {
        # 亚洲地区
        '中国': '亚洲',
        '日本': '亚洲',
        '韩国': '亚洲',
        '台湾': '亚洲',
        '香港': '亚洲',
        '泰国': '亚洲',
        '亚洲': '亚洲',

        # 亚美地区（美国和美洲国家）
        '美国': '亚洲',
        '美国2': '亚洲',
        '加拿大': '亚洲',
        '澳大利亚': '亚洲',

        # 南美洲地区
        '巴西': '南美洲',
        '阿根廷': '南美洲',
        '哥伦比亚': '南美洲',
        '墨西哥': '南美洲',
        '南美洲': '南美洲',

        # 欧美地区
        '英国': '欧美',
        '德国': '欧美',
        '法国': '欧美',
        '意大利': '欧美',
        '西班牙': '欧美',
        '俄罗斯': '欧美',
        '俄罗斯2': '欧美',
        '乌克兰': '欧美',
        '捷克': '欧美',
        '波兰': '欧美',
        '爱沙尼亚': '欧美',
        '欧盟': '欧美',
        '欧美': '欧美',

        # 特殊分类
        'Cosplay': '特殊',
        '系列': '特殊',
        '素人': '特殊',
        '素人2': '特殊',
        '素人3': '特殊',
        '自慰': '特殊',
        '动漫': '特殊',
        '特殊': '特殊',
        '未分国籍': '其他',
        '已注销': '其他',
        '男模': '其他',
    }

    def __init__(self, config_path: str = None):
        """
        初始化地区-盘符映射管理器

        Args:
            config_path: 配置文件路径
        """
        if config_path is None:
            import sys
            if getattr(sys, 'frozen', False):
                base_path = os.path.dirname(sys.executable)
            else:
                base_path = Path(__file__).parent.parent.parent.parent
            
            config_dir = os.path.join(base_path, 'data', 'config')
            Path(config_dir).mkdir(parents=True, exist_ok=True)
            config_path = os.path.join(config_dir, 'disk_region_config.json')

        self.config_path = config_path
        self.region_disk_map: Dict[str, str] = {}  # 地区 -> 盘符映射
        self.disk_info: Dict[str, Dict] = {}  # 盘符信息

        self.load_config()

    def load_config(self):
        """加载配置"""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.region_disk_map = config.get('region_disk_map', {})
                    # 加载国家-地区映射
                    if 'country_region_map' in config:
                        self.COUNTRY_REGION_MAP.update(config['country_region_map'])
                    logger.info(f"加载地区-盘符配置: {self.region_disk_map}")
                    logger.info(f"加载国家-地区映射: {len(self.COUNTRY_REGION_MAP)} 个国家")
            else:
                # 创建默认配置
                self.region_disk_map = {}
                self.save_config()
        except Exception as e:
            logger.error(f"加载地区-盘符配置失败: {e}")
            self.region_disk_map = {}

    def save_config(self):
        """保存配置"""
        try:
            config = {
                'region_disk_map': self.region_disk_map,
                'country_region_map': self.COUNTRY_REGION_MAP
            }
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            logger.info(f"保存地区-盘符配置: {self.region_disk_map}")
            logger.info(f"保存国家-地区映射: {len(self.COUNTRY_REGION_MAP)} 个国家")
        except Exception as e:
            logger.error(f"保存地区-盘符配置失败: {e}")

    def get_available_drives(self) -> List[str]:
        """
        获取可用的盘符列表

        Returns:
            盘符列表，如 ['C:', 'D:', 'E:']
        """
        drives = []
        if os.name == 'nt':  # Windows系统
            import win32api
            drives = win32api.GetLogicalDriveStrings().split('\0')[:-1]
        else:
            # Linux/Mac系统
            for mount in ['/mnt', '/media']:
                if os.path.exists(mount):
                    drives.extend([os.path.join(mount, d) for d in os.listdir(mount)])

        logger.info(f"可用盘符: {drives}")
        return drives

    def get_drive_info(self, drive: str) -> Dict:
        """
        获取盘符信息

        Args:
            drive: 盘符，如 'D:'

        Returns:
            盘符信息字典
        """
        try:
            if os.name == 'nt':  # Windows系统
                import win32file
                import win32api

                # 获取总空间和可用空间
                total_bytes = win32api.GetDiskFreeSpaceEx(drive)[1]
                free_bytes = win32api.GetDiskFreeSpaceEx(drive)[0]
                used_bytes = total_bytes - free_bytes

                # 获取卷标
                try:
                    volume_name = win32api.GetVolumeInformation(drive)[0]
                except Exception:
                    volume_name = "未知"

                return {
                    'drive': drive,
                    'volume_name': volume_name,
                    'total_bytes': total_bytes,
                    'free_bytes': free_bytes,
                    'used_bytes': used_bytes,
                    'total_gb': round(total_bytes / (1024**3), 2),
                    'free_gb': round(free_bytes / (1024**3), 2),
                    'used_gb': round(used_bytes / (1024**3), 2),
                    'usage_percent': round((used_bytes / total_bytes) * 100, 2) if total_bytes > 0 else 0
                }
            else:
                # Linux/Mac系统
                import shutil
                total, used, free = shutil.disk_usage(drive)
                return {
                    'drive': drive,
                    'volume_name': drive,
                    'total_bytes': total,
                    'free_bytes': free,
                    'used_bytes': used,
                    'total_gb': round(total / (1024**3), 2),
                    'free_gb': round(free / (1024**3), 2),
                    'used_gb': round(used / (1024**3), 2),
                    'usage_percent': round((used / total) * 100, 2) if total > 0 else 0
                }
        except Exception as e:
            logger.error(f"获取盘符信息失败 {drive}: {e}")
            return {
                'drive': drive,
                'volume_name': '未知',
                'total_bytes': 0,
                'free_bytes': 0,
                'used_bytes': 0,
                'total_gb': 0,
                'free_gb': 0,
                'used_gb': 0,
                'usage_percent': 0
            }

    def refresh_disk_info(self):
        """刷新所有盘符信息"""
        drives = self.get_available_drives()
        self.disk_info = {}
        for drive in drives:
            self.disk_info[drive] = self.get_drive_info(drive)
        logger.info(f"刷新盘符信息完成，共 {len(self.disk_info)} 个盘符")

    def set_region_disk(self, region: str, disk: str):
        """
        设置地区对应的盘符

        Args:
            region: 地区名称，如 '亚洲', '欧美'
            disk: 盘符，如 'D:'
        """
        self.region_disk_map[region] = disk
        self.save_config()
        logger.info(f"设置地区盘符: {region} -> {disk}")

    def get_region_disk(self, region: str) -> Optional[str]:
        """
        获取地区对应的盘符

        Args:
            region: 地区名称

        Returns:
            盘符，如果未设置则返回None
        """
        return self.region_disk_map.get(region)

    def get_country_region(self, country: str) -> str:
        """
        获取国家对应的地区

        Args:
            country: 国家名称

        Returns:
            地区名称
        """
        return self.COUNTRY_REGION_MAP.get(country, '其他')

    def get_country_disk(self, country: str) -> Optional[str]:
        """
        获取国家对应的盘符

        Args:
            country: 国家名称

        Returns:
            盘符，如果未设置则返回None
        """
        region = self.get_country_region(country)
        return self.get_region_disk(region)

    def build_local_directory(self, country: str, name: str, module: str = 'PORN') -> Optional[str]:
        """
        按项目标准格式生成模特本地目录路径（与批量更新逻辑一致）：
            PORN 模块：盘符\\国家\\[Channel] 模特名
            其它模块：盘符\\国家\\模特名
        盘符由国家→地区→盘符映射得到；若未配置该地区盘符或名字为空则返回 None。

        Args:
            country: 国家名称
            name:    模特名
            module:  模块标识（默认 PORN）

        Returns:
            标准格式的本地目录路径，或 None（盘符未配置 / 名字为空）
        """
        if not name:
            return None
        disk = self.get_country_disk(country)
        if not disk:
            return None
        dir_name = f"[Channel] {name}" if module == 'PORN' else name
        return os.path.join(disk, country, dir_name)

    def get_all_regions(self) -> List[str]:
        """
        获取所有地区

        Returns:
            地区列表
        """
        regions = set(self.COUNTRY_REGION_MAP.values())
        return sorted(list(regions))

    def get_countries_by_region(self, region: str) -> List[str]:
        """
        获取指定地区的所有国家

        Args:
            region: 地区名称

        Returns:
            国家列表
        """
        countries = [country for country, r in self.COUNTRY_REGION_MAP.items() if r == region]
        return sorted(list(set(countries)))

    def batch_update_local_directories(self, models_data: List[Dict]) -> List[Dict]:
        """
        批量更新模特的本地目录

        Args:
            models_data: 模特数据列表，每个元素包含 {'id': int, 'country': str, 'name': str, 'module': str}

        Returns:
            更新结果列表
        """
        results = []
        for model in models_data:
            model_id = model.get('id')
            country = model.get('country', '未分国籍')
            model_name = model.get('name', '')
            module = model.get('module', 'PORN')  # 默认 PORN 模块

            # 获取国家对应的盘符
            disk = self.get_country_disk(country)

            if disk:
                # PORN 模块需要 [Channel] 前缀
                if module == 'PORN':
                    dir_name = f"[Channel] {model_name}"
                else:
                    dir_name = model_name
                
                # 构建本地目录路径：盘符\国家\[Channel] 模特名
                local_dir = os.path.join(disk, country, dir_name)
                results.append({
                    'id': model_id,
                    'country': country,
                    'local_directory': local_dir,
                    'success': True
                })
            else:
                results.append({
                    'id': model_id,
                    'country': country,
                    'local_directory': None,
                    'success': False,
                    'error': f'未设置 {self.get_country_region(country)} 地区的盘符'
                })

        return results

    def get_region_summary(self) -> Dict[str, Dict]:
        """
        获取各地区盘符配置摘要

        Returns:
            地区配置摘要字典
        """
        summary = {}
        for region in self.get_all_regions():
            disk = self.get_region_disk(region)
            countries = self.get_countries_by_region(region)

            if disk:
                disk_info = self.disk_info.get(disk, {})
            else:
                disk_info = {}

            summary[region] = {
                'disk': disk,
                'countries': countries,
                'country_count': len(countries),
                'disk_info': disk_info
            }

        return summary


# 全局实例
_disk_region_manager = None


def get_disk_region_manager() -> DiskRegionManager:
    """获取全局地区-盘符映射管理器实例"""
    global _disk_region_manager
    if _disk_region_manager is None:
        _disk_region_manager = DiskRegionManager()
    return _disk_region_manager
