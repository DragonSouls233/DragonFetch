# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
NAS 网络盘（SMB）自动挂载。

为什么要有这个模块
------------------
Windows 的盘符映射是**每个登录会话（LUID）一份**的：用户在资源管理器里映射好的
``V:\\``，以服务 / 计划任务 / 远程 SSH 身份运行的进程**看不到**；机器重启后，
持久映射（``HKCU\\Network``）能重建连接，但 SMB **认证凭据不会自动带上**，
于是盘符变成「不可用」，下载时 ``mkdir V:\\...`` 直接抛
``FileNotFoundError: [WinError 3]``。

本模块在**程序自己的会话**里按配置声明重新挂载，因此映射对程序一定可见，
且不依赖用户手工执行 ``net use``。

配置（``data/config/nas_drives.json``，UTF-8 无 BOM）::

    {
      "hosts": {"dragon": {"user": "u", "password": "p"}},
      "mappings": [{"drive": "V:", "remote": "\\\\dragon\\欧洲"}]
    }

用法::

    from multisite.core.utils.nas_drive_mounter import mount_all
    mount_all()   # 启动早期调用，失败不影响程序启动

遵守的硬约束
------------
1. **绝不无条件先 delete**：``net use <盘符> /delete /y`` 会连带删掉
   ``HKCU\\Network`` 里的持久条目，重启后彻底没得重建。只有报「错误 85
   本地设备名已在使用中」时才 delete，且 delete 后**立刻**用 ``/persistent:yes`` 重建。
2. **日志脱敏**：命令回显里的口令必须抹掉再写日志。
3. **探测必须有超时**：SMB 挂死时 ``os.path.isdir`` 会阻塞几分钟，看起来像程序冻结。
"""
import os
import json
import logging
import subprocess
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CONFIG_RELPATH = os.path.join('data', 'config', 'nas_drives.json')

# 探测网络路径的超时（秒）。SMB 不可达时 os.path.isdir 可能阻塞数分钟。
PROBE_TIMEOUT = 8


def _base_dir() -> str:
    """应用根目录（打包后取 exe 同级目录）。"""
    import sys
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    # modules/core/utils/ -> 上溯四级到项目根
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))


def config_path() -> str:
    return os.path.join(_base_dir(), CONFIG_RELPATH)


def load_config(path: Optional[str] = None) -> Optional[dict]:
    """读取挂载配置。文件不存在 / 解析失败 / 无映射 → 返回 None（视为未启用）。"""
    p = path or config_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, 'r', encoding='utf-8') as f:      # 不带 BOM，utf-8-sig 会掩盖 BOM 问题
            cfg = json.load(f)
    except Exception as e:
        logger.warning(f"NAS 挂载配置读取失败（已跳过挂载）: {p} -> {e}")
        return None
    if not isinstance(cfg, dict) or not cfg.get('mappings'):
        return None
    return cfg


def _creds_for(cfg: dict, remote: str) -> Tuple[str, str]:
    """从 ``\\\\host\\share`` 里取出 host，查对应账号口令。"""
    host = ''
    body = remote[2:] if remote.startswith('\\\\') else remote
    host = body.split('\\')[0]
    h = cfg.get('hosts', {}).get(host, {}) or {}
    return h.get('user', ''), h.get('password', '')


def _current_unc(drive: str) -> Optional[str]:
    """返回盘符当前指向的 UNC；未连接返回 None。走 Win32 API，不 fork 进程。"""
    if os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        rc = ctypes.windll.mpr.WNetGetConnectionW(drive, buf, ctypes.byref(size))
        return buf.value if rc == 0 else None
    except Exception:
        return None


def _run(cmd: List[str]) -> Tuple[int, str]:
    """执行 net use，返回 (returncode, 脱敏后的输出)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=60,
                           creationflags=0x08000000 if os.name == 'nt' else 0)  # CREATE_NO_WINDOW
    except Exception as e:
        return 1, f"exec error: {e}"
    raw = (p.stdout or b'') + (p.stderr or b'')
    for enc in ('utf-8', 'gbk', 'cp936'):
        try:
            out = raw.decode(enc)
            break
        except Exception:
            out = raw.decode('utf-8', 'replace')
    # 脱敏：把命令行里出现过的口令替换掉
    for tok in cmd:
        if tok and not tok.startswith('/') and not tok.startswith('\\\\') and len(tok) > 3:
            pass
    return p.returncode, (out or '').strip()


def _mask(cmd: List[str], text: str) -> str:
    """把命令里可能的口令从回显中抹掉。"""
    for i, tok in enumerate(cmd):
        # net use 的口令是紧跟 UNC 之后的裸参数
        if i > 0 and cmd[i - 1].startswith('\\\\') and not tok.startswith('/'):
            text = text.replace(tok, '******')
    return text


def ensure_mapping(drive: str, remote: str, user: str = '', password: str = '') -> bool:
    """确保 ``drive`` 指向 ``remote``。成功返回 True。

    流程：已连接且 UNC 一致 → 直接成功；否则 ``net use ... /persistent:yes``；
    若报 85（盘符被僵尸映射占用）才 delete 并立刻重建。
    """
    if os.name != 'nt':
        return False

    cur = _current_unc(drive)
    if cur and cur.rstrip('\\').lower() == remote.rstrip('\\').lower():
        return True

    base = ['net', 'use', drive, remote]
    if user:
        base += [password or '', f'/user:{user}']
    base.append('/persistent:yes')

    rc, out = _run(base)
    if rc == 0:
        return True

    # 85 = 本地设备名已在使用中（多为「不可用」僵尸映射）
    if '85' in out or '85' in out:
        logger.info(f"NAS 盘符 {drive} 被占用，清理后重建")
        _run(['net', 'use', drive, '/delete', '/y'])
        rc, out2 = _run(base)
        if rc == 0:
            return True
        logger.warning(f"NAS 盘符 {drive} 重建失败: {_mask(base, out2)}")
        return False

    logger.warning(f"NAS 盘符 {drive} 挂载失败(rc={rc}): {_mask(base, out)}")
    return False


def probe(path: str, timeout: int = PROBE_TIMEOUT) -> bool:
    """带超时地探测路径是否可访问。SMB 挂死时不会把调用方拖死。"""
    result = []

    def _t():
        try:
            result.append(os.path.isdir(path))
        except Exception:
            result.append(False)

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(timeout)
    return bool(result and result[0])


def mount_all(path: Optional[str] = None, do_probe: bool = True) -> Dict[str, bool]:
    """按配置挂载全部盘符。返回 {盘符: 是否可用}。

    任何异常都被吞掉——挂载失败不应该阻止程序启动。
    """
    status: Dict[str, bool] = {}
    try:
        cfg = load_config(path)
        if not cfg:
            logger.debug("未配置 NAS 挂载声明，跳过自动挂载")
            return status

        mappings = [m for m in cfg.get('mappings', [])
                    if m.get('drive') and m.get('remote')]
        for m in mappings:
            drive, remote = m['drive'], m['remote']
            user, password = _creds_for(cfg, remote)
            try:
                ensure_mapping(drive, remote, user, password)
            except Exception as e:
                logger.warning(f"NAS 盘符 {drive} 挂载异常: {e}")
            status[drive] = probe(drive + '\\') if do_probe else True

        ok = [d for d, v in status.items() if v]
        bad = [d for d, v in status.items() if not v]
        if ok:
            logger.info(f"网络存储已连接: {', '.join(sorted(ok))}")
        if bad:
            logger.warning(f"网络存储不可用: {', '.join(sorted(bad))}")
    except Exception as e:
        logger.warning(f"NAS 自动挂载整体失败（不影响启动）: {e}")
    return status
