# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 龙之魂 (DragonSouls233)
"""
统一查重模块使用示例

本示例展示如何使用新的核心模块进行查重检测
解决：本地100个 + 网站158个 → 显示需要下载58个
"""
import asyncio
import sys
import os

# 添加模块路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from multisite.core import (
    UnifiedDetector,
    MissingFinder,
    LocalScanner,
    TitleNormalizer,
    ParserFactory,
    quick_detect,
    quick_detect_sync,
)


def example_basic_usage():
    """基本使用示例"""
    print("=" * 60)
    print("示例1: 基本查重检测")
    print("=" * 60)
    
    # 配置
    config = {
        'cache_enabled': True,
        'cache_dir': 'cache',
        'timeout': 30,
        'max_retries': 3,
    }
    
    # 创建检测器
    detector = UnifiedDetector(config)
    
    # 执行检测（同步方式）
    result = detector.detect_sync(
        model_url='https://www.pornhub.com/model/username',  # 替换为实际URL
        local_directory='D:/Videos/Model',  # 替换为实际目录
        use_cache=True,
        max_pages=100
    )
    
    # 输出结果
    print(f"在线视频: {result['online_count']} 个")
    print(f"本地视频: {result['local_count']} 个")
    print(f"已匹配:   {result['matched_count']} 个")
    print(f"需下载:   {result['missing_count']} 个")


async def example_async_usage():
    """异步使用示例"""
    print("=" * 60)
    print("示例2: 异步查重检测")
    print("=" * 60)
    
    # 使用便捷函数
    result = await quick_detect(
        model_url='https://www.pornhub.com/model/username',
        local_directory='D:/Videos/Model',
    )
    
    print(f"检测结果: {result}")


def example_batch_detect():
    """批量检测示例"""
    print("=" * 60)
    print("示例3: 批量查重检测")
    print("=" * 60)
    
    # 模特列表
    models = [
        {
            'name': '模特A',
            'url': 'https://www.pornhub.com/model/model-a',
            'directory': 'D:/Videos/模特A',
        },
        {
            'name': '模特B',
            'url': 'https://www.pornhub.com/model/model-b',
            'directory': 'D:/Videos/模特B',
        },
    ]
    
    detector = UnifiedDetector()
    results = asyncio.run(detector.batch_detect(models))
    
    for result in results:
        print(f"{result['model_name']}: 需下载 {result['missing_count']} 个")


def example_local_scanner():
    """本地扫描示例"""
    print("=" * 60)
    print("示例4: 本地视频扫描")
    print("=" * 60)
    
    scanner = LocalScanner()
    
    # 扫描目录
    videos = scanner.scan_directory('D:/Videos/Model')
    
    print(f"找到 {len(videos)} 个视频文件:")
    for video in videos[:5]:  # 只显示前5个
        print(f"  - {video['title']}")
    
    # 获取统计信息
    stats = scanner.get_video_stats('D:/Videos/Model')
    print(f"\n统计信息:")
    print(f"  总数: {stats['total_videos']}")
    print(f"  大小: {stats['total_size_gb']} GB")


def example_title_normalizer():
    """标题归一化示例"""
    print("=" * 60)
    print("示例5: 标题归一化")
    print("=" * 60)
    
    normalizer = TitleNormalizer()
    
    # 测试标题
    titles = [
        "【模特名】视频标题－完整版 1080P",
        "[Channel] 视频标题-完整版 HD",
        "视频标题－完整版 (PornHub)",
        "Video Title - Full Version [PH]",
    ]
    
    print("归一化结果:")
    for title in titles:
        normalized = normalizer.normalize(title)
        print(f"  原始: {title}")
        print(f"  归一化: {normalized}")
        print()
    
    # 测试相似度匹配
    title1 = "视频标题-完整版"
    title2 = "视频标题－完整版"
    similar = normalizer.are_similar(title1, title2)
    print(f"'{title1}' 和 '{title2}' 相似: {similar}")


def example_missing_finder():
    """缺失视频查找示例"""
    print("=" * 60)
    print("示例6: 缺失视频查找")
    print("=" * 60)
    
    finder = MissingFinder()
    
    # 执行查找
    result = finder.find_missing_sync(
        model_url='https://www.pornhub.com/model/username',
        local_directory='D:/Videos/Model',
        max_pages=10,  # 限制页数用于测试
    )
    
    # 生成报告
    report = finder.generate_report(result, output_format='text')
    print(report)
    
    # 保存下载列表
    detector = UnifiedDetector()
    detector.generate_download_list(result, 'download_list.txt')


def example_parser_factory():
    """解析器工厂示例"""
    print("=" * 60)
    print("示例7: 解析器工厂")
    print("=" * 60)
    
    # 获取支持的站点
    sites = ParserFactory.get_supported_sites()
    print(f"支持的站点: {sites}")
    
    # 检测站点类型
    url = 'https://www.pornhub.com/model/username'
    site = ParserFactory.detect_site(url)
    print(f"URL '{url}' 识别为站点: {site}")
    
    # 创建解析器
    parser = ParserFactory.create_parser('porn')
    print(f"创建解析器: {parser.get_site_name()}")


def main():
    """主函数"""
    print("\n" + "=" * 60)
    print("统一查重模块使用示例")
    print("=" * 60 + "\n")
    
    # 运行示例
    example_title_normalizer()
    example_local_scanner()
    example_parser_factory()
    
    # 以下示例需要实际URL和目录
    # example_basic_usage()
    # asyncio.run(example_async_usage())
    # example_batch_detect()
    # example_missing_finder()
    
    print("\n示例运行完成!")


if __name__ == '__main__':
    main()
