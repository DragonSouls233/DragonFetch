# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
统一标题归一化模块
解决本地文件名和在线标题匹配不一致的问题
"""
import re
import unicodedata
from typing import Set


class TitleNormalizer:
    """
    统一的标题归一化器
    用于本地视频标题和在线视频标题的对比匹配
    """
    
    # 需要移除的分辨率标识
    RESOLUTION_PATTERNS = [
        r'\b(4K|2160p|1080p|720p|480p|360p|240p)\b',
        r'\b(HD|FHD|UHD|SD)\b',
        r'\b(HDR|HDR10|DolbyVision)\b',
        r'\b(60fps|30fps)\b',
    ]
    
    # 需要移除的视频编码标识
    CODEC_PATTERNS = [
        r'\b(H\.?264|H\.?265|HEVC|AVC|VP9|AV1)\b',
        r'\b(x264|x265|xvid|divx)\b',
    ]
    
    # 需要移除的网站标识
    SITE_PATTERNS = [
        r'\b(PornHub|Porn|PH|XVideos|XV|XHamster|XH|Porn91|91Porn)\b',
        r'\[(PornHub|Porn|PH|XVideos|XV|XHamster|XH)\]',
    ]
    
    # 全角字符到半角字符的映射
    FULLWIDTH_MAP = {
        '０': '0', '１': '1', '２': '2', '３': '3', '４': '4',
        '５': '5', '６': '6', '７': '7', '８': '8', '９': '9',
        'Ａ': 'A', 'Ｂ': 'B', 'Ｃ': 'C', 'Ｄ': 'D', 'Ｅ': 'E',
        'Ｆ': 'F', 'Ｇ': 'G', 'Ｈ': 'H', 'Ｉ': 'I', 'Ｊ': 'J',
        'Ｋ': 'K', 'Ｌ': 'L', 'Ｍ': 'M', 'Ｎ': 'N', 'Ｏ': 'O',
        'Ｐ': 'P', 'Ｑ': 'Q', 'Ｒ': 'R', 'Ｓ': 'S', 'Ｔ': 'T',
        'Ｕ': 'U', 'Ｖ': 'V', 'Ｗ': 'W', 'Ｘ': 'X', 'Ｙ': 'Y', 'Ｚ': 'Z',
        'ａ': 'a', 'ｂ': 'b', 'ｃ': 'c', 'ｄ': 'd', 'ｅ': 'e',
        'ｆ': 'f', 'ｇ': 'g', 'ｈ': 'h', 'ｉ': 'i', 'ｊ': 'j',
        'ｋ': 'k', 'ｌ': 'l', 'ｍ': 'm', 'ｎ': 'n', 'ｏ': 'o',
        'ｐ': 'p', 'ｑ': 'q', 'ｒ': 'r', 'ｓ': 's', 'ｔ': 't',
        'ｕ': 'u', 'ｖ': 'v', 'ｗ': 'w', 'ｘ': 'x', 'ｙ': 'y', 'ｚ': 'z',
        '　': ' ', '＿': '_', '－': '-',
    }
    
    # 破折号统一
    DASH_PATTERNS = [
        (r'[\u2013\u2014\u2015\u2212]', '-'),  # 各种破折号统一为 -
        (r'—', '-'),  # 中文破折号
        (r'–', '-'),  # en-dash
    ]
    
    # 引号统一
    QUOTE_PATTERNS = [
        (r'[""「」『』]', '"'),
        (r"[''′′]", "'"),
    ]

    # 斜杠/下划线统一（Windows文件名中/被替换为_）
    SLASH_PATTERNS = [
        (r'[/\\]', '_'),
    ]
    
    def __init__(self, config: dict = None):
        """
        初始化归一化器
        
        Args:
            config: 配置字典，可包含以下选项：
                - case_sensitive: 是否区分大小写（默认False）
                - strip_punctuation: 是否移除标点（默认True）
                - strip_resolution: 是否移除分辨率标识（默认True）
                - strip_site_tags: 是否移除网站标识（默认True）
        """
        self.config = config or {}
        self.case_sensitive = self.config.get('case_sensitive', False)
        self.strip_punctuation = self.config.get('strip_punctuation', True)
        self.strip_resolution = self.config.get('strip_resolution', True)
        self.strip_site_tags = self.config.get('strip_site_tags', True)
    
    def normalize(self, title: str) -> str:
        """
        归一化标题
        
        Args:
            title: 原始标题
        
        Returns:
            归一化后的标题
        """
        if not title:
            return ''
        
        # 1. 基本清理
        result = title.strip()
        
        # 2. 全角转半角
        result = self._fullwidth_to_halfwidth(result)
        
        # 3. 破折号统一
        for pattern, replacement in self.DASH_PATTERNS:
            result = re.sub(pattern, replacement, result)
        
        # 4. 引号统一
        for pattern, replacement in self.QUOTE_PATTERNS:
            result = re.sub(pattern, replacement, result)
        
        # 4.1 斜杠统一为下划线（Windows文件名中/被替换为_）
        for pattern, replacement in self.SLASH_PATTERNS:
            result = re.sub(pattern, replacement, result)
        
        # 5. 移除分辨率标识
        if self.strip_resolution:
            for pattern in self.RESOLUTION_PATTERNS:
                result = re.sub(pattern, '', result, flags=re.IGNORECASE)
            for pattern in self.CODEC_PATTERNS:
                result = re.sub(pattern, '', result, flags=re.IGNORECASE)
        
        # 6. 移除网站标识
        if self.strip_site_tags:
            for pattern in self.SITE_PATTERNS:
                result = re.sub(pattern, '', result, flags=re.IGNORECASE)
        
        # 7. 移除方括号内容（如 [Channel]）
        result = re.sub(r'\[[^\]]*\]', '', result)
        
        # 8. 移除圆括号内的分辨率信息
        result = re.sub(r'\([^)]*(p|P|HD|FHD|4K)[^)]*\)', '', result)
        
        # 9. 清理多余空格
        result = re.sub(r'\s+', ' ', result).strip()
        
        # 10. 大小写处理
        if not self.case_sensitive:
            result = result.lower()
        
        # 11. 移除标点（用于最终对比）
        if self.strip_punctuation:
            result = re.sub(r'[\W_]+', '', result, flags=re.UNICODE)
        
        return result
    
    def normalize_set(self, titles: Set[str]) -> Set[str]:
        """
        归一化标题集合
        
        Args:
            titles: 标题集合
        
        Returns:
            归一化后的标题集合
        """
        return {self.normalize(t) for t in titles if t}
    
    def _fullwidth_to_halfwidth(self, text: str) -> str:
        """
        全角字符转半角字符
        
        Args:
            text: 原始文本
        
        Returns:
            转换后的文本
        """
        result = []
        for char in text:
            if char in self.FULLWIDTH_MAP:
                result.append(self.FULLWIDTH_MAP[char])
            else:
                result.append(char)
        return ''.join(result)
    
    def are_similar(self, title1: str, title2: str, threshold: float = 0.85) -> bool:
        """
        判断两个标题是否相似
        
        Args:
            title1: 标题1
            title2: 标题2
            threshold: 相似度阈值（默认0.85）
        
        Returns:
            是否相似
        """
        norm1 = self.normalize(title1)
        norm2 = self.normalize(title2)
        
        # 完全匹配
        if norm1 == norm2:
            return True
        
        # 包含关系
        if norm1 in norm2 or norm2 in norm1:
            return True
        
        # 相似度计算（简单版：使用集合交集）
        if len(norm1) > 0 and len(norm2) > 0:
            set1 = set(norm1)
            set2 = set(norm2)
            intersection = len(set1 & set2)
            union = len(set1 | set2)
            if union > 0:
                similarity = intersection / union
                return similarity >= threshold
        
        return False
    
    def find_best_match(self, target: str, candidates: Set[str], threshold: float = 0.85) -> str:
        """
        在候选集合中找到最佳匹配
        
        Args:
            target: 目标标题
            candidates: 候选标题集合
            threshold: 相似度阈值
        
        Returns:
            最佳匹配的标题，如果没有匹配则返回None
        """
        target_norm = self.normalize(target)
        
        # 先尝试精确匹配
        for candidate in candidates:
            if self.normalize(candidate) == target_norm:
                return candidate
        
        # 再尝试相似度匹配
        best_match = None
        best_score = 0
        
        for candidate in candidates:
            if self.are_similar(target, candidate, threshold):
                # 计算更精确的相似度分数
                cand_norm = self.normalize(candidate)
                set1 = set(target_norm)
                set2 = set(cand_norm)
                score = len(set1 & set2) / len(set1 | set2) if (set1 | set2) else 0
                
                if score > best_score:
                    best_score = score
                    best_match = candidate
        
        return best_match
