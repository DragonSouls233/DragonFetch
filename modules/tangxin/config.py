# -*- coding: utf-8 -*-
"""
糖心 Vlog (TangXin) 独立下载工具 —— 全局配置。
本工程与 PornSimilarityPlatform 完全独立：不共享代码、数据库、输出目录。
"""
import os
import sys

# ---- 站点与 CDN ----
# ⚠️ 这两个值会在启动时由 ``publish_page.sync_startup()`` 按发布页刷新
#    （站点改址后自动跟随），也可用 TX_ORIGIN / TX_CDN 环境变量强覆盖。
_DEFAULT_ORIGIN = "https://tangxinvlog.app"
_DEFAULT_CDN = "https://t.5gcdn.xyz"
ORIGIN = (os.environ.get("TX_ORIGIN") or _DEFAULT_ORIGIN).rstrip("/")
CDN = (os.environ.get("TX_CDN") or _DEFAULT_CDN).rstrip("/")
# 繁体站点路径前缀（简体为 "/"，繁体为 "/zh-tw"）。演员云/视频均在 /zh-tw 下。
LOCALE = "/zh-tw"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
REFERER = ORIGIN + "/"

# ---- 发布页（启动时用它解析「官网最新地址」）----
# 默认抓 app.js：daohang.pro 的首页 HTML 只是空壳（纯 JS 渲染），
# 真正的链接表在 app.js 的 CONFIG.links 里。
PUBLISH_PAGES = [p.strip() for p in (
    os.environ.get("TX_PUBLISH_PAGES")
    or "https://daohang.pro/app.js,https://daohang.pro/"
).split(",") if p.strip()]
# 发布页里糖心那条的名字关键词（用于在多条导航里定位糖心）
PUBLISH_KEYWORD = os.environ.get("TX_PUBLISH_KEYWORD", "糖心")


def set_origin(origin: str) -> str:
    """切换站点主域（同时更新 REFERER），返回旧值。

    为什么要函数：REFERER 是派生值，改了 ORIGIN 不同步改它，
    请求头就会带着旧域名的 Referer，站点侧可能直接 403。
    """
    global ORIGIN, REFERER
    old = ORIGIN
    o = (origin or "").strip().rstrip("/")
    if not o:
        return old
    ORIGIN = o
    REFERER = o + "/"
    return old


def set_cdn(cdn: str) -> str:
    """切换 CDN 域名，返回旧值。"""
    global CDN
    old = CDN
    c = (cdn or "").strip().rstrip("/")
    if c:
        CDN = c
    return old

# ---- 代理 ----
# 糖心站点(tangxinvlog.app)在境内被墙，必须经 socks5h 代理（远程解析 DNS）。
# 桌面与服务器 120 均运行 xray，监听 10808。CDN(t.5gcdn.xyz) 在服务器可直连、
# 桌面需走代理；CDN URL 不带 ip= 内嵌，不受 PornHub rule-474 限制，统一走代理最稳妥。
PROXY = os.environ.get("TX_PROXY", "socks5h://127.0.0.1:10808")
# SITE 请求始终走代理；CDN 默认也走代理（全环境一致）。
CDN_USE_PROXY = os.environ.get("TX_CDN_PROXY", "1") != "0"

# ---- 下载模式 ----
# "python"：用 requests 经代理抓取 + cryptography 解密 + ffmpeg 本地转封装（全环境可用）。
# "ffmpeg"：直接让 ffmpeg 拉 m3u8/key/分片并原生解密（需 CDN 直连，服务器更快）。
DOWNLOAD_MODE = os.environ.get("TX_DL_MODE", "python")

# ---- 请求参数 ----
TIMEOUT = 30
RETRIES = 3
SEGMENT_WORKERS = int(os.environ.get("TX_WORKERS", "8"))

# ---- 运行时根目录（打包关键）----
# ⚠️ 打包成 onefile 后，__file__ 指向临时解压目录(_MEIxxxx)，退出即删；
# 若把日志/数据库/下载写在那里会**静默丢失**。因此冻结模式下必须改用
# **exe 所在目录**；非冻结（本地跑源码）则用「包目录的上一级」= 项目/部署根。
def _runtime_root() -> str:
    if getattr(sys, "frozen", False):      # PyInstaller 打包后
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


RUNTIME_ROOT = os.environ.get("TX_ROOT") or _runtime_root()

# ---- 目录职责分离（用户定）----
# bin/  = 外部可执行文件（ffmpeg.exe / yt-dlp.exe / xray/）
# data/ = 数据库与设置（tangxin.db / tangxin_settings.json）
# 以前这些文件全堆在根目录，部署到服务器要手工搬一堆、漏一个就出问题。
#
# ⚠️ **APP_ROOT**（本工具自己的根）与 **RUNTIME_ROOT**（运行根）分开：
#   - 打包后：APP_ROOT = exe 同级目录 ⇒ 产出 <exe同级>/data、<exe同级>/bin
#   - 跑源码：APP_ROOT = tangxin 包目录 ⇒ 产出 tangxin/data、tangxin/bin
#   这样源码运行时**不会把糖心的数据库混进主平台的 data/**（两个工程必须隔离），
#   而打包产物的目录名又统一是 data/ 与 bin/。
#   显式给了 TX_ROOT（部署到自定义根）时，APP_ROOT 就跟着 TX_ROOT 走。
_env_root = os.environ.get("TX_ROOT")
if _env_root:
    APP_ROOT = _env_root
elif getattr(sys, "frozen", False):
    APP_ROOT = os.path.dirname(sys.executable)
else:
    APP_ROOT = os.path.dirname(os.path.abspath(__file__))

BIN_DIR = os.environ.get("TX_BIN") or os.path.join(APP_ROOT, "bin")
DATA_DIR = os.environ.get("TX_DATA") or os.path.join(APP_ROOT, "data")

# ---- ffmpeg ----
def _find_ffmpeg():
    import shutil
    import subprocess
    env = os.environ.get("TX_FFMPEG")
    candidates = []
    if env:
        candidates.append(env)
    # ⭐ 优先取「bin/ 目录」下的 ffmpeg（打包后 = exe 同级 bin/ffmpeg.exe）
    candidates.append(os.path.join(BIN_DIR, "ffmpeg.exe"))
    candidates.append(os.path.join(BIN_DIR, "ffmpeg"))
    # 打包资源目录（onefile 解压目录 / onedir 的 _internal）
    _bundle = getattr(sys, "_MEIPASS", "") or ""
    if _bundle:
        candidates.append(os.path.join(_bundle, "bin", "ffmpeg.exe"))
    # 兼容老布局：exe 同级 / 项目根
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"))
    else:
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ffmpeg.exe"))
    candidates += [
        r"E:\TangXinVlog\bin\ffmpeg.exe",                    # 服务器 120 专用部署目录
        r"E:\TangXinVlog\ffmpeg.exe",
        r"E:\bin\ffmpeg.exe",
        r"E:\ffmpeg.exe",                                   # 服务器 120（可能是符号链接/重解析点）
        r"E:\data\ffmpeg.exe",                               # 服务器数据盘
        r"E:\MDCX-Server\ffmpeg.exe",                        # 同机平台自带
        r"G:\PornSimilarityPlatform\ffmpeg.exe",             # 桌面开发
        "ffmpeg",                                            # PATH
        "ffmpeg.exe",
    ]
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        exe = c
        if c in ("ffmpeg", "ffmpeg.exe"):
            exe = shutil.which(c)
            if not exe:
                continue
        # 用「能否真正运行 -version」来判定，避免符号链接/重解析点导致 isfile 误判
        try:
            r = subprocess.run([exe, "-version"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=8)
            if r.returncode == 0:
                return exe
        except Exception:
            continue
    return "ffmpeg"  # 兜底，期望 PATH 中

FFMPEG = _find_ffmpeg()

# ---- 输出目录 ----
# 演员文件夹 / 视频标题.mp4 + .nfo + -poster.jpg 都放在这里。
OUTPUT_BASE = os.environ.get("TX_OUTPUT", os.path.join(RUNTIME_ROOT, "downloads"))

# ---- 临时下载目录（"先本地、后搬运"的第一站）----
# 与 PornSimilarityPlatform 主平台的 TempDownloadManager 同思路：
# 视频先下载到**本地盘**的临时目录，下载成功后才搬运到 OUTPUT_BASE 下的目标位置。
# 为什么：下载目录常指向 NAS/SMB 网络盘，让 ffmpeg 直接写网络盘既慢，又会在网络
# 抖动时中途失败并留下残片；先落本地可让下载过程不受网络盘影响，失败也只在临时目录。
#
# ⚠️ 目录必须**独立**：主平台用的是 ``temp/downloads``，若糖心共用同一目录，
#    两边的「过期清理 / 孤儿清理」会把对方正在下载的文件当成残留删掉
#    （实测就在该目录里发现了非糖心命名的 ``*.mp4.part`` 残留）。
#    故糖心固定用 ``temp/tangxin``，与 OUTPUT_BASE 一样不共用。
TEMP_DIR = os.environ.get("TX_TEMP", os.path.join(RUNTIME_ROOT, "temp", "tangxin"))

# 日志目录（打包后同样落在 exe 同级 logs/）
LOG_DIR = os.environ.get("TX_LOG_DIR", os.path.join(RUNTIME_ROOT, "logs"))

# ---- 数据库（**统一落在 data/ 目录**）----
# ⚠️ 以前 DB 散在根目录（tangxin_downloads.db / tangxin_cache.db 两个文件），
#    现在合并成 ``data/tangxin.db`` 一个文件：下载记录、已下载索引、演员云缓存、
#    作品缓存全在里面 —— 单文件、单连接、备份/同步都简单。
#    老文件由 tangxin/layout.py 自动迁进 data/。
DOWNLOAD_DB = os.environ.get("TX_DB", os.path.join(DATA_DIR, "tangxin.db"))
# 兼容旧引用名（缓存库现在与下载库同文件）
CACHE_DB = os.environ.get("TX_CACHE") or DOWNLOAD_DB

# ---- 已下载识别（跨根目录去重，"本机先下 → 后续转移服务器"）----
# 场景：本机与服务器的目录结构完全一致（{下载目录}/{演员}/{标题}.mp4），只是**盘符不同**。
# 把这些已知下载根目录列在这里，下载前会先识别是否已下过，避免重复下载。
# 多个目录用路径分隔符（Windows 是 ";"）分隔；也可在 GUI「设置」里维护（持久化）。
KNOWN_DIRS = [d.strip() for d in os.environ.get("TX_KNOWN_DIRS", "").split(os.pathsep) if d.strip()]

# 命中"其它目录已存在"时的动作：
#   False（默认）= 只跳过，不动旧文件（转移服务器时正是要这个行为）；
#   True         = 顺手把旧文件搬移到当前下载目录（本地换目录时省一次搬运）。
COPY_EXISTING = os.environ.get("TX_COPY_EXISTING", "0") == "1"
