@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ============================================================
rem  DragonFetch 源码启动器
rem   启动.bat              正常启动界面
rem   启动.bat --selftest   无界面自检（排错用，结果写入 logs 下的 selftest.txt）
rem   启动.bat --version    打印版本号
rem
rem  注意：本文件必须保存为 GBK 编码 + CRLF 换行。
rem ============================================================

if not exist "launcher\main.py" (
    echo [x] 当前目录下没有 launcher\main.py
    echo     请在 DragonFetch 根目录运行本脚本。
    pause
    exit /b 1
)
if not exist "modules\__init__.py" (
    echo [x] 当前目录下没有 modules（后端代码）
    echo     请在 DragonFetch 根目录运行本脚本。
    pause
    exit /b 1
)

call "%~dp0scripts\_find_python.bat"
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)

echo [i] Python: %PY% %PYOPT%
"%PY%" %PYOPT% --version

"%PY%" %PYOPT% -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo [i] 首次运行：正在安装依赖（约 1-3 分钟，只此一次）...
    "%PY%" %PYOPT% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [x] 依赖安装失败。请手动执行：
        echo     "%PY%" %PYOPT% -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

"%PY%" %PYOPT% -m launcher.main %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [x] 启动失败（退出码 %RC%）
    echo     运行日志：logs 目录下的 launcher-*.log
    echo     界面异常：logs 目录下的 ui-error.log
    echo     无界面自检：启动.bat --selftest
    pause
)

endlocal & exit /b %RC%
