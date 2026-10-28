# -*- coding: utf-8 -*-
"""后端门面：在同一进程内安全加载 modules/ 下的四个后端。

为什么要「门面」而不是直接 import：
  * 各后端原本是独立工程，内部用 ``from core import ...`` / ``from modules import ...``
    这类**顶级包名**，两个工程同时导入会互相覆盖。合并时已把它们改名成唯一
    命名空间（``hgcore`` / ``psp``），这里统一注入 sys.path。
  * 糖心/黄果的目录、代理、下载路径由前端设置决定，必须在**首次 import 之前**
    写进环境变量，否则模块级常量会固化旧值。

所有函数都是惰性 + 线程安全；导入失败抛 ``BackendError``（带可读原因）。
"""
from __future__ import annotations

import asyncio
import importlib
import os
import re
import sys
import threading
from types import SimpleNamespace

from . import paths
from .settings import settings


class BackendError(RuntimeError):
    """后端不可用（缺依赖、路径不对等），消息可直接展示给用户。"""


_LOCK = threading.RLock()
_CACHE: dict[str, SimpleNamespace] = {}
_prepared = False

# yt-dlp 是 PyInstaller onefile，可执行一次 ``--version`` 也会先自解压，实测约
# 3.7 秒。版本在同一进程内不会变化，因此单独加锁并永久缓存首次结果：既避免
# 并发的首页/状态栏各启动一个子进程，也不占用后端模块导入用的 ``_LOCK``。
_YTDLP_VERSION_LOCK = threading.Lock()
_YTDLP_VERSION_CACHE: str | None = None


def prepare() -> None:
    """注入环境变量与 sys.path（幂等）。"""
    global _prepared
    with _LOCK:
        if _prepared:
            return
        s = settings()
        paths.ensure_dirs()

        tx_data = paths.data_dir("tangxin")
        os.environ.update({
            "TX_ROOT": str(paths.ROOT),
            "TX_DATA": str(tx_data),
            "TX_TEMP": str(paths.temp_dir("tangxin")),
            "TX_BIN": str(paths.BIN),
            "TX_OUTPUT": str(s.get("tangxin.output_base") or (tx_data / "downloads")),
            "TX_SETTINGS": str(tx_data / "tangxin_settings.json"),
            "TX_PROXY": s.proxy_url if s.get("tangxin.proxy_enabled", True) else "",
            "DF_ROOT": str(paths.ROOT),
            "DF_BIN": str(paths.BIN),
            "DF_DATA_ROOT": str(paths.DATA),
            "DF_DATA": str(paths.data_dir("huangguo")),
            "DF_DOWNLOAD": str(s.get("huangguo.download_dir")
                               or paths.data_dir("huangguo", "downloads")),
        })

        for p in (paths.MODULES, paths.MODULES / "huangguo",
                  paths.MODULES / "multisite", paths.MODULES / "sites",
                  paths.MODULES / "madouqu",
                  paths.MODULES / "yttiktok", paths.MODULES / "jiuse"):
            sp = str(p)
            if sp not in sys.path:
                sys.path.insert(0, sp)
        if not paths.MODULES.is_dir():
            raise BackendError(
                f"未找到后端代码目录：{paths.MODULES}\n"
                "打包版请把 modules 文件夹放在 exe 同级目录。")
        _prepared = True


def _imp(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        raise BackendError(f"无法加载后端模块 {name}：{exc}") from exc


# ------------------------------------------------------------------ 糖心
def tangxin() -> SimpleNamespace:
    prepare()
    with _LOCK:
        if "tangxin" not in _CACHE:
            _CACHE["tangxin"] = SimpleNamespace(
                config=_imp("tangxin.config"),
                settings=_imp("tangxin.settings"),
                db=_imp("tangxin.db"),
                models=_imp("tangxin.models"),
                crawler=_imp("tangxin.crawler"),
                downloader=_imp("tangxin.downloader"),
                existing=_imp("tangxin.existing"),
                ytdlp_engine=_imp("tangxin.ytdlp_engine"),
                layout=_imp("tangxin.layout"),
                # 内容缓存层（SQLite）：演员云/作品列表缓存优先，避免每次开软件
                # 都重新联网抓 800+ 位演员。模块一直都在，但没暴露 ⇒ 界面层
                # 用不到 ⇒ 表现为「缓存机制好像都没有」。
                cache=_imp("tangxin.cache"),
            )
    ns = _CACHE["tangxin"]
    apply_tangxin_settings(ns)
    return ns


def apply_tangxin_settings(ns: SimpleNamespace | None = None) -> SimpleNamespace:
    """把前端设置同步到糖心后端（线路/代理/输出目录/并发）。"""
    ns = ns or tangxin()
    s = settings()
    try:
        ns.settings.load(apply_to_config=True)
    except Exception:
        pass
    try:
        route = (s.get("tangxin.route_url") or "").strip()
        if not route:
            # 没有手选线路时，优先用导航页解析出的「科学线路」（实测可直连）
            try:
                from . import nav as _nav
                for e in _nav.find_entries("tangxin"):
                    route = e.get("science_base_url") or e.get("base_url") or ""
                    if route:
                        break
            except Exception:
                route = ""
        if route:
            ns.config.set_origin(route)
        cdn = (s.get("tangxin.cdn") or "").strip()
        if cdn:
            ns.config.set_cdn(cdn)
        ns.config.PROXY = s.proxy_url if s.get("tangxin.proxy_enabled", True) else ""
        out = (s.get("tangxin.output_base") or "").strip()
        if out:
            ns.config.OUTPUT_BASE = out
        ns.config.SEGMENT_WORKERS = int(s.get("tangxin.workers") or
                                        getattr(ns.config, "SEGMENT_WORKERS", 8))
    except Exception:
        pass
    return ns


# ------------------------------------------------------------------ 黄果
def huangguo() -> SimpleNamespace:
    prepare()
    with _LOCK:
        if "huangguo" not in _CACHE:
            _CACHE["huangguo"] = SimpleNamespace(
                config=_imp("hgcore.config"),
                settings=_imp("hgcore.settings"),
                library_db=_imp("hgcore.library_db"),
                site_api=_imp("hgcore.site_api"),
                models=_imp("hgcore.models"),
                task_queue=_imp("hgcore.task_queue"),
                mirror_refresh=_imp("hgcore.mirror_refresh"),
                sites=_imp("hgcore.sites"),
                nfo_writer=_imp("hgcore.nfo_writer"),
                applog=_imp("hgcore.applog"),
                sync_mgr=_imp("hgcore.sync_mgr"),
                update_check=_imp("hgcore.update_check"),
                library_scan=_imp("hgcore.library_scan"),
                media_move=_imp("hgcore.media_move"),
                site_db=_imp("hgcore.site_db"),
                site_discovery=_imp("hgcore.site_discovery"),
                downloader=_imp("hgcore.downloader"),
                # 短剧平台聚合层：多站统一入库 + 跨站去重（「只提取独有的」）
                platform=_imp("hgcore.platform"),
                # 下面三个以前没暴露，导致「列表缓存」「封面解密」在界面层完全用不到：
                # hgcore/cache.py 被移植了却没有任何调用方，封面又是加密图，
                # 直接 QPixmap(url) 只能拿到一堆解不开的字节（＝封面一个都不显示）。
                cache=_imp("hgcore.cache"),
                media_crypto=_imp("hgcore.media_crypto"),
                http_client=_imp("hgcore.http_client"),
            )
    ns = _CACHE["huangguo"]
    apply_huangguo_settings(ns)
    return ns


def apply_huangguo_settings(ns: SimpleNamespace | None = None) -> SimpleNamespace:
    ns = ns or huangguo()
    s = settings()
    try:
        data = ns.settings.load()
        # 黄果的代理开关由「地址同步中心」按直连校验结果决定：
        # 镜像直连能过 → 关掉代理（下载更快）；过不了 → 保持代理，别硬关。
        hg_proxy = s.get("huangguo.proxy_enabled", None)
        if hg_proxy is None:
            hg_proxy = s.get("proxy.enabled", True)
        data["proxy_url"] = s.proxy_url if hg_proxy else ""
        data["concurrency"] = int(s.get("huangguo.concurrency") or data.get("concurrency") or 3)
        dl = (s.get("huangguo.download_dir") or "").strip()
        if dl:
            data["download_dir"] = dl
        sid = (s.get("huangguo.site_id") or "").strip()
        if sid:
            data["site_id"] = sid
        ns.config.apply_settings(data)
        ns.config.ensure_dirs()
        # 首次运行 / 空库时把表结构建好，否则查询会报 "no such table: dramas"
        ns.library_db.init_db()
    except Exception:
        pass
    return ns


def huangguo_site_meta() -> dict:
    """当前生效站点的展示信息（站点名 / 域名 / 代理状态）。"""
    ns = huangguo()
    cfg = ns.config
    site = getattr(cfg, "ACTIVE_SITE", {}) or {}
    return {
        "site_id": getattr(cfg, "ACTIVE_SITE_ID", "") or site.get("id", ""),
        "site_name": site.get("name", "") or site.get("id", ""),
        "base_url": getattr(cfg, "BASE_URL", ""),
        "mirrors": list(site.get("mirrors") or [])[:6],
    }


def huangguo_sync_targets() -> list:
    """外盘同步目标：优先取设置，其次自动识别移动硬盘。"""
    s = settings()
    out = []
    for role, key in (("download", "huangguo.sync_download"),
                      ("server", "huangguo.sync_server")):
        p = (s.get(key) or "").strip()
        if p:
            out.append({"role": role, "path": p})
    if not out:
        try:
            auto = huangguo().sync_mgr.detect_removable_target()
            if auto:
                out.append({"role": "download", "path": auto})
        except Exception:
            pass
    return out


# ------------------------------------------------------------------ 多站点
def multisite() -> SimpleNamespace:
    """多站点**共享解析引擎**（不含任何站点定义：站点在 modules/sites/<key>）。"""
    prepare()
    with _LOCK:
        if "multisite" not in _CACHE:
            _CACHE["multisite"] = SimpleNamespace(
                registry=_imp("multisite.engine.registry"),
                service=_imp("multisite.engine.service"),
            )
    return _CACHE["multisite"]


def site_service():
    """取多站点解析服务单例（代理跟随设置，直连站点可显式置空）。"""
    s = settings()
    proxy = s.proxy_url if s.get("sites.proxy_enabled", True) else ""
    return multisite().service.get_site_service(proxy=proxy or None)


# ------------------------------------------------------------------ 单站点模块
def sites_pkg() -> SimpleNamespace:
    """``modules/sites``：一个域名一个模块的注册表。"""
    prepare()
    with _LOCK:
        if "sites_pkg" not in _CACHE:
            _CACHE["sites_pkg"] = SimpleNamespace(mod=_imp("sites"))
    return _CACHE["sites_pkg"]


def site_modules() -> list:
    """按界面顺序返回全部站点模块对象（每个都带 label/domains/content/db()）。"""
    return list(sites_pkg().mod.all())


def site_module(key: str):
    """按 key 取单个站点模块；不存在抛 BackendError（给页面明确报错）。"""
    mod = sites_pkg().mod.get(key)
    if mod is None:
        raise BackendError(f"未知站点模块：{key}")
    return mod


def site_list() -> list:
    """给侧栏 / 下拉用的站点摘要。"""
    return [{
        "key": m.key,
        "label": m.label,
        "domain": m.domain,
        "homepage": m.homepage,
        "content": m.content,
        "needs_site_api": bool(m.needs_site_api),
    } for m in site_modules()]


def _norm_date(value) -> str:
    """把 ``2022-06-03T22:00:00+00:00`` / ``20220603`` 统一成 ``2022-06-03``。"""
    s = str(value or "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def resolve_meta(url: str, proxy: str | None = None) -> dict:
    """解析单个视频页，返回前端表格要的全部字段（含直链）。

    两条链路按站点分流：
      * yt-dlp 没有 extractor 的站点（porntrex / xfreehd / thumbzilla）
        → 站点包 ``unofficial-api`` 解析直链；
      * 其余站点 → yt-dlp ``-J``（一次拿到标题/时长/直链）。

    之后再拿站点包「补空」：有的站点 yt-dlp 不给观看数/日期，而
    ``unofficial-api`` 恰好给（实测 xvideos：yt-dlp 无 views/date，
    eaf 有 views=145,746、publish_date=2022-06-03）。
    """
    svc = site_service()
    spec = svc.detect(url)
    data: dict = {}

    if spec is not None and spec.needs_eaf_resolution:
        r = asyncio.run(svc.resolve(url, proxy=proxy))
        if r is not None:
            data = r.to_dict()
    else:
        data = dict(ytdlp().service.info_meta(url, proxy=proxy))

    if spec is not None and not spec.needs_eaf_resolution:
        sparse = not (data.get("author") and data.get("views") and data.get("date")
                      and data.get("categories"))
        if sparse:
            try:
                from multisite.engine import eaf_adapter

                if eaf_adapter.is_available(spec):
                    extra = asyncio.run(eaf_adapter.fetch_video(
                        spec, url, proxy=proxy, timeout=20))
                    if extra is not None:
                        d = extra.to_dict()
                        for src, dst in (("author", "author"), ("views", "views"),
                                         ("publish_date", "date"),
                                         ("duration", "duration")):
                            if not data.get(dst) and d.get(src):
                                data[dst] = d[src]
                        if not data.get("categories") and d.get("categories"):
                            data["categories"] = list(d["categories"])
                        if not (data.get("direct_urls") or data.get("direct_url")):
                            if d.get("direct_urls"):
                                data["direct_urls"] = list(d["direct_urls"])
            except Exception:
                pass

    if data.get("date"):
        data["date"] = _norm_date(data["date"])
    if data.get("publish_date"):
        data["publish_date"] = _norm_date(data["publish_date"])
    return data


_PLAY_URL_RE = re.compile(
    r'(https?://[^\s"\'<>\\]+?\.(?:m3u8|mp4)(?:\?[^\s"\'<>\\]*)?)', re.I)


def _is_jiuse_host(host: str) -> bool:
    """九色（91 系）的域名集合：settings 里的当前线路 + 常见别名。"""
    h = (host or "").lower()
    if not h:
        return False
    try:
        active = (settings().get("jiuse.active_url") or "").strip()
        if active and h in active.lower():
            return True
    except Exception:  # noqa: BLE001
        pass
    return any(k in h for k in ("91porn", "jiuse", "91sp", "91video"))


def resolve_direct_fast(url: str, timeout: float = 10.0) -> str:
    """快速直链解析：**一次 GET 页面 + 正则找 m3u8/mp4**。

    为什么要有它：``yt-dlp -J`` 对九色这类站点要 8 秒（先猜 extractor、再试
    多次请求），而详情页 HTML 里本来就明摆着一条 m3u8（实测 2.6s 就能拿到）。
    预览面板要求"点一下 8 秒内出画面"，光解析就把预算吃光了，所以这条快路
    只对**确认过页面内嵌直链**的站点开放（九色 91 系），其余仍走 yt-dlp，
    避免在多广告站点上误取到广告的 .mp4。
    """
    from urllib.parse import urlparse

    host = urlparse(url).netloc.split(":")[0]
    if not _is_jiuse_host(host):
        return ""
    try:
        import requests

        s = requests.Session()
        s.trust_env = True
        r = s.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; "
                                            "Win64; x64) AppleWebKit/537.36 "
                                            "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"},
                  timeout=timeout, verify=False)
        html = r.text or ""
    except Exception:  # noqa: BLE001
        return ""
    found = [m.group(1) for m in _PLAY_URL_RE.finditer(html)]
    if not found:
        return ""
    # m3u8 优先（站点的真实播放源都是 HLS），同类型取第一条
    for u in found:
        if ".m3u8" in u.lower():
            return u
    return found[0]


def warmup_hls(url: str, timeout: float = 8.0) -> bool:
    """预热 HLS：先抓 m3u8，再取第一个分片，把 DNS/TLS/连接都提前建立好。

    为什么需要：QtMultimedia(FFmpeg 后端)起播时自己会去拉 m3u8 + 首个分片，
    实测在跨境线路上这一步要 3~6 秒，而且第一次 TLS 握手失败会直接报
    "Error when loading first segment"（用户看到黑屏）。解析完直链后顺手
    把这两步做掉，真正播放时走的是已建好的连接 —— 首帧能稳定压在 8 秒内。
    """
    if not url or not _is_hls_url(url):
        return False
    try:
        import requests

        s = requests.Session()
        s.trust_env = True
        r = s.get(url, timeout=timeout, verify=False)
        if r.status_code != 200:
            return False
        base = url.rsplit("/", 1)[0] + "/"
        segs = [ln.strip() for ln in (r.text or "").splitlines()
                if ln.strip() and not ln.startswith("#")]
        if not segs:
            return False
        seg = segs[0]
        if not seg.lower().startswith("http"):
            seg = base + seg
        # 只取前 256KB 就够完成握手 + 首包，不必把整片拉下来
        with s.get(seg, timeout=timeout, verify=False, stream=True) as sr:
            got = 0
            for chunk in sr.iter_content(65536):
                got += len(chunk)
                if got >= 262144:
                    break
        return True
    except Exception:  # noqa: BLE001
        return False


def _is_hls_url(url: str) -> bool:
    return (url or "").split("?")[0].split("#")[0].lower().endswith(".m3u8")


def resolve_direct(url: str, proxy: str | None = None) -> str:
    """解析单个视频页，只返回**能直接播放**的直链（预览面板「在线播放」用）。

    选择顺序：
      0. 九色等「页面内嵌直链」站点：一次 GET + 正则（2~3 秒，见 resolve_direct_fast）；
      1. yt-dlp 顶层 ``url``（多数站点给的就是 m3u8，体积小、起播快）；
      2. ``formats`` 里分辨率最高的 http 直链；
      3. 站点包 / resolve_meta 给出的 direct_url(s)。
    返回空串表示解析不出（调用方负责提示），不抛异常。
    """
    if not url:
        return ""
    # ⓪ 快路：只有确认可靠的站点才走，否则容易抓到广告直链
    try:
        fast = resolve_direct_fast(url)
        if fast:
            return fast
    except Exception:  # noqa: BLE001
        pass
    # ① 先走 yt-dlp：能拿到就直接用
    try:
        info = ytdlp().service.probe(url, proxy=proxy)
        raw = getattr(info, "raw", None) or {}
        top = raw.get("url")
        if isinstance(top, str) and top.startswith("http"):
            return top
        best, best_h = "", -1
        for f in (raw.get("formats") or []):
            u = f.get("url")
            if not isinstance(u, str) or not u.startswith("http"):
                continue
            h = f.get("height") or 0
            if h > best_h:
                best, best_h = u, h
        if best:
            return best
    except Exception:  # noqa: BLE001
        pass
    # ② 退回站点包解析
    try:
        data = resolve_meta(url, proxy=proxy)
        for key in ("direct_urls", "direct_url"):
            v = data.get(key)
            if isinstance(v, list) and v:
                return str(v[0])
            if isinstance(v, str) and v.startswith("http"):
                return v
    except Exception:  # noqa: BLE001
        pass
    return ""


# ------------------------------------------------------------------ 麻豆
def madouqu() -> SimpleNamespace:
    """麻豆（磁力爬虫）：独立模块，自带 JSON 库。"""
    prepare()
    with _LOCK:
        if "madouqu" not in _CACHE:
            _CACHE["madouqu"] = SimpleNamespace(
                crawler=_imp("madouqu.core.crawler"),
                db=_imp("madouqu.core.database"),
                models=_imp("madouqu.core.models"),
            )
    return _CACHE["madouqu"]


def madouqu_db():
    """麻豆的库实例（物理目录 DATA/madouqu）。"""
    ns = madouqu()
    return ns.db.MadouquDatabase(db_dir=str(paths.data_dir("madouqu")))


def madouqu_crawler(use_selenium: bool = False):
    """构建麻豆爬虫：默认纯 requests（不依赖 selenium / chromedriver）。"""
    ns = madouqu()
    s = settings()
    cfg = {
        "base_url": (s.get("madouqu.base_url") or "").strip()
                    or "https://madouqu.shop",
        "use_selenium": bool(use_selenium),
        "timeout": int(s.get("madouqu.timeout") or 20),
        "max_retries": 2,
    }
    proxy = s.proxy_url if s.get("madouqu.proxy_enabled", True) else ""
    crawler = ns.crawler.MadouquCrawler(cfg)
    if proxy:
        try:
            crawler.session.proxies.update({"http": proxy, "https": proxy})
        except Exception:
            pass
    return crawler


# ------------------------------------------------------------------ 九色
def jiuse() -> SimpleNamespace:
    """九色：地址页（pages.dev）直连解析 + 站点解析 + 独立库。"""
    prepare()
    with _LOCK:
        if "jiuse" not in _CACHE:
            _CACHE["jiuse"] = SimpleNamespace(
                nav=_imp("jiuse.nav"),
                client=_imp("jiuse.client"),
                site=_imp("jiuse.site"),
                db=_imp("jiuse.db"),
            )
    return _CACHE["jiuse"]


def jiuse_nav_url() -> str:
    from .settings import settings as _s
    return (_s().get("jiuse.nav_url") or "").strip() or "https://dizhi222.pages.dev/"


# ------------------------------------------------------------------ 映取 / yt-dlp
def ytdlp() -> SimpleNamespace:
    prepare()
    with _LOCK:
        if "ytdlp" not in _CACHE:
            _CACHE["ytdlp"] = SimpleNamespace(
                service=_imp("yttiktok.core.ytdlp_service"),
                models=_imp("yttiktok.core.models"),
            )
    return _CACHE["ytdlp"]


def warm_up() -> list:
    """后台预热：把后端先导入好，避免首次切页时卡 2~4 秒。"""
    done = []
    for name in ("tangxin", "huangguo", "multisite", "sites_pkg", "madouqu",
                 "jiuse", "ytdlp"):
        try:
            globals()[name]()
            done.append(name)
        except Exception:
            pass
    return done


def cached_ytdlp_version(default: str = "检测中…") -> str:
    """返回已缓存的 yt-dlp 版本；尚未检测时绝不启动或等待子进程。"""
    # 单个对象引用的读取在 CPython 中是原子的；这里刻意不取查询锁，否则后台
    # 正在跑 3.7 秒子进程时，UI 为读占位文本也会被同一把锁卡住。
    value = _YTDLP_VERSION_CACHE
    return value if value is not None else default


def ytdlp_version() -> str:
    """查询并缓存 yt-dlp 版本。

    此函数本身仍是阻塞调用，供后台 :class:`~launcher.core.tasks.Task` 与命令行
    自检使用；GUI 主线程只能读取 :func:`cached_ytdlp_version`，再异步调用本函数。
    """
    global _YTDLP_VERSION_CACHE
    with _YTDLP_VERSION_LOCK:
        if _YTDLP_VERSION_CACHE is not None:
            return _YTDLP_VERSION_CACHE

        import subprocess

        exe = paths.bin_path("yt-dlp.exe")
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                 timeout=10)
            version = (out.stdout or "").strip() or "未知"
        except Exception:
            version = "不可用"
        _YTDLP_VERSION_CACHE = version
        return version


# ------------------------------------------------------------------ 概览统计
def stats() -> dict:
    """首页/状态栏用的轻量统计（任何一项失败都不影响其他项）。"""
    out: dict = {}

    try:
        ns = tangxin()
        out["tangxin"] = {
            "records": ns.db.count_by_status(),
            "existing": ns.db.count_existing(),
            "origin": getattr(ns.config, "ORIGIN", ""),
        }
    except Exception as exc:  # noqa: BLE001
        out["tangxin"] = {"error": str(exc)}

    try:
        ns = huangguo()
        out["huangguo"] = {
            "dramas": ns.library_db.count_dramas(None),
            "site": huangguo_site_meta(),
            "downloads": len(ns.library_db.recent_downloads(limit=200)),
        }
    except Exception as exc:  # noqa: BLE001
        out["huangguo"] = {"error": str(exc)}

    try:
        out["sites"] = {"count": len(site_list()), "keys": [s["key"] for s in site_list()]}
    except Exception as exc:  # noqa: BLE001
        out["sites"] = {"error": str(exc)}

    try:
        db = madouqu_db()
        st = db.get_stats()
        out["madouqu"] = {
            "models": st.get("model_count", 0),
            "videos": st.get("video_count", 0),
            "magnets": st.get("magnet_count", 0),
            "base_url": (settings().get("madouqu.base_url") or ""),
        }
    except Exception as exc:  # noqa: BLE001
        out["madouqu"] = {"error": str(exc)}

    try:
        ns = jiuse()
        st = ns.nav.current()
        out["jiuse"] = {
            "addresses": len(st.get("addresses") or []),
            "active": st.get("active", ""),
            "updated_at": st.get("updated_at", 0),
        }
    except Exception as exc:  # noqa: BLE001
        out["jiuse"] = {"error": str(exc)}

    out["ytdlp"] = ytdlp_version()
    return out
