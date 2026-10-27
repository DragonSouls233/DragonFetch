# -*- coding: utf-8 -*-
"""全局配置：路径、站点常量、网络参数、下载参数。

支持多站点（huangguoai / huangguome）：通过 set_active_site() 切换当前生效站点，
重建 BASE_URL / URL_HOME / CATEGORIES / DOWNLOAD_SITE_DIR 等。其他模块统一通过
config.XXX 动态读取，因此切换站点后无需重新 import 即可生效。

站点地址、代理、并发数等均可通过 settings.py 从本地配置文件读取，运行时可修改
（GUI 设置面板）并持久化保存。
"""

import os
from pathlib import Path

# 镜像站点使用自签/不受信证书，访问时关闭了证书校验（verify=False）。
# 屏蔽由此产生的 InsecureRequestWarning，避免日志被刷屏、误导用户以为报错。
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 站点注册表（多站点区分的核心）
from . import sites as _sites


# ---------------------------------------------------------------- 路径

# 应用目录（modules/huangguo/）
APP_DIR = Path(__file__).resolve().parent.parent

# 项目根：默认 = 模块目录；前端注入 DF_ROOT（DragonFetch 根）以共享 bin/
ROOT_DIR = Path(os.environ.get("DF_ROOT") or APP_DIR)

# 数据目录：默认 = <模块>/DATA（目录联接指向 <ROOT>/DATA/huangguo）
DATA_DIR = Path(os.environ.get("DF_DATA") or (APP_DIR / "DATA"))
DB_PATH = DATA_DIR / "hg.db"         # 剧库 / 下载 / 集状态 主数据库
SITE_DB_PATH = DATA_DIR / "site.db"  # 站点 URL 候选与健康检查（独立 DB，便于迁移与备份）

# 工具目录（共享 <ROOT>/bin，ffmpeg / yt-dlp）
BIN_DIR = Path(os.environ.get("DF_BIN") or (ROOT_DIR / "bin"))

YTDLP_PATH = BIN_DIR / "yt-dlp.exe"
FFMPEG_PATH = BIN_DIR / "ffmpeg.exe"
FFPROBE_PATH = BIN_DIR / "ffprobe.exe"

# 下载输出根目录（可被 settings 的 download_dir 覆盖，见 set_download_root）
DOWNLOAD_DIR = Path(os.environ.get("DF_DOWNLOAD") or (DATA_DIR / "downloads"))

# 是否把每集放进 "Season 01/" 子目录（Kodi/Emby/Jellyfin 标准结构）。
# 默认 False：保持 <剧名>/<剧名> S01E01.mp4 平铺，避免改变路径导致断点续传失效。
SEASON_FOLDER = False

# 外盘同步目标盘（增量镜像「近期更新」的剧集到其它盘符）。
# 由 apply_settings 从设置文件读取；空串 = 不同步到该盘。
SYNC_DOWNLOAD_DIR = ""   # 下载盘（备份盘）
SYNC_SERVER_DIR = ""      # 服务器运行盘

# 封面缓存目录（按站点分目录，did 命名）
CACHE_DIR = DATA_DIR / "covers"

# 列表结果缓存目录（L2 磁盘缓存，key = site_id + url 的哈希）
LIST_CACHE_DIR = DATA_DIR / "cache" / "lists"

# 运行日志目录（一天一个文件：YYYY-MM-DD.log）
LOG_DIR = DATA_DIR / "logs"

# 下载暂存目录（SSD，项目本地）：片段下载与音视频合并在此完成，
# 最终 MP4 再移动到 HDD 下载目录，避免在机械硬盘上做随机 IO 密集的合并。
TEMP_DIR = DATA_DIR / "tmp"

# 测试脚本与测试产物目录（放在项目主目录下，便于统一查看）
TESTS_DIR = ROOT_DIR / "tests"


# ---------------------------------------------------------------- 当前生效站点

ACTIVE_SITE_ID = _sites.SITE_HUANGUOAI["id"]
ACTIVE_SITE = _sites.SITE_HUANGUOAI

# 站点定义的公开访问器：其他模块统一经 config.get_site(...) 取站点，避免各自
# 直接 import sites。注：mirror_candidates / publish_pages 里早就裸调 get_site，
# 但一直没 import —— 只有在显式传 site_id 时才会触发 NameError（历史遗漏，此处补齐）。
get_site = _sites.get_site

# 这些常量由 _rebuild_urls 依据 ACTIVE_SITE 重建
BASE_URL = ACTIVE_SITE["base_url"]

URL_HOME = ACTIVE_SITE["home_url"]
URL_DETAIL = BASE_URL + "/detail/{did}/"
URL_PLAY = BASE_URL + "/video/{did}/"
URL_PLAY_EP = BASE_URL + "/video/{did}/ep-{ep}/"   # 第 1 集无后缀
URL_SEARCH = BASE_URL + "/search/video/{kw}/"
URL_TOPIC = BASE_URL + "/topics/{slug}/"

# 分类 TAB（顺序即界面显示顺序），由 _rebuild_urls 依据 ACTIVE_SITE 重建
CATEGORIES = []   # 实际内容在 _rebuild_urls 中填充

# 当前站点的下载子目录（区分不同站点存储）
DOWNLOAD_SITE_DIR = DOWNLOAD_DIR / ACTIVE_SITE["download_subdir"]

# 备用域名（镜像）管理：
#   AUTO_PICK_MIRROR   —— 启动时 / 下载前自动挑一个「可达」的备用域名，跳过被屏蔽的
#   SITE_MIRRORS       —— 用户手动添加的备用域名 {site_id: [url, ...]}，持久化在设置里
#   AUTO_REFRESH_MIRROR—— 本机现有域名「全部不可达」时，自动去读一次官方发布页
#                         （「回家的路」）拿最新域名，而不是干等着用户手动改。
#                         这是「发现不行了 → 自己更新到最新」的自动闭环。
AUTO_PICK_MIRROR = True
SITE_MIRRORS = {}
AUTO_REFRESH_MIRROR = True

# 自动刷新的节流：同一进程内 N 秒最多触发一次发布页刷新，
# 避免「每个 worker / 每次请求失败都去联网读发布页」把站点打爆、也拖慢任务。
_AUTO_REFRESH_MIN_INTERVAL = 600          # 秒
_LAST_AUTO_REFRESH = 0.0


# ---------------------------------------------------------------- 网络

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": BASE_URL + "/",
}

TIMEOUT = 20          # 单次请求超时（秒）
RETRY = 3             # 失败重试次数
RETRY_DELAY = 1.0     # 重试基础间隔（秒）

# 可选代理（默认空 = 直连，站点已验证在国内可直接访问）
# 配置优先级: 设置文件(.hg_config.json) > 环境变量 HG_PROXY > 空
PROXY_URL = os.environ.get("HG_PROXY", "").strip()

# ---------------------------------------------------------------- 下载

DEFAULT_CONCURRENCY = 3     # 同时下载的集数
HLS_CONCURRENT_FRAGMENTS = 12   # m3u8 分片并发
YTDLP_TIMEOUT = 3600        # 单集下载超时（秒）
YTDLP_COMMON_ARGS = []      # 由 _rebuild_urls 填充

# 批量准备阶段的并发抓取线程数（只抓详情页，不是下载）。
# 8 → 12：多选上百部剧时，这一阶段直接决定「点完下载后要等多久才动起来」。
BATCH_PREPARE_WORKERS = 12

# 元数据归纳时抓封面的超时（秒）。封面只是刮削资源，缺失不影响视频，
# 旧值 15s 会让下载线程在最坏情况下白等 30s（两个封面源），故收紧。
META_COVER_TIMEOUT = 8

# 下载限速（KB/s）。空字符串 = 不限速。借鉴 Ghost-Downloader-3 的速率控制，
# 通过 yt-dlp --rate-limit 生效，避免单任务占满带宽。
DOWNLOAD_RATE_LIMIT = ""    # 形如 "2048"，单位 KB/s

# ---------------------------------------------------------------- 界面

COVER_WIDTH = 160     # 剧集卡片封面宽
COVER_HEIGHT = 224    # 剧集卡片封面高（9:12 比例附近）
GRID_COLUMNS = 6      # 网格默认列数


# ---------------------------------------------------------------- 动态重建

DEFAULTS_BASE = "https://huangguoai.com"


def _rebuild_urls() -> None:
    """依据当前 ACTIVE_SITE 重建所有 URL 与通用参数、分类。"""
    global URL_HOME, URL_DETAIL, URL_PLAY, URL_PLAY_EP, URL_SEARCH, URL_TOPIC
    global YTDLP_COMMON_ARGS, CATEGORIES, DOWNLOAD_SITE_DIR, DEFAULT_HEADERS

    base = BASE_URL
    # URL_HOME 必须跟随当前生效的 BASE_URL（含镜像切换）：
    # 旧实现取自 ACTIVE_SITE["home_url"]（sites.py 写死的站点首页），切到镜像后
    # 不会跟着变，导致首页/分类浏览仍打被墙主域 →「读取不到」。现强制从 base 派生，
    # 保证 BASE_URL 是镜像时 URL_HOME 也是镜像。
    URL_HOME = base + "/"
    ACTIVE_SITE["home_url"] = URL_HOME
    URL_DETAIL = base + "/detail/{did}/"
    URL_PLAY = base + "/video/{did}/"
    URL_PLAY_EP = base + "/video/{did}/ep-{ep}/"
    URL_SEARCH = base + "/search/video/{kw}/"
    URL_TOPIC = base + "/topics/{slug}/"

    DEFAULT_HEADERS["Referer"] = base + "/"

    YTDLP_COMMON_ARGS[:] = [
        "--no-warnings",
        "--no-playlist",
        "--no-part",             # 不保留 .part 临时文件
        "--concurrent-fragments", str(HLS_CONCURRENT_FRAGMENTS),   # 分片并发
        "--retries", "10",
        "--fragment-retries", "10",
        # 站点 CDN 偶发掐断连接（日志里的 IncompleteRead: 0 bytes read），
        # 加上抓取阶段与文件写入的重试、以及重试间隔，降低网络抖动导致的失败率
        "--extractor-retries", "5",
        "--file-access-retries", "3",
        "--retry-sleep", "2",
        "--newline",             # 逐行输出，便于解析进度
    ]

    # 分类由当前站点提供（标签, 基于 base 拼接的路径）
    CATEGORIES[:] = [
        (name, base + path) for name, path in ACTIVE_SITE.get("categories", [])
    ]

    DOWNLOAD_SITE_DIR = DOWNLOAD_DIR / ACTIVE_SITE.get("download_subdir", "default")


def set_active_site(site: dict) -> None:
    """切换当前生效站点并重建所有相关常量。

    会就地修改模块级全局变量；已 import 进来的其他模块通过 config.XXX
    动态读取，因此都能立即生效。
    """
    global BASE_URL, ACTIVE_SITE_ID, ACTIVE_SITE
    ACTIVE_SITE = site
    ACTIVE_SITE_ID = site["id"]
    BASE_URL = site["base_url"].rstrip("/")
    _rebuild_urls()


def category_url(marker: str, default: str = "") -> str:
    """按路径特征取当前站点的分类地址（如 marker="/ranks/"、"/topics/"）。

    比「按下标取 CATEGORIES[4]」更稳：站点分类顺序调整、或不同站点分类项
    数量不一致时，排行榜 / 专题不会取到错的页面。取不到时返回 default，
    再退到当前站点首页。
    """
    for _name, url in CATEGORIES:
        if marker in url:
            return url
    return default or (ACTIVE_SITE.get("home_url") or BASE_URL)


def set_download_root(path_str: str) -> str:
    """设置下载根目录（来自设置面板 / 设置文件）。

    接受绝对路径或相对项目根的路径；空串则回退到默认 <ROOT>/downloads。
    返回实际生效的绝对路径字符串（供回显）。
    """
    global DOWNLOAD_DIR
    raw = (path_str or "").strip()
    if not raw:
        DOWNLOAD_DIR = ROOT_DIR / "downloads"
    else:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = ROOT_DIR / p
        DOWNLOAD_DIR = p
    _rebuild_urls()
    return str(DOWNLOAD_DIR)


def apply_settings(s: dict) -> None:
    """应用一份设置字典（来自 settings.load / 设置面板）。

    会就地修改模块级全局变量；已 import 进来的其他模块通过 config.XXX 动态读取，
    因此都能立即生效（注意引用的是属性而非本地副本）。
    """
    global BASE_URL, PROXY_URL, DEFAULT_CONCURRENCY, HLS_CONCURRENT_FRAGMENTS
    global SEASON_FOLDER, DOWNLOAD_RATE_LIMIT, TEMP_DIR
    global SYNC_DOWNLOAD_DIR, SYNC_SERVER_DIR
    global AUTO_PICK_MIRROR, SITE_MIRRORS, AUTO_REFRESH_MIRROR

    # 若提供了 base_url / home_url，则视为覆盖「当前站点」的地址
    if s.get("base_url"):
        BASE_URL = s.get("base_url").rstrip("/")
        ACTIVE_SITE["base_url"] = BASE_URL
    if s.get("home_url"):
        ACTIVE_SITE["home_url"] = s.get("home_url")

    PROXY_URL = (s.get("proxy_url") or os.environ.get("HG_PROXY", "")).strip()
    DEFAULT_CONCURRENCY = max(1, int(s.get("concurrency") or 3))
    HLS_CONCURRENT_FRAGMENTS = max(1, int(s.get("fragments") or 12))
    SEASON_FOLDER = bool(s.get("season_folder") or False)

    # 下载限速（KB/s）：0 或空 = 不限速
    rate = int(s.get("rate_limit") or 0)
    DOWNLOAD_RATE_LIMIT = str(rate) if rate and rate > 0 else ""

    # 暂存目录（SSD）：自定义优先，否则回退到 DATA/tmp
    temp_dir = (s.get("temp_dir") or "").strip()
    if temp_dir:
        p = Path(temp_dir).expanduser()
        if not p.is_absolute():
            p = ROOT_DIR / p
        TEMP_DIR = p
    else:
        TEMP_DIR = DATA_DIR / "tmp"

    # 下载目录（空 = 默认）
    set_download_root(s.get("download_dir") or "")

    # 外盘同步目标盘（空 = 不同步到该盘）
    SYNC_DOWNLOAD_DIR = (s.get("sync_download_dir") or "").strip()
    SYNC_SERVER_DIR = (s.get("sync_server_dir") or "").strip()

    # 备用域名（镜像）：自动挑选 + 自动读发布页刷新 + 用户手动添加的列表
    AUTO_PICK_MIRROR = bool(s.get("auto_pick_mirror", True))
    AUTO_REFRESH_MIRROR = bool(s.get("auto_refresh_mirror", True))
    raw_mirrors = s.get("site_mirrors") or {}
    SITE_MIRRORS = {k: list(v) for k, v in raw_mirrors.items() if v}

    _rebuild_urls()


# 启动时应用已保存设置（站点选择由 site_discovery 在启动后覆盖）
from . import settings as _settings  # noqa: E402  (放置在函数定义之后)


def _site_domain_of(url: str) -> str:
    """提取 URL 的主机名（端口剥离），用于与站点主域比对。"""
    from urllib.parse import urlparse
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:                  # noqa: BLE001
        return ""


def _allowed_mirror_hosts() -> set:
    """当前站点所有「合法基址」的主机名集合：主域 + 内置镜像 + 用户添加的镜像。

    用于 _validate_active_base_url：只要 BASE_URL 命中其中之一就视为合法，
    不被强制回退到主域——这样用户设置的备用域名（镜像）能稳定保留。
    """
    site = ACTIVE_SITE
    hosts = set()
    dom = (site.get("domain") or "").lower()
    if dom:
        hosts.add(dom)
    for u in mirror_candidates():
        h = _site_domain_of(u)
        if h:
            hosts.add(h)
    return hosts


def _validate_active_base_url(canonical_base: str) -> None:
    """若当前 BASE_URL 不合法（既非主域也非任一镜像），回退到原始站点 base_url。

    修复场景：旧版本保存的 base_url 指向另一站点的域名（如 huangguome 站却
    带着 huangguoai 的 base_url），自愈为站点自身的 base_url，避免跨站错位。
    但合法镜像域名（含用户添加的）一律保留，不会被回退。
    canonical_base 必须在 apply_settings 之前从 ACTIVE_SITE 取出，否则会被覆盖。
    """
    global BASE_URL
    host = _site_domain_of(BASE_URL)
    if host and any(host == h or host.endswith("." + h) for h in _allowed_mirror_hosts()):
        return
    BASE_URL = canonical_base.rstrip("/")
    ACTIVE_SITE["base_url"] = BASE_URL
    _rebuild_urls()


def mirror_candidates(site_id: str = None) -> list:
    """当前站点的全部可用基址（去重、保序）：规范域名 + 内置镜像 + 用户添加的镜像。"""
    site = get_site(site_id) if site_id else ACTIVE_SITE
    if not site:
        return []
    urls = [site.get("base_url", "")]
    urls += list(site.get("mirrors", []) or [])
    urls += list(SITE_MIRRORS.get(site.get("id"), []) or [])
    seen, out = set(), []
    for u in urls:
        u = (u or "").strip().rstrip("/")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def publish_pages(site_id: str = None) -> list:
    """当前站点的「发布页 / 中转页」列表（去重、保序）。

    这些页面**不是剧集站**（无 hg-drama-card），只能用来读取最新入口域名，
    绝不可作为 base_url。见 core/mirror_refresh.py。
    """
    site = get_site(site_id) if site_id else ACTIVE_SITE
    if not site:
        return []
    seen, out = set(), []
    for u in (site.get("publish_pages") or []):
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def probe_reachable(url: str, timeout: int = 5) -> bool:
    """快速探测某个基址是否可达（不重试，超时即视为不可达）。

    与下载走的 http_client 不同：探测用一次性短超时请求，避免被屏蔽时
    因多次重试而长时间卡住。返回布尔。
    """
    import requests  # 延迟导入，避免对无网络环境（测试）产生硬依赖
    try:
        requests.get(
            url.rstrip("/") + "/",
            headers={"User-Agent": USER_AGENT, "Referer": url.rstrip("/") + "/"},
            timeout=timeout,
            verify=False,
        )
        return True
    except Exception:                          # noqa: BLE001
        return False


def _apply_base_url(url: str) -> str:
    """把当前生效基址切到 url，并同步 ACTIVE_SITE / 重建派生 URL。

    统一出口，避免各处「改了 BASE_URL 却忘了 home_url / CATEGORIES」的不一致。
    """
    global BASE_URL
    BASE_URL = (url or "").strip().rstrip("/")
    ACTIVE_SITE["base_url"] = BASE_URL
    ACTIVE_SITE["home_url"] = BASE_URL + "/"
    _rebuild_urls()
    return BASE_URL


def _should_auto_refresh() -> bool:
    """自动刷新节流：同一进程内 ``_AUTO_REFRESH_MIN_INTERVAL`` 秒最多触发一次。

    防止「每个 worker / 每次请求失败都去联网读发布页」——那会把站点打爆、
    也会让任务莫名变慢。第一次调用总是放行。
    """
    global _LAST_AUTO_REFRESH
    import time as _time
    now = _time.time()
    if now - _LAST_AUTO_REFRESH < _AUTO_REFRESH_MIN_INTERVAL:
        return False
    _LAST_AUTO_REFRESH = now
    return True


def resolve_active_base_url(auto_refresh: bool = False) -> str:
    """按 AUTO_PICK_MIRROR 选择可达基址。

    规则：
      - 关闭自动挑选 → 保持当前 BASE_URL（尊重用户手动固定）。
      - 开启（默认）→ 当前 BASE_URL 若可达则保留（尊重用户固定）；
        否则依次探测镜像候选，第一个可达者生效并写回 ACTIVE_SITE / 重建 URL。
      - 全不可达 且 ``auto_refresh=True`` 且 ``AUTO_REFRESH_MIRROR`` 开启 →
        自动读一次官方发布页刷新域名（带节流），再用刷新结果切换。
        —— 这就是「发现不行了，程序自己更新到最新」的入口，无需手动改代码。
      - 仍全不可达 → 保持原值（保证至少有东西可连）。
    返回最终生效的 BASE_URL。
    """
    global BASE_URL
    if not AUTO_PICK_MIRROR:
        return BASE_URL
    if probe_reachable(BASE_URL):
        return BASE_URL
    for u in mirror_candidates():
        if u == BASE_URL:
            continue
        if probe_reachable(u):
            _apply_base_url(u)
            from . import applog
            applog.info("已切换到可达镜像域名：%s", u)
            return BASE_URL

    # 本地候选全灭 → 可选：读发布页拿最新域名（自动闭环）
    if auto_refresh and AUTO_REFRESH_MIRROR and _should_auto_refresh():
        try:
            from . import applog
            from . import mirror_refresh          # 延迟导入：避免 config ↔ mirror_refresh 循环
            applog.info("现有备用域名全部不可达，自动读取发布页刷新域名…")
            res = mirror_refresh.refresh(save=True)
            if res.get("switched_to"):
                applog.info("已按发布页结果切换域名：%s", res["switched_to"])
            elif res.get("verified"):
                _apply_base_url(res["verified"][0])
                applog.info("已按发布页结果切换域名：%s", BASE_URL)
            else:
                applog.bug("自动刷新域名未取得可用地址：%s", res.get("errors"))
        except Exception as exc:                   # noqa: BLE001
            try:
                from . import applog
                applog.bug("自动刷新域名失败：%s", exc)
            except Exception:                      # noqa: BLE001
                pass
    return BASE_URL


try:
    _saved = _settings.load()
    _saved_site = _sites.get_site(_saved.get("site_id") or "")
    _canonical_base = ACTIVE_SITE["base_url"]
    if _saved_site:
        # 先恢复上次生效的站点（保留其原始 base_url 作自愈依据），再叠加设置
        _canonical_base = _saved_site["base_url"]
        set_active_site(_saved_site)
    apply_settings(_saved)
    _validate_active_base_url(_canonical_base)
except Exception:                 # noqa: BLE001
    _rebuild_urls()


def ensure_dirs() -> None:
    """确保输出与缓存目录存在，并迁移旧版 .cache/ 与 .hg_site.db。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_SITE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_data()


def _migrate_legacy_data() -> None:
    """把早期版本的数据（.cache/covers 与 .hg_site.db）迁移到 DATA 目录。

    仅做一次性 best-effort 复制：源已存在且目标未覆盖才搬，并保留源目录以防回退。
    """
    import shutil
    # 1) 旧封面缓存 -> DATA/covers
    old_cache = ROOT_DIR / ".cache" / "covers"
    if old_cache.is_dir() and old_cache != CACHE_DIR:
        try:
            for site_dir in old_cache.iterdir():
                if not site_dir.is_dir():
                    continue
                dest = CACHE_DIR / site_dir.name
                dest.mkdir(parents=True, exist_ok=True)
                for f in site_dir.iterdir():
                    target = dest / f.name
                    if f.is_file() and not target.exists():
                        shutil.copy2(f, target)
        except OSError:
            pass

    # 2) 旧站点 DB -> DATA/site.db
    old_site_db = ROOT_DIR / ".hg_site.db"
    if old_site_db.is_file() and not SITE_DB_PATH.exists():
        try:
            shutil.copy2(old_site_db, SITE_DB_PATH)
        except OSError:
            pass


def check_tools() -> dict:
    """检查外部工具是否就绪，返回 {名称: (是否可用, 路径)}。"""
    return {
        "yt-dlp": (YTDLP_PATH.is_file(), str(YTDLP_PATH)),
        "ffmpeg": (FFMPEG_PATH.is_file(), str(FFMPEG_PATH)),
        "ffprobe": (FFPROBE_PATH.is_file(), str(FFPROBE_PATH)),
    }
