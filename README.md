# DragonFetch

> **一款成人全能下载器（Adult All-in-One Downloader）**
>
> 把分散在多个工程里的下载模块合并成 **一个项目 / 一个进程 / 一个窗口**：
> 支持多站点链接下载、并发下载池、作品库管理与导航解析，覆盖糖心 Vlog、
> 黄果短剧、九色视频、12 个独立站点模块、麻豆磁力、映取 · YouTube / TikTok。
> 前端是全新的 PyQt6 应用（`launcher/`），各模块在 `modules/` 下**只保留后端代码**。
>
> **每个下载目标是独立模块**（`modules/<name>` + `DATA/<name>` + 独立页面/入口），
> 互相之间不共享代码、不共享数据目录。

当前版本 **V2.1.0**（2026.10 全量重建）。

---

## English / 英文说明

**DragonFetch** is an adult all-in-one downloader that merges the download modules
scattered across several projects into **one project / one process / one window**.

- **Unified GUI**: a brand-new PyQt6 front-end (`launcher/`), with all modules
  keeping **backend-only** code under `modules/`.
- **Multi-site downloading**: Tangxin Vlog, Huangguo short drama, Jiuse, 12
  per-domain site modules, Madouqu magnet search, YT/TikTok, navigation parsing.
  Every target is an **independent module** (`modules/<name>` + `DATA/<name>`),
  and each site is its **own page** — no shared site picker anywhere.
- **Concurrent download pool** with pause / resume / stop, library management and
  NFO writing, mirror & update detection, and cross-drive sync.

**Current version:** V2.1.0 (full rebuild, 2026.10).

**Requirements:** Python 3.11+, PyQt6, and the shared `bin/` binaries
(yt-dlp / ffmpeg) used by all pages.

---

## 能力总览

| 页面 | 后端来源 | 命名空间 | 主要能力 |
|------|----------|----------|----------|
| 总览 | — | — | 各库统计、媒体库体积、快捷入口 |
| 糖心 Vlog | `PornSimilarityPlatform/tangxin` | `tangxin` | 演员云、作品库（带本地状态）、**并发下载池**（暂停/继续/停止）、下载记录 |
| 黄果短剧 | `G:\黄果\hg-downloader` | `hgcore` | 剧库、选集、并发队列、**更新检测**、**外盘同步**、下载历史、站点与镜像 |
| 九色视频 | `dizhi222.pages.dev` 地址页 | `jiuse` | 取最新入口 → 线路检测 → **直连**解析下载、检测/下载记录 |
| 站点 ×12（一域名一页） | `PornSimilarityPlatform/modules` | `sites.<key>` | 每站独立页面：**解析直链 / 抓取列表 / 搜索**、8 列可编辑结果表、勾选批量下载 |
| └ 解析引擎 | 同上 | `multisite.engine` | 只负责页面→直链、列表→条目，本身不知道有哪些站点 |
| 麻豆磁力 | `PornSimilarityPlatform` 麻豆模块 | `madouqu` | 按关键词抓 BT 磁力、去重入库、导出 CSV |
| 映取 · YT/TikTok | 本项目 | `yttiktok` | 批量链接、画质档位、仅音频、封面、并发下载 |
| 导航页 · 黄视频 | 本项目 | — | daohang.pro 入口表、黄视频解析下载 |
| **地址同步** | 本项目 | — | 糖心 / 黄果 / 麻豆 / 九色 **一处同步地址**：先验上次地址 → 再读发布页 → **直连校验**后自动开关代理；启动时自动跑一次 |
| **库管理** | 本项目 | — | 各库统一浏览 / 就地改值 / 删行 / 导出 CSV / 备份 / 清空 |
| 设置 | 本项目 | — | 代理自测、目录、并发、主题（即时生效）、系统信息与维护 |

## 目录结构

```
DragonFetch/
├── bin/                共享二进制：yt-dlp.exe / ffmpeg.exe / ffprobe.exe / ffplay.exe
├── DATA/               所有运行期数据（tangxin / huangguo / jiuse / sites / madouqu / yttiktok / app）
├── TEMP/               所有临时文件（按模块分）
├── assets/             图标（打包也用）
├── launcher/           ★ 唯一前端（PyQt6）
│   ├── main.py            入口（含 --selftest / --version）
│   ├── core/              不含界面的前端逻辑
│   │   ├── paths.py       路径约定（源码/打包统一，支持 DF_ROOT）
│   │   ├── version.py     版本号（界面、打包资源、日志统一引用）
│   │   ├── settings.py    设置（DATA/app/settings.json，点号读写）
│   │   ├── backend.py     后端门面：注入 sys.path/环境变量并惰性导入
│   │   ├── dbstore.py     统一库访问（SQLite + JSON：读/改/删/导出/备份）
│   │   ├── nav.py         导航页服务：代理抓 daohang.pro → 解析 → 缓存
│   │   ├── navsync.py     ★ 地址同步中心：四个国内模块取址 + 直连校验 + 开关代理
│   │   ├── tasks.py       QThread 任务执行器（禁止 UI 线程联网）
│   │   └── logbus.py      日志总线（UI + logs/launcher-*.log）
│   └── gui/               界面层
│       ├── theme.py       设计令牌 + 全局 QSS（深/浅色）
│       ├── widgets.py     卡片、页头、统计卡、提示条、日志面板、表格工具
│       ├── shell.py       主窗口：侧栏导航 + 页面栈 + 状态栏 + Toast
│       └── pages/         home / tangxin / huangguo / jiuse / site_page（×12 站点）
│                          madouqu / yttiktok / navpage / navsync / library / settings_page
├── modules/            ★ 只有后端代码（无 GUI、无 DATA、无 TEMP）
│   ├── tangxin/           糖心后端（相对导入，独立包）
│   ├── huangguo/hgcore/   黄果后端（原 core 改名）
│   ├── jiuse/             九色后端（direct-only 抓取 + 地址页 + 独立库）
│   ├── sites/             ★ 一域名一模块：xvideos/ xnxx/ … thumbzilla/（12 个包）
│   ├── multisite/         共享解析引擎（engine/：registry + eaf / yt-dlp 适配器）
│   ├── madouqu/           麻豆后端（独立包：crawler / database / models）
│   └── yttiktok/          映取后端（ytdlp_service / models）
└── packaging/          ★ 打包（spec / build.bat / assemble.py / 图标 / 说明）
```

## 数据约束

- **modules 里只放代码**：无数据库、无封面、无下载产物、无缓存。
- 运行期数据统一在 `DATA/<模块>`，临时文件在 `TEMP/<模块>`。
- 黄果 `modules/huangguo/DATA` 是指向根 `DATA/huangguo` 的**目录联接**（打包时自动跳过）。
- 糖心通过 `TX_DATA / TX_TEMP / TX_BIN / TX_OUTPUT / TX_SETTINGS` 环境变量重定向。
- 每个模块**独立数据库**（跨进程 + SQLite 单写锁决定，不共享写库）：
  `DATA/tangxin/tangxin.db`、`DATA/huangguo/hg.db`、`DATA/jiuse/jiuse.db`、
  `DATA/sites/<key>/sites.db`（**每个站点一个库**）、`DATA/madouqu/{models,videos}.json`。

## 运行

```bat
启动.bat                 :: 推荐双击（自动挑解释器、缺依赖会自己装）
python -m launcher.main  :: 等价的命令行写法
```

需要 Python 3.11+ 且装好 PyQt6（`pip install -r requirements.txt`）；
`bin/` 内的 yt-dlp / ffmpeg 由全部页面共享。

解释器选择顺序：`DRAGONFETCH_PYTHON` → `C:\Python31x` → 用户级 `Python31x`
→ `py -3` → PATH 里的 `python`（这段逻辑在 `scripts/_find_python.bat`，启动与打包共用）。

自检（不打开界面，验证路径/后端/bin）：

```bat
启动.bat --selftest      :: 结果也写入 logs/selftest.txt
python -m launcher.main --selftest
```

## 打包

双击根目录 **`打包.bat`**，产出 `dist\DragonFetch\`（整目录即可分发）。
可选参数：`--no-bin`（不打包 yt-dlp/ffmpeg）、`--skip-build`（只重新组装
modules/bin/DATA）、`--distpath <目录>`（换产物目录）。
打包结束后会自动跑一次 `DragonFetch.exe --selftest` 并打印结果。

详见 [`packaging/README.md`](packaging/README.md)。要点：

- onedir：启动快、误报少；`modules/` 以源码形式放在 exe 旁，**改后端不用重打包**。
- 第三方依赖（requests / bs4 / lxml / yaml / cryptography / pycryptodome / curl_cffi …）写进 spec 的 `hiddenimports`。
- 打包后可执行 `DragonFetch.exe --selftest` 验证 6 个后端（tangxin/huangguo/multisite/madouqu/jiuse/ytdlp）与 yt-dlp。

> **给维护者**：`*.bat` 必须存成 **GBK + CRLF**。用 LF 换行 cmd 会把整个脚本解析错乱
> （双击直接报「'xxx' 不是内部或外部命令」）；存成 UTF-8 再 `chcp 65001` 会让 cmd 的
> 文件指针偏移错乱，中文注释被当成命令执行。`tests/test_scripts.py` 会检查这两条。

## 地址同步（四个国内模块共用一套逻辑）

`launcher/core/navsync.py` 是这四个模块**唯一**的地址管理者，页面在「系统 → 地址同步」。

| 模块 | 地址来源 | 校验方式 |
|------|----------|----------|
| 糖心 | `daohang.pro/app.js` 的 `CONFIG.links`（科学线路优先） | 直连打开 + 站点指纹 |
| 黄果 | 官方发布页（`hgcore.mirror_refresh`） | 发布页候选逐个直连内容校验 |
| 麻豆 | 站点自带备用域名表（sbs/club/cc/net/org） | 直连 → 不通再代理复测 |
| 九色 | `dizhi222.pages.dev` 地址页 → 它在引用的 `publish.js` 里 | 并行探测 + 站点指纹 |

每次同步按「三步」走：

1. **先验上次地址**（直连）——通了就直接沿用，不去抓发布页（最快路径，通常 < 2s）；
2. **不通才读发布页/地址页**（这一步允许走代理，因为发布页常在墙外）；
3. **落地**：直连能通 → 写该模块的 `route_url / base_url` 并把它的 `proxy_enabled`
   **关掉**（这就是「不开代理也能下载」的来源）；直连不通 → 保留代理开关并在状态里
   如实标「需代理」，绝不让模块看起来能用、实际下不了。

启动时主窗口会在 2.5s 后自动跑一次（可在页面里关掉「启动时自动同步」），结果写
`DATA/app/navsync.json`，侧栏与状态栏都显示摘要。

> 国外站点（XVideos / XNXX / …12 个）**不在这里**，它们仍按「设置 → 代理」正常走代理。

## 导航页流程（糖心 / 黄果 / 九色）

1. 设置里填写代理（默认 `socks5h://127.0.0.1:10808`）。
2. 「导航页 · 黄视频」或糖心页点 **代理刷新导航**：抓 `https://daohang.pro/app.js`，
   解析 `CONFIG.links`（含糖心主站、`scienceUrl` 科学线路、黄视频等入口）。
3. 选中糖心那条 → **应用线路**，写入设置；之后站点请求与下载按开关**直连**
   （实测 `tangxin01.top` 科学线路可直连）。
4. 黄果页点 **代理刷新域名**：读官方发布页拿到最新镜像，验证可达后自动切换，下载直连。
5. 九色页点 **刷新最新地址**：直连读 `https://dizhi222.pages.dev/`（不挂代理），
   抽出 `*.sbs` 线路后逐条探测，选中可用线路即可采集/下载。
6. 结果缓存到 `DATA/app/nav.json`，代理不可用时仍能读取上次入口。

## 后端命名空间（重要）

原工程各自使用顶级包名 `core` / `modules`，同进程导入会互相覆盖。合并时统一改名：

| 原名 | 现名 | 导入方式 |
|------|------|----------|
| `tangxin.*` | 不变（相对导入） | `sys.path += modules/` |
| `core.*`（黄果） | `hgcore.*` | `sys.path += modules/huangguo` |
| `modules.*`（PSP） | `multisite.engine.*` | `sys.path += modules/multisite` |
| —（新增，站点模块） | `sites.<key>.*` | `sys.path += modules/sites` |
| `core.*`（麻豆） | `madouqu.*` | `sys.path += modules/madouqu` |
| `core.*`（映取） | `yttiktok.core.*` | `sys.path += modules/` |
| —（新增） | `jiuse.*` | `sys.path += modules/jiuse` |

由上表的唯一性保证六个后端可**同进程**加载，因此前端才能做成真正的单窗口多页面。

## 说明 / 已知限制

- 原 PH 专属页面（快速加模特、已注销归档）随 PORNHUB 后端一并剥离；
  JAVDB 模块已按要求**整块移除**（代码 / 库 / 页面入口都不再存在）。
- **站点解析需要代理**：实测（2026-10）直连 `www.xvideos.com` ConnectTimeout，
  走 `socks5h://127.0.0.1:10808` 返回 200。`settings.json` 里 `sites.proxy_enabled`
  默认 `true`，个别站可单独关掉。
- **麻豆域名当前不可用**：实测本机 5 个备用域名（`madouqu.sbs/.club/.cc/.net/.org`）
  经代理 502 / 读超时，直连亦不可达。页面仍保留「检测域名 / 自动找可用域名」，
  拿到新域名后填进「域名」框即可继续使用（模块本身不依赖 yt-dlp）。
- **站点页面由模块自动生成**：`modules/sites/__init__.py` 的 `ORDER` 决定侧栏顺序；
  新增站点 = 新建 `modules/sites/<key>/__init__.py`（一个 `SITE`）+ 注册一行，
  侧栏入口、本站库、库管理条目都会自动出现，不需要改任何别的站点。
- 糖心并发数可调（同时部数 × 单部分片并发），过高会触发站点限速。

## 免责声明 / Disclaimer

本软件仅供技术学习与个人收藏研究使用，请勿用于任何侵权或非法用途。
请在下载前确认您所在地区的法律法规允许，并尊重各站点版权与使用条款。
使用者因使用本软件产生的任何后果自负。

*This software is for personal learning and research only. Do not use it for any
infringing or unlawful purpose. Please ensure your local laws permit such use and
respect the copyright and terms of each site. Users are solely responsible for
any consequences arising from its use.*
