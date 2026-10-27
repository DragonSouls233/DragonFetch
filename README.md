# DragonFetch

> 一款 **视频 / 内容下载软件**：把分散在多个工程里的下载模块合并成 **一个项目 / 一个进程 / 一个窗口**，
> 支持多站点链接下载、并发下载池、作品库管理与导航解析。
> 前端是全新的 PyQt6 应用（`launcher/`），各模块在 `modules/` 下**只保留后端代码**。

当前版本 **v2.1.0**（2026.10 全量重建）。

## 能力总览

| 页面 | 后端来源 | 命名空间 | 主要能力 |
|------|----------|----------|----------|
| 总览 | — | — | 各库统计、媒体库体积、快捷入口 |
| 糖心 Vlog | `PornSimilarityPlatform/tangxin` | `tangxin` | 演员云、作品库（带本地状态）、**并发下载池**（暂停/继续/停止）、下载记录 |
| 黄果短剧 | `G:\黄果\hg-downloader` | `hgcore` | 剧库、选集、并发队列、**更新检测**、**外盘同步**、下载历史、站点与镜像 |
| 多站点 | `PornSimilarityPlatform/modules` | `psp` | 通用链接下载、JAVDB 采集/入库、Madouqu 搜索/入库 |
| 映取 · YT/TikTok | 本项目 | `yttiktok` | 批量链接、画质档位、仅音频、封面、并发下载 |
| 导航页 · 黄视频 | 本项目 | — | daohang.pro 入口表、黄视频解析下载 |
| **库管理** | 本项目 | — | 四个库统一浏览 / 就地改值 / 删行 / 导出 CSV / 备份 / 清空 / 快速录入 |
| 设置 | 本项目 | — | 代理自测、目录、并发、主题（即时生效）、系统信息与维护 |

## 目录结构

```
DragonFetch/
├── bin/                共享二进制：yt-dlp.exe / ffmpeg.exe / ffprobe.exe / ffplay.exe
├── DATA/               所有运行期数据（tangxin / huangguo / sites / yttiktok / app）
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
│   │   ├── tasks.py       QThread 任务执行器（禁止 UI 线程联网）
│   │   └── logbus.py      日志总线（UI + logs/launcher-*.log）
│   └── gui/               界面层
│       ├── theme.py       设计令牌 + 全局 QSS（深/浅色）
│       ├── widgets.py     卡片、页头、统计卡、提示条、日志面板、表格工具
│       ├── shell.py       主窗口：侧栏导航 + 页面栈 + 状态栏 + Toast
│       └── pages/         home / tangxin / huangguo / sites / yttiktok /
│                          navpage / library / settings_page
├── modules/            ★ 只有后端代码（无 GUI、无 DATA、无 TEMP）
│   ├── tangxin/           糖心后端（相对导入，独立包）
│   ├── huangguo/hgcore/   黄果后端（原 core 改名）
│   ├── sites/psp/         多站点后端（原 modules 改名 psp）
│   └── yttiktok/          映取后端（ytdlp_service / models）
└── packaging/          ★ 打包（spec / build.bat / assemble.py / 图标 / 说明）
```

## 数据约束

- **modules 里只放代码**：无数据库、无封面、无下载产物、无缓存。
- 运行期数据统一在 `DATA/<模块>`，临时文件在 `TEMP/<模块>`。
- 黄果 `modules/huangguo/DATA`、多站点 `modules/sites/data` 是指向根 `DATA/` 的**目录联接**（打包时自动跳过，不会把数据带进发布包）。
- 糖心通过 `TX_DATA / TX_TEMP / TX_BIN / TX_OUTPUT / TX_SETTINGS` 环境变量重定向。
- 每个模块**独立数据库**（跨进程 + SQLite 单写锁决定，不共享写库）：
  `DATA/tangxin/tangxin.db`、`DATA/huangguo/hg.db`、`DATA/sites/javdb|madouqu/*.json`。

## 运行

```bat
python -m launcher.main      :: 或双击 启动.bat
```

需要 Python 3.11+ 且装好 PyQt6（`pip install -r requirements.txt`）；
`bin/` 内的 yt-dlp / ffmpeg 由全部页面共享。

自检（不打开界面，验证路径/后端/bin）：

```bat
python -m launcher.main --selftest      :: 结果也写入 logs/selftest.txt
```

## 打包

双击根目录 **`打包.bat`**，产出 `dist\DragonFetch\`（整目录即可分发）。
详见 [`packaging/README.md`](packaging/README.md)。要点：

- onedir：启动快、误报少；`modules/` 以源码形式放在 exe 旁，**改后端不用重打包**。
- 第三方依赖（requests / bs4 / lxml / yaml / cryptography / pycryptodome / curl_cffi …）写进 spec 的 `hiddenimports`。
- 打包后可执行 `DragonFetch.exe --selftest` 验证 4 个后端与 yt-dlp。

## 导航页流程（糖心 / 黄果）

1. 设置里填写代理（默认 `socks5h://127.0.0.1:10808`）。
2. 「导航页 · 黄视频」或糖心页点 **代理刷新导航**：抓 `https://daohang.pro/app.js`，
   解析 `CONFIG.links`（含糖心主站、`scienceUrl` 科学线路、黄视频等入口）。
3. 选中糖心那条 → **应用线路**，写入设置；之后站点请求与下载按开关**直连**
   （实测 `tangxin01.top` 科学线路可直连）。
4. 黄果页点 **代理刷新域名**：读官方发布页拿到最新镜像，验证可达后自动切换，下载直连。
5. 结果缓存到 `DATA/app/nav.json`，代理不可用时仍能读取上次入口。

## 后端命名空间（重要）

原工程各自使用顶级包名 `core` / `modules`，同进程导入会互相覆盖。合并时统一改名：

| 原名 | 现名 | 导入方式 |
|------|------|----------|
| `tangxin.*` | 不变（相对导入） | `sys.path += modules/` |
| `core.*`（黄果） | `hgcore.*` | `sys.path += modules/huangguo` |
| `modules.*`（PSP） | `psp.*` | `sys.path += modules/sites` |
| `core.*`（映取） | `yttiktok.core.*` | `sys.path += modules/` |

由上表的唯一性保证四个后端可**同进程**加载，因此前端才能做成真正的单窗口多页面。

## 已验证（2026-10 重建）

- `compileall` 全绿；8 个页面在离屏与真机下均能实例化，0 异常。
- 四个后端同进程加载成功；`--selftest` 源码版与打包版均通过。
- 糖心：科学线路直连，演员云 808 位、1.1 秒；作品库 248 部。
- 黄果：发布页 → 镜像刷新 → 在线抓取 21 部 → 详情 3 集。
- 打包：`dist\DragonFetch` 445 MB，exe + `--selftest` 验证通过。

## 说明 / 已知限制

- 原 PH 专属页面（快速加模特、已注销归档）随 PORNHUB 后端一并剥离，
  由「库管理 → 快速录入 / 备份与清理」提供等价的数据维护能力。
- JAVDB 采集依赖 `selenium` + `undetected-chromedriver` + 本机 Chrome。
- 糖心并发数可调（同时部数 × 单部分片并发），过高会触发站点限速。
