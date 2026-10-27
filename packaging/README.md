# DragonFetch 打包说明

目标是产出**绿色版**：解压即用、数据可备份、后端可现场改。

## 一键打包

双击项目根目录的 **`打包.bat`**（等价于 `packaging\build.bat`）。

脚本会自动：
1. 选择解释器（优先 `C:\Python314\python.exe`，可用环境变量 `DRAGONFETCH_PYTHON` 指定）
2. 检查并补装 `PyQt6` / `PyInstaller`（缺少时 `pip install`）
3. 生成 `assets/icon.ico`（若不存在）
4. 清理 `build/` `dist/`，执行 PyInstaller
5. 执行 `assemble.py`：复制 `modules/`、`bin/`、图标、DATA 骨架与使用说明
6. 询问是否立即试启动

## 产物结构

```
dist/DragonFetch/
├── DragonFetch.exe
├── _internal/          PyQt6 等依赖（PyInstaller 生成，别删）
├── modules/            后端源码（糖心/黄果/多站点/映取）—— 可直接改
├── bin/                yt-dlp.exe / ffmpeg.exe / ffprobe.exe / ffplay.exe
├── assets/icon.ico
├── DATA/               运行期数据（DB/下载/缓存/设置）
├── TEMP/              临时文件
├── logs/              前端日志
└── 使用说明.txt
```

整个 `dist/DragonFetch` 文件夹就是可分发单元，压缩后即可发人。

## 为什么是 onedir，且 modules 不进 bundle

| 取舍 | 原因 |
|---|---|
| onedir 而非 onefile | 启动快（不用每次解压）、杀软误报少、`_internal` 可排查 |
| `modules/` 作为源码放在 exe 旁 | 后端是**运行时动态 import**，PyInstaller 静态分析看不到；放外面还带来「改后端不重新打包」与「与 DATA/BIN 分离」的好处 |
| `hiddenimports` 只列第三方 | `requests / bs4 / lxml / yaml / cryptography / Crypto / curl_cffi / psutil / aiohttp ...` 必须真正打进 bundle，因为外部源码运行时仍要 import 它们 |
| `bin/` 不打进 exe | ffmpeg 体积大、且要能被用户替换升级 |

## 体积与依赖

- `dist/DragonFetch` 约 **400~450 MB**（其中 `bin/` 约 313 MB，`_internal` 约 80~110 MB）。
- 目标机器需要 **VC++ 2015-2022 x64 运行库**（多数机器已有）。
- 想瘦身：`python packaging\assemble.py --no-bin` 跳过二进制，由用户自备 `bin/`。

## 常见坑

1. **提示「未找到后端代码目录」**
   `modules/` 必须与 exe 同级。只拷 exe 是不行的。
2. **`Crypto` 缺失**
   黄果下载封面解密要用 `pycryptodome`：`pip install pycryptodome` 后重新打包。
3. **JAVDB 采集失败**
   该链路依赖 `selenium` + `undetected-chromedriver` + 本机 Chrome。两者在打包环境存在时会自动进包；目标机需有 Chrome。
4. **升级后端**
   直接替换 `modules` 文件夹，不必重新打包 exe。
5. **把程序放到别的盘**
   设环境变量 `DF_ROOT=<程序目录>`；数据仍在该目录的 `DATA/` 下。

## 手动分步（不用 bat）

```bat
python packaging\make_icon.py
python -m PyInstaller packaging\DragonFetch.spec --noconfirm --clean
python packaging\assemble.py
```
