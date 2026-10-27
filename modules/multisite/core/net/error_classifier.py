# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""下载错误的语义分类器

为什么需要它
------------
2026-09-20 的下载雪崩，根因之一是"用关键词在异常文本里找 410/404"来判断
视频是否已删除。这套判定有两个致命弱点：

1. **判定依据可能被截断**。错误文本原实现只保留前 200/500 字符，而
   ``HTTP Error 410`` 往往出现在 yt-dlp 输出的尾部，于是"视频已删除"被
   误判成"代理故障"，触发本不该发生的节点切换。
   （截断问题已在 ``download_manager.YtDlpError`` 修掉，本模块负责第二层。）

2. **判定结果只有一个布尔位**。原代码只能回答"是不是视频删除"，
   回答不了"这次失败该不该算在代理头上"。于是所有非删除类失败——包括
   站点 429 限流、Cloudflare 拦截、磁盘写不进去、ffmpeg 后处理失败——
   被无差别当成代理故障，一律触发节点切换，这正是雪崩的燃料。

本模块把判定升级为**按语义分类**，分类结果直接给出三个决策位：

===============  =============  ==========  =========
决策位            含义           用于          取值来源
===============  =============  ==========  =========
``affects_proxy`` 是否算代理失败  节点切换闸门  仅 NETWORK 为 True
``retryable``     是否值得重试    任务重试策略  见下表
``permanent``     是否永久不可用  标记/去重     仅 VIDEO_UNAVAILABLE
===============  =============  ==========  =========

关键设计取舍：**``UNKNOWN`` 一律 ``affects_proxy=False``**。
宁可少切节点（最多是多重试几次），也不许乱切节点（会杀掉别人的连接、
重启代理服务，进而演变成雪崩）。这是从事故里换来的默认值。

设计参考
--------
"按语义对错误分类、由类型决定重试与换代理策略"这个设计思想，来自
EchterAlsFake/Porn_Fetch 的 ``shared/errors.py``（AppNetworkError /
AppNotFoundError / AppBotBlocked / AppDownloadFailed 四类 + ``safe_api_call``
按异常类名翻译）。

许可说明
--------
本项目自 2026-09-20 起改用 **GPL-3.0-or-later**（原因见 README「为什么是
GPL-3.0」：GUI 依赖的 PyQt5 社区版本身即以 GPL v3 授权），与 Porn_Fetch 的
GPL-3.0-or-later 许可**互相兼容**，原先的授权冲突已不存在。

即便如此，本文件仍是**独立编写**的：未复制其任何代码、常量或数据结构，
仅借鉴上述设计思想；规则表、模式串、数据结构与判定流程均为自行设计。
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Pattern, Tuple

__all__ = [
    'ErrorKind',
    'ErrorClassification',
    'classify_error',
    'KIND_LABELS',
]


class ErrorKind(str, Enum):
    """下载错误的语义类别。"""

    #: 视频本身不可用（已删除 / 下架 / 私有），与代理无关，重试无意义
    VIDEO_UNAVAILABLE = 'VIDEO_UNAVAILABLE'
    #: 被反爬拦截（Cloudflare / 验证码 / 限流），换代理无用，换指纹才有用
    BOT_BLOCKED = 'BOT_BLOCKED'
    #: 站点侧 5xx，属服务端临时故障，换代理无用
    SERVER_ERROR = 'SERVER_ERROR'
    #: 网络/代理链路故障，**唯一**允许影响代理健康统计的类别
    NETWORK = 'NETWORK'
    #: 媒体流或后处理问题（分片缺失、时长不符、ffmpeg 失败）
    MEDIA_ERROR = 'MEDIA_ERROR'
    #: 本地磁盘/文件系统问题
    DISK_ERROR = 'DISK_ERROR'
    #: 未识别
    UNKNOWN = 'UNKNOWN'


KIND_LABELS = {
    ErrorKind.VIDEO_UNAVAILABLE: '视频不可用',
    ErrorKind.BOT_BLOCKED: '反爬拦截',
    ErrorKind.SERVER_ERROR: '站点服务端错误',
    ErrorKind.NETWORK: '网络/代理故障',
    ErrorKind.MEDIA_ERROR: '媒体流/后处理错误',
    ErrorKind.DISK_ERROR: '本地磁盘错误',
    ErrorKind.UNKNOWN: '未识别错误',
}


@dataclass(frozen=True)
class ErrorClassification:
    """一次错误分类的结果。"""

    kind: ErrorKind
    #: 是否应计入代理失败（并允许其推动节点切换）
    affects_proxy: bool
    #: 是否值得重试
    retryable: bool
    #: 是否应永久标记为不可用（不再入队）
    permanent: bool
    #: 命中的模式（便于日志排查；未命中为 None）
    matched: Optional[str] = None
    #: 命中的规则名（便于统计各类错误的真实分布）
    rule: Optional[str] = None

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind.value)

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return (f"{self.kind.value}({self.label})"
                f" affects_proxy={self.affects_proxy}"
                f" retryable={self.retryable}"
                f" permanent={self.permanent}"
                f" rule={self.rule}")


def _compile(*patterns: str) -> Tuple[Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE | re.DOTALL) for p in patterns)


# ---------------------------------------------------------------------------
# 规则表
#
# 分两级：
#   STRONG —— 特征词足够独特，几乎不可能误判
#   WEAK   —— 特征词较短（如 'not found' / '403'），可能出现在别的语境里
#
# 匹配顺序 = 先 STRONG 全表，再 WEAK 全表，同级内自上而下第一条命中即返回。
# 顺序原则：**把"重试无害"的类别排在前面，"误判会丢数据"的类别排在后面**。
#   - 网络类在前：把网络故障误判成视频删除会导致好视频被永久跳过（数据损失）
#   - 视频删除类在后：把已删除视频误判成网络故障只是浪费时间重试（可控）
# ---------------------------------------------------------------------------

_Rules = Tuple[ErrorKind, bool, bool, bool, str, Tuple[Pattern[str], ...]]

# (kind, affects_proxy, retryable, permanent, rule_name, patterns)
_STRONG_RULES: Tuple[_Rules, ...] = (
    # ---- 本地磁盘：换代理、重试都没用 ----
    (ErrorKind.DISK_ERROR, False, True, False, 'disk_no_space', _compile(
        r'no space left',
        r'disk quota exceeded',
        r'errno\s*28\b',
        r'not enough space',
        r'磁盘空间不足',
    )),

    # ---- 明确的 TLS / 代理链路故障（特征独特，先判为网络） ----
    (ErrorKind.NETWORK, True, True, False, 'network_tls', _compile(
        r'curl:\s*\(\s*(7|28|35|52|55|56|92)\s*\)',
        r'http/2 stream \d+ was not closed',
        r'http/2 stream \d+ reset',
        r'ssl_connect',
        r'ssl error',
        r'tlsv1',
        r'ssl: wrong version number',
        r'certificate verify failed',
        r'certificate has expired',
        r'proxyerror',
        r'proxy error',
        r'proxy connect aborted',
        r'socks',
        r'connection reset by peer',
        r'connection was reset',
        r'connection refused',
        r'tunnel connection failed',
        r'remote end closed connection',
        r'connection closed abruptly',
        r'temporary failure in name resolution',
        r'nodename nor servname',
        r'getaddrinfo failed',
        r'name or service not known',
        r'network is unreachable',
        r'no route to host',
        r'read timed out',
        r'connection timed out',
        r'the read operation timed out',
    )),

    # ---- CDN 拒绝：媒体直链里的授权出口 IP 与实际请求 IP 不一致 ----
    # 2026-09-28 从 video_gone 里搬出来。原先 ``\b474\b`` 归在
    # VIDEO_UNAVAILABLE（permanent=True，永久跳过），实测是**误判**：
    #   * 线上 474 在负载均衡生效（17:35）后 17:36:42 首次出现，此前全天 0 次；
    #   * 同一视频同一时刻：走 balancer 立刻 474，只走任一单节点稳定下载
    #     （566~604 KiB/s）。
    # 机制：PornHub 媒体直链形如 ``...1080P.mp4?ip=<出口IP>&hash=...``，
    # 站点校验**实际发起请求的 IP**；逐连接轮换出口时页面/分片分属不同节点，
    # 于是 CDN 回 474 "Client Error"。⇒ 这是**出口不一致**问题，换出口/重试
    # 有效，绝不能当"视频已删除"永久跳过（那会把好视频整批烧掉）。
    # 必须排在 media 规则之前：后者的 'unable to download video data' 会先行
    # 命中，而 media 的 affects_proxy=False 会让坏节点逃过失败计数。
    (ErrorKind.NETWORK, True, True, False, 'cdn_ip_mismatch_474', _compile(
        r'http error 474',
        r'\b474 client error\b',
        r'\b474\b',
    )),

    # ---- 视频已删除 / 下架（特征独特） ----
    (ErrorKind.VIDEO_UNAVAILABLE, False, False, True, 'video_gone', _compile(
        r'http error 410',
        r'\b410 gone\b',
        r'error:?\s*410\b',
        r'http 410',
        r'has been (disabled|removed|deleted)',
        r'removed by the uploader',
        r'no longer available',
        r'video unavailable',
        r'this video is not available',
        r'private video',
        r'\bis private\b',
        r'视频已删除',
    )),

    # ---- 反爬拦截：换代理无用，换浏览器指纹才有用 ----
    (ErrorKind.BOT_BLOCKED, False, True, False, 'bot_blocked', _compile(
        r'http error 429',
        r'\b429 too many requests\b',
        r'cloudflare',
        r'captcha',
        r'are you a robot',
        r'checking your browser',
        r'sign in to confirm',
        r'confirm you.?re not a bot',
        r'access denied',
        r'http error 403',
    )),

    # ---- 站点服务端 5xx：等一会儿再来，别换代理 ----
    (ErrorKind.SERVER_ERROR, False, True, False, 'server_5xx', _compile(
        r'http error 50\d',
        r'\b50[0-4]\s+(bad gateway|service unavailable|gateway time-?out|internal server error)',
        r'server error',
    )),

    # ---- 媒体流 / 后处理 ----
    (ErrorKind.MEDIA_ERROR, False, True, False, 'media', _compile(
        r'significantly different duration',
        r'unable to download video data',
        r'fragment \d+ not found',
        r'requested range not satisfiable',
        r'\b416\b',
        r'postprocessing',
        r'ffmpeg',
        r'merge failed',
        r'no such file or directory',
    )),
)

_WEAK_RULES: Tuple[_Rules, ...] = (
    # ---- 网络（弱特征：靠上下文，可能与其他语义重叠） ----
    (ErrorKind.NETWORK, True, True, False, 'network_weak', _compile(
        r'\btimed? ?out\b',
        r'\btimeout\b',
        r'unable to connect',
        r'failed to connect',
        r'connection aborted',
        r'connection error',
        r'http error 5(02|03|04)\b',
    )),

    # ---- 视频不可用（弱特征：'not found' / '404' 可能是别的意思） ----
    (ErrorKind.VIDEO_UNAVAILABLE, False, False, True, 'video_gone_weak', _compile(
        r'http error 404',
        r'\b404 not found\b',
        r'\bnot found\b',
    )),
)


def classify_error(text: Optional[str], exit_code: Optional[int] = None) -> ErrorClassification:
    """把一次失败的诊断文本分类。

    Args:
        text: 完整诊断文本。**必须传完整输出**，不要传被截断的片段，
            否则尾部关键词会丢失（这正是 2026-09-20 事故的诱因之一）。
            可直接使用 :func:`modules.core.download_manager.get_error_diagnostic`。
        exit_code: 进程退出码（可选，仅用于日志与 UNKNOWN 的补充说明）。

    Returns:
        :class:`ErrorClassification`
    """
    haystack = text or ''
    for rules in (_STRONG_RULES, _WEAK_RULES):
        for kind, affects_proxy, retryable, permanent, rule, patterns in rules:
            for pat in patterns:
                m = pat.search(haystack)
                if m:
                    return ErrorClassification(
                        kind=kind,
                        affects_proxy=affects_proxy,
                        retryable=retryable,
                        permanent=permanent,
                        matched=m.group(0)[:120],
                        rule=rule,
                    )

    # 未识别：保守处理——不碰代理，允许重试
    return ErrorClassification(
        kind=ErrorKind.UNKNOWN,
        affects_proxy=False,
        retryable=True,
        permanent=False,
        matched=None,
        rule=('unknown_exit_%s' % exit_code) if exit_code is not None else 'unknown',
    )
