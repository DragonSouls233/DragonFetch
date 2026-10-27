# -*- coding: utf-8 -*-
"""糖心 Vlog —— 无头命令行模式（供服务器批量运行）。

用法示例：
  # 列出演员云前 N 位
  python -m tangxin.cli --list-actors --limit 20

  # 下载某演员的全部视频到指定目录
  python -m tangxin.cli --actor "岛国梦工厂" --output E:\\TangXinVlog\\downloads

  # 下载多个演员（逗号分隔）
  python -m tangxin.cli --actors "岛国梦工厂,星空无限传媒" --output E:\\TangXinVlog\\downloads --workers 4

  # 从选择文件下载（JSON: [{"actor":"xxx","videos":["id1","id2"]}] 或 [{"actor":"xxx"}] 抓全部）
  python -m tangxin.cli --from-file selection.json --output E:\\TangXinVlog\\downloads

说明：服务器上 CDN 直连、站点经本地 xray(10808) 代理；与 PornHub 平台部署完全隔离。
"""
import argparse
import json
import os
import sys

from . import config, TangXinCrawler, TangXinDownloader
from . import nfo as nfo_mod
from . import db
from . import existing
from . import logger
from .models import Video
from .utils import sanitize_filename


def _progress(cur, tot, phase, msg):
    if phase == "download":
        sys.stdout.write("\r  [%s] %d/%d %s" % (phase, cur, tot, msg))
        sys.stdout.flush()
    else:
        print("  [%s] %s" % (phase, msg))


def _print_db_summary():
    """打印下载记录库统计，便于无 GUI 时核对结果。"""
    try:
        counts = db.count_by_status()
    except Exception as e:
        print("  [下载记录] 读取数据库失败：%s" % e)
        return
    if not counts:
        print("  [下载记录] 数据库暂无记录。")
        return
    parts = ["%s=%d" % (k, v) for k, v in counts.items()]
    print("  [下载记录] %s" % "  ".join(parts))
    print("  [下载记录] 库文件：%s" % db.get_db_path())


def cmd_list_actors(args):
    cr = TangXinCrawler()
    actors = cr.fetch_actor_cloud(progress=print)
    cr.close()
    lim = args.limit or len(actors)
    for a in actors[:lim]:
        print("%s\t%d\t%s" % (a.name, a.count, a.slug))
    print("共 %d 位演员（显示前 %d）" % (len(actors), lim))


def cmd_download_actors(names, output, workers, get_cover):
    cr = TangXinCrawler()
    all_actors = cr.fetch_actor_cloud(progress=print)
    cr.close()
    by_name = {a.name: a for a in all_actors}
    by_slug = {a.slug: a for a in all_actors}
    targets = []
    for n in names:
        a = by_name.get(n) or by_slug.get(n)
        if not a:
            # 也允许按解码名反查（slug 是编码的）
            for x in all_actors:
                if x.name == n or x.slug == n:
                    a = x
                    break
        if a:
            targets.append(a)
        else:
            print("[警告] 未找到演员：%s" % n)
    if not targets:
        print("没有可下载的演员，退出。")
        return
    dl = TangXinDownloader(mode=config.DOWNLOAD_MODE)
    ok = fail = 0
    for actor in targets:
        print("=== 演员：%s（%d 个视频）===" % (actor.name, actor.count))
        cr2 = TangXinCrawler()
        vids = cr2.fetch_actor_videos(actor, progress=print)
        cr2.close()
        out_dir = os.path.join(output, sanitize_filename(actor.name))
        for v in vids:
            try:
                res = dl.download_video(v, out_dir, progress=_progress, get_cover=get_cover)
                if res.get("skipped"):
                    if res.get("existing_elsewhere"):
                        print("  跳过(其它目录已下载): %s  ← %s"
                              % (v.title, res.get("path")))
                    else:
                        print("  跳过(已存在): %s" % v.title)
                else:
                    nfo_mod.write_video_nfo(v, res["path"], res.get("poster"))
                    print("\n  ✔ %s" % os.path.basename(res["path"]))
                ok += 1
            except Exception as e:
                fail += 1
                print("\n  ✘ 失败 %s: %s" % (v.title, e))
    dl.close()
    print("下载结束：成功 %d，失败 %d。" % (ok, fail))
    _print_db_summary()


def cmd_from_file(path, output, workers, get_cover):
    with open(path, encoding="utf-8") as f:
        sel = json.load(f)
    cr = TangXinCrawler()
    all_actors = cr.fetch_actor_cloud(progress=print)
    cr.close()
    by_name = {a.name: a for a in all_actors}
    dl = TangXinDownloader(mode=config.DOWNLOAD_MODE)
    ok = fail = 0
    for item in sel:
        name = item.get("actor") or item.get("name")
        actor = by_name.get(name)
        if not actor:
            print("[警告] 未找到演员：%s" % name)
            continue
        out_dir = os.path.join(output, sanitize_filename(actor.name))
        if item.get("videos"):
            wanted = set(str(x) for x in item["videos"])
            cr2 = TangXinCrawler()
            vids = cr2.fetch_actor_videos(actor, progress=print)
            cr2.close()
            vids = [v for v in vids if v.vid in wanted]
        else:
            cr2 = TangXinCrawler()
            vids = cr2.fetch_actor_videos(actor, progress=print)
            cr2.close()
        for v in vids:
            try:
                res = dl.download_video(v, out_dir, progress=_progress, get_cover=get_cover)
                if res.get("skipped"):
                    if res.get("existing_elsewhere"):
                        print("  跳过(其它目录已下载): %s  ← %s"
                              % (v.title, res.get("path")))
                    else:
                        print("  跳过(已存在): %s" % v.title)
                else:
                    nfo_mod.write_video_nfo(v, res["path"], res.get("poster"))
                    print("\n  ✔ %s" % os.path.basename(res["path"]))
                ok += 1
            except Exception as e:
                fail += 1
                print("\n  ✘ 失败 %s: %s" % (v.title, e))
    dl.close()
    print("下载结束：成功 %d，失败 %d。" % (ok, fail))
    _print_db_summary()


def main():
    # 日志最先初始化（写文件 + 控制台），启动期异常也能落盘
    log = logger.setup_logging()
    log.info("糖心 Vlog CLI 启动；输出目录=%s 代理=%s CDN走代理=%s 模式=%s",
             config.OUTPUT_BASE, config.PROXY, config.CDN_USE_PROXY, config.DOWNLOAD_MODE)
    # 防止在非 UTF-8 控制台打印中文时崩溃
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="糖心 Vlog 无头下载器")
    p.add_argument("--list-actors", action="store_true", help="列出演员云")
    p.add_argument("--limit", type=int, default=0, help="list-actors 时显示数量")
    p.add_argument("--actor", type=str, default="", help="单个演员名/slug")
    p.add_argument("--actors", type=str, default="", help="多个演员，逗号分隔")
    p.add_argument("--from-file", type=str, default="", help="选择文件 JSON")
    p.add_argument("--output", type=str, default=config.OUTPUT_BASE, help="输出根目录")
    p.add_argument("--workers", type=int, default=config.SEGMENT_WORKERS, help="分片并发数")
    p.add_argument("--no-cover", action="store_true", help="不下载封面")
    p.add_argument("--proxy", type=str, default="", help="覆盖代理地址")
    p.add_argument("--cdn-direct", action="store_true",
                   help="CDN 直连（服务器推荐，跳过代理更快）")
    p.add_argument("--mode", type=str, default=config.DOWNLOAD_MODE,
                   help="下载模式 python/ffmpeg")
    p.add_argument("--known-dir", type=str, action="append", default=[],
                   metavar="DIR", dest="known_dirs",
                   help="已下载过的目录（可重复多次）；命中就跳过，避免重复下载。"
                        "用于「本机先下、后续转移服务器」盘符不同的场景")
    p.add_argument("--copy-existing", action="store_true",
                   help="命中其它目录已存在时，把旧文件搬移到当前输出目录（默认只跳过）")
    p.add_argument("--rescan-existing", action="store_true",
                   help="强制重建已下载索引（扫一遍 --known-dir）后退出")
    args = p.parse_args()

    if args.proxy:
        config.PROXY = args.proxy
    if args.cdn_direct:
        config.CDN_USE_PROXY = False
    config.SEGMENT_WORKERS = args.workers
    config.DOWNLOAD_MODE = args.mode
    config.OUTPUT_BASE = args.output

    # ---- 已下载识别（跨根目录去重）----
    known = list(getattr(config, "KNOWN_DIRS", []) or [])
    for d in args.known_dirs:
        if d and d not in known:
            known.append(d)
    config.KNOWN_DIRS = known
    if args.copy_existing:
        config.COPY_EXISTING = True
    idx = existing.apply_known_dirs(known)
    if args.rescan_existing:
        n = idx.refresh(on_progress=lambda root, cnt: print("  扫描 %s … %d 个文件" % (root, cnt)))
        st = idx.stats()
        print("已下载索引：%d 个目录，%d 个文件（按 vid 可匹配 %d 个）。"
              % (st["dirs"], st["files"], st["by_vid"]))
        print("索引库：%s" % db.get_db_path())
        return
    # 后台预热索引（不阻塞；扫不完就按未命中处理，绝不阻断下载）
    if known:
        started = idx.ensure_ready()
        log.info("已下载识别：%d 个已知目录%s", len(known),
                 "，后台重建索引中…" if started else "，使用现有索引")

    if args.list_actors:
        cmd_list_actors(args)
        return
    if args.from_file:
        cmd_from_file(args.from_file, args.output, args.workers, not args.no_cover)
        return
    names = []
    if args.actor:
        names.append(args.actor)
    if args.actors:
        names.extend([x.strip() for x in args.actors.split(",") if x.strip()])
    if names:
        cmd_download_actors(names, args.output, args.workers, not args.no_cover)
        return
    p.print_help()


if __name__ == "__main__":
    main()
