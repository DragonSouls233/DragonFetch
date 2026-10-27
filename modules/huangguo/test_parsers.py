# -*- coding: utf-8 -*-
r"""解析器回归测试。

用本地已抓取的 HTML 样本验证三个解析器是否工作正常，不发起网络请求。
样本目录: G:\黄果\_tmp_scrape

运行:
    <venv>\Scripts\python.exe test_parsers.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hgcore.parser_home import parse_home
from hgcore.parser_detail import parse_detail
from hgcore.parser_play import parse_play_srcs, pick_episode

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "_tmp_scrape"


def read(name: str) -> str:
    p = SAMPLE_DIR / name
    if not p.is_file():
        print(f"[跳过] 样本不存在: {p}")
        return ""
    return p.read_text(encoding="utf-8", errors="ignore")


def check(cond: bool, msg: str) -> bool:
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    return cond


def test_home() -> bool:
    print("\n=== 首页解析 (home.html) ===")
    html = read("home.html")
    if not html:
        return False
    dramas = parse_home(html)
    ok = check(len(dramas) > 0, f"解析到 {len(dramas)} 部剧（期望 > 0）")
    if not dramas:
        return False

    d = dramas[0]
    print(f"  样本: id={d.did} 标题={d.title!r} 评分={d.score} 集数={d.episode_count}")
    ok &= check(bool(d.did), "剧集 ID 非空")
    ok &= check(bool(d.title), "标题非空")
    ok &= check(d.cover.startswith("http"), f"封面为绝对地址: {d.cover[:50]}...")

    with_cover = sum(1 for x in dramas if x.cover.startswith("http"))
    with_title = sum(1 for x in dramas if x.title)
    print(f"  覆盖: {with_cover}/{len(dramas)} 有封面, {with_title}/{len(dramas)} 有标题")
    return ok


def test_detail() -> bool:
    print("\n=== 详情页解析 (detail117.html) ===")
    html = read("detail117.html")
    if not html:
        return False
    info = parse_detail(html)
    print(f"  标题={info['title']!r} 集数={info['episode_count']} 标签={info['tags']}")
    ok = check(info["title"] == "高三爱情故事", "标题解析正确")
    ok &= check(info["episode_count"] == 36, f"集数=36（实际 {info['episode_count']}）")
    ok &= check(len(info["episodes"]) == 36, "集数列表长度 36")
    ok &= check(bool(info["desc"]), "简介非空")
    ok &= check(info["cover"].startswith("http"), "封面为绝对地址")
    return ok


def test_play() -> bool:
    print("\n=== 播放页解析 (play117.html / p5.html) ===")
    ok = True

    html = read("play117.html")
    if html:
        srcs = parse_play_srcs(html)
        ok &= check(len(srcs) > 0, f"第1集页解析到 {len(srcs)} 条播放源")
        if srcs:
            for ep, url in sorted(srcs.items()):
                ok &= check(".m3u8" in url, f"  集{ep} 是 m3u8: {url[:60]}...")
                ok &= check("auth_key=" in url, f"  集{ep} 带 auth_key")

    html5 = read("p5.html")
    if html5:
        srcs5 = parse_play_srcs(html5)
        ok &= check(len(srcs5) > 0, f"第5集页解析到 {len(srcs5)} 条播放源")
        ok &= check(5 in srcs5, "包含第 5 集地址")
        if 5 in srcs5:
            print(f"  集5: {srcs5[5][:70]}...")
        picked = pick_episode(srcs5, 5)
        ok &= check(bool(picked), "pick_episode(5) 能取到地址")
    return ok


def main() -> int:
    print("=" * 60)
    print("黄果短剧下载器 - 解析器回归测试")
    print("=" * 60)
    results = [test_home(), test_detail(), test_play()]
    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r)
    print(f"结果: {passed}/{len(results)} 组通过")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
