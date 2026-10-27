@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0.."

echo ============================================
echo   DragonFetch 打包脚本（onedir）
echo ============================================
echo.

rem ---------- 1. 选择解释器 ----------
set "PY=%DRAGONFETCH_PYTHON%"
if not defined PY if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [x] 未找到 Python。请安装 Python 3.11+ 或设置 DRAGONFETCH_PYTHON 指向 python.exe
    pause
    exit /b 1
)
echo [i] 使用解释器：%PY%
"%PY%" --version

rem ---------- 2. 依赖 ----------
"%PY%" -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo [i] 安装 PyQt6 等运行依赖...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [x] 依赖安装失败
        pause
        exit /b 1
    )
)

"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [i] 安装 PyInstaller...
    "%PY%" -m pip install "pyinstaller>=6.10"
    if errorlevel 1 (
        echo [x] PyInstaller 安装失败
        pause
        exit /b 1
    )
)

rem ---------- 3. 图标 ----------
if not exist "assets\icon.ico" (
    echo [i] 生成图标...
    "%PY%" packaging\make_icon.py
)

rem ---------- 4. 清理旧产物 ----------
echo [i] 清理 build\ dist\ ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

rem ---------- 5. PyInstaller ----------
echo.
echo [1/2] PyInstaller 打包中（首次约 1-3 分钟）...
"%PY%" -m PyInstaller packaging\DragonFetch.spec --noconfirm --clean --distpath dist --workpath build
if errorlevel 1 (
    echo [x] PyInstaller 失败，请看上面的报错
    pause
    exit /b 1
)
if not exist "dist\DragonFetch\DragonFetch.exe" (
    echo [x] 没有生成 dist\DragonFetch\DragonFetch.exe
    pause
    exit /b 1
)

rem ---------- 6. 组装运行期文件 ----------
echo.
echo [2/2] 复制 modules / bin / DATA 骨架...
"%PY%" packaging\assemble.py
if errorlevel 1 (
    echo [x] 组装失败
    pause
    exit /b 1
)

echo.
echo ============================================
echo   完成： dist\DragonFetch\DragonFetch.exe
echo   可分发的整个文件夹是： dist\DragonFetch
echo ============================================
echo.
choice /c YN /n /m "现在试启动一次？(Y/N) "
if errorlevel 2 goto :done
start "" "dist\DragonFetch\DragonFetch.exe"
:done
endlocal
