# -*- coding: utf-8 -*-
"""播放页解析：提取真实 m3u8 播放地址。

播放页 HTML 内嵌一个 JSON 字段 epPlaySrcs（同义字段 contentUrl / previewSrc）:

    "epPlaySrcs": {
      "1": "https://yd-hls.hrppxr.cn/videos5/{md5}/{md5}.m3u8?auth_key=...",
      "2": "https://...m3u8?auth_key=..."
    }

要点:
  - 首屏只暴露当前集 + 下一集，因此需要逐集请求 /video/{id}/ep-{n}/ 提取
  - auth_key 有时效，必须「现抓现用」，不能长期缓存
"""

import json
import re
from typing import Dict

from bs4 import BeautifulSoup


def parse_play_srcs(html: str) -> Dict[int, str]:
    """从播放页提取 {集号: m3u8 地址}。

    优先解析 epPlaySrcs；失败时退回 contentUrl / previewSrc / 裸 m3u8 链接。
    """
    srcs: Dict[int, str] = {}

    # ---- 方式一：正则抠出 epPlaySrcs 对象，再按 JSON 解析（最可靠）
    m = re.search(r'"epPlaySrcs"\s*:\s*\{(.*?)\}\s*[,}]', html, re.S)
    if m:
        try:
            raw = "{" + m.group(1) + "}"
            data = json.loads(raw)
            for k, v in data.items():
                if isinstance(v, str) and v.startswith("http"):
                    try:
                        srcs[int(k)] = v.replace("\\u0026", "&")
                    except ValueError:
                        continue
        except json.JSONDecodeError:
            srcs = {}

    # ---- 方式二：退回 JSON-LD 的 contentUrl（只有当前集）
    if not srcs:
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            # 可能是对象也可能是数组
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "VideoObject":
                    url = item.get("contentUrl") or item.get("embedUrl") or ""
                    if ".m3u8" in url:
                        ep = int(item.get("ep") or 1)
                        srcs[ep] = url.replace("\\u0026", "&")

    # ---- 方式三：全文扫 m3u8（兜底，集号无法保证）
    if not srcs:
        for url in set(re.findall(r'https?://[^"\'\\\s]+?\.m3u8[^"\'\\\s]*', html)):
            srcs[1] = url.replace("\\u0026", "&")
            break

    return srcs


def pick_episode(srcs: Dict[int, str], ep: int) -> str:
    """从解析结果中取指定集的地址。

    epPlaySrcs 里可能只含当前集与下一集，因此用「精确匹配 -> 最小集号回退」
    的策略，保证总能拿到一个可用地址。
    """
    if not srcs:
        return ""
    if ep in srcs:
        return srcs[ep]
    # 页面可能只带了两集，取最接近的一集
    if len(srcs) == 1:
        return next(iter(srcs.values()))
    return ""
