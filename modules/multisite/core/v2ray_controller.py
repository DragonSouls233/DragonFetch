# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
Xray 核心控制器

直接调用 Xray 核心运行代理，无需启动 v2rayN 的 GUI 窗口。
全部在程序后台运行，用户完全感知不到。

节点信息从 guiNDB.db 读取，切换节点时生成新配置并重启 Xray。
"""
import os
import sys
import time
import json
import atexit
import logging
import shutil
import sqlite3
import threading
import subprocess
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class XrayCoreController:
    """
    Xray 核心控制器
    
    直接调用 xray.exe 运行代理，无窗口、后台运行。
    切换节点时生成新配置、重启 Xray，全程无感。
    """
    
    SOCKS_PORT = 10808
    
    def __init__(self):
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None
        self._current_node_index = 0
        self._total_nodes = 0
        self._node_switch_interval = 7200  # 默认每2小时切换
        self._last_switch_time = 0
        self._enabled = False
        self._switch_count = 0
        self._node_ids: List[str] = []
        self._running = False
        self._on_switch_callbacks: List = []
        self._started = False  # 标记是否已完成首次启动
        # 多出口负载均衡（2026-09-28）：所有节点同时承载流量，而不是
        # 一次只用一个、定时切换还要重启 Xray。见 _generate_balanced_config。
        self._load_balance = True
        self._LB_TAG = 'proxy-balance'
        # 「节点专属入口」端口（2026-09-28 第二批）：每个节点一个固定入口，
        # 由下载器**按任务**挑一个，保证同一次下载的所有请求走同一个出口。
        # 起因：负载均衡的 roundRobin 是**逐连接**轮换的，而 PornHub 的媒体
        # 直链里带 ?ip=<出口IP>&... —— 页面/m3u8 走 A 节点、分片走 B 节点时
        # CDN 直接回 HTTP 474 "Client Error"，表现为「一开负载均衡就全部下载
        # 失败」。所以下载流量不能走 balancer，要走专属入口。
        self._pinned_ports: List[int] = []
        self._pinned_nodes: List[str] = []
        
        # 确定目录
        self._base_dir = self._find_base_dir()
        self._xray_exe = os.path.join(self._base_dir, 'bin', 'xray', 'xray.exe')
        self._config_path = os.path.join(self._base_dir, 'guiConfigs', 'config.json')
        self._gui_config_path = os.path.join(self._base_dir, 'guiConfigs', 'guiNConfig.json')
        self._db_path = os.path.join(self._base_dir, 'guiConfigs', 'guiNDB.db')
        
        # 加载配置
        self._load_config()
        
        if self._enabled:
            # 读取节点列表
            self._refresh_node_list()
            
            # 启动前从数据库重新生成 config.json（避免旧配置导致 Xray 启动失败）
            self._regenerate_config_for_current_node()
            
            # 注册退出清理
            atexit.register(self.stop)
            
            # 启动 Xray 核心（无窗口）
            self.start()
            
            logger.info(f"XrayCore 启动完成: {self._total_nodes} 个节点, "
                       f"切换间隔={self._node_switch_interval//3600}小时")
        else:
            logger.info("XrayCore 未启用（请在配置中设置 v2ray.enabled=true）")
    
    def _find_base_dir(self) -> str:
        """查找 v2rayN 基础目录（支持打包环境）"""
        # 打包环境：EXE 所在目录
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
            logger.info(f"打包环境，EXE 目录: {base}")
        else:
            # 开发环境：项目根目录
            base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            logger.info(f"开发环境，项目目录: {base}")
        
        # 尝试 EXE 同级目录下的 v2rayN
        exe_dir = os.path.join(base, 'v2rayN')
        if os.path.exists(os.path.join(exe_dir, 'bin', 'xray', 'xray.exe')):
            logger.info(f"找到 v2rayN: {exe_dir}")
            return exe_dir
        
        # 尝试 EXE 所在目录直接包含 guiConfigs（打包时可能平级放置）
        if os.path.exists(os.path.join(base, 'guiConfigs', 'guiNDB.db')):
            logger.info(f"找到 v2rayN（平级）: {base}")
            return base
        
        # 已有安装（备用）
        existing = 'D:/v2rayN-windows-64'
        if os.path.exists(os.path.join(existing, 'bin', 'xray', 'xray.exe')):
            logger.info(f"使用已有安装 v2rayN: {existing}")
            return existing
        
        logger.warning(f"未找到 v2rayN，回退到: {exe_dir}")
        return exe_dir
    
    def _load_config(self):
        """从配置文件加载设置"""
        try:
            from multisite.core.utils.path_utils import get_config_path
            config_file = get_config_path('app_config.json')
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                v2ray_config = config_data.get('v2ray', {})
                self._enabled = v2ray_config.get('enabled', False)
                interval_hours = v2ray_config.get('switch_interval_hours', 2)
                self._node_switch_interval = interval_hours * 3600
                # 关掉它即回到「单出口 + 定时切换」的老行为（逃生开关）
                self._load_balance = bool(v2ray_config.get('load_balance', True))
        except Exception as e:
            logger.warning(f"加载配置失败: {e}")
    
    # ------------------------------------------------------------------
    # 节点库自愈（2026-09-29）
    # ------------------------------------------------------------------
    # 起因：节点真源 `v2rayN/guiConfigs/guiNDB.db` 位于 v2rayN 目录内，
    # 任何手工脚本（典型如残留的「应用新节点库.bat」用改名换库）都能把它
    # 换成旧备份 —— 节点凭空少掉，且每次都要记得去删那个脚本，忘了就复发。
    # 09-28→09-29 两次上线均因此丢失最快的 vps2 节点。
    #
    # 对策：在**程序自己的 config 目录**维护一份「黄金副本」nodes_master.db
    # —— 换库脚本只碰 v2rayN 目录，够不到这里。每次启动双向比对：
    #   · guiNDB 少了节点  ⇒ 判定被覆盖，从黄金副本恢复（自动补回）
    #   · guiNDB 多了节点  ⇒ 用户新加了节点，提升为新的黄金副本
    # 于是「忘记删除脚本」不再是故障：即使脚本再次覆盖，下次启动也会自愈。
    _MASTER_DB_NAME = 'nodes_master.db'
    _MASTER_EXCLUDE_NAME = 'nodes_master.exclude'

    def _master_db_path(self) -> str:
        """黄金副本路径（config 目录，换库脚本够不到）"""
        try:
            from multisite.core.utils.path_utils import get_config_path
            return get_config_path(self._MASTER_DB_NAME)
        except Exception:
            # base_dir 形如 <root>/v2rayN，其上级即程序根目录
            return os.path.join(os.path.dirname(self._base_dir), 'config',
                                self._MASTER_DB_NAME)

    @staticmethod
    def _read_node_sigs(db_path: str) -> Optional[set]:
        """读取节点签名集合 'Remarks|Address|Port'；读失败返回 None（不动作）。"""
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT Remarks, Address, Port FROM ProfileItem")
            rows = cur.fetchall()
        except Exception as e:
            logger.debug("读取节点签名失败 %s: %s", db_path, e)
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return {'%s|%s|%s' % (r[0] or '', r[1] or '', r[2] or '') for r in rows}

    def _load_exclude(self, master_path: str) -> set:
        """墓碑名单：列在里面的节点名不会被自愈补回（用于永久移除坏节点）。"""
        p = os.path.join(os.path.dirname(master_path), self._MASTER_EXCLUDE_NAME)
        if not os.path.exists(p):
            return set()
        try:
            with open(p, 'r', encoding='utf-8-sig') as f:
                return {ln.strip() for ln in f
                        if ln.strip() and not ln.lstrip().startswith('#')}
        except Exception as e:
            logger.debug("读取节点墓碑失败: %s", e)
            return set()

    def _guard_node_db(self) -> None:
        """启动前自愈节点库（详见上方注释）。任何异常都不得阻断启动。"""
        db_path = self._db_path
        if not os.path.exists(db_path):
            return
        master = self._master_db_path()
        try:
            if not os.path.exists(master):
                # 首次自举：以当前节点库为基线
                shutil.copy2(db_path, master)
                logger.warning("节点库基线已建立: %s —— 若当前节点不全，"
                               "请用完整节点库覆盖此文件后再重启", master)
                return

            cur = self._read_node_sigs(db_path)
            base = self._read_node_sigs(master)
            if cur is None or base is None:
                return

            excl = self._load_exclude(master)
            name = lambda s: s.split('|', 1)[0]          # noqa: E731
            only_master = [s for s in sorted(base - cur) if name(s) not in excl]
            only_cur = [s for s in sorted(cur - base) if name(s) not in excl]
            if not only_master and not only_cur:
                return

            if only_master and not only_cur:
                # 节点库被砍 ⇒ 恢复（备份带时间戳，绝不复用固定名 .bak-old）
                bak = db_path + '.bak-' + time.strftime('%Y%m%d_%H%M%S')
                shutil.copy2(db_path, bak)
                shutil.copy2(master, db_path)
                logger.warning(
                    "⚠️ 节点库缺失 %d 个节点（疑似被旧备份覆盖），已从基线自动恢复；"
                    "旧库存档 %s；补回: %s",
                    len(only_master), os.path.basename(bak),
                    ', '.join(name(s) for s in only_master))
                return

            # 有新增 ⇒ 把 guiNDB 提升为新基线（用户加的节点要能留下来）
            shutil.copy2(db_path, master)
            if only_master:
                logger.warning(
                    "节点库已更新基线（新增 %d 个: %s）；但基线中的 %s 未出现在"
                    "当前节点库，已按当前库为准。若属误删请重启一次即可补回；"
                    "若确要永久移除，把节点名写进 %s",
                    len(only_cur), ', '.join(name(s) for s in only_cur),
                    ', '.join(name(s) for s in only_master),
                    self._MASTER_EXCLUDE_NAME)
            else:
                logger.info("节点库新增 %d 个节点，已更新基线: %s",
                            len(only_cur), ', '.join(name(s) for s in only_cur))
        except Exception as e:
            logger.warning("节点库自愈检查失败（不影响启动）: %s", e)

    def _refresh_node_list(self):
        """从 SQLite 数据库读取节点列表"""
        if not os.path.exists(self._db_path):
            logger.warning(f"数据库不存在: {self._db_path}")
            return
        
        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT IndexId, Remarks, Address, Port FROM ProfileItem ORDER BY rowid")
            rows = cursor.fetchall()
            conn.close()
            
            self._node_ids = [row[0] for row in rows]
            self._total_nodes = len(rows)
            
            logger.info(f"Xray 节点列表 ({self._total_nodes} 个):")
            for i, row in enumerate(rows):
                logger.info(f"  [{i+1}] {row[1]} ({row[2]}:{row[3]})")
            
            # 获取当前选中的节点索引
            if self._gui_config_path and os.path.exists(self._gui_config_path):
                with open(self._gui_config_path, 'r', encoding='utf-8') as f:
                    gui_config = json.load(f)
                current_id = gui_config.get('IndexId', '')
                if current_id in self._node_ids:
                    self._current_node_index = self._node_ids.index(current_id)
                    logger.info(f"当前选中节点: [{self._current_node_index + 1}/{self._total_nodes}]")
        except Exception as e:
            logger.error(f"读取数据库失败: {e}")
    
    def _config_is_valid(self, path: str) -> bool:
        """用 ``xray -test`` 校验配置文件能否被加载。

        ⚠️ 这一步不是可选优化：实测把 reality 的 ``mldsa65Verify``
        截断后，Xray 会以退出码 23 报 ``Failed to build outbound
        config``。若这种配置被写进 config.json，Xray 下次启动直接
        失败 —— 等于全站断网。所有新配置落盘前都必须过这一关。
        """
        if not os.path.exists(self._xray_exe):
            logger.warning('找不到 xray 可执行文件，跳过配置校验')
            return True
        try:
            proc = subprocess.run(
                [self._xray_exe, 'run', '-test', '-c', path],
                cwd=os.path.dirname(self._xray_exe),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            )
            if proc.returncode == 0:
                return True
            out = (proc.stdout or b'').decode('utf-8', 'replace').strip()
            logger.error('配置校验失败 (rc=%s): %s' % (proc.returncode, out[:400]))
            return False
        except Exception as e:
            logger.warning('配置校验异常，按通过处理: %s' % e)
            return True

    def _write_config_checked(self, new_config) -> bool:
        """写临时文件 -> xray -test 校验 -> 通过才原子替换正式配置。

        这样 config.json 永远不会被「Xray 加载不了」的配置污染：
        校验失败时原文件毫发无损，调用方可以安全回退。
        """
        # ⚠️ 临时文件必须以 .json 结尾：Xray 是按扩展名判文件格式的，
        # 实测 config.json.new 会报 ＂Failed to get format of ...＂。
        _base, _ext = os.path.splitext(self._config_path)
        tmp = _base + '.new' + (_ext or '.json')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(new_config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error('写临时配置失败: %s' % e)
            return False

        if self._config_is_valid(tmp):
            try:
                os.replace(tmp, self._config_path)
                return True
            except Exception as e:
                logger.error('替换配置文件失败: %s' % e)
                return False

        logger.error('新配置未通过 xray -test，已保留原配置（避免 Xray 起不来）')
        try:
            os.remove(tmp)
        except Exception:
            logger.debug('删除临时配置失败', exc_info=True)
        return False

    def _same_as_current_config(self, new_config) -> bool:
        """新配置与现有 config.json 是否等价。

        负载均衡模式下「节点集合」通常不变，重新生成的结果与现有文件完全
        一致。此时重启 Xray 只会掐断所有在途下载、收益为零 —— 用它短路。
        """
        try:
            if not os.path.exists(self._config_path):
                return False
            with open(self._config_path, 'r', encoding='utf-8') as f:
                cur = json.load(f)
            return (json.dumps(cur, sort_keys=True, ensure_ascii=False)
                    == json.dumps(new_config, sort_keys=True, ensure_ascii=False))
        except Exception:
            logger.debug("配置比较失败", exc_info=True)
            return False

    def _regenerate_config_for_current_node(self):
        """根据当前选中节点从数据库重新生成 config.json（启动前调用）"""
        if not self._node_ids:
            logger.warning("无可用节点，跳过配置生成")
            return
        
        try:
            current_id = self._node_ids[self._current_node_index]
            node_info = self._read_node_info(current_id)
            if not node_info:
                logger.error(f"无法读取当前节点信息: {current_id}")
                return
            
            all_nodes = self.get_all_nodes_info() if self._load_balance else None
            new_config = self._generate_xray_config(node_info, nodes_for_balance=all_nodes)
            if not new_config:
                logger.error("生成 Xray 配置失败")
                return

            # 校验通过才替换正式配置；失败则保留原配置（避免 Xray 起不来）
            if not self._write_config_checked(new_config):
                logger.error('启动前配置写入失败，沿用现有 config.json')
                return
            mode = ('负载均衡 %d 节点' % len(all_nodes)) if all_nodes else '单出口'
            logger.info(f"启动前已重新生成 config.json [{mode}] (节点: {node_info.get('Remarks', '?')} "
                       f"{node_info.get('Address', '?')}:{node_info.get('Port', '?')})")
        except Exception as e:
            logger.error(f"启动前生成配置失败: {e}")

    def start(self):
        """启动 Xray 核心（无窗口、后台运行）"""
        if self._process and self._process.poll() is None:
            logger.debug("Xray 已在运行")
            return
        
        if not os.path.exists(self._xray_exe):
            logger.error(f"Xray 核心不存在: {self._xray_exe}")
            return
        
        if not os.path.exists(self._config_path):
            logger.error(f"Xray 配置不存在: {self._config_path}")
            return
        
        try:
            logger.info(f"启动 Xray 核心: {self._xray_exe}")
            
            # 确保数据文件在 xray 目录下
            xray_dir = os.path.dirname(self._xray_exe)
            bin_dir = os.path.dirname(xray_dir)
            for data_file in ['geosite.dat', 'geoip.dat', 'Country.mmdb']:
                src = os.path.join(bin_dir, data_file)
                dst = os.path.join(xray_dir, data_file)
                if os.path.exists(src) and not os.path.exists(dst):
                    try:
                        import shutil
                        shutil.copy2(src, dst)
                        logger.info(f"已复制 {data_file} 到 xray 目录")
                    except Exception as e:
                        logger.warning(f"复制 {data_file} 失败: {e}")
            
            self._process = subprocess.Popen(
                [self._xray_exe, '-c', self._config_path],
                cwd=xray_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            )
            
            self._running = True
            
            # 等待代理就绪
            if not self._wait_for_proxy_ready(timeout=15):
                if self._process.poll() is not None:
                    logger.error(f"Xray 进程已退出 (code={self._process.returncode})")
                    self._running = False
                    return
            
            pid = self._process.pid
            logger.info(f"Xray 核心已启动 (PID: {pid}, SOCKS5: 127.0.0.1:{self.SOCKS_PORT})")
            
            # 记录启动时间作为定时切换的基准（避免 _last_switch_time=0 导致立即触发）
            self._last_switch_time = time.time()
            
            # 启动后台切换线程
            self._switch_thread = threading.Thread(target=self._background_switch, daemon=True)
            self._switch_thread.start()
            
        except Exception as e:
            logger.error(f"启动 Xray 失败: {e}")
    
    def stop(self):
        """停止 Xray 核心"""
        self._running = False
        
        if self._process and self._process.poll() is None:
            pid = self._process.pid
            logger.info(f"关闭 Xray (PID: {pid})...")
            
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
                logger.info("Xray 已关闭")
            except Exception:
                try:
                    self._process.kill()
                    self._process.wait(timeout=3)
                    logger.info("Xray 已强制关闭")
                except Exception as e:
                    logger.warning(f"关闭 Xray 失败: {e}")
            
            self._process = None
    
    def _background_switch(self):
        """后台线程：定时切换节点"""
        time.sleep(15)
        
        while self._running:
            try:
                time.sleep(60)
                
                if self._total_nodes < 2:
                    continue

                # ⭐ 2026-09-28：负载均衡模式下所有节点都在同时承载流量，
                # 「定时切换」的收益为零，代价却是重启 Xray、掐断全部在途
                # 下载（历史上一天切 12~15 次，最高纪录 1156 次）。因此
                # 该模式下只保留健康巡检的必要性，不再做主动切换。
                if self._load_balance:
                    continue

                current_time = time.time()
                elapsed = current_time - self._last_switch_time
                
                if elapsed >= self._node_switch_interval:
                    logger.info(f"定时切换节点（距上次 {elapsed//60:.0f} 分钟）")
                    self.switch_to_next_node()
                    self._last_switch_time = current_time
                    
            except Exception as e:
                logger.debug(f"后台切换异常: {e}")
    
    def switch_to_next_node(self, kill_downloads: bool = False) -> bool:
        """切换到下一个节点

        Args:
            kill_downloads: 是否终止下载进程。默认 False —— 切换节点不应影响
                其他正在进行的下载任务（详见 _kill_ytdlp_processes 说明）。
        """
        if self._total_nodes < 2:
            logger.warning(f"节点数不足 ({self._total_nodes})")
            return False
        
        next_index = (self._current_node_index + 1) % self._total_nodes
        return self.switch_to_node(next_index, kill_downloads=kill_downloads)
    
    def switch_to_node(self, index: int, kill_downloads: bool = False) -> bool:
        """切换到指定节点（从0开始）"""
        if index < 0 or index >= self._total_nodes:
            logger.warning(f"无效索引: {index}")
            return False
        
        with self._lock:
            success = self._do_switch_node(index, kill_downloads=kill_downloads)
            if success:
                self._current_node_index = index
                self._switch_count += 1
                logger.info(f"✅ 已切换到节点 {index + 1}/{self._total_nodes}（第{self._switch_count}次切换）")
            return success
    
    def _do_switch_node(self, index: int, kill_downloads: bool = False) -> bool:
        """执行节点切换：从数据库读取节点信息，重新生成 config.json，再重启 Xray"""
        if index >= len(self._node_ids):
            return False
        
        try:
            # 0. 【2026-09-20 修复】不再无条件杀掉所有 yt-dlp 进程。
            #    原实现调用 self._kill_ytdlp_processes()（taskkill /F /IM yt-dlp.exe）
            #    会把并发中的下载任务一并杀死；被误杀的任务无输出、退出码为 1，
            #    又被上层判定为失败并再次触发切换，形成正反馈雪崩。
            #    切换节点后，正在下载的任务本次失败后自行重试即可。
            if kill_downloads:
                self._kill_ytdlp_processes()
            
            target_id = self._node_ids[index]
            
            # 1. 从数据库读取目标节点的完整信息
            node_info = self._read_node_info(target_id)
            if not node_info:
                logger.error(f"无法读取节点信息: {target_id}")
                return False
            
            logger.info(f"IndexId: {self._node_ids[self._current_node_index][:8] if self._current_node_index < len(self._node_ids) else '???'}... -> {target_id[:8]}... "
                       f"({node_info.get('Remarks', '?')} {node_info.get('Address', '?')}:{node_info.get('Port', '?')})")
            
            # 2. 根据节点信息重新生成 config.json（多出口模式下含全部节点）
            all_nodes = self.get_all_nodes_info() if self._load_balance else None
            new_config = self._generate_xray_config(node_info, nodes_for_balance=all_nodes)
            if not new_config:
                logger.error("生成 Xray 配置失败")
                return False

            # ⭐ 2026-09-28：负载均衡模式下所有节点本就在配置里，节点集合
            # 通常没有变化。若生成结果与现有配置一致，重启 Xray 只会掐断
            # 全部在途下载 —— 纯损失，直接短路返回。
            #
            # 🔴 2026-10-09 修复：短路的前提是 **Xray 仍然活着并在监听 SOCKS 端口**。
            # 原实现只比配置内容、不看端口，于是当 Xray 变成「进程还在但端口全关」
            # 的僵尸态时，每次切节点都短路返回 True => 永不重启 Xray =>
            # 每个请求都 WinError 10061（端口被拒），切 1252 次也是空转，
            # 表现为「扫描卡死在第 366 页」。
            if self._load_balance and self._same_as_current_config(new_config) and self._is_proxy_alive():
                logger.info("负载均衡模式：节点集合未变化且代理正常，跳过重启 Xray（避免掐断在途下载）")
                try:
                    if self._gui_config_path and os.path.exists(self._gui_config_path):
                        with open(self._gui_config_path, 'r', encoding='utf-8') as f:
                            gui_config = json.load(f)
                        gui_config['IndexId'] = target_id
                        with open(self._gui_config_path, 'w', encoding='utf-8') as f:
                            json.dump(gui_config, f, indent=2)
                except Exception as e:
                    logger.debug(f"同步 guiNConfig 失败: {e}")
                return True

            if self._load_balance and self._same_as_current_config(new_config) and not self._is_proxy_alive():
                logger.warning("代理端口不可用但配置未变化 —— 强制重启 Xray 以恢复监听")

            if not self._write_config_checked(new_config):
                logger.error('配置写入失败，保留原 config.json')
                return False
            logger.info(f"已重新生成 config.json (节点: {node_info.get('Remarks', '?')})")
            
            # 3. 同步更新 guiNConfig.json 的 IndexId（保持一致性）
            if self._gui_config_path and os.path.exists(self._gui_config_path):
                with open(self._gui_config_path, 'r', encoding='utf-8') as f:
                    gui_config = json.load(f)
                gui_config['IndexId'] = target_id
                with open(self._gui_config_path, 'w', encoding='utf-8') as f:
                    json.dump(gui_config, f, indent=2)
            
            # 4. 重启 Xray 核心（让新配置生效）
            self.stop()
            time.sleep(1)
            self.start()
            
            # 5. 等待 SOCKS5 就绪
            if not self._wait_for_proxy_ready():
                logger.error("切换节点后 SOCKS5 未就绪")
                return False
            
            # 6. 触发切换回调
            for callback in self._on_switch_callbacks:
                try:
                    callback()
                except Exception as e:
                    logger.debug(f"切换回调失败: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"切换节点失败: {e}")
            return False
    
    def _read_node_info(self, node_id: str) -> Optional[Dict]:
        """从数据库读取指定节点的完整信息"""
        if not os.path.exists(self._db_path):
            logger.error(f"数据库不存在: {self._db_path}")
            return None
        
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ProfileItem WHERE IndexId=?", (node_id,))
            row = cursor.fetchone()
            conn.close()
            
            if not row:
                logger.warning(f"未找到节点: {node_id}")
                return None
            
            return dict(row)
        except Exception as e:
            logger.error(f"读取节点信息失败: {e}")
            return None
    
    def _generate_xray_config(self, node: Dict,
                              nodes_for_balance=None) -> Optional[Dict]:
        """根据节点信息生成完整的 Xray 配置

        Args:
            node: 目标节点（单出口模式下即当前节点）
            nodes_for_balance: 参与负载均衡的全部节点；数量 ≥2 且开启
                ``v2ray.load_balance`` 时生成多出口 + balancer 配置，
                否则退回原有的单出口逻辑。
        """
        try:
            # ---- 多出口负载均衡（2026-09-28）----
            # 放在最前面：它不依赖单出口那套 outbound 构造，成功即返回；
            # 失败则继续往下走单出口路径，保证「一定会产出一个能用的配置」。
            if self._load_balance and nodes_for_balance and len(nodes_for_balance) >= 2:
                balanced = self._generate_balanced_config(nodes_for_balance)
                if balanced:
                    return balanced
                logger.warning('负载均衡配置生成失败，回退单出口模式')

            # 走单出口了 ⇒ 上一轮留下的「专属入口」端口记录必须清掉，
            # 否则下载器会去连一个本次并没有监听的端口（连接被拒）。
            self._pinned_ports = []
            self._pinned_nodes = []

            config_type = node.get('ConfigType', 0)
            
            # 读取现有 config.json 作为基础（保留 inbound、routing、dns 等配置）
            base_config = {}
            if os.path.exists(self._config_path):
                with open(self._config_path, 'r', encoding='utf-8') as f:
                    base_config = json.load(f)
            
            # 根据 ConfigType 生成 outbound
            # ConfigType: 1=vmess, 2=custom, 3=shadowsocks, 4=socks, 5=vless, 6=vless2
            outbound = self._generate_outbound(node)
            if not outbound:
                return None
            
            # 更新 outbound（proxy 标签）
            outbounds = base_config.get('outbounds', [])
            found = False
            for i, ob in enumerate(outbounds):
                if ob.get('tag') == 'proxy':
                    outbounds[i] = outbound
                    found = True
                    break
            if not found:
                outbounds.insert(0, outbound)
            
            base_config['outbounds'] = outbounds
            
            # 单出口模式下不能留下专属入口：端口 10809+ 会被复用，routing
            # 里指向 proxy-i 的规则也会指向一个本次并不存在的 tag。从负载
            # 均衡切回单出口、或节点数掉到 1 时最容易残留。
            self._strip_pinned_inbounds(base_config)
            self._strip_pinned_rules(base_config)

            # 确保 inbound 存在
            if 'inbounds' not in base_config or not base_config['inbounds']:
                base_config['inbounds'] = [{
                    "tag": "socks",
                    "port": self.SOCKS_PORT,
                    "listen": "127.0.0.1",
                    "protocol": "socks",
                    "sniffing": {
                        "enabled": True,
                        "destOverride": ["http", "tls"],
                        "routeOnly": False
                    },
                    "settings": {
                        "auth": "noauth",
                        "udp": True,
                        "allowTransparent": False
                    }
                }]
            
            return base_config
            
        except Exception as e:
            logger.error(f"生成配置失败: {e}")
            return None
    
    def _build_balance_outbounds(self, nodes) -> List[Dict]:
        """把多个节点编译成带独立 tag 的 outbound 列表（负载均衡用）

        tag 统一用 ``proxy-<序号>`` 前缀，balancer 靠前缀 selector 选中它们。
        """
        outs: List[Dict] = []
        # outbound 里**不能**塞自定义键（Xray 会按自己的 schema 解析），
        # 节点名另存一份平行列表，供「专属入口」日志/诊断使用。
        self._balance_node_names = []
        for i, n in enumerate(nodes, 1):
            ob = self._generate_outbound(n)
            if not ob:
                logger.warning('节点 %s 生成 outbound 失败，已从负载均衡池剔除'
                               % n.get('Remarks', '?'))
                continue
            ob['tag'] = 'proxy-%d' % i
            outs.append(ob)
            self._balance_node_names.append(str(n.get('Remarks') or 'proxy-%d' % i))
        return outs

    def _generate_balanced_config(self, nodes) -> Optional[Dict]:
        """生成「多出口 + balancer」的完整 Xray 配置（2026-09-28 新增）

        为什么
        ------
        单出口模式下一次只有一个节点在跑：其余节点闲置，切换还要重启
        Xray 掐断在途下载；而站点侧看到的**同一个出口 IP** 的请求密度
        也高得多，更容易被按 IP 限流。改成多出口 + roundRobin 之后：

        * 所有节点**同时**在承载流量，不存在闲置节点；
        * 单个出口 IP 的请求密度降到 1/N，更不容易被站点识别；
        * 任一节点故障只影响 1/N 的请求，而不是全站瘫痪。

        约束
        -----
        必须保留 base 配置里原有的 routing 规则：服务端配置中有一条
        ``outboundTag: proxy`` 的规则，多出口下要把这类规则改写成
        ``balancerTag``，否则它会指向一个已经不存在的 tag。
        """
        try:
            base_config = {}
            if os.path.exists(self._config_path):
                with open(self._config_path, 'r', encoding='utf-8') as f:
                    base_config = json.load(f)

            outs = self._build_balance_outbounds(nodes)
            if len(outs) < 2:
                logger.warning('可用节点不足 2 个（%d），不做负载均衡' % len(outs))
                return None

            # 保留原有的非 proxy outbound（direct / block / api ...）
            keep = []
            for ob in base_config.get('outbounds', []):
                tag = str(ob.get('tag', ''))
                if tag == 'proxy' or tag.startswith('proxy-'):
                    continue
                keep.append(ob)
            if not any(str(o.get('tag')) == 'direct' for o in keep):
                keep.append({'tag': 'direct', 'protocol': 'freedom'})
            if not any(str(o.get('tag')) == 'block' for o in keep):
                keep.append({'tag': 'block', 'protocol': 'blackhole'})
            base_config['outbounds'] = outs + keep

            # routing：把指向 proxy 的规则改指向 balancer
            routing = base_config.get('routing') or {}
            rules = list(routing.get('rules') or [])
            for r in rules:
                if r.get('outboundTag') == 'proxy':
                    r.pop('outboundTag', None)
                    r['balancerTag'] = self._LB_TAG
            # 兜底规则：没有它的话，未命中任何规则的流量会走「第一个
            # outbound」= proxy-1，等于绕过了负载均衡。
            # ⚠️ 判据**不能**是「rules 里有没有 balancerTag」—— 上面被
            #    改写的那条（domain:googleapis.cn/gstatic.com）也带
            #    balancerTag，会让这里误判成「已有兜底」而跳过。
            #    2026-09-28 线上就是这样漏掉的：config.json 里 balancers
            #    明明在，实测 4 个请求（含 cn.pornhub.com）全部 `>> proxy-1`。
            #    只有「纯兜底」——除 type/network/balancerTag 外没有任何
            #    限定条件——才算数。
            _CATCHALL_ONLY_KEYS = {'type', 'network', 'balancerTag'}
            _has_catchall = any(
                r.get('balancerTag') == self._LB_TAG
                and set(r.keys()) <= _CATCHALL_ONLY_KEYS
                for r in rules)
            if not _has_catchall:
                rules.append({'type': 'field', 'network': 'tcp,udp',
                              'balancerTag': self._LB_TAG})
            routing['rules'] = rules
            routing.setdefault('domainStrategy', 'AsIs')
            routing['balancers'] = [{
                'tag': self._LB_TAG,
                'selector': ['proxy-'],
                'strategy': {'type': 'roundRobin'},
            }]
            base_config['routing'] = routing

            if not base_config.get('inbounds'):
                base_config['inbounds'] = [{
                    'tag': 'socks',
                    'port': self.SOCKS_PORT,
                    'listen': '127.0.0.1',
                    'protocol': 'socks',
                    'sniffing': {'enabled': True,
                                 'destOverride': ['http', 'tls'],
                                 'routeOnly': False},
                    'settings': {'auth': 'noauth', 'udp': True,
                                 'allowTransparent': False},
                }]
            self._build_pinned_inbounds(base_config, outs)
            return base_config
        except Exception as e:
            logger.error('生成负载均衡配置失败: %s' % e)
            return None

    # ------------------------------------------------------------------
    # 节点专属入口（2026-09-28 第二批）
    # ------------------------------------------------------------------
    #: 专属入口的 tag 前缀；重生成配置时靠它清理上一轮遗留的入口，避免
    #: 「同一个端口被两个 inbound 抢」导致 Xray 起不来。
    _PIN_TAG_PREFIX = 'socks-node-'

    def _is_pinned_rule(self, rule) -> bool:
        """判断一条 routing 规则是不是「专属入口」规则。

        判据必须**收窄**，不能只看 inboundTag 里有没有 ``socks-node-*`` 就
        完事 —— 一旦放宽，base 配置里正常的 inboundTag 规则会被误删。这里
        要求同时满足三点：

        * ``type == 'field'``；
        * 除 ``type`` / ``inboundTag`` / ``outboundTag`` 外没有任何其它键
          （带 ``domain`` / ``port`` 的条件规则一律不算）；
        * ``inboundTag`` 是**且仅是**一个 ``socks-node-*``。
        """
        if not isinstance(rule, dict):
            return False
        if rule.get('type') != 'field':
            return False
        if set(rule.keys()) - {'type', 'inboundTag', 'outboundTag'}:
            return False
        inbound = rule.get('inboundTag')
        if not isinstance(inbound, list) or len(inbound) != 1:
            return False
        return str(inbound[0]).startswith(self._PIN_TAG_PREFIX)

    def _strip_pinned_inbounds(self, base_config: Dict) -> None:
        """移除上一轮遗留的专属入口 inbound。

        端口 10809+ 会被复用，留着必然冲突（同一个端口被两个 inbound 抢，
        Xray 直接起不来）。
        """
        inbounds = list(base_config.get('inbounds') or [])
        base_config['inbounds'] = [
            b for b in inbounds
            if not str(b.get('tag', '')).startswith(self._PIN_TAG_PREFIX)]

    def _strip_pinned_rules(self, base_config: Dict) -> None:
        """移除上一轮遗留的专属入口路由规则。

        为什么必须单独清：``base_config`` 是从**上一轮写出的 config.json**
        读回来的（见 _generate_balanced_config 开头），所以它本身就带着上一
        轮的产物。只清 inbound 不清 rule 的话，每生成一次配置就会在**最前
        面**再追加一组 ``socks-node-i → proxy-i``：

            2026-09-29 线上实测 —— 启动若干次后 config.json 里累积了 6 组
            （12 条）完全相同的规则。功能上不致命（第一条命中即返回），但
            每启动一次就膨胀一组，长期会污染配置。

        ⚠️ inbound 那半边是**幂等**的（见 _strip_pinned_inbounds），正是这
           个不对称掩盖了缺陷：inbounds 看着只剩 3 个，rules 却在悄悄翻倍。
        """
        routing = base_config.get('routing')
        if not isinstance(routing, dict):
            return
        rules = list(routing.get('rules') or [])
        kept = [r for r in rules if not self._is_pinned_rule(r)]
        if len(kept) != len(rules):
            logger.info('已清理上一轮遗留的专属入口路由规则 %d 条'
                        % (len(rules) - len(kept)))
        routing['rules'] = kept

    def _build_pinned_inbounds(self, base_config: Dict, outs: List[Dict]) -> None:
        """给每个出口加一个「固定指向它」的 SOCKS5 入口，并置顶路由规则。

        为什么需要
        ----------
        balancer 的 roundRobin 是**逐连接**选出口的。PornHub 的媒体直链形如
        ``https://ev.phcdn.com/.../1080P.mp4?ip=<出口IP>&hash=...`` —— 站点把
        授权的出口 IP 写进了 URL，并且校验**实际发起请求的 IP**。于是：

        * 页面 / m3u8 走节点 A，拿到 ``ip=A`` 的直链；
        * 紧接着取分片时那条连接轮到节点 B；
        * CDN 认为「这个 URL 不该由 B 来取」⇒ ``HTTP 474: Client Error``。

        实测（2026-09-28）：同一视频同一时刻，走 balancer → 立刻 474；只走
        任一单节点 → 稳定下载（最高 566~604 KiB/s）。线上 474 也正好在负载
        均衡生效后首现（17:36:42，此前全天 0 次）。

        做法
        ----
        在保留 ``socks``（10808，仍走 balancer，供抓取等无状态请求使用）的
        前提下，再开 N 个入口 ``socks-node-i``，端口从 ``SOCKS_PORT+1`` 起，
        每个入口用**置顶的 inboundTag 规则**固定指向 ``proxy-i``。下载器只要
        「一个任务挑一个入口」，就能保证整条下载链路（页面 + m3u8 + 分片）
        出口一致，同时三个节点仍然都在干活。

        ⚠️ 置顶顺序是硬要求：Xray 路由是**自上而下第一条命中即返回**，把
        inboundTag 规则放在 ``port 443/udp → block``、``geosite:cn → direct``
        之后的话，专属入口的流量会被前面的规则截走。
        """
        # 清掉上一轮遗留的专属入口 —— inbound 与 routing 规则**两侧都要清**：
        # 端口会被复用；规则只清 inbound 不清 rule 会在每次启动时成倍累积
        # （2026-09-29 线上实测累积到 6 组），详见 _strip_pinned_rules。
        inbounds = list(base_config.get('inbounds') or [])
        inbounds = [b for b in inbounds
                    if not str(b.get('tag', '')).startswith(self._PIN_TAG_PREFIX)]
        self._strip_pinned_rules(base_config)

        pinned_ports: List[int] = []
        pinned_nodes: List[str] = []
        pin_rules: List[Dict] = []
        for i, ob in enumerate(outs, 1):
            port = self.SOCKS_PORT + i
            tag = '%s%d' % (self._PIN_TAG_PREFIX, i)
            inbounds.append({
                'tag': tag,
                'port': port,
                'listen': '127.0.0.1',
                'protocol': 'socks',
                'sniffing': {'enabled': True,
                             'destOverride': ['http', 'tls'],
                             'routeOnly': False},
                'settings': {'auth': 'noauth', 'udp': True,
                             'allowTransparent': False},
            })
            pin_rules.append({'type': 'field', 'inboundTag': [tag],
                              'outboundTag': str(ob.get('tag'))})
            pinned_ports.append(port)
            names = getattr(self, '_balance_node_names', []) or []
            pinned_nodes.append(names[i - 1] if i - 1 < len(names) else str(ob.get('tag')))

        base_config['inbounds'] = inbounds
        routing = base_config.setdefault('routing', {})
        rules = list(routing.get('rules') or [])
        routing['rules'] = pin_rules + rules
        self._pinned_ports = pinned_ports
        self._pinned_nodes = pinned_nodes
        logger.info('节点专属入口已生成: %s'
                    % ', '.join('%d→%s' % (p, n)
                                for p, n in zip(pinned_ports, pinned_nodes)))

    def _generate_outbound(self, node: Dict) -> Optional[Dict]:
        """根据节点信息生成 Xray outbound 配置"""
        config_type = node.get('ConfigType', 0)
        address = node.get('Address', '')
        port = node.get('Port', 0)
        user_id = node.get('Id', '')
        network = node.get('Network', 'tcp')
        stream_security = node.get('StreamSecurity', '')
        sni = node.get('Sni', '')
        fingerprint = node.get('Fingerprint', '')
        flow = node.get('Flow', '')
        
        if config_type == 5:
            # VLESS
            outbound = {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {
                    "vnext": [{
                        "address": address,
                        "port": port,
                        "users": [{
                            "id": user_id,
                            "email": "t@t.tt",
                            "security": "auto",
                            "encryption": "none",
                        }]
                    }]
                },
                "streamSettings": {
                    "network": network,
                },
                "mux": {
                    "enabled": False,
                    "concurrency": -1
                }
            }
            
            # flow (xtls-rprx-vision 等)
            if flow:
                outbound["settings"]["vnext"][0]["users"][0]["flow"] = flow
            
            # 传输层安全
            if stream_security == 'reality':
                outbound["streamSettings"]["security"] = "reality"
                outbound["streamSettings"]["realitySettings"] = {
                    "serverName": sni,
                    "fingerprint": fingerprint or "chrome",
                    "show": False,
                    "shortId": node.get('ShortId', ''),
                    "spiderX": node.get('SpiderX', '/'),
                }
                # Reality 的 publicKey
                public_key = node.get('PublicKey', '')
                if public_key:
                    outbound["streamSettings"]["realitySettings"]["publicKey"] = public_key
                # ⭐ 2026-09-28 修复：REALITY 的后量子签名参数（ML-DSA-65）。
                # 该字段一直存在节点库里，却在生成配置时被静默丢弃 ——
                # 服务端启用 PQ 时客户端不带此参数会导致握手参数不匹配。
                # ⚠️ 值必须**完整**传入：实测截断会让 Xray 拒绝加载配置
                #（退出码 23），因此落盘前一律过 _config_is_valid 自检。
                mldsa65 = (node.get('Mldsa65Verify') or '').strip()
                if mldsa65:
                    outbound["streamSettings"]["realitySettings"]["mldsa65Verify"] = mldsa65
            elif stream_security == 'tls':
                outbound["streamSettings"]["security"] = "tls"
                tls_settings = {}
                if sni:
                    tls_settings["serverName"] = sni
                if fingerprint:
                    tls_settings["fingerprint"] = fingerprint
                allow_insecure = node.get('AllowInsecure', 'false')
                if allow_insecure == 'true':
                    tls_settings["allowInsecure"] = True
                alpn = node.get('Alpn', '')
                if alpn:
                    tls_settings["alpn"] = [a.strip() for a in alpn.split(',')]
                if node.get('Cert'):
                    tls_settings["certificates"] = [{"certificate": [node['Cert']]}]
                outbound["streamSettings"]["tlsSettings"] = tls_settings
            elif stream_security:
                outbound["streamSettings"]["security"] = stream_security
            
            # network 类型处理
            if network == 'ws':
                ws_settings = {}
                path = node.get('Path', '')
                host = node.get('RequestHost', '')
                if path:
                    ws_settings["path"] = path
                if host:
                    ws_settings["headers"] = {"Host": host}
                outbound["streamSettings"]["wsSettings"] = ws_settings
            elif network == 'grpc':
                grpc_settings = {}
                service_name = node.get('RequestHost', '') or node.get('Path', '')
                if service_name:
                    grpc_settings["serviceName"] = service_name
                outbound["streamSettings"]["grpcSettings"] = grpc_settings
            elif network == 'h2':
                h2_settings = {}
                path = node.get('Path', '')
                host = node.get('RequestHost', '')
                if path:
                    h2_settings["path"] = path
                if host:
                    h2_settings["host"] = [h.strip() for h in host.split(',')]
                outbound["streamSettings"]["httpSettings"] = h2_settings
            
            return outbound
            
        elif config_type == 1:
            # VMess
            outbound = {
                "tag": "proxy",
                "protocol": "vmess",
                "settings": {
                    "vnext": [{
                        "address": address,
                        "port": port,
                        "users": [{
                            "id": user_id,
                            "alterId": node.get('AlterId', 0),
                            "security": node.get('Security', 'auto'),
                        }]
                    }]
                },
                "streamSettings": {
                    "network": network,
                },
                "mux": {
                    "enabled": False,
                    "concurrency": -1
                }
            }
            
            if stream_security == 'tls':
                outbound["streamSettings"]["security"] = "tls"
                tls_settings = {}
                if sni:
                    tls_settings["serverName"] = sni
                if fingerprint:
                    tls_settings["fingerprint"] = fingerprint
                outbound["streamSettings"]["tlsSettings"] = tls_settings
            
            if network == 'ws':
                ws_settings = {}
                path = node.get('Path', '')
                host = node.get('RequestHost', '')
                if path:
                    ws_settings["path"] = path
                if host:
                    ws_settings["headers"] = {"Host": host}
                outbound["streamSettings"]["wsSettings"] = ws_settings
            elif network == 'grpc':
                grpc_settings = {}
                service_name = node.get('RequestHost', '') or node.get('Path', '')
                if service_name:
                    grpc_settings["serviceName"] = service_name
                outbound["streamSettings"]["grpcSettings"] = grpc_settings
            
            return outbound
            
        elif config_type == 3:
            # Shadowsocks
            outbound = {
                "tag": "proxy",
                "protocol": "shadowsocks",
                "settings": {
                    "servers": [{
                        "address": address,
                        "port": port,
                        "password": user_id,
                        "method": node.get('Security', 'aes-256-gcm'),
                    }]
                },
                "streamSettings": {
                    "network": network,
                },
                "mux": {
                    "enabled": False,
                    "concurrency": -1
                }
            }
            return outbound
        
        else:
            logger.warning(f"不支持的 ConfigType: {config_type}，尝试按 VLESS 处理")
            if address and port and user_id:
                # 兜底：按 VLESS 处理
                node_copy = dict(node)
                node_copy['ConfigType'] = 5
                return self._generate_outbound(node_copy)
            return None
    
    def _kill_ytdlp_processes(self, pids: Optional[List[int]] = None):
        """定点终止指定的 yt-dlp 进程（默认不执行任何操作）

        Args:
            pids: 需要终止的进程 PID 列表；为 None 或空列表时直接返回。

        安全说明（2026-09-20 事故修复）:
            原实现使用 `taskkill /F /IM yt-dlp.exe` 按镜像名通杀全机器上所有
            yt-dlp 进程，会误杀并发中的其他下载任务。被误杀的进程没有 stderr
            输出、退出码为 1，上层会把它们再次判定为「下载失败」并触发下一轮
            节点切换，与「失败即切换」叠加形成正反馈雪崩。
            事故数据：全天 1144 次失败中有 1046 次为被误杀（错误输出为空）。
            因此改为必须显式传入 PID 才终止，杜绝误伤。
        """
        if not pids:
            return
        for pid in pids:
            try:
                result = subprocess.run(
                    ['taskkill', '/F', '/PID', str(pid)],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    logger.info(f"已终止下载进程 PID: {pid}")
            except Exception as e:
                logger.debug(f"终止进程 {pid} 失败: {e}")
    
    def _is_proxy_alive(self) -> bool:
        """检查 SOCKS5 端口是否真的在监听（1 次 TCP 连接，超时 2s）

        2026-10-09：用于识别「进程在、端口全关」的僵尸 Xray。
        这种情况下所有出站请求都会 WinError 10061，必须重启核心才能恢复。
        """
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            s.connect(('127.0.0.1', self.SOCKS_PORT))
            s.close()
            return True
        except Exception:
            return False

    def _wait_for_proxy_ready(self, timeout: int = 30):
        """等待 SOCKS5 代理就绪"""
        import socket
        start = time.time()
        while time.time() - start < timeout:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2)
                s.connect(('127.0.0.1', self.SOCKS_PORT))
                s.close()
                logger.info(f"SOCKS5 已就绪 (127.0.0.1:{self.SOCKS_PORT})")
                return True
            except Exception:
                time.sleep(1)
        logger.warning(f"SOCKS5 就绪超时 ({timeout}秒)")
        return False
    
    def get_all_nodes_info(self) -> List[Dict]:
        """获取所有节点的完整信息（用于测速等）"""
        if not os.path.exists(self._db_path):
            return []
        
        try:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ProfileItem ORDER BY rowid")
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"获取所有节点信息失败: {e}")
            return []

    def test_node_speed(self, node_info: Dict, timeout: int = 10) -> Dict:
        """
        测试单个节点速度（使用临时端口启动独立 Xray 进程）。
        
        返回:
            {
                'success': bool,
                'delay_ms': int,        # 延迟(毫秒)，-1 表示失败
                'speed_kbps': float,    # 下载速度(KB/s)，-1 表示失败
                'error': str,           # 错误信息
            }
        """
        import urllib.request
        
        result = {'success': False, 'delay_ms': -1, 'speed_kbps': -1, 'error': ''}
        
        # 生成临时配置
        xray_config = self._generate_xray_config(node_info)
        if not xray_config:
            result['error'] = '生成配置失败'
            return result
        
        # 使用临时端口（避免与主进程冲突）
        test_port = 20000 + (hash(node_info.get('IndexId', '')) % 10000)
        for inbound in xray_config.get('inbounds', []):
            inbound['port'] = test_port
            inbound['listen'] = '127.0.0.1'
        
        # 写临时配置文件
        config_dir = os.path.dirname(self._config_path)
        tmp_config = os.path.join(config_dir, f'_test_{test_port}.json')
        
        try:
            with open(tmp_config, 'w', encoding='utf-8') as f:
                json.dump(xray_config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            result['error'] = f'写临时配置失败: {e}'
            return result
        
        # 启动临时 Xray 进程
        xray_dir = os.path.dirname(self._xray_exe)
        proc = None
        try:
            proc = subprocess.Popen(
                [self._xray_exe, '-c', tmp_config],
                cwd=xray_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            )
            
            # 等待临时端口就绪
            import socket
            ready = False
            for _ in range(int(timeout * 2)):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1)
                    s.connect(('127.0.0.1', test_port))
                    s.close()
                    ready = True
                    break
                except Exception:
                    time.sleep(0.5)
            
            if not ready:
                result['error'] = 'Xray 启动超时'
                return result
            
            # 测延迟
            proxy_handler = urllib.request.ProxyHandler({
                'http': f'socks5h://127.0.0.1:{test_port}',
                'https': f'socks5h://127.0.0.1:{test_port}',
            })
            opener = urllib.request.build_opener(proxy_handler)
            
            test_url = 'https://www.google.com/generate_204'
            
            start = time.time()
            try:
                resp = opener.open(test_url, timeout=timeout)
                resp.read()
                delay_ms = int((time.time() - start) * 1000)
                result['delay_ms'] = delay_ms
            except Exception as e:
                # google 不通，尝试用 baidu
                test_url = 'https://www.baidu.com'
                start = time.time()
                try:
                    resp = opener.open(test_url, timeout=timeout)
                    resp.read()
                    delay_ms = int((time.time() - start) * 1000)
                    result['delay_ms'] = delay_ms
                except Exception as e2:
                    result['error'] = f'连接失败: {e2}'
                    return result
            
            # 测下载速度（下载 1MB 数据）
            speed_url = 'https://speed.cloudflare.com/__down?bytes=1000000'
            start = time.time()
            try:
                resp = opener.open(speed_url, timeout=timeout)
                data = resp.read()
                elapsed = time.time() - start
                if elapsed > 0 and len(data) > 0:
                    result['speed_kbps'] = int(len(data) / 1024 / elapsed)
            except Exception:
                result['speed_kbps'] = 0
            
            result['success'] = True
            return result
            
        except Exception as e:
            result['error'] = f'测速异常: {e}'
            return result
        finally:
            # 清理临时进程
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        logger.debug("test_node_speed 忽略异常", exc_info=True)
            # 删除临时配置
            try:
                os.remove(tmp_config)
            except Exception:
                logger.debug("test_node_speed 忽略异常", exc_info=True)

    def on_switch(self, callback):
        """注册切换节点后的回调"""
        self._on_switch_callbacks.append(callback)
    
    def get_status(self) -> Dict:
        """获取状态"""
        return {
            'enabled': self._enabled,
            'running': self._running and self._process and self._process.poll() is None,
            'current_node': self._current_node_index + 1 if self._total_nodes > 0 else 0,
            'total_nodes': self._total_nodes,
            'switch_interval_hours': self._node_switch_interval // 3600,
            'switch_count': self._switch_count,
            'socks_proxy': f'socks5://127.0.0.1:{self.SOCKS_PORT}',
            'mode': 'v2rayN（无窗口后台运行）',
            'load_balance': bool(self._load_balance) and self._total_nodes >= 2,
            # 节点专属入口：下载器按任务挑一个，保证单次下载出口一致。
            # 空列表 = 当前是单出口模式，下载器应退回 socks_proxy。
            'pinned_proxies': [
                {'port': p, 'node': n, 'url': 'socks5://127.0.0.1:%d' % p}
                for p, n in zip(self._pinned_ports, self._pinned_nodes)
            ],
        }

    def add_node(self, remarks: str, address: str, port: int, config_type: int = 5,
                 id: str = "", network: str = "tcp", path: str = "",
                 stream_security: str = "", request_host: str = "",
                 subid: str = "", sni: str = "", fingerprint: str = "",
                 public_key: str = "", short_id: str = "", spider_x: str = "/",
                 flow: str = "", alpn: str = "", mldsa65_verify: str = "",
                 security: str = "auto") -> bool:
        """添加节点到数据库

        Args:
            remarks: 节点名称
            address: 服务器地址
            port: 端口
            config_type: 配置类型 (1=vmess, 5=vless)
            id: UUID
            network: 网络类型 (tcp/ws/grpc)
            path: 路径
            stream_security: 传输安全 (tls/none/reality)
            request_host: 请求主机 (Host)
            subid: 订阅 ID
            sni: REALITY/TLS 的 serverName
            fingerprint: REALITY/TLS 指纹 (chrome/firefox/...)
            public_key: REALITY 公钥
            short_id: REALITY shortId
            spider_x: REALITY spiderX
            flow: VLESS flow（如 xtls-rprx-vision）
            alpn: TLS ALPN 列表（逗号分隔）
            mldsa65_verify: REALITY 后量子签名公钥（ML-DSA-65）
            security: VMess 加密方式

        修复记录（2026-09-28）:
            原签名里**没有** sni/public_key/short_id/fingerprint 这些
            REALITY 必需字段，INSERT 时全部写空串。而 ``_generate_outbound``
            是 ``if public_key:`` 才写入 ``publicKey`` —— 于是用它加出来的
            reality 节点**必然**握手失败（实测统计残表里那个 Delay=-1 的
            节点就是这么来的）。现补齐全部字段，并保持旧调用方式可用。
        """
        if not os.path.exists(self._db_path):
            logger.error(f"数据库不存在: {self._db_path}")
            return False

        try:
            import uuid as uuid_mod
            index_id = str(uuid_mod.uuid4().int)[:19]

            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ProfileItem (
                    IndexId, ConfigType, ConfigVersion, Address, Port, Id,
                    AlterId, Security, Network, Remarks, HeaderType,
                    RequestHost, Path, StreamSecurity, AllowInsecure,
                    Subid, IsSub, Flow, Sni, Alpn, CoreType,
                    PreSocksPort, Fingerprint, DisplayLog, PublicKey,
                    ShortId, SpiderX, Extra, Ports, Mldsa65Verify,
                    MuxEnabled, Cert, EchConfigList, EchForceQuery
                ) VALUES (
                    ?, ?, 5, ?, ?, ?,
                    0, ?, ?, ?, 'none',
                    ?, ?, ?, 'none',
                    ?, 0, ?, ?, ?, 0,
                    0, ?, 0, ?,
                    ?, ?, '', '', ?,
                    0, '', '', ''
                )
            """, (
                index_id, config_type, address, port, id,
                security, network, remarks, request_host, path, stream_security,
                subid, flow, sni, alpn,
                fingerprint, public_key, short_id, spider_x, mldsa65_verify
            ))
            if cursor.rowcount == 0:
                logger.warning("插入操作未影响任何行")
            conn.commit()
            conn.close()

            logger.info(f"节点已添加: {remarks} ({address}:{port})")
            return True
        except Exception as e:
            logger.error(f"添加节点失败: {e}")
            return False

    def delete_node(self, index_id: str) -> bool:
        """从数据库删除节点

        Args:
            index_id: 节点 IndexId
        """
        if not os.path.exists(self._db_path):
            logger.error(f"数据库不存在: {self._db_path}")
            return False

        try:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM ProfileItem WHERE IndexId=?", (index_id,))
            affected = cursor.rowcount
            conn.commit()
            conn.close()

            if affected > 0:
                logger.info(f"节点已删除: {index_id}")
                return True
            else:
                logger.warning(f"未找到要删除的节点: {index_id}")
                return False
        except Exception as e:
            logger.error(f"删除节点失败: {e}")
            return False

    def import_subscription(self, url: str, timeout: int = 20) -> Dict:
        """导入订阅链接：经代理拉取 -> base64 解码 -> 逐条解析 -> 批量加节点（去重）。

        只支持 ``vless://`` / ``vmess://`` 链接（与 :func:`parse_share_link` 一致）；
        ``trojan://`` / ``ss://`` 等会统计进 ``skipped``。按 (地址, 端口) 去重，
        不覆盖已有节点。返回 ``{'ok','added','skipped','errors','total','error'}``。
        """
        import base64

        result: Dict = {'ok': False, 'added': 0, 'skipped': 0,
                        'errors': 0, 'total': 0, 'error': ''}
        try:
            import requests
        except Exception as e:
            result['error'] = f'缺少 requests 依赖: {e}'
            return result

        # 订阅主机多在墙外，走本地 SOCKS5 代理更稳
        proxies = {}
        if self._enabled:
            p = f'socks5h://127.0.0.1:{self.SOCKS_PORT}'
            proxies = {'http': p, 'https': p}
        try:
            resp = requests.get(
                url, proxies=proxies, timeout=timeout,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            resp.raise_for_status()
            raw = (resp.text or '').strip()
        except Exception as e:
            result['error'] = f'订阅拉取失败: {e}'
            return result

        # base64 解码（兼容填充缺失）；解不出则按原文逐行处理
        decoded = None
        try:
            pad = '=' * (-len(raw) % 4)
            decoded = base64.b64decode(raw + pad).decode('utf-8', 'replace')
        except Exception:
            decoded = raw

        lines = [ln.strip() for ln in decoded.splitlines() if ln.strip()]
        result['total'] = len(lines)

        existing = {(n.get('Address'), n.get('Port'))
                    for n in self.get_all_nodes_info()}
        added = skipped = errors = 0
        for line in lines:
            if not (line.startswith('vless://') or line.startswith('vmess://')):
                skipped += 1
                continue
            d = parse_share_link(line)
            if not d or not d.get('address') or not d.get('port'):
                skipped += 1
                continue
            try:
                port = int(d['port'])
            except Exception:
                skipped += 1
                continue
            key = (d['address'], port)
            if key in existing:
                skipped += 1
                continue
            ok = self.add_node(
                remarks=d.get('remarks') or d['address'],
                address=d['address'], port=port, id=d.get('id', ''),
                config_type=d.get('config_type', 5),
                network=d.get('network', 'tcp'),
                path=d.get('path', ''), request_host=d.get('request_host', ''),
                stream_security=d.get('stream_security', ''),
                sni=d.get('sni', ''), fingerprint=d.get('fingerprint', ''),
                public_key=d.get('public_key', ''),
                short_id=d.get('short_id', ''),
            )
            if ok:
                added += 1
                existing.add(key)
            else:
                errors += 1

        self._refresh_node_list()
        result.update(ok=True, added=added, skipped=skipped, errors=errors)
        logger.info(f"订阅导入完成: 新增 {added} / 跳过 {skipped} / 失败 {errors} "
                    f"（共 {len(lines)} 条）")
        return result


# 全局单例
_global_xray: Optional['XrayCoreController'] = None
_xray_lock = threading.Lock()

def init_xray() -> 'XrayCoreController':
    """获取或初始化 Xray 控制器（单例）"""
    global _global_xray
    with _xray_lock:
        if _global_xray is None:
            _global_xray = XrayCoreController()
        return _global_xray

def get_xray() -> 'XrayCoreController':
    """获取已初始化的 Xray 控制器"""
    return _global_xray


def parse_share_link(link: str) -> Dict:
    """把节点分享链接解析成 :meth:`XrayCoreController.add_node` 的参数字典。

    为什么需要它（2026-09-28）
    --------------------------
    GUI 的「添加节点」表单里没有 reality 字段，而 reality 节点少了
    ``publicKey`` 一定握手失败；手工逐格填 8 个参数也不现实。让用户
    直接粘贴 ``vless://`` 链接是最不容易出错的方式。

    支持 ``vless://`` 与 ``vmess://``（base64 JSON）。解析失败返回空 dict。
    """
    from urllib.parse import urlparse, parse_qs, unquote

    link = (link or '').strip()
    if not link:
        return {}

    def _q(q, key, default=''):
        v = q.get(key)
        return v[0] if v else default

    try:
        if link.startswith('vless://'):
            u = urlparse(link)
            q = parse_qs(u.query or '')
            return {
                'config_type': 5,
                'id': u.username or '',
                'address': u.hostname or '',
                'port': u.port or 0,
                'remarks': unquote(u.fragment) if u.fragment else '',
                'network': _q(q, 'type', 'tcp'),
                'stream_security': _q(q, 'security'),
                'sni': _q(q, 'sni'),
                'fingerprint': _q(q, 'fp'),
                'public_key': _q(q, 'pbk'),
                'short_id': _q(q, 'sid'),
                'spider_x': _q(q, 'spx', '/'),
                'flow': _q(q, 'flow'),
                'mldsa65_verify': _q(q, 'pqv'),
                'path': _q(q, 'path'),
                'request_host': _q(q, 'host'),
            }

        if link.startswith('vmess://'):
            import base64
            body = link[len('vmess://'):].split('#')[0]
            pad = '=' * (-len(body) % 4)
            data = json.loads(base64.b64decode(body + pad).decode('utf-8', 'replace'))
            return {
                'config_type': 1,
                'id': data.get('id', ''),
                'address': data.get('add', ''),
                'port': int(data.get('port') or 0),
                'remarks': data.get('ps', ''),
                'network': data.get('net', 'tcp') or 'tcp',
                'stream_security': data.get('tls', '') or '',
                'sni': data.get('sni', '') or data.get('host', '') or '',
                'fingerprint': data.get('fp', '') or '',
                'public_key': data.get('pbk', '') or '',
                'short_id': data.get('sid', '') or '',
                'spider_x': data.get('spx', '/') or '/',
                'path': data.get('path', '') or '',
                'request_host': data.get('host', '') or '',
            }
    except Exception as e:
        logger.warning('解析节点链接失败: %s' % e)
    return {}
