# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
应用配置管理器
支持自定义数据库位置、性能优化等配置
"""
import os
import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class AppConfig:
    """应用配置管理器"""
    
    _instance = None
    _config = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if self._config is None:
            self._load_config()
    
    def _get_config_path(self) -> str:
        """获取配置文件路径"""
        from multisite.core.utils.path_utils import get_config_path
        return get_config_path('app_config.json')
    
    def _load_config(self):
        """加载配置"""
        config_path = self._get_config_path()
        
        # 默认配置
        default_config = {
            "database": {
                "custom_path": None,
                "auto_backup": True,
                "backup_interval_days": 7
            },
            "download": {
                "concurrency": 3,
                "max_retries": 5,
                "timeout": 60,
                "chunk_size": 8192,
                "buffer_size": 102400
            },
            "performance": {
                "max_threads": 10,
                "memory_limit_mb": 512,
                "cache_enabled": True,
                "cache_size_mb": 100,
                "gc_interval": 100
            },
            "network": {
                "proxy_enabled": True,
                "proxy_type": "socks5",
                "proxy_host": "127.0.0.1",
                "proxy_port": 10808
            }
        }
        
        # 尝试加载配置文件
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                
                # 合并配置
                self._config = self._merge_config(default_config, loaded_config)
                logger.info(f"配置已加载: {config_path}")
                
            except Exception as e:
                logger.error(f"加载配置失败: {e}")
                self._config = default_config
        else:
            # 创建默认配置文件
            self._config = default_config
            self._save_config()
            logger.info(f"已创建默认配置: {config_path}")
    
    def _merge_config(self, default: Dict, loaded: Dict) -> Dict:
        """合并配置（保留默认值）"""
        result = default.copy()
        
        for key, value in loaded.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = {**result[key], **value}
            else:
                result[key] = value
        
        return result
    
    def _save_config(self):
        """保存配置"""
        config_path = self._get_config_path()
        
        try:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=4, ensure_ascii=False)
            
            logger.info(f"配置已保存: {config_path}")
            
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
    
    def get(self, *keys, default=None) -> Any:
        """
        获取配置值
        
        Args:
            *keys: 配置键路径，如 get('database', 'custom_path')
            default: 默认值
        
        Returns:
            配置值
        """
        value = self._config
        
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        
        return value
    
    def set(self, *keys_and_value):
        """
        设置配置值
        
        Args:
            *keys_and_value: 配置键路径和值，如 set('database', 'custom_path', '/path/to/db')
        """
        if len(keys_and_value) < 2:
            return
        
        keys = keys_and_value[:-1]
        value = keys_and_value[-1]
        
        # 导航到目标位置
        current = self._config
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        
        # 设置值
        current[keys[-1]] = value
        
        # 保存
        self._save_config()
    
    def get_database_path(self) -> str:
        """
        获取数据库路径
        
        Returns:
            数据库路径
        """
        # 优先使用自定义路径
        custom_path = self.get('database', 'custom_path')
        if custom_path and os.path.isabs(custom_path):
            return custom_path
        
        # 使用默认路径
        from multisite.core.utils.path_utils import get_data_path
        return get_data_path('db', 'models.db')
    
    def set_database_path(self, path: str):
        """
        设置数据库路径
        
        Args:
            path: 数据库路径
        """
        self.set('database', 'custom_path', path)
    
    def get_download_config(self) -> Dict:
        """获取下载配置"""
        return self.get('download', default={})
    
    def get_performance_config(self) -> Dict:
        """获取性能配置"""
        return self.get('performance', default={})
    
    def get_network_config(self) -> Dict:
        """获取网络配置"""
        return self.get('network', default={})
    
    def reset_to_default(self):
        """重置为默认配置"""
        self._config = None
        self._load_config()


# 全局配置实例
app_config = AppConfig()
