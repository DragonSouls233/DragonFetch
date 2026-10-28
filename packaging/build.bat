@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0.."

rem ============================================================
rem  DragonFetch 打包脚本（PyInstaller onedir）
rem
rem   打包.bat                     完整打包，产物在 dist\DragonFetch
rem   打包.bat --no-bin            不复制 bin（yt-dlp / ffmpeg 不进包）
rem   打包.bat --skip-build        只重新组装 modules / bin / DATA
rem   打包.bat --distpath 目录名   指定产物目录，默认 dist
rem
rem  注意：本文件必须保存为 GBK 编码 + CRLF 换行。
rem ============================================================

set "NOBIN="
set "SKIPBUILD="
set "DISTPATH=dist"

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--no-bin" (
    set "NOBIN=1"
    shift
    goto :parse
)
if /i "%~1"=="--skip-build" (
    set "SKIPBUILD=1"
    shift
    goto :parse
)
if /i "%~1"=="--distpath" (
    if "%~2"=="" (
        echo [x] --distpath 后面缺少目录名
        pause
        exit /b 1
    )
    set "DISTPATH=%~2"
    shift
    shift
    goto :parse
)
echo [!] 忽略未知参数：%~1
shift
goto :parse

:parsed

echo ============================================
echo   DragonFetch 打包（onedir）
echo ============================================
echo.

rem ---------- 1. 目录检查 ----------
if not exist "launcher\main.py" (
    echo [x] 当前目录下没有 launcher\main.py，请在项目根目录运行。
    pause
    exit /b 1
)
if not exist "modules\__init__.py" (
    echo [x] 当前目录下没有 modules（后端代码）。
    pause
    exit /b 1
)

rem ---------- 2. 解释器 ----------
call "scripts\_find_python.bat"
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)
echo [i] Python: %PY% %PYOPT%
"%PY%" %PYOPT% --version

rem ---------- 3. 依赖 ----------
"%PY%" %PYOPT% -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo [i] 安装运行依赖 requirements.txt ...
    "%PY%" %PYOPT% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [x] 运行依赖安装失败
        pause
        exit /b 1
    )
)

"%PY%" %PYOPT% -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [i] 安装 PyInstaller ...
    "%PY%" %PYOPT% -m pip install "pyinstaller>=6.10"
    if errorlevel 1 (
        echo [x] PyInstaller 安装失败
        pause
        exit /b 1
    )
)

rem ---------- 4. 图标 ----------
if not exist "assets\icon.ico" (
    echo [i] 生成图标 assets\icon.ico ...
    "%PY%" %PYOPT% packaging\make_icon.py
)

rem ---------- 5. PyInstaller ----------
if defined SKIPBUILD goto :assemble

echo [i] 清理旧的 build 与 %DISTPATH% ...
if exist build rmdir /s /q build
if exist "%DISTPATH%" rmdir /s /q "%DISTPATH%"

echo.
echo [1/3] PyInstaller 打包中（首次约 1-3 分钟）...
"%PY%" %PYOPT% -m PyInstaller packaging\DragonFetch.spec --noconfirm --clean --distpath "%DISTPATH%" --workpath build
if errorlevel 1 (
    echo [x] PyInstaller 失败，请看上面的报错
    pause
    exit /b 1
)
if not exist "%DISTPATH%\DragonFetch\DragonFetch.exe" (
    echo [x] 没有生成 %DISTPATH%\DragonFetch\DragonFetch.exe
    pause
    exit /b 1
)

:assemble
rem ---------- 6. 组装运行期文件 ----------
echo.
echo [2/3] 复制 modules / bin / DATA 骨架...
set "ASMOPT=--dist %DISTPATH%\DragonFetch"
if defined NOBIN set "ASMOPT=%ASMOPT% --no-bin"
"%PY%" %PYOPT% packaging\assemble.py %ASMOPT%
if errorlevel 1 (
    echo [x] 组装失败
    pause
    exit /b 1
)

rem ---------- 7. 产物自检 ----------
echo.
echo [3/3] 运行产物自检...
set "STLOG=%DISTPATH%\DragonFetch\logs\selftest.txt"
if exist "%STLOG%" del /q "%STLOG%"
pushd "%DISTPATH%\DragonFetch"
DragonFetch.exe --selftest
set "STRC=%ERRORLEVEL%"
popd
if exist "%STLOG%" (
    "%PY%" %PYOPT% -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); sys.stdout.write(p.read_text(encoding='utf-8'))" "%STLOG%"
) else (
    echo [!] 没有生成自检报告：%STLOG%
)
if not "%STRC%"=="0" (
    echo.
    echo [x] 产物自检未通过（退出码 %STRC%）
    pause
    exit /b %STRC%
)

echo.
echo ============================================
echo   完成： %DISTPATH%\DragonFetch\DragonFetch.exe
echo   可分发的整个文件夹是： %DISTPATH%\DragonFetch
echo ============================================
echo.
choice /c YN /t 20 /d N /m "现在试启动一次？(Y/N，20 秒后默认 N)"
if errorlevel 2 goto :done
start "" "%DISTPATH%\DragonFetch\DragonFetch.exe"

:done
endlocal
exit /b 0
