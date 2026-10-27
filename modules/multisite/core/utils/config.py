# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
配置管理器
"""
import os
import yaml
from typing import Any, Dict, Optional
from pathlib import Path


class ConfigManager:
    """配置管理器"""
    
    _instance = None
    _config = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if self._config is None:
            self._load_config()
    
    def _load_config(self):
        """加载配置文件"""
        from multisite.core.utils.path_utils import get_config_path
        
        # 查找配置文件
        config_paths = [
            get_config_path('../config.yaml'),  # 尝试上级目录的config.yaml
            get_config_path('config.yaml'),     # 尝试config目录下的config.yaml
        ]
        
        config_path = None
        for path in config_paths:
            if os.path.exists(path):
                config_path = path
                break
        
        if config_path and os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f) or {}
        else:
            self._config = self._get_default_config()
    
    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            'proxy': {
                'enabled': False,
                'host': '127.0.0.1',
                'port': '10808',
                'type': 'socks5',
                'http': '',
                'https': '',
                'username': '',
                'password': '',
            },
            'download': {
                'engine': 'yt-dlp',
                'max_concurrent': 3,
                'timeout': 300,
                'retry_times': 3,
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                'speed_limit': 0,
                'cookie_file': '',
            },
            'database': {
                'data_dir': 'data/db',
                'models_db': 'models.db',
                'download_cache': 'download_cache.db',
            },
            'detect': {
                'max_pages': 100,
                'request_delay': 1.0,
                'timeout': 30,
            },
            'log': {
                'level': 'INFO',
                'file': 'logs/app.log',
                'console': True,
            },
        }
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值
        
        Args:
            key: 配置键，支持点号分隔，如 'proxy.enabled'
            default: 默认值
        
        Returns:
            配置值
        """
        keys = key.split('.')
        value = self._config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    def get_proxy_config(self) -> Dict[str, Any]:
        """获取代理配置"""
        return self._config.get('proxy', {})
    
    def get_download_config(self) -> Dict[str, Any]:
        """获取下载配置"""
        return self._config.get('download', {})
    
    def get_proxy_url(self) -> Optional[str]:
        """
        获取代理URL
        
        Returns:
            代理URL，如 'socks5://127.0.0.1:10808'
        """
        proxy_config = self.get_proxy_config()
        
        if not proxy_config.get('enabled', False):
            return None
        
        proxy_type = proxy_config.get('type', 'socks5')
        host = proxy_config.get('host', '127.0.0.1')
        port = proxy_config.get('port', '10808')
        username = proxy_config.get('username', '')
        password = proxy_config.get('password', '')
        
        if username and password:
            return f"{proxy_type}://{username}:{password}@{host}:{port}"
        else:
            return f"{proxy_type}://{host}:{port}"
    
    def get_proxies_dict(self) -> Dict[str, str]:
        """
        获取代理字典（用于requests）
        
        Returns:
            代理字典，如 {'http': 'socks5://...', 'https': 'socks5://...'}
        """
        proxy_url = self.get_proxy_url()
        
        if not proxy_url:
            return {}
        
        return {
            'http': proxy_url,
            'https': proxy_url,
        }
    
    def reload(self):
        """重新加载配置"""
        self._config = None
        self._load_config()


# 全局配置实例
config = ConfigManager()
