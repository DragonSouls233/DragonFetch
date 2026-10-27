# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
PORN下载管理器
整合检测、下载、缓存功能
"""
import os
import sys
import shutil
import time
import asyncio
import logging
import hashlib
import queue
import random
import threading
from typing import Dict, List, Any, Optional
from pathlib import Path

class SimpleDebugger:
    """完全空的调试器实现，避免任何外部引用"""
    def __getattr__(self, name):
        def no_op(*args, **kwargs):
            pass
        return no_op

def get_debugger():
    return SimpleDebugger()

from .database.download_cache import DownloadCacheDB
from .utils.filename_formatter import FileNameFormatter
from .utils.url_sync_logger import log_download_skip
from .v2ray_controller import XrayCoreController, init_xray
# 2026-09-20 事故修复（第二批）：把"该不该换代理"从字符串关键词匹配升级为
# 语义分类 + 主动探测。详见 modules/core/net/ 下两个模块的 docstring。
from .net.error_classifier import ErrorKind, classify_error
from .net import proxy_probe
# 2026-09-20：把 impersonate 写成库模式真正接受的形态（详情见该模块 docstring）。
# 原先 ydl_opts 里写 'impersonate': True，会让 YoutubeDL 构造阶段抛**空消息**
# 的 AssertionError —— 日志只留下「错误输出为空」，无法定位。
from .net.ytdlp_opts import apply_impersonate

logger = logging.getLogger(__name__)


# ==================== 节点切换全局互斥锁 ====================
# 2026-09-20 事故修复：多个下载线程可能同时判定失败并抢着切换节点，造成切换
# 风暴。此处保证同一时刻只有一个线程能真正执行节点切换。
_NODE_SWITCH_LOCK = threading.Lock()

# 探测闸门锁：换节点前的"代理健康探测"最多只允许一个线程在做，其余直接跳过，
# 避免失败风暴时一堆线程同时发探测请求。
_PROBE_GATE_LOCK = threading.Lock()

# 视频不可用（与代理无关）的判定关键词
#
# .. deprecated:: 2026-09-20 第二批修复
#    自本版起改由 ``modules.core.net.error_classifier.classify_error`` 统一分类。
#    保留此常量仅为兼容外部引用，新代码请勿再使用。
_VIDEO_UNAVAILABLE_KEYWORDS = (
    'http error 410', '410 gone', 'http error 404', 'not found',
    'has been disabled', 'has been removed', 'has been deleted',
    'private video', 'is private', '474', 'no longer available',
    'video has been',
)


class YtDlpError(Exception):
    """yt-dlp 执行失败异常（保留完整输出，供错误分类使用）

    原实现先把完整输出截断成 200 字符再抛 Exception，导致上层无法区分
    「视频已删除(410)」与「网络故障」，把已删除视频误判为代理问题并触发节点
    切换。此处保留完整输出，分类时使用 diagnostic_text。
    """

    def __init__(self, message: str, exit_code: Optional[int] = None,
                 full_output: str = ''):
        super().__init__(message)
        self.exit_code = exit_code
        self.full_output = full_output or ''

    @property
    def diagnostic_text(self) -> str:
        """用于错误分类的完整文本（异常消息 + yt-dlp 原始输出）"""
        return f"{self}\n{self.full_output}"


def get_error_diagnostic(err: BaseException) -> str:
    """提取异常的可分类文本；YtDlpError 返回完整输出，其余返回 str(err)"""
    text = getattr(err, 'diagnostic_text', None)
    if isinstance(text, str) and text:
        return text
    return str(err)


# yt-dlp 失败时，error_output 的**开头**几乎总是「Extracting URL / Downloading webpage /
# Downloading m3u8 information」这类进度行，真正的 ERROR 在**末尾**。
# 原实现一律 `error_output[:200]` ⇒ 取到的全是进度，真因被砍在 200 字符之外。
# 服务端实测（2026-09-22 体检）：2,211 条 error_msg 长度恰好 220
# （= 20 字符前缀 + 200 截断），09-22 那 78 条失败全部无法诊断。
# ⇒ 改用「头 + 尾」摘要，保证末尾 ERROR 一定被保留。
ERROR_SUMMARY_HEAD = 200
ERROR_SUMMARY_TAIL = 1300


def summarize_error_output(
    text: str,
    *,
    head: int = ERROR_SUMMARY_HEAD,
    tail: int = ERROR_SUMMARY_TAIL,
) -> str:
    """把 yt-dlp 原始输出压成「头 + 尾」摘要。

    短文本原样返回；超长时保留头部 ``head`` 字符与尾部 ``tail`` 字符，
    中间以省略标记替代 —— 因为 yt-dlp 把 ERROR 放在最后几行。
    """
    if not text:
        return ''
    text = text.strip()
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[省略 {omitted} 字符]...\n{text[-tail:]}"


# ==================== 工具函数 ====================

def _normalize_name(name: str) -> str:
    """标准化演员名：小写、去分隔符"""
    import re
    normalized = name.lower().strip()
    normalized = re.sub(r'[_\-\.]', ' ', normalized)
    normalized = re.sub(r'[^\w\s]', ' ', normalized)
    return ' '.join(normalized.split())


def _advanced_normalize(name: str) -> str:
    """高级标准化：拆分驼峰命名"""
    import re
    basic = _normalize_name(name)
    camel_split = re.sub(r'([a-z])([A-Z])', r'\1 \2', basic)
    return ' '.join(camel_split.split())


def _similarity_match(name1: str, name2: str) -> float:
    """基于词汇的相似性匹配"""
    import difflib
    words1 = set(_advanced_normalize(name1).split())
    words2 = set(_advanced_normalize(name2).split())
    common = words1 & words2
    union = words1 | words2
    if not union:
        return 0.0
    jaccard = len(common) / len(union)
    norm1 = _advanced_normalize(name1)
    norm2 = _advanced_normalize(name2)
    sequence_ratio = difflib.SequenceMatcher(None, norm1, norm2).ratio()
    return max(jaccard, sequence_ratio)


def _find_model_id(models: list, model_name: str) -> int:
    """在订阅列表中查找演员 ID（精确→模糊→相似度匹配）"""
    # 精确匹配
    for m in models:
        if m.get('name', '').strip().lower() == model_name.strip().lower():
            return m.get('id')

    # 高级标准化精确匹配
    norm_name = _advanced_normalize(model_name)
    for m in models:
        if _advanced_normalize(m.get('name', '')).strip() == norm_name:
            return m.get('id')

    # 相似度匹配
    best_id = None
    best_score = 0
    for m in models:
        score = _similarity_match(model_name, m.get('name', ''))
        if score > best_score:
            best_score = score
            best_id = m.get('id')

    return best_id if best_score >= 0.8 else None


class ProxyManager:
    """
    代理管理器 - 多代理轮换，降低反爬虫检测概率
    
    支持多个代理地址自动轮换，下载失败时自动切换到下一个代理。
    每个代理有健康状态跟踪，避免使用已失效的代理。
    
    代理来源：
    1. 本地配置文件（app_config.json）
    2. GitHub 公共代理列表（自动抓取）
    3. v2rayN 订阅链接（支持 vmess/vless/trojan/ss/socks）
    """
    
    # v2rayN 控制器已集成到 __init__ 中
    
    def __init__(self):
        self._lock = threading.Lock()
        self._current_index = 0
        self._proxies: List[Dict] = []  # [{url, enabled, fail_count, last_used}]
        self._consecutive_failures = 0
        self._max_failures_per_proxy = 3
        # 下载专用的「节点专属入口」（2026-09-28 第二批）：每个元素是
        # socks5://127.0.0.1:<port>，一个入口固定对应一个节点。
        self._pinned_urls: List[str] = []
        self._pinned_index = 0
        # 节点下载权重：port(int/str) 或 节点名 -> 权重；<=0 表示不参与下载
        # 轮询（被降权）。来源 app_config.json -> network.node_download_weights。
        # 实测 vpn-自己(10810) 出口仅 ~48KiB/s 且常超时，配置里已将其权重设为 0。
        self._node_weights = self._load_node_download_weights()

        # 初始化 Xray 控制器（单例，无窗口后台运行）
        self.v2ray = init_xray()
        
        # 如果 Xray 已启用，自动添加其 SOCKS5 代理
        if self.v2ray.get_status().get('enabled'):
            socks_url = f"socks5://127.0.0.1:{XrayCoreController.SOCKS_PORT}"
            self._proxies.append({
                'url': socks_url,
                'enabled': True,
                'fail_count': 0,
                'last_used': 0,
                'name': 'v2rayN',
                'source': 'v2ray',
            })
            logger.info(f"自动添加 v2rayN 代理: {socks_url}")
            self._register_pinned_proxies()
        
        # 加载手动配置的代理
        self._load_proxies_from_config()

    def _load_node_download_weights(self) -> Dict:
        """读取各节点专属入口的「下载权重」。

        来源 ``app_config.json -> network.node_download_weights``，键可以是
        端口(int/str)或节点名(Remarks)，值为正整数（轮询份额）或 <=0
        （不参与下载轮询，即被降权）。缺省为 1（普通轮询）。

        退化处理：读不到/格式错一律按默认 1，绝不影响下载启动。
        """
        try:
            import json
            from multisite.core.utils.path_utils import get_config_path
            path = get_config_path('app_config.json')
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                w = (data.get('network') or {}).get('node_download_weights') or {}
                if isinstance(w, dict):
                    logger.info("已加载节点下载权重: %s", w)
                    return w
        except Exception as e:
            logger.warning("读取节点下载权重失败（按默认 1）: %s", e)
        return {}

    def _weight_for(self, port, node) -> int:
        """查某节点专属入口的下载权重，缺省 1。"""
        w = self._node_weights
        for key in (port, str(port) if port is not None else None, node):
            if key is not None and key in w:
                try:
                    val = int(w[key])
                    return val
                except (TypeError, ValueError):
                    return 1
        return 1

    def _register_pinned_proxies(self):
        """把「节点专属入口」注册成独立代理项（2026-09-28 第二批）

        为什么
        ------
        负载均衡的 roundRobin 是**逐连接**换出口的，而 PornHub 的媒体直链把
        授权出口 IP 写进了 URL（``...?ip=<出口IP>&hash=...``）。同一次下载里
        页面与分片落到不同节点时，CDN 直接回 ``HTTP 474: Client Error`` ——
        线上表现为「一启用负载均衡就全部 474」。

        因此下载流量改用专属入口：**一个下载任务固定一个入口**，整条链路
        （页面 → m3u8 → 分片）出口一致；多个任务轮流用不同入口，三个节点
        依然都在承载。10808（balancer）保留给抓取等无状态请求。

        入口若拿不到（单出口模式），``_pinned_urls`` 保持为空，
        :meth:`get_download_proxy` 会自动退回 10808，行为与改造前一致。
        """
        try:
            status = self.v2ray.get_status()
        except Exception as e:
            logger.warning(f"读取 Xray 专属入口失败: {e}")
            return
        for item in (status.get('pinned_proxies') or []):
            url = item.get('url') or 'socks5://127.0.0.1:%s' % item.get('port')
            if not url or any(p['url'] == url for p in self._proxies):
                continue
            name = 'v2rayN#%s' % (item.get('node') or item.get('port'))
            weight = self._weight_for(item.get('port'), item.get('node'))
            self._proxies.append({
                'url': url,
                'enabled': True,
                'fail_count': 0,
                'last_used': 0,
                'name': name,
                'source': 'v2ray-pinned',
                'weight': weight,
            })
            if weight <= 0:
                logger.info("节点已降权（不参与下载轮询）: %s (%s, 权重 %s)",
                            name, url, weight)
            self._pinned_urls.append(url)
        if self._pinned_urls:
            logger.info('已注册 %d 个节点专属入口（下载按任务固定出口）: %s'
                        % (len(self._pinned_urls), ', '.join(self._pinned_urls)))
    
    def _load_proxies_from_config(self):
        """从配置文件加载代理列表"""
        try:
            import json
            from multisite.core.utils.path_utils import get_config_path
            
            config_file = get_config_path('app_config.json')
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                
                network = config_data.get('network', {})
                
                # 加载手动配置的代理列表
                proxy_list = network.get('proxy_list', [])
                if proxy_list and isinstance(proxy_list, list):
                    for p in proxy_list:
                        p_type = p.get('type', 'socks5')
                        p_host = p.get('host', '127.0.0.1')
                        p_port = p.get('port', 10808)
                        proxy_url = f"{p_type}://{p_host}:{p_port}"
                        # 避免重复添加 v2rayN 的代理
                        if any(existing['url'] == proxy_url for existing in self._proxies):
                            continue
                        self._proxies.append({
                            'url': proxy_url,
                            'enabled': True,
                            'fail_count': 0,
                            'last_used': 0,
                            'name': p.get('name', f'代理{len(self._proxies)+1}'),
                            'source': 'config',
                            'weight': 1,
                        })
                    if self._proxies:
                        logger.info(f"从配置加载了 {len(self._proxies)} 个代理")

                # 如果没有配置任何代理，使用单个默认代理
                if not self._proxies and network.get('proxy_enabled', False):
                    p_type = network.get('proxy_type', 'socks5')
                    p_host = network.get('proxy_host', '127.0.0.1')
                    p_port = network.get('proxy_port', 10808)
                    proxy_url = f"{p_type}://{p_host}:{p_port}"
                    self._proxies.append({
                        'url': proxy_url,
                        'enabled': True,
                        'fail_count': 0,
                        'last_used': 0,
                        'name': '默认代理',
                        'source': 'config',
                        'weight': 1,
                    })
                    logger.info(f"使用单个代理: {proxy_url}")
        except Exception as e:
            logger.warning(f"加载代理配置失败: {e}")

        # 如果没有任何代理，添加默认
        if not self._proxies:
            self._proxies.append({
                'url': 'socks5://127.0.0.1:10808',
                'enabled': True,
                'fail_count': 0,
                'last_used': 0,
                'name': '默认代理',
                'source': 'default',
                'weight': 1,
            })
    





    def get_proxy_count(self) -> Dict:
        """获取代理统计信息"""
        with self._lock:
            total = len(self._proxies)
            enabled = sum(1 for p in self._proxies if p['enabled'])
            by_source = {}
            for p in self._proxies:
                src = p.get('source', 'unknown')
                by_source[src] = by_source.get(src, 0) + 1
            return {
                'total': total,
                'enabled': enabled,
                'disabled': total - enabled,
                'by_source': by_source,
            }
    
    def get_current_proxy(self) -> Optional[str]:
        """获取当前代理URL"""
        with self._lock:
            if not self._proxies:
                return None
            enabled = [p for p in self._proxies if p['enabled']]
            if not enabled:
                # 所有代理都被禁用了，重置
                logger.warning("所有代理已被禁用，重置代理状态")
                for p in self._proxies:
                    p['enabled'] = True
                    p['fail_count'] = 0
                enabled = self._proxies
            return enabled[0]['url']
    
    def get_next_proxy(self) -> Optional[str]:
        """轮换到下一个可用代理"""
        with self._lock:
            if not self._proxies:
                return None
            
            enabled = [i for i, p in enumerate(self._proxies) if p['enabled']]
            if not enabled:
                # 所有代理都被禁用，重置
                logger.warning("所有代理已被禁用，重置代理状态")
                for p in self._proxies:
                    p['enabled'] = True
                    p['fail_count'] = 0
                enabled = list(range(len(self._proxies)))
            
            # 轮换到下一个
            current_pos = enabled.index(self._current_index) if self._current_index in enabled else -1
            next_pos = (current_pos + 1) % len(enabled)
            self._current_index = enabled[next_pos]
            proxy = self._proxies[self._current_index]
            proxy['last_used'] = time.time()
            
            logger.info(f"切换到代理: {proxy['name']} ({proxy['url']})")
            return proxy['url']
    
    def get_download_proxy(self) -> Optional[str]:
        """下载专用：**按任务**挑一个「节点专属入口」（2026-09-28 第二批）

        与 :meth:`get_next_proxy` 的区别在于这里的候选**只含节点专属入口**，
        每个入口固定对应一个节点，因此同一次下载的全部 HTTP 请求出口一致。

        必要性：PornHub 的媒体直链里带 ``?ip=<出口IP>``，站点会校验实际发起
        请求的 IP；逐连接轮换出口会让分片请求落到另一个节点，CDN 回
        ``HTTP 474: Client Error``（实测同一视频：balancer → 立刻 474；
        单节点 → 稳定下载 566~604 KiB/s）。

        选择策略：在**未禁用且权重>0**的专属入口间，按权重构造轮询池
        （权重=该入口在池中的副本数），再近似 round-robin。**权重<=0 的入口
        不参与下载轮询**（即被降权，详见 :meth:`_load_node_download_weights`）。
        某个入口连续失败达到阈值会被 :meth:`report_failure` 禁用，从而自动让出
        流量；全部不可用时撤销禁用（安全网，避免主动断网），但权重=0 的节点
        仍不纳入（那是用户明确的降权意图），最终退回 balancer(10808)。

        单出口模式（没有专属入口）时返回 :meth:`get_current_proxy`，即改造前
        的行为，保证向后兼容。
        """
        with self._lock:
            if not self._pinned_urls:
                return None
            # 候选 = 启用且权重>0 的专属入口
            candidates = [
                p for p in self._proxies
                if p['url'] in self._pinned_urls and p['enabled']
                and p.get('weight', 1) > 0
            ]
            if not candidates:
                # 全部禁用或权重归零：撤销禁用（安全网）。权重=0 的节点保持不参与。
                logger.warning("节点专属入口无可用候选，重置禁用状态（权重=0 节点仍不纳入）")
                for p in self._proxies:
                    if p['url'] in self._pinned_urls:
                        p['enabled'] = True
                        p['fail_count'] = 0
                candidates = [
                    p for p in self._proxies
                    if p['url'] in self._pinned_urls and p.get('weight', 1) > 0
                ]
            # 按权重构造轮询池
            pool = []
            for p in candidates:
                w = max(1, int(p.get('weight', 1)))
                pool.extend([p['url']] * w)
            if not pool:
                # 实在没有候选（全是权重0）：退回 balancer，不当机
                return self.get_current_proxy()
            self._pinned_index = (self._pinned_index + 1) % len(pool)
            url = pool[self._pinned_index]
            for p in self._proxies:
                if p['url'] == url:
                    p['last_used'] = time.time()
                    logger.info(f"下载使用专属入口: {p['name']} ({url}) [权重 {p.get('weight', 1)}]")
                    break
            return url

    def get_random_proxy(self) -> Optional[str]:
        """随机获取一个代理"""
        with self._lock:
            enabled = [p for p in self._proxies if p['enabled']]
            if not enabled:
                return self.get_next_proxy()
            proxy = random.choice(enabled)
            proxy['last_used'] = time.time()
            return proxy['url']
    
    def report_success(self, proxy_url: str):
        """报告代理下载成功"""
        with self._lock:
            for p in self._proxies:
                if p['url'] == proxy_url:
                    p['fail_count'] = 0
                    self._consecutive_failures = 0
                    break
    
    def get_consecutive_failures(self) -> int:
        """获取连续失败次数（供节点切换限流判断）"""
        with self._lock:
            return self._consecutive_failures
    
    def reset_consecutive_failures(self):
        """重置连续失败计数（节点切换成功后调用）"""
        with self._lock:
            self._consecutive_failures = 0
    
    def report_failure(self, proxy_url: str, is_video_deleted: bool = False,
                       affects_proxy: Optional[bool] = None):
        """报告代理下载失败
        
        Args:
            proxy_url: 代理URL
            is_video_deleted: 是否是视频已删除（非代理问题）
            affects_proxy: 该失败是否应算在代理头上。为 None 时按老语义推导
                （``not is_video_deleted``）。推荐由
                :func:`modules.core.net.error_classifier.classify_error` 给出。
        """
        # affects_proxy 缺省时退回老语义，保证既有调用方行为不变
        if affects_proxy is None:
            affects_proxy = not is_video_deleted

        with self._lock:
            # 【2026-09-20 第二批修复】原实现先 ``_consecutive_failures += 1``
            # 再判断 is_video_deleted，于是"视频已删除"这种与代理毫无关系的
            # 失败也会累积到节点切换阈值上——3 个已删除视频就能把阈值顶满，
            # 让之后任意一次失败触发切换，限流闸门被旁路。
            if not affects_proxy:
                logger.debug(
                    f"不计入代理失败（{'视频不可用' if is_video_deleted else '与代理无关'}）: "
                    f"{proxy_url}"
                )
                return

            self._consecutive_failures += 1
            
            for p in self._proxies:
                if p['url'] == proxy_url:
                    p['fail_count'] += 1
                    logger.warning(f"代理 {p['name']} 失败次数: {p['fail_count']}/{self._max_failures_per_proxy}")
                    
                    if p['fail_count'] >= self._max_failures_per_proxy:
                        # 只有在"还有别的可用代理"时才禁用。若这是唯一可用代理，
                        # 禁用它等于主动断网，且 get_next_proxy() 会立刻把它重置
                        # 启用，形成"禁用↔重置"抖动（事故日志里代理状态反复翻转
                        # 就是这个循环）。
                        others = [q for q in self._proxies
                                  if q['url'] != p['url'] and q['enabled']]
                        if others:
                            p['enabled'] = False
                            logger.warning(f"代理 {p['name']} 连续失败 {p['fail_count']} 次，暂时禁用")
                        else:
                            logger.warning(
                                f"代理 {p['name']} 连续失败 {p['fail_count']} 次，"
                                f"但它是唯一可用代理，保持启用（交给节点切换处理）"
                            )
                    break
    
    def add_proxy(self, name: str, proxy_type: str, host: str, port: int):
        """添加新代理"""
        with self._lock:
            proxy_url = f"{proxy_type}://{host}:{port}"
            self._proxies.append({
                'url': proxy_url,
                'enabled': True,
                'fail_count': 0,
                'last_used': 0,
                'name': name,
            })
            logger.info(f"添加代理: {name} ({proxy_url})")
    
    def remove_proxy(self, name: str):
        """移除代理"""
        with self._lock:
            self._proxies = [p for p in self._proxies if p['name'] != name]
            logger.info(f"移除代理: {name}")
    
    def get_proxy_list(self) -> List[Dict]:
        """获取代理列表"""
        with self._lock:
            return [{
                'name': p['name'],
                'url': p['url'],
                'enabled': p['enabled'],
                'fail_count': p['fail_count'],
                'source': p.get('source', 'unknown'),
                'weight': p.get('weight', 1),
            } for p in self._proxies]
    
    def get_stats(self) -> Dict:
        """获取代理统计信息"""
        return self.get_proxy_count()


# 全局代理管理器实例
_global_proxy_manager = None
_proxy_manager_lock = threading.Lock()

def get_proxy_manager() -> ProxyManager:
    """获取全局代理管理器单例"""
    global _global_proxy_manager
    with _proxy_manager_lock:
        if _global_proxy_manager is None:
            _global_proxy_manager = ProxyManager()
        return _global_proxy_manager


class PornDownloadManager:
    """
    PORN下载管理器

    功能：
    1. 订阅模特
    2. 检测缺失视频
    3. 下载缺失视频
    4. 缓存管理
    """

    #: 可用物理内存低于此值(MB)即进入「内存压力」处理：gc + 通知重任务让路。
    #: 2026-10-22 修正：旧值 1024 在本项目「常驻服务端」上几乎永远触发——
    #: 该机总内存 8 GB，但常驻可用内存长期只有 ~950~1024 MB（standby/驱动锁
    #: 内存不可回收），于是下载/检测被永久熔断（表现即「一点都不能下载/检测」）。
    #: 真实崩溃点（2026-10-08 事故）在 460 MB，故下探到 256：远低于常态余量
    #: （保证能跑），又比崩溃点高 200 MB（流式下载/检测内存增量小，足够保命）。
    _MEMORY_LOW_MB = 256

    def __init__(self, config: dict = None):
        import platform
        import psutil
        import threading

        self.config = {}
        self.app_config = None
        self._download_count = 0  # 累计下载数量
        self._last_memory_check = 0  # 上次内存检查时间
        #: 内存压力标志（由内存监控线程维护；重任务用 is_memory_pressure() 查询）
        self._memory_pressure = False

        # ===== 节点切换限流（2026-09-20 事故修复）=====
        # 原实现「任一失败即切换节点」且切换时会全局杀进程，导致雪崩式失败。
        # 现要求：连续失败达阈值 + 超过冷却时间 + 抢到全局锁，才允许切换。
        self._last_switch_time = 0.0
        self._switch_failure_threshold = 3
        self._min_switch_interval = 60.0

        # ===== 换节点前的代理探测（2026-09-20 第二批修复）=====
        # 主动确认"代理本身还通不通"，避免把站点侧问题当成代理问题白切一次。
        self._proxy_probe_timeout = 4.0      # 单次探测超时（秒）
        self._proxy_probe_cache_ttl = 15.0   # 探测结果缓存（秒）
        self._probe_veto_limit = 3           # 探测连续否决多少次后改用切换兜底
        self._probe_veto_count = 0
        
        # 记录系统信息（使用非阻塞方式获取 CPU 信息）
        system_info = {
            'platform': platform.platform(),
            'python_version': platform.python_version(),
            'cpu_count': psutil.cpu_count(logical=True),
            'cpu_percent': 0,  # 延迟获取，避免阻塞
            'memory_total': psutil.virtual_memory().total / (1024*1024*1024),
            'memory_available': psutil.virtual_memory().available / (1024*1024*1024)
        }
        
        logger.info(f"系统信息: {system_info}")
        
        # 启动内存监控线程
        self._memory_monitor_running = True
        self._memory_monitor_thread = threading.Thread(target=self._memory_monitor, daemon=True)
        self._memory_monitor_thread.start()
        
        # 初始化代理管理器（多代理轮换）
        self.proxy_manager = get_proxy_manager()
        proxy_url = self.proxy_manager.get_current_proxy()
        
        # 使用已有的 Xray 控制器，不重复初始化（避免阻塞 GUI 启动）
        try:
            from .v2ray_controller import get_xray, init_xray
            self.v2ray_controller = get_xray()
            if self.v2ray_controller is None:
                # 后台线程初始化 Xray
                def _init_xray_bg():
                    try:
                        self.v2ray_controller = init_xray()
                    except Exception as e:
                        logger.warning(f"Xray 后台初始化失败: {e}")
                threading.Thread(target=_init_xray_bg, daemon=True).start()
        except Exception as e:
            logger.warning(f"Xray 控制器初始化失败: {e}")
            self.v2ray_controller = None
        
        try:
            from multisite.core.utils.app_config import app_config
            self.app_config = app_config
            
            if config is not None:
                self.config = config.copy() if isinstance(config, dict) else {}
                if not self.config.get('proxy'):
                    self.config['proxy'] = proxy_url
            else:
                porn_config = self.app_config.get('porn', default={})
                if isinstance(porn_config, dict):
                    self.config = porn_config.copy()
                else:
                    self.config = {}
                self.config['proxy'] = proxy_url
        except Exception as e:
            logger.error(f"从app_config加载配置失败: {e}，使用配置文件回退")
            if config is not None and isinstance(config, dict):
                self.config = config.copy()
            if not self.config.get('proxy'):
                self.config['proxy'] = proxy_url
        
        if not self.config.get('proxy'):
            self.config['proxy'] = proxy_url
        
        if self.config is None:
            self.config = {}
        
        # 记录代理信息
        proxy_list = self.proxy_manager.get_proxy_list()
        logger.info(f"PornDownloadManager初始化完成, 可用代理: {len(proxy_list)} 个")
        for p in proxy_list:
            logger.info(f"  代理: {p['name']} ({p['url']}) {'✅' if p['enabled'] else '❌'} 失败{p['fail_count']}次")
        
        # 数据库（使用 ModuleDatabaseManager 统一管理 models.db）
        from multisite.core.database.module_db_manager import ModuleDatabaseManager
        self.db_mgr = ModuleDatabaseManager()
        self.cache = DownloadCacheDB()
        
        # 兼容 PornDatabase 接口的适配器
        class _PornDBAdapter:
            """将 ModuleDatabaseManager 适配为 PornDatabase 兼容接口"""
            def __init__(self, mgr):
                self._mgr = mgr
                self.MODULE = 'PORN'
            
            def get_model(self, model_id=None, url=None, name=None):
                if model_id:
                    return self._mgr.get_model_by_id(model_id)
                if name:
                    models = self._mgr.get_porn_models()
                    for m in models:
                        if m.get('name', '').lower() == name.lower():
                            return m
                return None
            
            def get_all_models(self):
                return self._mgr.get_porn_models()
            
            def add_video(self, model_id, title, url, duration=None, thumbnail=None, status='missing'):
                """添加或更新视频（通过URL去重）"""
                # 先检查是否已存在
                existing = self._mgr.get_videos(model_id)
                for v in existing:
                    if v.get('url') == url:
                        # 更新状态
                        self._mgr.update_video_status(v['id'], status)
                        return v['id']
                # 新增
                from multisite.core.database.models_db_adapter import ModelsDBAdapter
                from multisite.core.utils.path_utils import get_data_path
                adapter = ModelsDBAdapter(get_data_path('db', 'models.db'))
                return adapter.add_video(model_id, title, url, status)
            
            def get_videos_by_model(self, model_id):
                return self._mgr.get_videos(model_id)
            
            def get_missing_videos(self, model_id):
                return self._mgr.get_missing_videos(model_id)
            
            def update_video_status(self, video_id, status, local_path=None, file_size=0):
                self._mgr.update_video_status(video_id, status, local_path)
            
            def update_model_stats(self, model_id, online_count=None, local_count=None,
                                    missing_count=None, downloaded_count=None, shorty_count=None):
                self._mgr.update_model_stats(model_id, online_count=online_count,
                                              local_count=local_count, missing_count=missing_count,
                                              downloaded_count=downloaded_count,
                                              shorty_count=shorty_count)
            
            def delete_model(self, model_id):
                self._mgr.delete_model(model_id)
            
            def get_stats(self):
                return self._mgr.get_porn_stats()
        
        self.db = _PornDBAdapter(self.db_mgr)
        
        # 文件命名
        # FileNameFormatter的方法都是静态方法，不需要实例化
        
        # 下载配置
        self.download_dir = self.config.get('download_dir', 'downloads')
        # 根据 CPU 性能调整并发数 - N150 处理器建议使用较低的并发数
        self.max_concurrent = self.config.get('max_concurrent', 1)  # N150 处理器建议使用 1 个并发
        
        # 定时任务配置
        self.scheduler_config = self.config.get('scheduler', {})
        self.auto_download_enabled = self.scheduler_config.get('auto_download', False)
        self.scheduled_hour = self.scheduler_config.get('hour', 0)
        self.scheduled_minute = self.scheduler_config.get('minute', 0)
        
        # 下载器（延迟加载）
        self._downloader = None
        
        # 初始化调试器
        self.debugger = get_debugger()
        # 简化调试器的使用，减少日志写入
        self.debugger.info('DOWNLOAD_MANAGER', '下载管理器初始化')
        
        # 初始化定时任务
        self._init_scheduler()
    
    def _memory_monitor(self):
        """内存监控线程 - 定期检查内存使用情况并触发垃圾回收 / 全局降载"""
        import time
        import gc
        import psutil

        while self._memory_monitor_running:
            try:
                time.sleep(60)  # 每分钟检查一次

                # 获取内存使用情况
                mem = psutil.virtual_memory()
                available_mb = mem.available / (1024 * 1024)
                used_percent = mem.percent

                logger.debug(f"内存监控: 可用 {available_mb:.1f} MB, 使用 {used_percent:.1f}%")

                # 🔴 2026-10-09：顺带巡检代理端口。Xray 可能变成
                #「进程还在、四个 SOCKS 端口全关」的僵尸态，此时所有出站请求
                #都返回 WinError 10061，刮削会卡死在某一页狂切节点空转
                #（实测切了 1252 次仍不恢复）。这里主动发现并重启。
                self._check_proxy_health()

                # 阈值 1 GB（原 512 MB）：8 GB 机器上 Qt 的 qFatal/abort 会在
                # 可用内存跌破 ~500 MB 时触发（见 2026-10-08 15:08:38 闪退事故），
                # 512 MB 太晚，等触发时进程已经站在崩溃边缘。
                if available_mb < self._MEMORY_LOW_MB:
                    logger.warning(
                        f"内存紧张！可用 {available_mb:.1f} MB（阈值 {self._MEMORY_LOW_MB} MB），"
                        f"触发强制垃圾回收")
                    gc.collect()

                    # 再次检查
                    mem = psutil.virtual_memory()
                    available_after = mem.available / (1024 * 1024)
                    logger.info(f"垃圾回收完成，可用内存: {available_after:.1f} MB")

                    # gc 之后仍然不足 => 是真实占用（不是垃圾），必须降载：
                    # 置全局标志，让归档/复制等重 IO 任务主动让路，避免与下载抢内存
                    # 把进程推到 Qt 的 abort 边缘。
                    if available_after < self._MEMORY_LOW_MB:
                        if not self._memory_pressure:
                            logger.error(
                                f"严重内存不足（gc 后仅 {available_after:.1f} MB）→ "
                                f"已置全局内存压力标志，重任务将主动让路")
                        self._memory_pressure = True
                    else:
                        # 内存已恢复，解除标志（避免永久卡在降载态）
                        if self._memory_pressure:
                            logger.info(
                                f"可用内存回升至 {available_after:.1f} MB → 解除内存压力标志")
                            self._memory_pressure = False
                else:
                    # 正常区间，若之前处于压力态则尝试解除
                    if self._memory_pressure:
                        logger.info(
                            f"可用内存回升至 {available_mb:.1f} MB → 解除内存压力标志")
                        self._memory_pressure = False

            except Exception as e:
                logger.error(f"内存监控异常: {e}")

    def _check_proxy_health(self):
        """巡检 Xray 代理端口；端口全关则重启核心。

        2026-10-09：僵尸 Xray（进程在、端口全关）会让一切出站请求返回
        WinError 10061。这里做主动自愈，避免等到刮削撞上才被动发现。
        """
        vc = getattr(self, 'v2ray_controller', None)
        if vc is None:
            vc = getattr(self, 'v2ray', None)
        if vc is None:
            return
        try:
            if not hasattr(vc, '_is_proxy_alive'):
                return
            if vc._is_proxy_alive():
                return
            logger.error("代理端口未监听（僵尸 Xray）→ 正在重启核心以恢复下载")
            try:
                vc.stop()
                time.sleep(1)
                vc.start()
                vc._wait_for_proxy_ready(timeout=30)
                logger.info("Xray 已重启，代理端口恢复监听")
            except Exception as e:
                logger.error(f"Xray 自愈重启失败: {e}")
        except Exception as e:
            logger.debug(f"代理健康巡检跳过: {e}")

    def is_memory_pressure(self) -> bool:
        """当前是否处于内存压力态（重任务应主动让路）"""
        return bool(getattr(self, '_memory_pressure', False))

    def get_available_memory_mb(self) -> float:
        """当前可用物理内存(MB)；取不到返回 -1"""
        try:
            import psutil
            return psutil.virtual_memory().available / (1024 * 1024)
        except Exception:
            return -1.0

    def _stop_memory_monitor(self):
        """停止内存监控线程"""
        self._memory_monitor_running = False
    
    def _load_proxy_from_config_file(self):
        try:
            import json
            from multisite.core.utils.path_utils import get_config_path
            
            config_file = get_config_path('app_config.json')
            
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                
                network_config = config_data.get('network', {})
                proxy_enabled = network_config.get('proxy_enabled', False)
                proxy_type = network_config.get('proxy_type', 'socks5')
                proxy_host = network_config.get('proxy_host', '127.0.0.1')
                proxy_port = network_config.get('proxy_port', 10808)
                
                if proxy_enabled:
                    proxy_url = f"{proxy_type}://{proxy_host}:{proxy_port}"
                    logger.info(f"从配置文件加载代理: {proxy_url}")
                    return proxy_url
            
            proxy_config = config_data.get('proxy', {})
            if isinstance(proxy_config, dict) and proxy_config.get('enabled', False):
                p_type = proxy_config.get('type', 'socks5')
                p_host = proxy_config.get('host', '127.0.0.1')
                p_port = proxy_config.get('port', '10808')
                proxy_url = f"{p_type}://{p_host}:{p_port}"
                logger.info(f"从proxy配置加载代理: {proxy_url}")
                return proxy_url
        except Exception as e:
            logger.warning(f"直接读取配置文件获取代理失败: {e}")
        
        return None
    
    def _get_downloader(self):
        """获取下载器"""
        if self._downloader is None:
            try:
                import yt_dlp
                self._downloader = yt_dlp
            except ImportError:
                logger.error("请安装yt-dlp: pip install yt-dlp")
                raise
        return self._downloader
    
    def _find_yt_dlp_executable(self):
        """查找 yt-dlp 可执行文件路径
        
        开发环境：按以下顺序查找：
        1. 环境变量 PATH 中的 yt-dlp 或 yt-dlp.exe
        2. 当前模块目录下的 yt-dlp 或 yt-dlp.exe
        3. 项目根目录下的 yt-dlp 或 yt-dlp.exe
        4. 返回 None（使用 Python 库模式）
        
        打包环境：只在 exe 所在目录查找 yt-dlp.exe
        """
        import os
        
        logger.info("===== 查找 yt-dlp 可执行文件 =====")
        
        # 检查是否是打包环境
        is_frozen = getattr(sys, 'frozen', False)
        
        if is_frozen:
            # ===== 打包环境：优先在 exe 所在目录查找，再查当前目录和 PATH =====
            logger.info("打包环境：查找 yt-dlp.exe")
            exe_dir = os.path.dirname(sys.executable)
            logger.info(f"exe 所在目录: {exe_dir}")
            
            search_dirs = [exe_dir, os.getcwd()]
            # 去重
            seen = set()
            unique_dirs = []
            for d in search_dirs:
                if d not in seen:
                    seen.add(d)
                    unique_dirs.append(d)
            
            for search_dir in unique_dirs:
                for exe_name in ['yt-dlp.exe', 'yt-dlp']:
                    exe_path = os.path.join(search_dir, exe_name)
                    if os.path.isfile(exe_path):
                        if os.access(exe_path, os.X_OK):
                            logger.info(f"✅ 找到: {exe_path}")
                            return exe_path
                        else:
                            logger.warning(f"⚠️ 找到但无权限: {exe_path}")
            
            # 最后尝试 PATH 环境变量
            yt_dlp_in_path = shutil.which('yt-dlp') or shutil.which('yt-dlp.exe')
            if yt_dlp_in_path:
                logger.info(f"✅ 在 PATH 中找到: {yt_dlp_in_path}")
                return yt_dlp_in_path
            
            logger.warning("❌ 未找到 yt-dlp 可执行文件，将使用 Python 库模式")
            return None
        
        # ===== 开发环境：广泛搜索 =====
        logger.info("开发环境：广泛搜索 yt-dlp 可执行文件")
        
        # 打印当前工作目录
        cwd = os.getcwd()
        logger.info(f"当前工作目录: {cwd}")
        
        # 检查当前目录 (modules/core/)
        current_dir = os.path.dirname(os.path.abspath(__file__))
        logger.info(f"当前模块目录: {current_dir}")
        
        for exe_name in ['yt-dlp.exe', 'yt-dlp']:
            exe_path = os.path.join(current_dir, exe_name)
            if os.path.isfile(exe_path):
                if os.access(exe_path, os.X_OK):
                    logger.info(f"✅ 在模块目录找到: {exe_path}")
                    return exe_path
                else:
                    logger.warning(f"⚠️ 找到但无权限: {exe_path}")
        
        # 检查项目根目录（优先于PATH，确保使用项目自带的修复版）
        project_root = os.path.dirname(os.path.dirname(current_dir))
        logger.info(f"项目根目录: {project_root}")
        
        for exe_name in ['yt-dlp.exe', 'yt-dlp']:
            exe_path = os.path.join(project_root, exe_name)
            if os.path.isfile(exe_path):
                if os.access(exe_path, os.X_OK):
                    logger.info(f"✅ 在项目根目录找到: {exe_path}")
                    return exe_path
                else:
                    logger.warning(f"⚠️ 找到但无权限: {exe_path}")
        
        # 检查当前工作目录
        for exe_name in ['yt-dlp.exe', 'yt-dlp']:
            exe_path = os.path.join(cwd, exe_name)
            if os.path.isfile(exe_path):
                if os.access(exe_path, os.X_OK):
                    logger.info(f"✅ 在工作目录找到: {exe_path}")
                    return exe_path
                else:
                    logger.warning(f"⚠️ 找到但无权限: {exe_path}")
        
        # 最后才检查环境变量 PATH（避免使用旧版）
        path_dirs = os.environ.get('PATH', '').split(os.pathsep)
        logger.info(f"PATH 目录数量: {len(path_dirs)}")
        
        for path in path_dirs:
            if not path:
                continue
            
            for exe_name in ['yt-dlp.exe', 'yt-dlp', 'youtube-dl.exe', 'youtube-dl']:
                exe_path = os.path.join(path, exe_name)
                if os.path.isfile(exe_path):
                    if os.access(exe_path, os.X_OK):
                        logger.info(f"✅ 找到可执行的 yt-dlp (PATH): {exe_path}")
                        return exe_path
                    else:
                        logger.warning(f"⚠️ 找到文件但无执行权限: {exe_path}")
        
        logger.warning("❌ 未找到 yt-dlp 可执行文件，将使用 Python 库模式")
        return None
    
    # ==================== 订阅管理 ====================
    
    def subscribe(
        self,
        model_url: str,
        model_name: str = None,
        local_directory: str = None,
        auto_download: bool = False,
        country: str = ''
    ) -> int:
        """
        订阅模特

        Args:
            model_url: 模特URL
            model_name: 模特名（可选，自动提取）
            local_directory: 本地目录
            auto_download: 是否自动下载
            country: 国家代码

        Returns:
            模特ID
        """
        # 提取模特名
        if not model_name:
            model_name = self._extract_model_name(model_url)
        
        # 使用ModuleDatabaseManager添加模特，以支持模式类型
        from multisite.core.database import ModuleDatabaseManager
        manager = ModuleDatabaseManager()
        model_id = manager.add_porn_model(
            name=model_name,
            url=model_url,
            country=country,
            local_directory=local_directory,
            auto_download=auto_download
        )
        
        logger.info(f"订阅模特: {model_name}")
        return model_id
    
    def get_subscriptions(self) -> List[Dict]:
        """获取所有订阅"""
        from multisite.core.database import ModuleDatabaseManager
        manager = ModuleDatabaseManager()
        return manager.get_porn_models()
    
    def unsubscribe(self, model_id: int):
        """取消订阅"""
        self.db.delete_model(model_id)
    
    # ==================== 检测功能 ====================
    
    async def check_model(self, model_id: int) -> Dict[str, Any]:
        """
        检测模特更新
        
        Args:
            model_id: 模特ID
        
        Returns:
            检测结果
        """
        model = self.db.get_model(model_id)
        if not model:
            return {'error': '模特不存在'}
        
        logger.info(f"检测模特: {model['name']}")
        
        # 获取在线视频
        online_videos = []
        if model['url'] and model['url'].startswith('http'):
            from .parser import ParserFactory
            
            parser = ParserFactory.create_parser('porn', self.config)
            online_videos = await parser.parse_model_page(model['url'])
        else:
            logger.warning(f"模特 {model['name']} 没有有效的URL，跳过在线视频获取")
        
        # 过滤小视频
        from ..porn.core.utils.pornhub_url_detector import PornHubUrlDetector
        regular_videos = []
        shorty_videos = []
        
        for video in online_videos:
            video_url = video.get('url', '')
            if PornHubUrlDetector.is_shorty(video_url):
                shorty_videos.append(video)
            else:
                regular_videos.append(video)
        
        # 扫描本地视频
        local_dir = model.get('local_directory', '')
        local_titles = set()
        
        if local_dir:
            # 检查目录是否存在，不存在则创建
            if not os.path.exists(local_dir):
                logger.info(f"本地目录不存在，自动创建: {local_dir}")
                try:
                    os.makedirs(local_dir, exist_ok=True)
                    logger.info("目录创建成功")
                except Exception as e:
                    logger.error(f"目录创建失败: {e}")
            
            # 扫描目录
            from .local_scanner import LocalScanner
            scanner = LocalScanner(self.config)
            local_videos = scanner.scan_directory(local_dir)
            local_titles = {v['normalized_title'] for v in local_videos}
        
        # 对比
        from .local_scanner import TitleNormalizer
        normalizer = TitleNormalizer(self.config)
        
        missing_videos = []
        for video in regular_videos:
            norm_title = normalizer.normalize(video['title'])
            if norm_title not in local_titles:
                # 检查是否已下载
                if not self.cache.is_downloaded(video['url']):
                    missing_videos.append(video)
        
        # 更新数据库
        self.db.update_model_stats(
            model_id,
            online_count=len(regular_videos),
            local_count=len(local_titles),
            missing_count=len(missing_videos),
            shorty_count=len(shorty_videos)
        )
        
        # 保存视频记录
        for video in regular_videos:
            status = 'local' if normalizer.normalize(video['title']) in local_titles else 'missing'
            self.db.add_video(
                model_id=model_id,
                title=video['title'],
                url=video['url'],
                duration=video.get('duration'),
                thumbnail=video.get('thumbnail'),
                status=status
            )
        
        return {
            'model_id': model_id,
            'model_name': model['name'],
            'online_count': len(regular_videos),
            'shorty_count': len(shorty_videos),
            'local_count': len(local_titles),
            'missing_count': len(missing_videos),
            'missing_videos': missing_videos,
            'shorty_videos': shorty_videos,
        }
    
    async def check_all_models(self) -> List[Dict]:
        """检测所有订阅"""
        models = self.db.get_all_models()
        results = []
        
        for model in models:
            # 跳过"已注销"国籍的模特
            if model.get('country') == '已注销':
                logger.info(f"跳过已注销模特: {model['name']}")
                continue
            result = await self.check_model(model['id'])
            results.append(result)
        
        return results
    
    # ==================== 下载功能 ====================
    
    async def download_video_native(
        self,
        url: str,
        model_name: str,
        title: str,
        output_dir: str = None,
        progress_callback=None,
        model_id: int = None
    ) -> Dict[str, Any]:
        """
        原生请求下载器 - 绕过 yt-dlp 直接使用 requests 下载
        
        当 yt-dlp 返回 410 错误时使用此方法作为备用方案
        """
        import requests as req
        import re
        from urllib.parse import urlparse
        
        logger.info(f"===== 原生请求下载开始 =====")
        logger.info(f"URL: {url[:50]}...")
        
        result = {
            'status': 'pending',
            'url': url,
            'title': title,
            'model_name': model_name,
            'file_path': None,
            'file_size': 0,
            'error': None
        }
        
        try:
            proxy = self.config.get('proxy', self.app_config.get('proxy', 'socks5://127.0.0.1:10808'))
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Referer': f"{urlparse(url).scheme}://{urlparse(url).netloc}/",
            }
            
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            
            # 1. 获取视频页面
            logger.info("步骤1: 获取视频页面...")
            session = req.Session()
            session.headers.update(headers)
            
            resp = session.get(url, proxies=proxies, timeout=30)
            if resp.status_code != 200:
                raise Exception(f"页面访问失败: HTTP {resp.status_code}")
            
            html = resp.text
            logger.info(f"页面获取成功, 长度: {len(html)}")
            
            # 2. 提取视频URL (从 flashvars 或 playerObj 中)
            logger.info("步骤2: 提取视频URL...")
            video_url = None
            
            # 尝试多种方式提取视频URL
            patterns = [
                r'"videoUrl"\s*:\s*"([^"]+)"',
                r'"mediaDefinitions"\s*:\s*\[([^\]]+)\]',
                r'flashvars_\d+\s*=\s*(\{[^}]+\})',
                r'src="([^"]+\.mp4[^"]*)"',
                r'(https?://[^"]+\.mp4[^"]*)',
            ]
            
            for pattern in patterns:
                match = re.search(pattern, html)
                if match:
                    video_url = match.group(1) if not pattern.startswith('"media') else None
                    if video_url and video_url.startswith('http'):
                        break
            
            # 如果没找到直接的视频URL，尝试提取 master m3u8
            if not video_url:
                m3u8_match = re.search(r'"masterUrl"\s*:\s*"([^"]+)"', html)
                if m3u8_match:
                    video_url = m3u8_match.group(1)
                    logger.info("找到 M3U8 播放列表")
            
            if not video_url:
                raise Exception("无法从页面提取视频URL")
            
            # ===== 修复 URL 转义问题 =====
            # 有时候 URL 会被 JSON 转义，如 https:\/\/ 变成 https:\/\/
            # 需要反转义为正常的 URL
            if '\\/' in video_url:
                logger.info(f"检测到转义 URL，正在反转义...")
                video_url = video_url.replace('\\/', '/')
                logger.info(f"反转义后 URL: {video_url[:80]}...")
            # ===== URL 转义修复结束 =====
            
            logger.info(f"视频URL: {video_url[:80]}...")
            
            # 3. 确定输出路径
            temp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'temp', 'downloads')
            os.makedirs(temp_dir, exist_ok=True)
            
            file_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            filename = FileNameFormatter.format(title or f"Video_{file_hash}", model_name)
            output_file = os.path.join(temp_dir, f"{file_hash}_{filename}.mp4")
            
            # 4. 下载视频文件
            logger.info("步骤3: 下载视频文件...")
            
            if video_url.endswith('.m3u8'):
                # M3U8 流媒体 - 使用 M3U8 引擎
                from ..downloader.engines.m3u8_engine import M3U8Engine
                
                engine_config = {
                    'proxy': proxy,
                    'thread_count': 16,
                    'retry_count': 5,
                }
                
                engine = M3U8Engine(engine_config)
                
                def on_progress(state):
                    if progress_callback:
                        progress_callback({
                            'status': 'downloading',
                            'progress': state.downloaded_duration / max(state.total_duration, 1),
                            'speed': state.speed,
                            'eta': state.eta,
                        })
                
                await engine.download(video_url, output_file, on_progress)
                
            else:
                # 直接视频文件 - 使用 requests 流式下载
                with session.get(video_url, proxies=proxies, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    
                    total_size = int(r.headers.get('content-length', 0))
                    
                    with open(output_file, 'wb') as f:
                        downloaded = 0
                        start_time = time.time()
                        
                        for chunk in r.iter_content(chunk_size=1024*1024):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                
                                # 进度回调
                                if progress_callback and time.time() - start_time > 1:
                                    speed = downloaded / (time.time() - start_time)
                                    eta = int((total_size - downloaded) / speed) if speed > 0 else 0
                                    
                                    progress_callback({
                                        'status': 'downloading',
                                        'progress': downloaded / total_size if total_size > 0 else 0,
                                        'speed': speed,
                                        'eta': eta,
                                    })
                                    start_time = time.time()
                    
                    file_size = os.path.getsize(output_file)
                    logger.info(f"下载完成: {output_file}")
                    logger.info(f"文件大小: {file_size / 1024 / 1024:.2f} MB")
            
            # 5. 移动到目标目录
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                final_path = os.path.join(output_dir, os.path.basename(output_file))
                import shutil
                shutil.move(output_file, final_path)
                output_file = final_path
            
            result['status'] = 'completed'
            result['file_path'] = output_file
            result['file_size'] = os.path.getsize(output_file) if os.path.exists(output_file) else 0
            
            logger.info(f"✅ 原生下载成功: {output_file}")
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ 原生下载失败: {error_msg}")
            result['status'] = 'failed'
            result['error'] = error_msg
        
        return result
    
    async def download_video(
        self,
        url: str,
        model_name: str,
        title: str,
        output_dir: str = None,
        progress_callback=None,
        model_id: int = None
    ) -> Dict[str, Any]:
        """
        下载单个视频
        
        流程: 先下载到TEMP临时目录 -> 下载完成后移动到目标目录 -> 清理TEMP残留
        
        Args:
            url: 视频URL
            model_name: 模特名
            title: 视频标题
            output_dir: 输出目录
            progress_callback: 进度回调
            model_id: 模特ID（用于临时文件管理）
        
        Returns:
            下载结果
        """
        self.debugger.checkpoint('开始下载', f'URL: {url[:50]}... | 标题: {title[:30]}...')
        
        if self.cache.is_downloaded(url, verify_file_exists=True):
            logger.info(f"视频已下载，跳过: {title}")
            self.debugger.info('DOWNLOAD', f"视频已下载，跳过: {title}")
            # 记录到独立日志
            log_download_skip(model_name, title, url, "already_downloaded")
            # 更新数据库状态
            try:
                if model_id:
                    videos = self.db.get_videos_by_model(model_id)
                    for v in videos:
                        if v.get('url') == url and v.get('status') in ('missing', 'online'):
                            self.db.update_video_status(v['id'], status='downloaded')
                            logger.info(f"更新视频状态为downloaded (缓存命中): {title}")
                            break
            except Exception as e:
                logger.warning(f"更新视频状态失败: {e}")
            return {'status': 'skipped', 'reason': 'already_downloaded'}
        
        if not output_dir:
            output_dir = self.download_dir
            model_subdir = os.path.join(output_dir, model_name)
        else:
            model_subdir = output_dir
        
        Path(model_subdir).mkdir(parents=True, exist_ok=True)
        
        from .utils.filename_formatter import FileNameFormatter
        filename = FileNameFormatter.format(title, model_name, url=url)
        final_output_path = os.path.join(model_subdir, filename)
        
        if os.path.exists(final_output_path):
            logger.info(f"目标文件已存在，跳过: {title}")
            self.debugger.info('DOWNLOAD', f'目标文件已存在，跳过: {title}')
            # 更新数据库状态：文件已存在，标记为downloaded
            try:
                if model_id:
                    # 查找数据库中该URL对应的视频记录并更新状态
                    videos = self.db.get_videos_by_model(model_id)
                    for v in videos:
                        if v.get('url') == url and v.get('status') in ('missing', 'online'):
                            self.db.update_video_status(
                                v['id'],
                                status='downloaded',
                                local_path=final_output_path
                            )
                            logger.info(f"更新视频状态为downloaded: {title}")
                            break
            except Exception as e:
                logger.warning(f"更新视频状态失败: {e}")
            return {'status': 'skipped', 'reason': 'file_exists', 'file_path': final_output_path}
        
        from .utils.temp_download_manager import TempDownloadManager
        temp_mgr = TempDownloadManager()
        
        # ===== 关键修复：标记下载开始，防止临时文件被误删 =====
        if model_id is None:
            # 尝试从数据库获取模特ID
            try:
                models = self.get_subscriptions()
                for m in models:
                    if m.get('name', '').strip().lower() == model_name.strip().lower():
                        model_id = m.get('id')
                        break
            except Exception:
                model_id = 0
        
        # 最大重试次数
        max_retries = 3
        retry_count = 0
        last_progress = 0
        download_result = None
        # 按任务的分片并发度：默认 -N 3（快），撞 416 时降级为 -N 1（单连接顺序）。
        # 416 是「并发 range 请求被 CDN 拒绝」，与节点无关，故降级参数而非换节点。
        _cf = '3'
        _chunk = '10M'
        
        # 检查是否有旧的失败记录，如果不是410错误则清除，允许重新尝试
        if self.cache.is_permanently_failed(url, max_retries=5):
            # 获取失败记录详情
            url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()
            logger.info(f"URL {url[:50]}... 已标记为永久失败，跳过下载")
            return {'status': 'failed', 'url': url, 'title': title, 'error': '已标记为永久失败'}
        
        while retry_count < max_retries:
            # 调用 start_download 标记为活跃下载
            temp_file = temp_mgr.start_download(
                video_url=url,
                video_title=title,
                model_id=model_id or 0,
                model_name=model_name,
                target_dir=model_subdir
            )
            
            logger.info(f"开始下载: {title} -> TEMP: {temp_file}")
            self.debugger.info('DOWNLOAD', f"开始下载: {title} -> TEMP: {temp_file}")
            self.debugger.update_download_state(f"开始下载: {title}")
            
            try:
                download_result = await self._do_download(
                    url=url,
                    title=title,
                    model_name=model_name,
                    temp_file=temp_file,
                    temp_mgr=temp_mgr,
                    progress_callback=progress_callback,
                    concurrent_fragments=_cf,
                    http_chunk_size=_chunk
                )
                
                # ===== 失败且判定为可重试 → 退避后重试（2026-09-20 修复）=====
                # 原报告 B5：此前**只对「停滞」重试**，而 _do_download 返回
                # status='failed'（yt-dlp 非零退出 —— 网络故障走的就是这条路）
                # 时直接落到 break 放弃。error_classifier 早已算出
                # retryable=True 却无人消费：实测 571 个队列成功率仅 28%，
                # 约七成视频被静默丢弃。
                if (download_result.get('status') == 'failed'
                        and self._is_retryable_failure(download_result)):
                    retry_count += 1
                    _err = download_result.get('error') or ''
                    _is_416 = ('416' in _err) or ('Requested Range Not Satisfiable' in _err)
                    _stalled = '停滞' in _err
                    _reason = '下载停滞' if _stalled else ('HTTP 416 降级重试' if _is_416 else '下载失败')
                    if _is_416:
                        # 416 = 并发 range 被 CDN 拒绝。降级本任务到 -N 1（单连接顺序下载），
                        # 不再发并发 range 请求即可绕开；换节点无效（根因是 range 而非出口）。
                        # cleanup_single_temp 会清掉 .part，避免续传再次撞 416。
                        _cf = '1'
                        _chunk = '1M'
                        logger.warning(
                            f"撞 HTTP 416（并发 range 被拒），本任务降级 -N1 后重试: {title}")
                        self.debugger.warning('416-FALLBACK',
                                              f"416 降级 -N1: {title} | 分类={download_result.get('error_code') or 'UNKNOWN'}")
                    logger.warning(
                        f"{_reason}，进行第 {retry_count}/{max_retries} 次重试: {title}"
                        f" | 分类={download_result.get('error_code') or 'UNKNOWN'}"
                    )
                    self.debugger.warning('RETRY', f"{_reason}重试 {retry_count}/{max_retries}: {title}")
                    
                    # 清理临时文件
                    try:
                        temp_mgr.cleanup_single_temp(temp_file)
                    except Exception as e:
                        logger.warning(f"清理临时文件失败: {e}")
                    
                    # 等待代理恢复后再重试（递增等待）
                    wait_time = 15 * retry_count  # 15秒, 30秒, 45秒
                    logger.info(f"等待 {wait_time} 秒后重试（让代理恢复）...")
                    await asyncio.sleep(wait_time)
                    continue
                
                # 下载成功 / 永久失败（视频已删除等）/ 重试耗尽，退出循环
                break
            
            except Exception as e:
                error_str = str(e)
                
                # 检查是否是410 Gone错误 - 尝试使用原生下载器
                if '410' in error_str or 'Gone' in error_str:
                    logger.warning(f"yt-dlp 返回 410 Gone，尝试使用原生下载器: {title}")
                    self.debugger.warning('FALLBACK', f"yt-dlp 返回 410，切换到原生下载器: {title}")
                    
                    # 使用原生下载器作为备用方案
                    native_result = await self.download_video_native(
                        url=url,
                        model_name=model_name,
                        title=title,
                        output_dir=model_subdir,
                        progress_callback=progress_callback,
                        model_id=model_id
                    )
                    
                    if native_result.get('status') == 'completed':
                        download_result = native_result
                        break
                    else:
                        logger.error(f"原生下载器也失败: {native_result.get('error')}")
                        download_result = {'status': 'failed', 'url': url, 'title': title, 'error': error_str, 'error_code': '410'}
                        break
                
                # 检查是否是视频已被禁用的错误
                if 'disabled' in error_str.lower() or 'This video has been disabled' in error_str:
                    logger.warning(f"视频已被禁用: {title}")
                    download_result = {'status': 'failed', 'url': url, 'title': title, 'error': error_str, 'error_code': 'DISABLED'}
                    break
                
                # 永久性失败（视频已删除/下架等）重试没有意义，直接放弃
                _exc_cls = self._classify_failure(
                    get_error_diagnostic(e),
                    exit_code=getattr(e, 'exit_code', None))
                if _exc_cls.permanent:
                    logger.warning(
                        f"永久性失败（{_exc_cls.kind.value}），不再重试: {title}")
                    download_result = {
                        'status': 'failed', 'url': url, 'title': title,
                        'error': str(e), 'error_code': _exc_cls.kind.value,
                        'permanent': True,
                    }
                    break
                
                retry_count += 1
                logger.warning(f"下载异常，进行第 {retry_count} 次重试: {e}")
                self.debugger.warning('RETRY', f"下载异常重试 {retry_count}/{max_retries}: {title} - {e}")
                
                # 清理临时文件
                try:
                    temp_mgr.cleanup_single_temp(temp_file)
                except Exception as cleanup_e:
                    logger.warning(f"清理临时文件失败: {cleanup_e}")
                
                if retry_count < max_retries:
                    await asyncio.sleep(5)
                    continue
                
                # 重试耗尽
                download_result = {'status': 'failed', 'url': url, 'title': title, 'error': str(e)}
        
        if download_result:
            # 记录失败状态到缓存
            if download_result.get('status') == 'failed':
                error_code = download_result.get('error_code')
                error_message = download_result.get('error')
                self.cache.add_failed_download(
                    url=url,
                    title=title,
                    module='porn',
                    model_name=model_name,
                    error_code=error_code,
                    error_message=error_message
                )
            return download_result
        
        return {'status': 'failed', 'url': url, 'title': title, 'error': '未知错误'}
    
    async def _resolve_engine_url(self, url: str) -> str:
        """把"需要兜底解析"的站点 URL 换成直链，其余**恒等返回**。

        2026-09-20 新增（多站点接入）。判定与解析全部委托给
        ``modules.sites.service.SiteService``，本方法只负责"接进来"：

        - **PornHub**：恒等返回。原有下载链路一行都不改。
        - yt-dlp 有 extractor 的站点（xvideos / xnspankbang 等 9 站）：
          恒等返回，直接交给现有 yt-dlp 链路。
        - yt-dlp **没有** extractor 的站点（porntrex / xfreehd / thumbzilla）：
          先用 ``unofficial-api-for-*`` 解析出直链再返回。

        任何异常都被吞掉并返回原 URL —— 多站点扩展绝不能影响原有下载。
        """
        try:
            from ..sites.service import get_site_service
        except Exception as exc:  # pragma: no cover - 包缺失时不致命
            logger.debug(f"多站点模块不可用，跳过: {exc}")
            return url

        try:
            proxy = self.proxy_manager.get_current_proxy()
            if not proxy:
                proxy = self._load_proxy_from_config_file()
            service = get_site_service(proxy=proxy)
            return await service.engine_url(url, proxy=proxy)
        except Exception as exc:
            logger.debug(f"多站点直链解析跳过（不影响原有流程）: {exc}")
            return url

    async def _do_download(self, url, title, model_name, temp_file, temp_mgr,
                      progress_callback=None,
                      concurrent_fragments='3', http_chunk_size='10M'):
        """执行实际的下载操作"""
        # 根据配置的引擎选择下载方式
        engine = self.config.get('engine', 'ytdlp')
        logger.info(f"使用下载引擎: {engine}")
        
        if engine in ('m3u8', 'direct'):
            # 使用多引擎下载器
            return await self._do_download_multi_engine(url, title, model_name, temp_file, temp_mgr, progress_callback, engine)
        elif engine == 'auto':
            # 自动选择：先尝试 yt-dlp，失败则尝试其他引擎
            try:
                return await self._do_download_yt_dlp(url, title, model_name, temp_file, temp_mgr,
                                                   progress_callback,
                                                   concurrent_fragments=concurrent_fragments,
                                                   http_chunk_size=http_chunk_size)
            except Exception as e:
                logger.warning(f"yt-dlp 下载失败，尝试多引擎: {e}")
                return await self._do_download_multi_engine(url, title, model_name, temp_file, temp_mgr, progress_callback, None)
        else:
            # 默认使用 yt-dlp
            return await self._do_download_yt_dlp(url, title, model_name, temp_file, temp_mgr,
                                               progress_callback,
                                               concurrent_fragments=concurrent_fragments,
                                               http_chunk_size=http_chunk_size)
    
    async def _do_download_multi_engine(self, url, title, model_name, temp_file, temp_mgr, progress_callback=None, preferred_engine=None):
        """使用多引擎下载"""
        try:
            from .downloader.video_downloader import VideoDownloader
            
            # 创建下载器配置
            # auto fallback 时 preferred_engine=None，让多引擎内部自动轮换
            engine = preferred_engine or 'auto'
            # 【2026-09-28 第二批】同样要按任务固定出口：多引擎里包含
            # download_video_native（PornHub 原生下载），它也会去取媒体直链，
            # 而直链里带 ?ip=<出口IP>。若这里仍用 10808（balancer，逐连接换
            # 出口），分片照样会被 CDN 判 474。
            _task_proxy = (self.proxy_manager.get_download_proxy()
                           or self.config.get('proxy'))
            download_config = {
                'proxy': _task_proxy,
                'timeout': 300,
                'retry_times': 3,
                'engine': engine,
                'fallback': True,
            }
            
            downloader = VideoDownloader(download_config)
            downloader._progress_callback = progress_callback
            
            # 执行下载
            result = await downloader.download_async(url, temp_file)
            
            if result:
                logger.info(f"多引擎下载完成: {title}")
                return {'status': 'completed', 'url': url, 'title': title, 'file_path': temp_file}
            else:
                raise Exception("多引擎下载失败")
                
        except Exception as e:
            logger.error(f"多引擎下载失败: {e}")
            raise Exception(f"多引擎下载失败: {e}")
    
    async def _do_download_yt_dlp(self, url, title, model_name, temp_file, temp_mgr,
                                 progress_callback=None,
                                 concurrent_fragments='3', http_chunk_size='10M'):
        """使用 yt-dlp 执行实际的下载操作"""
        yt_dlp = self._get_downloader()
        
        # 记录上一次心跳时间
        last_heartbeat_time = 0
        heartbeat_interval = 300  # 每5分钟更新一次心跳
        
        # 进度停滞检测
        last_progress = 0
        last_progress_time = time.time()
        progress_stalled_threshold = 180  # 3分钟无进度更新视为停滞
        download_stalled = False
        
        def _progress_hook(d):
            nonlocal last_heartbeat_time
            nonlocal last_progress
            nonlocal last_progress_time
            nonlocal download_stalled
            
            try:
                current_time = time.time()
                
                if d['status'] == 'downloading':
                    total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    downloaded_bytes = d.get('downloaded_bytes', 0)
                    speed = d.get('speed', 0)
                    eta = d.get('eta', 0)
                    
                    if total_bytes > 0:
                        progress = downloaded_bytes / total_bytes
                    else:
                        progress = 0
                    
                    if progress_callback:
                        try:
                            progress_callback(progress, speed, eta)
                        except Exception as e:
                            logger.warning(f"进度回调执行失败: {e}")
                    
                    speed_mb = (speed / (1024 * 1024)) if speed is not None else 0
                    total_mb = (total_bytes / (1024 * 1024)) if total_bytes is not None else 0
                    downloaded_mb = (downloaded_bytes / (1024 * 1024)) if downloaded_bytes is not None else 0
                    
                    if total_bytes > 0:
                        logger.info(f"下载进度: {progress*100:.1f}% | {downloaded_mb:.1f}/{total_mb:.1f}MB | 速度: {speed_mb:.1f}MB/s | 剩余: {eta}s")
                        self.debugger.log_download_progress(url, progress, speed, eta)
                    elif speed > 0:
                        logger.info(f"下载中: {downloaded_mb:.1f}MB | 速度: {speed_mb:.1f}MB/s | 剩余: {eta}s")
                        self.debugger.debug('PROGRESS', f"下载中: {downloaded_mb:.1f}MB | 速度: {speed_mb:.1f}MB/s | 剩余: {eta}s")
                    
                    # 进度停滞检测
                    if progress > last_progress + 0.001:  # 进度有明显变化（0.1%）
                        last_progress = progress
                        last_progress_time = current_time
                    else:
                        # 检查是否停滞
                        if current_time - last_progress_time > progress_stalled_threshold:
                            logger.error(f"下载进度停滞！URL: {url[:50]}... 当前进度: {progress*100:.1f}%, 停滞时间: {(current_time - last_progress_time)/60:.1f} 分钟")
                            self.debugger.error('STALLED', f"下载进度停滞: {title} - {progress*100:.1f}%")
                            download_stalled = True
                    
                    # 定期更新心跳
                    if current_time - last_heartbeat_time > heartbeat_interval:
                        try:
                            temp_mgr.update_download_progress(url, temp_file, progress)
                            last_heartbeat_time = current_time
                            logger.debug(f"更新下载心跳: {title}")
                            self.debugger.debug('HEARTBEAT', f"更新下载心跳: {title} - {progress*100:.1f}%")
                        except Exception as e:
                            logger.debug(f"更新心跳失败: {e}")
                            self.debugger.warning('HEARTBEAT', f"更新心跳失败: {e}")
                    
                    # 如果检测到停滞，抛出异常终止下载
                    if download_stalled:
                        raise Exception(f"下载进度停滞超过 {progress_stalled_threshold/60} 分钟")
                
                elif d['status'] == 'finished':
                    total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                    total_mb = total_bytes / (1024 * 1024) if total_bytes else 0
                    elapsed = d.get('elapsed', 0)
                    speed = d.get('speed', 0)
                    speed_mb = speed / (1024 * 1024) if speed else 0
                    logger.info(f"下载完成: {total_mb:.1f}MB | 平均速度: {speed_mb:.1f}MB/s | 耗时: {elapsed:.1f}s")
                    self.debugger.info('DOWNLOAD', f"下载完成: {total_mb:.1f}MB | 平均速度: {speed_mb:.1f}MB/s | 耗时: {elapsed:.1f}s")
                    self.debugger.update_download_state("下载完成")
                    
                    if progress_callback:
                        try:
                            progress_callback(1.0, 0, 0)
                        except Exception as e:
                            logger.warning(f"进度回调执行失败: {e}")
            except Exception as e:
                logger.warning(f"yt-dlp进度钩子异常: {e}")
                self.debugger.error('HOOK', f"yt-dlp进度钩子异常: {e}", exc_info=True)
        
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': temp_file,
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'progress_hooks': [_progress_hook],
            'socket_timeout': 30,
            'connect_timeout': 15,
            'read_timeout': 60,
            'extractor_retries': 5,
            'fragment_retries': 5,
            'file_access_retries': 3,
            'noprogress': True,
            'retries': 10,
            # 关键修复: 模拟 Chrome 浏览器绕过 PornHub 410/403 错误
            # 参考: https://github.com/yt-dlp/yt-dlp/issues/16729
            #
            # ⚠️ 2026-09-20 修复：此处**不要**再写 'impersonate': True / 'chrome'。
            # 库模式下 YoutubeDL.__init__ 会执行
            #     assert isinstance(target, ImpersonateTarget)
            # 传 True 或字符串会直接 AssertionError；而它的 str() 是空串，
            # 日志里只剩「错误输出为空」，根本看不出问题在哪。
            # 正确写法：由下方 apply_impersonate() 转成对象后再写入（转不了就不写）。
        }

        # 把 impersonate 以「ImpersonateTarget 对象」形态写入 ydl_opts。
        # 这一步是库模式能否真正跑起来的关键——原写法会让每次下载都在
        # 构造 YoutubeDL 时就失败（且报错信息为空）。
        apply_impersonate(ydl_opts, 'chrome')
        
        # ===== 多代理轮换：获取当前代理 =====
        # 【2026-09-28 第二批】下载改走「节点专属入口」：按任务固定一个出口。
        # 原实现用 get_current_proxy()（恒为 10808）——在负载均衡下 10808 是
        # 逐连接轮换的，而 PornHub 媒体直链把授权出口 IP 写进了 URL，分片落到
        # 另一个节点就会被 CDN 判为 HTTP 474。专属入口下整条下载链路出口一致。
        proxy = self.proxy_manager.get_download_proxy()
        if not proxy:
            proxy = self.proxy_manager.get_current_proxy()
        if not proxy:
            proxy = self._load_proxy_from_config_file()
            if proxy:
                logger.info(f"下载时回退加载代理: {proxy}")
        
        if proxy:
            ydl_opts['proxy'] = proxy
            logger.info(f"已设置代理: {proxy}")
        else:
            logger.warning("警告: 代理配置为空，将不使用代理")
        
        # ===== 多站点接入（2026-09-20）：只有"实际下载用的 URL"会被换成直链 =====
        # `url` 仍保持为页面地址，继续充当缓存/数据库主键——后面的
        # temp_mgr.complete_download(url) 依赖它来查下载记录，所以绝不能动。
        # PornHub 与 yt-dlp 支持的站点在这里恒等返回，行为与接入前逐字节一致。
        engine_url = await self._resolve_engine_url(url)

        try:
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._run_yt_dlp_download(
                        yt_dlp, ydl_opts, engine_url,
                        concurrent_fragments=concurrent_fragments,
                        http_chunk_size=http_chunk_size)
                )
            except Exception as e:
                if str(e) == "NO_EXECUTABLE":
                    # 未找到 yt-dlp 可执行文件，回退到 Python 库模式
                    logger.info("回退到 Python 库模式下载")
                    await loop.run_in_executor(
                        None,
                        lambda: self._run_yt_dlp_library(yt_dlp, ydl_opts, engine_url)
                    )
                else:
                    raise
            
            logger.info(f"yt-dlp下载完成，等待文件合并...")
            
            max_wait_seconds = 300
            check_interval = 2
            waited_seconds = 0
            
            while waited_seconds < max_wait_seconds:
                await asyncio.sleep(check_interval)
                waited_seconds += check_interval
                
                import glob
                temp_dir = os.path.dirname(temp_file)
                base_name = os.path.basename(temp_file)
                if '.' in base_name:
                    base_no_ext = base_name.rsplit('.', 1)[0]
                else:
                    base_no_ext = base_name
                
                part_files = (glob.glob(os.path.join(temp_dir, f"{base_no_ext}*.part")) + 
                              glob.glob(os.path.join(temp_dir, f"{base_no_ext}*.part-Frag*")) +
                              glob.glob(os.path.join(temp_dir, f"{base_no_ext}*.frag*")) +
                              glob.glob(os.path.join(temp_dir, f"{base_no_ext}*.ytdl")))
                
                if not part_files:
                    logger.info(f"分片文件已合并完成")
                    break
                
                logger.debug(f"等待分片文件合并... ({waited_seconds}/{max_wait_seconds}s, 剩余 {len(part_files)} 个分片文件)")
            
            logger.info(f"调用 complete_download 处理文件...")
            result = temp_mgr.complete_download(url)
            
            if not result.get('success'):
                logger.error(f"complete_download 失败: {result.get('error')}")
                temp_mgr.fail_download(url, result.get('error', 'unknown error'))
                temp_mgr.cleanup_single_temp(temp_file)
                temp_mgr.smart_cleanup(max_size_mb=2048, max_age_minutes=120)
                return {
                    'status': 'failed',
                    'url': url,
                    'title': title,
                    'error': result.get('error', 'complete_download failed'),
                }
            
            final_output_path = result.get('target_file')
            logger.info(f"文件移动成功: {final_output_path}")
            self.debugger.info('DOWNLOAD', f"文件移动成功: {final_output_path}")
            
            # 报告代理下载成功
            # ⚠️ 必须用**本次任务实际使用的** proxy。原实现取
            # self.config['proxy']（固定 10808），于是专属入口的失败/成功
            # 全部记到了 10808 头上，per-entry 的失败计数永远为 0 ⇒
            # 「坏节点自动让出流量」这条链根本不会触发。
            current_proxy = proxy
            if current_proxy:
                self.proxy_manager.report_success(current_proxy)
        
        except Exception as e:
            error_msg = str(e)
            if '停滞' in error_msg:
                logger.error(f"下载停滞: {url} - {error_msg}")
                
                # ===== 报告失败，必要时限流切换节点 =====
                current_proxy = proxy
                if current_proxy:
                    # 停滞是链路层面的症状（长时间没有任何进度），算作网络类失败；
                    # 用户可见的成因可能是站点限速，所以后面仍要过探测/阈值/冷却三道闸门。
                    self.proxy_manager.report_failure(current_proxy, affects_proxy=True)
                    self._try_switch_node('下载进度停滞', affects_proxy=True)
                # ===== 切换完成 =====
                
                return {
                    'status': 'failed',
                    'url': url,
                    'title': title,
                    'error': f"下载进度停滞: {error_msg}",
                    # 2026-09-20：停滞是链路层症状，交回上层按“可重试”处理
                    'retryable': True,
                    'permanent': False,
                    'error_code': 'STALLED',
                }
            
            logger.error(f"下载失败: {e}")
            self.debugger.error('DOWNLOAD', f"下载失败: {e}", exc_info=True)
            self._on_download_failed(model_name, url, title, str(e))
            
            # 报告代理失败，必要时限流切换节点
            # （同样用本次任务实际使用的 proxy，理由见上面成功分支的注释）
            current_proxy = proxy
            # ===== 失败分类：**必须**在 if current_proxy 之外算 =====
            # 2026-09-20 修复：原先 cls 只在 `if current_proxy:` 分支内赋值，
            # 而下面的 return 在分支之外 —— 未配置代理时异常处理器**自身**
            # 会抛 NameError，把真实下载错误彻底盖掉。
            diagnostic = get_error_diagnostic(e)
            cls = self._classify_failure(
                diagnostic, exit_code=getattr(e, 'exit_code', None))
            logger.info(
                f"失败分类: {cls}"
                + (f" | 命中片段={cls.matched!r}" if cls.matched else '')
            )

            # 报告代理失败，必要时限流切换节点
            if current_proxy:
                is_deleted = (cls.kind is ErrorKind.VIDEO_UNAVAILABLE)
                self.proxy_manager.report_failure(
                    current_proxy,
                    is_video_deleted=is_deleted,
                    affects_proxy=cls.affects_proxy,
                )
                self._try_switch_node(
                    f'下载失败[{cls.kind.value}]',
                    is_video_deleted=is_deleted,
                    affects_proxy=cls.affects_proxy,
                )
            
            temp_mgr.fail_download(url, str(e))
            temp_mgr.cleanup_single_temp(temp_file)
            temp_mgr.smart_cleanup(max_size_mb=2048, max_age_minutes=120)
            return {
                'status': 'failed',
                'url': url,
                'title': title,
                'error': str(e),
                # 2026-09-20：把分类结论带回上层供重试策略消费。
                # 此前 retryable 只被写进日志，没有任何代码读它。
                'retryable': cls.retryable,
                'permanent': cls.permanent,
                'error_code': cls.kind.value,
            }
        
        self.cache.add_download(
            url=url,
            title=title,
            module='porn',
            model_name=model_name,
            file_path=final_output_path
        )
        
        logger.info(f"下载完成: {title}")
        logger.debug(f"保存URL到缓存: {url}")
        self.debugger.log_download_complete(url, final_output_path)
        self.debugger.checkpoint('下载完成', f'文件: {final_output_path}')

        self._on_download_completed(model_name, url, title, final_output_path)

        # 定期清理资源
        self._download_count += 1
        if self._download_count % 20 == 0:
            logger.info(f"已下载 {self._download_count} 个视频，执行资源清理")
            try:
                import gc
                gc.collect()
                temp_mgr.smart_cleanup(max_size_mb=2048, max_age_minutes=120)
            except Exception as e:
                logger.warning(f"定期清理失败: {e}")

        return {
            'status': 'completed',
            'url': url,
            'title': title,
            'file_path': final_output_path,
        }
    
    async def download_missing(
        self,
        model_id: int,
        max_count: int = None,
        progress_callback=None
    ) -> List[Dict]:
        """
        下载模特的缺失视频
        
        Args:
            model_id: 模特ID
            max_count: 最大下载数量
            progress_callback: 进度回调
        
        Returns:
            下载结果列表
        """
        self.debugger.checkpoint('开始批量下载', f'模特ID: {model_id}')
        
        model = self.db.get_model(model_id)
        if not model:
            self.debugger.warning('DOWNLOAD', f'模特不存在: ID={model_id}')
            return []
        
        # 获取缺失视频
        missing_videos = self.db.get_missing_videos(model_id)
        
        if max_count:
            missing_videos = missing_videos[:max_count]
        
        self.debugger.info('DOWNLOAD', f'开始下载 {len(missing_videos)} 个缺失视频')
        
        results = []
        
        for i, video in enumerate(missing_videos):
            if progress_callback:
                progress_callback(i + 1, len(missing_videos), video['title'])
            
            self.debugger.checkpoint('开始下载视频', f'[{i+1}/{len(missing_videos)}] {video["title"][:30]}...')
            
            result = await self.download_video(
                url=video['url'],
                model_name=model['name'],
                title=video['title'],
                output_dir=model.get('local_directory'),
                model_id=model_id  # ===== 关键修复：传入 model_id =====
            )
            
            # 更新视频状态
            if result['status'] == 'completed':
                self.db.update_video_status(
                    video['id'],
                    status='downloaded',
                    local_path=result.get('file_path')
                )
                self.debugger.info('DOWNLOAD', f'视频下载成功: {video["title"][:30]}...')
            elif result.get('status') == 'skipped':
                # 跳过（文件已存在或已下载），更新数据库状态
                file_path = result.get('file_path')
                self.db.update_video_status(
                    video['id'],
                    status='downloaded',
                    local_path=file_path
                )
                self.debugger.info('DOWNLOAD', f'视频已存在，更新状态: {video["title"][:30]}...')
            elif result.get('status') == 'failed' and '永久失败' in result.get('error', ''):
                # 永久失败的视频，标记为failed状态，避免下次再次尝试
                self.db.update_video_status(video['id'], status='failed')
                self.debugger.warning('DOWNLOAD', f'视频永久失败: {video["title"][:30]}... - {result.get("error", "未知错误")}')
            else:
                self.debugger.warning('DOWNLOAD', f'视频下载失败: {video["title"][:30]}... - {result.get("error", "未知错误")}')
            
            results.append(result)
        
        # 更新模特统计（包含已完成和已跳过的）
        downloaded_count = sum(1 for r in results if r['status'] in ('completed', 'skipped'))
        self.db.update_model_stats(
            model_id,
            downloaded_count=model.get('downloaded_count', 0) + downloaded_count
        )
        
        self.debugger.checkpoint('批量下载完成', f'成功: {downloaded_count}/{len(missing_videos)}')
        
        return results
    
    # ==================== 统计 ====================
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        db_stats = self.db.get_stats()
        cache_stats = self.cache.get_stats('porn')
        
        return {
            **db_stats,
            'total_downloaded': cache_stats['total_downloads'],
            'total_size': cache_stats['total_size_gb'],
        }
    
    def _init_scheduler(self):
        """初始化定时任务"""
        try:
            from .utils.scheduler import get_scheduler
            
            scheduler = get_scheduler()
            # 添加定时全模特检测任务
            scheduler.add_daily_task(
                self.scheduled_hour, 
                self.scheduled_minute, 
                0, 
                self._scheduled_check_all_models
            )
            
            # 启动定时任务管理器
            scheduler.start()
            logger.info(f"定时任务已设置: 每天 {self.scheduled_hour:02d}:{self.scheduled_minute:02d} 执行全模特检测")
            if self.auto_download_enabled:
                logger.info("自动下载功能已启用")
        except Exception as e:
            logger.error(f"初始化定时任务失败: {e}")
    
    def update_scheduler_config(self, hour: int, minute: int, auto_download: bool):
        """更新定时任务配置
        
        Args:
            hour: 执行小时
            minute: 执行分钟
            auto_download: 是否自动下载
        """
        self.scheduled_hour = hour
        self.scheduled_minute = minute
        self.auto_download_enabled = auto_download
        
        # 更新配置字典
        self.scheduler_config = {
            'hour': hour,
            'minute': minute,
            'auto_download': auto_download
        }
        self.config['scheduler'] = self.scheduler_config
        
        # 保存配置到应用配置
        self._save_config()
        
        # 重新初始化调度器
        try:
            from .utils.scheduler import get_scheduler
            scheduler = get_scheduler()
            scheduler.stop()
            scheduler.tasks.clear()
            scheduler.add_daily_task(hour, minute, 0, self._scheduled_check_all_models)
            scheduler.start()
            logger.info(f"定时任务已更新: 每天 {hour:02d}:{minute:02d} 执行, 自动下载: {'开启' if auto_download else '关闭'}")
        except Exception as e:
            logger.error(f"更新定时任务失败: {e}")
    
    def set_concurrent_downloads(self, concurrent: int):
        """设置并发下载数量
        
        Args:
            concurrent: 并发下载数量
        """
        if concurrent < 1:
            concurrent = 1
        elif concurrent > 10:
            concurrent = 10
        
        self.max_concurrent = concurrent
        # 更新配置字典
        self.config['max_concurrent'] = concurrent
        
        # 保存配置到应用配置
        self._save_config()
        
        logger.info(f"并发下载数量已设置为: {concurrent} 个")
    
    def _save_config(self):
        """保存配置到应用配置"""
        try:
            # 直接使用set方法保存PORN模块配置
            self.app_config.set('porn', self.config)
            logger.info("PORN模块配置已保存")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
    
    def _classify_failure(self, diagnostic_text: str,
                          exit_code: Optional[int] = None):
        """对失败做语义分类。

        实现见 ``modules.core.net.error_classifier``。返回对象上的
        ``affects_proxy`` / ``retryable`` / ``permanent`` 三个决策位，
        是整个"该不该换代理"判断的唯一依据。
        """
        return classify_error(diagnostic_text, exit_code=exit_code)

    def _is_video_unavailable(self, diagnostic_text: str) -> bool:
        """判断失败是否由视频本身不可用导致（而非代理/网络问题）

        必须传入完整诊断文本（见 get_error_diagnostic），否则关键词可能落在
        被截断的部分之外，导致「视频已删除」被误判为代理故障并触发节点切换。

        .. deprecated:: 2026-09-20 第二批修复
            已改为 :class:`modules.core.net.error_classifier.ErrorClassification`
            语义分类的薄封装。新代码请直接用 :meth:`_classify_failure`，
            因为它还能回答"该不该换代理"这个问题。
        """
        return self._classify_failure(diagnostic_text).kind is ErrorKind.VIDEO_UNAVAILABLE

    def _is_retryable_failure(self, download_result: dict) -> bool:
        """判断一次失败是否值得重试。

        2026-09-20 新增。此前 ``download_video`` **只对「停滞」重试**：
        ``_do_download`` 返回 ``status='failed'``（yt-dlp 非零退出 —— 网络故障
        走的正是这条路）时直接 ``break`` 放弃。而 ``error_classifier`` 早已算出
        ``retryable=True``，却没有任何代码消费它（全项目 grep 只命中分类器自身
        与一句 docstring）。实测后果：571 个视频的队列成功率仅 28%，
        约七成视频被静默丢弃。

        判定优先级：

        1. ``permanent``（视频已删除 / 已下架 / 410 Gone）→ **永不重试**；
        2. 分类器给出 ``retryable`` → 以它为准；
        3. 没有分类结论（本地合并失败等）→ 退回历史行为，仅当错误信息含
           「停滞」时才重试。
        """
        if download_result.get('permanent'):
            return False
        retryable = download_result.get('retryable')
        if retryable is not None:
            return bool(retryable)
        return '停滞' in (download_result.get('error') or '')

    def _has_alternative_target(self) -> bool:
        """是否真的存在"可切换的目标"

        单代理 + 单节点的场景下，"切换"没有任何收益：代理 URL 不变、出口 IP
        不变，却要白白重启一次 Xray 并把链路打断几秒。原报告 P1-4 要求
        "单代理时短路"，这里实现为更精确的版本——**代理和节点都没有备选**
        才短路。

        之所以不照抄"单代理就不切"：本地 v2rayN 库中实际有 2 个节点
        （见 v2rayN/guiConfigs/guiNDB.db），节点切换会更换出口 IP，
        对"站点封 IP"这种场景是真实有效的，不能一并放弃。
        """
        try:
            enabled_proxies = int(self.proxy_manager.get_proxy_count().get('enabled', 0))
        except Exception:
            enabled_proxies = 1
        try:
            nodes = int(self.v2ray_controller.get_status().get('total_nodes', 0) or 0)
        except Exception:
            nodes = 0
        return not (enabled_proxies <= 1 and nodes < 2)

    def _proxy_looks_healthy(self) -> bool:
        """换节点前的最后一道确认：当前代理本身还通不通

        Returns:
            True 表示**探测到代理正常**，此时失败不应归咎于代理，
            应当跳过本次切换。

        实现说明：探测失败（抛异常 / 拿不到锁）一律返回 False，
        即"无法证伪就允许切换"——探测模块自身出问题不应该导致
        彻底失去切换能力。
        """
        proxy_url = self.config.get('proxy')
        if not proxy_url:
            return False
        if not _PROBE_GATE_LOCK.acquire(blocking=False):
            logger.debug("已有线程在探测代理，跳过本次探测")
            return False
        try:
            # 探测函数可注入（单测用），默认走带缓存的真实探测
            probe_fn = getattr(self, '_proxy_probe_fn', None) or proxy_probe.probe_proxy_cached
            res = probe_fn(
                proxy_url,
                timeout=getattr(self, '_proxy_probe_timeout', 4.0),
                cache_ttl=getattr(self, '_proxy_probe_cache_ttl', 15.0),
            )
        except Exception as e:
            logger.debug(f"代理探测异常，视为无法证伪: {e}")
            return False
        finally:
            _PROBE_GATE_LOCK.release()

        if res.ok:
            logger.info(
                f"代理探测正常（{res.latency_ms:.0f}ms / {res.target} / HTTP {res.status_code}），"
                f"判定本次失败与代理无关"
            )
            return True

        logger.info(f"代理探测失败（{res.error}），确认为代理侧问题，允许切换节点")
        return False

    def _try_switch_node(self, reason: str, is_video_deleted: bool = False,
                         affects_proxy: Optional[bool] = None) -> bool:
        """在满足限流条件时切换 Xray 节点（防止失败雪崩）

        2026-09-20 事故修复：原实现「任一失败即切换节点」，且切换时会全局杀掉
        所有 yt-dlp 进程；多个并发任务互相触发，13 秒内切换 9 次节点，代理服务
        反复重启、网络持续中断，全天 1144 次失败中 90% 集中在两小时内。

        现在必须**依次**满足以下条件才允许切换：

          1. ``affects_proxy`` 为真——错误语义上确实与代理有关
             （视频已删除 / 反爬拦截 / 站点 5xx / 磁盘错误 / 未识别错误都不算）；
          2. 存在备选目标（代理或节点至少有一个备选），否则切换是纯空转；
          3. 代理连续失败次数达到阈值（默认 3 次）；
          4. 距上次切换已超过冷却时间（默认 60 秒）；
          5. 探测确认当前代理**确实不通**（探测说正常则不切，见
             ``_proxy_veto_limit`` 的兜底说明）；
          6. 抢到全局切换锁（同一时刻只允许一个线程切换）。

        Args:
            reason: 触发原因，仅用于日志
            is_video_deleted: 兼容旧调用方；``affects_proxy`` 为 None 时用
                ``not is_video_deleted`` 推导
            affects_proxy: 该失败是否应算在代理头上，默认由分类器给出

        Returns:
            是否真的执行了切换
        """
        if affects_proxy is None:
            affects_proxy = not is_video_deleted

        # ---- 闸门 1：语义上是否与代理有关 ----
        if not affects_proxy:
            logger.debug(
                f"跳过节点切换（该错误不影响代理，"
                f"{'视频不可用' if is_video_deleted else '与代理无关'}）: {reason}"
            )
            return False

        if self.v2ray_controller is None:
            logger.debug("Xray 控制器不可用，跳过节点切换")
            return False

        # ---- 闸门 2：存在备选才值得切 ----
        if not self._has_alternative_target():
            logger.info(f"无备选代理且无可切换节点，切换无收益，跳过（{reason}）")
            return False

        # ---- 闸门 3：阈值 ----
        failures = self.proxy_manager.get_consecutive_failures()
        if failures < self._switch_failure_threshold:
            logger.info(
                f"连续失败 {failures}/{self._switch_failure_threshold} 次，"
                f"暂不切换节点（{reason}）"
            )
            return False

        # ---- 闸门 4：冷却 ----
        now = time.time()
        elapsed = now - self._last_switch_time
        if elapsed < self._min_switch_interval:
            logger.info(
                f"距上次切换仅 {elapsed:.0f}s（冷却 {self._min_switch_interval:.0f}s），"
                f"跳过本次节点切换（{reason}）"
            )
            return False

        # ---- 闸门 5：探测确认 ----
        # 探测说"代理正常"时默认不切。但探测只能证明"代理能出网"，
        # 证明不了"目标站没封我们的 IP"，所以同一代理连续被否决
        # _proxy_veto_limit 次之后，改由切换兜底，避免探测变成新的单点误判。
        if self._proxy_looks_healthy():
            self._probe_veto_count += 1
            if self._probe_veto_count < self._probe_veto_limit:
                logger.info(
                    f"代理正常，判为站点侧问题，跳过切换"
                    f"（第 {self._probe_veto_count}/{self._probe_veto_limit} 次否决，{reason}）"
                )
                return False
            logger.warning(
                f"代理已连续 {self._probe_veto_count} 次探测正常，但下载持续失败，"
                f"疑似站点侧 IP 封禁，改由节点切换兜底（{reason}）"
            )
        else:
            self._probe_veto_count = 0

        # ---- 闸门 6：全局互斥 ----
        if not _NODE_SWITCH_LOCK.acquire(blocking=False):
            logger.info(f"已有线程正在切换节点，跳过本次切换（{reason}）")
            return False

        try:
            # 先记录时间：即便切换失败也计入冷却，避免失败风暴下反复重试
            self._last_switch_time = time.time()

            new_proxy = self.proxy_manager.get_next_proxy()
            if new_proxy:
                self.config['proxy'] = new_proxy
                logger.info(f"{reason}，切换到代理: {new_proxy}")

            ok = self.v2ray_controller.switch_to_next_node()
            if ok:
                self.proxy_manager.reset_consecutive_failures()
                self._probe_veto_count = 0
                # 出口 IP 变了，旧的探测结论立即失效
                proxy_probe.invalidate_cache()
                logger.info(f"节点切换成功（{reason}），已重置连续失败计数与探测缓存")
            else:
                logger.warning(f"节点切换未生效（{reason}）")
            return bool(ok)
        except Exception as ve:
            logger.warning(f"切换 Xray 节点失败: {ve}")
            return False
        finally:
            _NODE_SWITCH_LOCK.release()

    async def _scheduled_check_all_models(self):
        """定时执行的全模特检测任务"""
        # ⭐ 2026-09-22 防重入（第二道防线）：
        # 定时器层（modules/core/utils/scheduler.py）已按任务名拦截「同一任务并发起两轮」，
        # 这里再加一道，防止将来出现第二个调用方时又跑成两轮。
        # 实测背景：09-22 服务端单日把 759 个模特检测跑了 2 遍（1518 次、4.4 小时、
        # 抓取量翻倍，且与下载抢同一个 socks5 代理）。
        if getattr(self, '_scheduled_check_running', False):
            logger.warning("定时全模特检测已在执行中，跳过本次触发")
            return
        self._scheduled_check_running = True

        logger.info("========== 开始定时全模特检测 ==========")

        try:
            results = await self.check_all_models()

            # 如果启用了自动下载，则下载缺失视频
            if self.auto_download_enabled:
                logger.info("自动下载功能已启用，开始下载缺失视频...")
                await self._auto_download_missing_videos(results)

            logger.info("========== 定时任务完成 ==========")
        except Exception as e:
            logger.error(f"定时全模特检测失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
        finally:
            # ⭐ 必须复位：否则一次异常会让这道防线永久锁死
            self._scheduled_check_running = False
    
    async def _auto_download_missing_videos(self, check_results: List[Dict]):
        """自动下载缺失视频
        
        Args:
            check_results: 检测结果列表
        """
        import asyncio
        
        total_missing = 0
        for result in check_results:
            total_missing += result.get('missing_count', 0)
        
        if total_missing == 0:
            logger.info("没有缺失视频，跳过下载")
            return
        
        logger.info(f"发现 {total_missing} 个缺失视频，开始下载...")
        
        # 收集所有缺失视频
        all_missing_videos = []
        for result in check_results:
            model_name = result.get('model_name', '')
            model_id = result.get('model_id', 0)
            missing_videos = result.get('missing_videos', [])
            
            for video in missing_videos:
                all_missing_videos.append({
                    'model_id': model_id,
                    'model_name': model_name,
                    'video': video
                })
        
        if not all_missing_videos:
            return
        
        # 并行下载（使用信号量控制并发数）
        semaphore = asyncio.Semaphore(self.max_concurrent)
        downloaded_count = 0
        
        async def download_with_semaphore(video_info):
            nonlocal downloaded_count
            async with semaphore:
                try:
                    video = video_info['video']
                    model_name = video_info['model_name']
                    model_id = video_info['model_id']
                    
                    result = await self.download_video(
                        url=video.get('url', ''),
                        model_name=model_name,
                        title=video.get('title', ''),
                        model_id=model_id  # ===== 关键修复：传入 model_id =====
                    )
                    
                    if result.get('status') == 'completed':
                        downloaded_count += 1
                        # 更新视频状态
                        self.db.update_video_status(
                            video.get('id'),
                            status='downloaded',
                            local_path=result.get('file_path')
                        )
                        logger.info(f"下载成功: {video.get('title', '')[:30]}")
                    elif result.get('status') == 'skipped':
                        logger.info(f"已下载，跳过: {video.get('title', '')[:30]}")
                    
                except Exception as e:
                    logger.error(f"下载失败: {video_info['video'].get('title', '')} - {e}")
        
        # 创建下载任务
        tasks = [download_with_semaphore(video_info) for video_info in all_missing_videos]
        
        # 等待所有任务完成
        await asyncio.gather(*tasks, return_exceptions=True)
        
        logger.info(f"自动下载完成: 成功 {downloaded_count}/{len(all_missing_videos)} 个视频")
    
    def _rescue_locked_part(self, output_path: str, error_output: str = "",
                            max_retries: int = 10, delay: float = 2.0) -> bool:
        r"""抢救被 Windows 文件锁阻断的最终改名（.part -> 最终文件）

        yt-dlp 的 native HLS 下载流程::

            下载分片 xxx.part-FragN -> 合并写入 xxx.mp4.part -> rename 成 xxx.mp4

        最后一步 rename 会被**瞬时文件锁**阻断 —— 未及时释放句柄的并发分片
        线程、杀软实时扫描、Windows 索引服务都可能持锁，报::

            [WinError 32] 另一个程序正在使用此文件，进程无法访问。
            '...\xxx.mp4.part' -> '...\xxx.mp4'

        yt-dlp 内部重试 3 次后放弃并返回退出码 1，**但此时 .part 已是完整成品**
        （分片全部下完并合并后才会走到 rename）。

        实测 2026-09-22 服务端 ``temp/downloads``：7 个 .mp4.part 里有 5 个
        **没有配套 .part-Frag* 分片**（最大 1348 MB）—— 即「已下完、只差改名」
        的成品被当成失败删除后重下。

        判据取「无分片残留」，比只看 ``.part`` 存在更严：有分片残留说明合并尚未
        完成，``.part`` 不完整，绝不能当成品改名（会得到一个损坏视频）。

        Returns:
            True 表示已成功改名为最终文件（调用方应视为下载成功）
        """
        if not output_path:
            return False

        part_path = f"{output_path}.part"
        if not os.path.exists(part_path):
            logger.debug(f"抢救改名跳过（无 .part）: {output_path}")
            return False

        # 有配套分片残留 -> 合并未完成，.part 不完整，拒绝改名
        import glob as _glob
        leftovers = []
        for pat in (f"{output_path}.part-Frag*", f"{output_path}-Frag*",
                    f"{output_path}.part.part-Frag*"):
            leftovers.extend(_glob.glob(pat))
        if leftovers:
            logger.warning(
                f"抢救改名放弃：仍有 {len(leftovers)} 个分片残留，"
                f".part 可能不完整: {os.path.basename(output_path)}")
            return False

        try:
            part_size = os.path.getsize(part_path)
        except OSError as e:
            logger.warning(f"抢救改名失败：读 .part 大小出错: {e}")
            return False
        if part_size <= 0:
            logger.warning(f"抢救改名放弃：.part 为空: {os.path.basename(part_path)}")
            return False

        for attempt in range(1, max_retries + 1):
            try:
                if os.path.exists(output_path):
                    # 目标已存在（前次残留）：先清掉，否则 Windows 上 rename 会失败
                    os.remove(output_path)
                os.rename(part_path, output_path)
                logger.info(
                    f"🔧 抢救改名成功(第{attempt}次): "
                    f"{os.path.basename(output_path)} ({part_size/1024/1024:.1f} MB)")
                # 顺手清掉 yt-dlp 的下载存档残留：--no-download-archive 下不会再产生，
                # 但历史残留会让上层「等待分片合并」的循环空转 300 秒
                for stale in _glob.glob(f"{output_path}.ytdl"):
                    try:
                        os.remove(stale)
                    except OSError:
                        logger.debug("清理 .ytdl 残留失败", exc_info=True)
                return True
            except OSError as e:
                if attempt < max_retries:
                    logger.debug(
                        f"抢救改名被占用，等待重试({attempt}/{max_retries}): "
                        f"{os.path.basename(part_path)} - {e}")
                    time.sleep(delay)
                else:
                    logger.warning(
                        f"抢救改名失败（文件仍被锁定）: "
                        f"{os.path.basename(part_path)} - {e}")
        return False

    def _run_yt_dlp_download(self, yt_dlp, ydl_opts, url,
                              concurrent_fragments='3', http_chunk_size='10M'):
        """
        执行 yt_dlp 下载（在 asyncio 线程池中运行）

        优先使用 yt-dlp 可执行文件，支持强制终止卡住的下载进程。
        如果找不到可执行文件，则回退到 Python 库模式。

        concurrent_fragments / http_chunk_size 可由上层按任务覆盖：
        撞 416（并发 range 被 CDN 拒绝）时，重试该任务会降级到 -N 1。
        """
        import time
        import os
        import gc
        import threading
        import subprocess
        import sys
        
        # 记录开始时间
        start_time = time.time()
        download_timed_out = False
        error_output = ""  # 必须在此初始化，否则异常处理会崩溃
        timeout_seconds = 900  # 15分钟超时（单个视频下载足够）
        warning_seconds = 60  # 1分钟无响应警告
        terminate_seconds = 120  # 2分钟无响应强制终止（原8分钟）
        
        logger.info(f"===== yt-dlp 下载开始 =====")
        logger.info(f"URL: {url[:50]}...")
        
        if hasattr(self, 'debugger'):
            self.debugger.info('DOWNLOAD_START', f"开始下载: {url[:50]}...")
        
        # 获取输出路径
        output_path = ydl_opts.get('outtmpl', '')
        logger.info(f"输出路径: {output_path[:100] if output_path else '未设置'}")
        
        # 确保输出目录存在
        if output_path:
            output_dir = os.path.dirname(output_path)
            if output_dir and not os.path.exists(output_dir):
                try:
                    os.makedirs(output_dir, exist_ok=True)
                    logger.info(f"创建输出目录: {output_dir}")
                except Exception as e:
                    logger.error(f"创建输出目录失败: {e}")
                    raise Exception(f"无法创建输出目录: {output_dir}")
        
        # 查找 yt-dlp 可执行文件
        yt_dlp_exe = self._find_yt_dlp_executable()
        
        # 构建命令行参数
        if yt_dlp_exe:
            cmd_args = [
                yt_dlp_exe,
                url,
                '-o', output_path,
                '--no-check-certificate',
                '--no-warnings',
                '--socket-timeout', '30',
                '--retries', '10',
                '--fragment-retries', '10',
                '--extractor-retries', '5',
                '--no-cache-dir',
                '--no-playlist',
                '--ignore-errors',
                '--skip-unavailable-fragments',
                '--no-download-archive',
                # ⚠️ 取值沿革（git 溯源）：无(默认1) → 4（05-17 4d3ff2d 引入）
                # → 3（09-20 dd63537）→ 1（09-22 08:32 8ac5198）→ 3（本次恢复）。
                # 【当初为何降到 1】多线程并发写分片时，各线程写完各自 rename
                # (.part-FragN.part -> .part-FragN)，句柄未及时释放会让随后
                # 的最终 rename (.mp4.part -> .mp4) 撞上 WinError 32
                # 《另一个程序正在使用此文件》。yt-dlp 上游 issue #170 的报错
                # 格式与此逐字一致，维护者定性为「未正常退出的实例持锁」。
                # 实测 2026-09-22 服务端单日 114 条 WinError 32，且 5 个已下完
                # 的成品 .mp4.part（最大 1348 MB）因此被判失败、删掉重下。
                # 【为何现在恢复 3】-N 1 的代价过大：实测单文件仅 27~109 KB/s
                # （3 并发合计 ~165 KB/s），而待下队列 4,389 个，按此要跑数月。
                # 且 B15 已补 _rescue_locked_part()（定义 2390 / 调用点 2713-2727），
                # 改名被锁时能就地抢救、不再判失败重下 ⇒ 交由抢救兜底，恢复并发。
                # ⚠️ 若 WinError 32 复发且抢救接不住 ⇒ 只回退本行到 '1'（单点回滚）。
                # ⚠️ 注意：yt-dlp 的 -N **默认值就是 1** ⇒ 「降到 1」= 彻底关掉分片并发。
                '--concurrent-fragments', concurrent_fragments,
                '--http-chunk-size', http_chunk_size,
                # ⚠️ 2026-09-20 修正：此处**不要**加 '--http1.1'。
                # yt-dlp 根本没有这个选项，加上会让进程直接以
                # "error: no such option: --http1.1" 退出（退出码 2），
                # 结果是 100% 下载失败——比它想修的问题严重得多。
                # socks5 下的 HTTP/2 RST 交给 --impersonate chrome（curl_cffi）
                # 处理；如需显式控制协议版本，只能改 curl_cffi 的 http_version，
                # 不是 yt-dlp 命令行参数。
                # 关键修复: 使用 --impersonate chrome 绕过 PornHub 410 错误。
                # 这是**全局** TLS 指纹开关（依赖 curl_cffi）；PornHub extractor
                # 内部本身已硬编码 impersonate=True（yt_dlp/extractor/pornhub.py
                # 的 _download_json(..., impersonate=True)），由这里提供具体指纹。
                '--impersonate', 'chrome',
                # ⚠️ 2026-09-20 移除 '--extractor-args', 'pornhub:impersonate=chrome'。
                # 已逐行核对 yt-dlp 官方 pornhub.py：该 extractor **不读取任何
                # configuration_arg**（全文无 _configuration_arg 调用），所以这条
                # 是彻头彻尾的 no-op，只会让人误以为它在起作用。真正生效的是上面的
                # --impersonate。yt-dlp 会静默忽略未知 extractor arg，故移除无风险。
            ]
            logger.info(f"使用 yt-dlp 可执行文件: {yt_dlp_exe}")
        else:
            # 在打包后的环境中，sys.executable 指向的是打包后的可执行文件，不能用于运行 Python 模块
            # 所以直接使用 yt_dlp 库的 API，而不是创建子进程
            logger.info("未找到 yt-dlp 可执行文件，将直接使用 Python 库 API")
            # 返回特殊标记，让调用者知道使用库模式
            raise Exception("NO_EXECUTABLE")

        proxy = ydl_opts.get('proxy')
        if proxy:
            cmd_args.extend(['--proxy', proxy])
            logger.info(f"使用代理: {proxy}")

        # 使用 --impersonate chrome 时不需要 cookies，impersonate 会自动处理浏览器指纹
        # 如果同时指定 --no-cookies 和 --cookies 会产生冲突
        
        fmt = ydl_opts.get('format', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best')
        cmd_args.extend(['-f', fmt])
        logger.info(f"格式选择: {fmt}")
        
        # 输出完整命令（用于调试）
        full_cmd = ' '.join(cmd_args)
        logger.debug(f"完整命令: {full_cmd[:200]}..." if len(full_cmd) > 200 else f"完整命令: {full_cmd}")
        
        process = None
        process_start_time = time.time()
        last_progress_time = time.time()  # 最后有进度的时间（watchdog 和主循环共享）
        last_warning_time = 0
        warned_count = 0
        
        def watchdog():
            nonlocal download_timed_out, last_warning_time, warned_count, last_progress_time
            while not download_timed_out:
                time.sleep(10)
                if process and process.poll() is None:
                    # 使用无进度时间，而不是总运行时间
                    stalled_time = time.time() - last_progress_time
                    
                    if stalled_time > warning_seconds and (time.time() - last_warning_time > 120):
                        warned_count += 1
                        last_warning_time = time.time()
                        logger.warning(f"下载长时间无响应 [{warned_count}]: {url[:50]}... 已持续 {stalled_time/60:.1f} 分钟")
                    
                    if stalled_time > terminate_seconds:
                        logger.error(f"下载停滞超过 {terminate_seconds/60:.0f} 分钟，强制终止进程: {url[:50]}...")
                        
                        download_timed_out = True
                        try:
                            process.terminate()
                            time.sleep(3)
                            if process.poll() is None:
                                logger.warning("terminate() 无效，尝试 kill()")
                                process.kill()
                                time.sleep(2)
                            logger.info(f"进程已强制终止")
                        except Exception as e:
                            logger.error(f"终止进程失败: {e}")
        
        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()
        
        try:
            # 输出完整的调试信息
            full_cmd = ' '.join(cmd_args)
            logger.info(f"=== 开始执行 yt-dlp ===")
            logger.info(f"命令: {full_cmd}")
            logger.info(f"输出路径: {output_path}")
            logger.info(f"使用代理: {proxy if proxy else '否'}")
            
            creationflags = 0x08000000 if sys.platform == 'win32' else 0
            
            try:
                process = subprocess.Popen(
                    cmd_args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # 将 stderr 合并到 stdout
                    stdin=subprocess.PIPE,
                    text=True,
                    creationflags=creationflags,
                    startupinfo=None if sys.platform != 'win32' else subprocess.STARTUPINFO()
                )
                logger.info(f"进程已创建，PID: {process.pid}")
            except Exception as create_e:
                logger.error(f"创建进程失败: {create_e}")
                raise Exception(f"创建进程失败: {create_e}")
            
            # ⭐ 2026-10-02 修复卡死：改用「后台读线程 + 队列」消费输出。
            # 原来主循环里 `line = process.stdout.readline()` 是**阻塞**调用 ——
            # yt-dlp 一旦静默（代理握手挂住 / CDN 无响应 / 管道写满对端不读），
            # readline() 会永久阻塞在那一行，循环体根本回不到顶部，
            # 于是 `if download_timed_out: break` 与 watchdog 的
            # `terminate()/kill()` 全部形同虚设：进程被杀掉了，但主线程仍卡在
            # readline 上（管道在进程被杀后才拿到 EOF，通常能解开；真正死锁场景是
            # 进程活着却永不写管道）。实测 2026-10-02 卡死现场：6 个 yt-dlp.exe
            # 存活、每个仅占 1~3 MB 内存、temp/downloads 里 3 个 .part 全是 0 字节、
            # 日志停在 14:15 后 1h47m 无任何输出 —— 典型的「进程活着但管道静默」。
            # 现在读取放到 daemon 线程，主循环用 queue.get(timeout=...) 轮询，
            # 保证 download_timed_out 一定能被看到。
            all_output_lines = []

            out_queue: "queue.Queue[str]" = queue.Queue()
            _read_done = threading.Event()

            def _pump_stdout():
                """后台线程：把 stdout 逐行塞进队列，EOF 后置位 _read_done"""
                try:
                    for raw in iter(process.stdout.readline, ''):
                        out_queue.put(raw)
                except Exception:
                    logger.debug("读取 yt-dlp stdout 线程异常", exc_info=True)
                finally:
                    _read_done.set()

            pump_thread = threading.Thread(target=_pump_stdout, daemon=True)
            pump_thread.start()

            while True:
                if download_timed_out:
                    break
                if process.poll() is not None and _read_done.is_set() and out_queue.empty():
                    break
                try:
                    # ⭐ 关键：0.5s 超时轮询，而非阻塞 readline
                    line = out_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if not line:
                    continue
                line_stripped = line.strip()
                if line_stripped:
                    all_output_lines.append(line_stripped)
                # 任何输出都说明进程在运行，更新最后进度时间
                now = time.time()
                last_progress_time = now
                # 检查是否是进度信息
                if '%' in line_stripped or 'ETA' in line_stripped or 'MB/s' in line_stripped or 'MiB/s' in line_stripped:
                    if now - last_progress_time > 5:
                        logger.info(f"[PROGRESS] {line_stripped[:100]}")
                elif ('Retrying fragment' in line_stripped
                      or 'Retrying in ' in line_stripped
                      or 'Sleeping ' in line_stripped):
                    # yt-dlp 对分片/整段的内部自动重试（例如 PornHub CDN 的 HTTP 472 限流），
                    # 属自愈行为：重试成功则下载正常完成。若记为 ERROR，单个视频可产生
                    # 数千条错误日志，把真正的失败彻底淹没（实测单日 8044/8434 条都是它）。
                    # 最终失败仍会由下面的「返回非零退出码 / 下载失败」以 ERROR 记录。
                    logger.debug(f"[YT-DLP RETRY] {line_stripped[:200]}")
                elif 'ERROR' in line_stripped.upper() or 'FAILED' in line_stripped.upper():
                    logger.error(f"[YT-DLP ERROR] {line_stripped[:200]}")
                elif 'WARNING' in line_stripped.upper():
                    logger.warning(f"[YT-DLP WARN] {line_stripped[:200]}")

            if download_timed_out:
                raise Exception(f"下载停滞超过 {terminate_seconds/60:.0f} 分钟，已强制终止")

            # ⭐ 2026-09-22 加固：进程退出后把管道里剩余的输出读完。
            # 原来的 `while process.poll() is None: readline()` 在进程一退出就
            # 结束循环，**缓冲区里尚未读走的行会被整体丢弃**。而 yt-dlp 失败时
            # "Unable to rename" 正好在输出的最后几行 —— 丢了下方的文件锁抢救
            # 分支就永远不会触发，已下完的成品仍会被当成失败删除重下。
            # 实测：用秒退的替身脚本复现，读循环只拿到第 1 行，其余全丢。
            # 2026-10-02：剩余输出改从队列取，避免与后台读线程争抢同一个 stdout。
            try:
                while True:
                    try:
                        _rest = out_queue.get_nowait()
                    except queue.Empty:
                        break
                    if _rest and _rest.strip():
                        all_output_lines.append(_rest.strip())
            except Exception:
                logger.debug("读取 yt-dlp 剩余输出失败", exc_info=True)
            
            # 合并所有输出行
            error_output = '\n'.join(all_output_lines)
            
            logger.info(f"进程退出，返回码: {process.returncode}")
            
            if process.returncode != 0:
                
                logger.error(f"yt-dlp 返回非零退出码: {process.returncode}")
                logger.error(f"错误输出: {error_output[:1500]}")
                
                # ⭐ 2026-09-22 抢救：分片已全部下完并合并，仅最后一步改名被
                # Windows 文件锁阻断（yt-dlp 内部重试 3 次后放弃，退出码 1）。
                # 此时 outtmpl.part 已是**完整成品** —— rename 是整条下载流程的
                # 最后一步，前面几百 MB 的流量都已付过。若在此判为失败，上层
                # _do_download_yt_dlp 的 except 会调 cleanup_single_temp() 把成品
                # 直接删掉再重下（这正是 temp/downloads 残留 5 个无配套分片的
                # .mp4.part、最大 1348 MB 的由来）。
                if "Unable to rename" in error_output:
                    if self._rescue_locked_part(output_path, error_output):
                        logger.info(
                            f"文件锁抢救成功，跳过重下: {os.path.basename(output_path)}")
                        if hasattr(self, 'debugger'):
                            self.debugger.info(
                                'DOWNLOAD_RESCUED',
                                f"文件锁抢救成功（无需重下）: {url[:50]}...")
                    else:
                        raise YtDlpError(
                            f"yt-dlp 改名失败且抢救未成功 (代码 {process.returncode}): "
                            f"{summarize_error_output(error_output)}",
                            exit_code=process.returncode,
                            full_output=error_output,
                        )
                elif "significantly different duration" in error_output:
                    logger.warning(f"yt-dlp 报告时长差异，但下载可能已完成: {url[:50]}...")
                elif "ffmpeg" in error_output.lower() and "not found" in error_output.lower():
                    logger.warning(f"ffmpeg 未找到，但下载可能已完成: {url[:50]}...")
                elif "WARNING:" in error_output and "not downloading" not in error_output.lower():
                    logger.warning(f"yt-dlp 警告: {error_output[:100]}")
                elif "HTTP Error 429" in error_output or "Too Many Requests" in error_output:
                    raise Exception("请求过于频繁，服务器拒绝")
                elif "HTTP Error 410" in error_output or "Gone" in error_output:
                    # 视频已被删除，抛出特殊异常
                    raise Exception("HTTP Error 410: Gone")
                elif "HTTP Error 416" in error_output or "Requested Range Not Satisfiable" in error_output:
                    raise Exception("HTTP Error 416: Requested Range Not Satisfiable - 分片下载失败，服务器不支持范围请求")
                elif "Connection refused" in error_output or "Connection reset" in error_output:
                    raise Exception("连接被拒绝或重置")
                elif "Could not find executable" in error_output:
                    raise Exception("找不到 yt-dlp 可执行文件")
                elif "permission denied" in error_output.lower():
                    raise Exception("权限不足，无法执行 yt-dlp")
                elif "No such file or directory" in error_output:
                    raise Exception("文件或目录不存在")
                else:
                    raise YtDlpError(
                        f"yt-dlp 执行失败 (代码 {process.returncode}): "
                        f"{summarize_error_output(error_output)}",
                        exit_code=process.returncode,
                        full_output=error_output,
                    )
            
            logger.info(f"下载完成: {url[:50]}...")
            if hasattr(self, 'debugger'):
                self.debugger.info('DOWNLOAD_COMPLETE', f"下载完成: {url[:50]}...")
            
            gc.collect()
            logger.info(f"下载完成 | 耗时: {(time.time() - start_time)/60:.1f} 分钟")
            
        except Exception as e:
            error_msg = str(e)
            if download_timed_out:
                error_msg = f"下载停滞超过 {terminate_seconds/60:.0f} 分钟，已强制终止"
            
            # 收集详细的错误信息
            error_details = []
            error_details.append(f"URL: {url}")
            error_details.append(f"错误类型: {type(e).__name__}")
            error_details.append(f"错误信息: {error_msg}")
            error_details.append(f"下载超时: {download_timed_out}")
            error_details.append(f"已使用代理: {proxy if proxy else '否'}")
            
            # 添加最后的输出内容（如果有）
            if 'error_output' in locals() and error_output:
                error_details.append(f"yt-dlp输出: {error_output[:1500]}")
            
            # 检查是否是特定错误
            if "410" in error_msg or "Gone" in error_msg:
                error_details.append("❗ 错误类型: HTTP 410 Gone - 视频已被删除或不可用")
            elif "404" in error_msg or "Not Found" in error_msg:
                error_details.append("❗ 错误类型: HTTP 404 Not Found - 视频不存在")
            elif "403" in error_msg or "Forbidden" in error_msg:
                error_details.append("❗ 错误类型: HTTP 403 Forbidden - 访问被拒绝，可能需要Cookie")
            elif "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                error_details.append("❗ 错误类型: 超时 - 网络连接问题或服务器响应慢")
            elif "stuck" in error_msg.lower() or "停滞" in error_msg:
                error_details.append("❗ 错误类型: 下载停滞 - 网络问题或服务器无响应")
            elif "disabled" in error_msg.lower() or "This video has been disabled" in error_msg:
                error_details.append("❗ 错误类型: 视频已被禁用 - 视频被PornHub删除或禁用")
            elif "has been removed" in error_msg.lower() or "removed" in error_msg.lower():
                error_details.append("❗ 错误类型: 视频已被移除")
            elif "private" in error_msg.lower():
                error_details.append("❗ 错误类型: 私有视频 - 只有上传者可以观看")
            elif "requires verification" in error_msg.lower() or "verification" in error_msg.lower():
                error_details.append("❗ 错误类型: 需要验证 - 年龄验证或账号验证")
            
            # 检查error_output中的额外错误信息
            if error_output:
                if "410" in error_output or "Gone" in error_output:
                    error_details.append("❗ yt-dlp输出显示: HTTP 410 Gone")
                elif "This video has been disabled" in error_output:
                    error_details.append("❗ yt-dlp输出显示: 视频已被禁用")
                elif "requires age verification" in error_output.lower():
                    error_details.append("❗ yt-dlp输出显示: 需要年龄验证")
            
            # 输出详细日志
            logger.error("=" * 60)
            logger.error("📌 下载失败详情:")
            for detail in error_details:
                logger.error(f"  {detail}")
            logger.error("=" * 60)
            
            if hasattr(self, 'debugger'):
                self.debugger.error('YT-DLP-ERROR', f"yt-dlp 下载失败: {url} - {error_msg}", exc_info=True)
            
            gc.collect()
            if isinstance(e, YtDlpError):
                # 保留原始异常（含完整输出与退出码），供上层准确分类
                raise
            raise YtDlpError(error_msg, full_output=locals().get('error_output', '') or '')
        finally:
            download_timed_out = True
            if process and process.poll() is None:
                try:
                    process.terminate()
                    time.sleep(1)
                    if process.poll() is None:
                        process.kill()
                    # 2026-09-22: kill 后必须 wait() 回收进程对象。Windows 上文件
                    # 句柄要等进程真正退出才释放；不 wait 会让 yt-dlp 在后台短暂
                    # 残留，成为下一次同路径下载触发 WinError 32 的持锁者。
                    try:
                        process.wait(timeout=10)
                    except Exception:
                        logger.debug("等待 yt-dlp 进程退出超时", exc_info=True)
                except Exception as e:
                    logger.debug(f"清理进程失败: {e}")
    
    def _run_yt_dlp_library(self, yt_dlp, ydl_opts, url):
        """
        使用 yt_dlp 库 API 直接下载（当无法使用可执行文件时回退到此方法）
        """
        import gc
        start_time = time.time()
        logger.info(f"===== yt-dlp 库模式下载开始 =====")
        logger.info(f"URL: {url[:50]}...")
        
        output_path = ydl_opts.get('outtmpl', '')
        logger.info(f"输出路径: {output_path[:100] if output_path else '未设置'}")
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                logger.info(f"=== 开始执行 yt-dlp 库模式 ===")
                ydl.download([url])
            
            logger.info(f"下载完成: {url[:50]}...")
            if hasattr(self, 'debugger'):
                self.debugger.info('DOWNLOAD_COMPLETE', f"下载完成: {url[:50]}...")
            
            gc.collect()
            logger.info(f"下载完成 | 耗时: {(time.time() - start_time)/60:.1f} 分钟")
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"yt-dlp 库模式下载失败: {url} - {error_msg}")
            if hasattr(self, 'debugger'):
                self.debugger.error('YT-DLP-ERROR', f"yt-dlp 库模式下载失败: {url} - {error_msg}", exc_info=True)
            
            gc.collect()
            raise Exception(error_msg)

    def _extract_model_name(self, url: str) -> str:
        """从URL提取模特名"""
        import re
        
        # /model/username 或 /pornstar/username
        match = re.search(r'/(model|pornstar)/([^/?#]+)', url)
        if match:
            return match.group(2).replace('-', ' ').title()
        
        return 'Unknown'

    def _on_download_completed(
        self,
        model_name: str,
        video_url: str,
        video_title: str,
        file_path: str
    ):
        """
        下载完成后的数据库更新和事件发射

        Args:
            model_name: 模特名
            video_url: 视频URL
            video_title: 视频标题
            file_path: 文件路径
        """
        self._log_download(model_name, video_url, video_title, file_path, 'success', None)

        # 追加该模特的下载记录到 data/download_records/<模特>.md（非致命）
        try:
            from multisite.core.utils.download_record_md import append_record
            append_record(model_name, video_title, video_url, file_path)
        except Exception:
            logger.debug("_on_download_completed 记录MD追加失败(非致命)", exc_info=True)

        try:
            # 获取该模特的所有订阅
            models = self.get_subscriptions()
            model_id = _find_model_id(models, model_name)

            if model_id:
                # 更新 videos 表中该视频的状态为 local（同步缺失→本地）
                try:
                    from multisite.core.database import ModuleDatabaseManager
                    db_mgr = ModuleDatabaseManager()
                    videos = db_mgr.get_videos(model_id)
                    for v in videos:
                        if v.get('url') == video_url and v.get('status') in ('missing', 'pending'):
                            db_mgr.update_video_status(v['id'], 'local', file_path)
                            logger.info(f"视频状态已更新: {video_title} missing->local")
                            break
                except Exception as e:
                    logger.warning(f"更新视频状态失败: {e}")

                # 从订阅列表中获取模特信息和统计
                model = next((m for m in models if m.get('id') == model_id), None)
                if not model:
                    logger.warning(f"模特 ID={model_id} 在订阅列表中不存在，跳过统计更新")
                else:
                    # 直接使用从 get_subscriptions() 获取的统计信息，避免额外的数据库查询
                    old_stats = {
                        'local': model.get('local_count', 0) or 0,
                        'missing': model.get('missing_count', 0) or 0,
                        'online': model.get('online_count', 0) or 0,
                        'downloaded': model.get('downloaded_count', 0) or 0
                    }

                    new_local = old_stats['local'] + 1
                    new_missing = max(0, old_stats['missing'] - 1)
                    new_downloaded = old_stats['downloaded'] + 1

                    self.db.update_model_stats(
                        model_id,
                        local_count=new_local,
                        missing_count=new_missing,
                        downloaded_count=new_downloaded
                    )

                    # 发射数据库变更事件
                    from .utils.db_change_observer import emit_download_completed, emit_model_updated

                    # 计算文件大小
                    file_size = 0
                    try:
                        if os.path.exists(file_path):
                            file_size = os.path.getsize(file_path)
                    except Exception:
                        logger.debug("_on_download_completed 忽略异常", exc_info=True)

                    emit_download_completed(
                        module='PORN',
                        model_id=model_id,
                        model_name=model_name,
                        video_url=video_url,
                        video_title=video_title,
                        file_size=file_size
                    )

                    emit_model_updated(
                        module='PORN',
                        model_id=model_id,
                        model_name=model_name,
                        old_stats=old_stats,
                        new_stats={
                            'local': new_local,
                            'missing': new_missing,
                            'online': old_stats['online'],
                            'downloaded': new_downloaded
                        }
                    )

                    logger.info(f"数据库已更新: {model_name} 本地+1={new_local}, 缺失-1={new_missing}")
            else:
                logger.warning(f"未找到模特: {model_name}，无法更新数据库统计信息")

        except Exception as e:
            logger.error(f"更新数据库失败: {e}")
    
    def _on_download_failed(
        self,
        model_name: str,
        video_url: str,
        video_title: str,
        error_msg: str
    ):
        """
        下载失败后的处理

        Args:
            model_name: 模特名
            video_url: 视频URL
            video_title: 视频标题
            error_msg: 错误信息
        """
        self._log_download(model_name, video_url, video_title, None, 'error', error_msg)
    
    def _log_download(
        self,
        model_name: str,
        video_url: str,
        video_title: str,
        file_path: str,
        status: str,
        error_msg: str
    ):
        """
        记录下载日志

        Args:
            model_name: 模特名
            video_url: 视频URL
            video_title: 视频标题
            file_path: 文件路径
            status: 下载状态
            error_msg: 错误信息
        """
        # 计算文件大小
        file_size = 0
        try:
            if file_path and os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
        except Exception:
            logger.debug("_log_download 忽略异常", exc_info=True)
        
        # 查找模特ID
        model_id = None
        try:
            models = self.get_subscriptions()
            model_id = _find_model_id(models, model_name)
        except Exception:
            logger.debug("_log_download 忽略异常", exc_info=True)
        
        # 记录下载日志
        try:
            from multisite.core.database.download_log_db import DownloadLogDB
            log_db = DownloadLogDB()
            log_db.add_download_log(
                model_id=model_id or 0,  # 如果找不到模特ID，使用0
                model_name=model_name,
                video_title=video_title,
                video_url=video_url,
                file_path=file_path,
                file_size=file_size,
                status=status,
                error_msg=error_msg,
                module='PORN'
            )
            log_db.close()
            logger.info(f"下载日志已记录: {model_name} - {video_title[:30]}... (状态: {status})")
        except Exception as log_e:
            logger.error(f"记录下载日志失败: {log_e}")
