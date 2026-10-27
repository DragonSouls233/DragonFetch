# -*- coding: utf-8 -*-
"""命令行刷新备用域名：读取官方发布页 → 内容校验 → 更新配置。

用途
----
* 「发现打不开了」时，双击项目根 / J 盘的「更新域名.bat」，一键更新到最新。
* 也可手动跑::

      <venv>/python.exe tools/refresh_domains.py
      <venv>/python.exe tools/refresh_domains.py --no-proxy    # 完全不使用系统代理
      <venv>/python.exe tools/refresh_domains.py --no-save     # 只预览，不写配置

与设置面板「从发布页刷新」按钮共用同一套逻辑（core/mirror_refresh.refresh）。

关于「要不要先开全局代理」
------------------------
* **读发布页**：直连优先；直连读不到时才回退系统代理（除非 --no-proxy）。
  所以直连正常时，开不开代理结果一样；直连读不到发布页时，开代理才有用。
* **校验域名**：一律直连（与下载走的通道一致）。
  因此哪怕开着全局代理刷新，写进列表的也一定是**本机能直接下载**的地址，
  不会出现「看着能用、实际下不了」。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LINE = "-" * 68


def _print_result(res: dict, no_save: bool) -> int:
    from hgcore import config

    print(LINE)
    page = res.get("publish_page_used")
    if page:
        via = "（经系统代理读取）" if res.get("fetched_via_proxy") else "（直连读取）"
        print(f"  发布页     : {page} {via}")
    else:
        print("  发布页     : 未取到")

    added = res.get("added") or []
    removed = res.get("removed") or []
    mirrors = res.get("mirrors") or []
    print(f"  新增可用   : {len(added)} 个" + ("  " + "、".join(added) if added else ""))
    print(f"  剔除失效   : {len(removed)} 个" + ("  " + "、".join(removed) if removed else ""))
    print(f"  当前可用   : {len(mirrors)} 个")
    for u in mirrors:
        print(f"        - {u}")

    if res.get("switched_to"):
        print(f"  >> 原域名已失效，已自动切换为：{res['switched_to']}")
    if res.get("proxy_only"):
        print("  !! 注意：以下域名本机直连不通、只有经代理才能打开，已排除：")
        for u in res["proxy_only"]:
            print(f"        - {u}")
    if res.get("errors"):
        print("  备注       : " + "；".join(str(e) for e in res["errors"][:4]))

    print(LINE)
    print(f"  生效域名   : {config.BASE_URL}")

    ok = bool(mirrors) or bool(res.get("switched_to"))
    if ok:
        if no_save:
            print("  结果       : 预览完成（--no-save，未写入配置）")
        else:
            print("  结果       : 已写入配置 ✅")
            print("  生效方式   : 重启 App 后生效（运行中的界面仍用旧域名）")
    else:
        print("  结果       : ❌ 没有取到任何可用域名，配置保持不变")
        print("  建议       : 1) 确认网络能打开发布页；2) 开全局代理后再跑一次本脚本；")
        print("               3) 若仍失败，把上面的「备注」发出来排查。")
    return 0 if ok else 1


def main() -> int:
    try:                                  # 控制台编码异常时用替换字符，避免直接崩
        sys.stdout.reconfigure(errors="replace")   # type: ignore[attr-defined]
    except Exception:                     # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(
        description="从官方发布页刷新备用域名（读取 → 内容校验 → 更新配置）")
    ap.add_argument("--site", default=None, help="站点 id，默认当前生效站点")
    ap.add_argument("--timeout", type=int, default=8, help="单次请求超时秒数（默认 8）")
    ap.add_argument("--no-proxy", action="store_true",
                    help="完全不使用系统代理：连发布页都强制直连")
    ap.add_argument("--no-save", action="store_true",
                    help="只预览结果，不写入配置文件")
    args = ap.parse_args()

    from hgcore import config, mirror_refresh

    sid = args.site or config.ACTIVE_SITE_ID
    print("=" * 68)
    print("  黄果短剧 · 备用域名刷新（读取官方发布页 → 校验 → 更新）")
    print("=" * 68)
    print(f"  站点       : {config.ACTIVE_SITE.get('name')} ({sid})")
    print(f"  当前域名   : {config.BASE_URL}")
    print(f"  已保存域名 : {len(config.SITE_MIRRORS.get(sid) or [])} 个")
    print(f"  代理回退   : {'关闭（强制直连）' if args.no_proxy else '开启（仅在直连读不到发布页时使用）'}")
    print(LINE)

    def log(msg: str) -> None:
        print("  " + str(msg))

    try:
        res = mirror_refresh.refresh(
            site_id=args.site,
            timeout=args.timeout,
            save=not args.no_save,
            allow_proxy=not args.no_proxy,
            log=log,
        )
    except Exception as exc:                      # noqa: BLE001
        print(LINE)
        print(f"  ❌ 刷新异常：{type(exc).__name__}: {exc}")
        return 2
    return _print_result(res, args.no_save)


if __name__ == "__main__":
    sys.exit(main())
