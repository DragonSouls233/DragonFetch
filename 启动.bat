@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ---------- 选择解释器：优先装了 PyQt6 的那个 ----------
set "PY=%DRAGONFETCH_PYTHON%"
if not defined PY if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [DragonFetch] 未找到 Python，请安装 Python 3.11+ 后重试。
    pause
    exit /b 1
)

"%PY%" -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo [DragonFetch] 当前解释器缺少 PyQt6，正在安装...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [DragonFetch] 依赖安装失败，请手动执行： "%PY%" -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

"%PY%" -m launcher.main
if errorlevel 1 (
    echo.
    echo [DragonFetch] 启动失败，请查看上面输出或 logs\launcher-*.log
    pause
)
endlocal
