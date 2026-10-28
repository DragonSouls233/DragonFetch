@echo off
rem ============================================================
rem  共用片段：找出可用的 Python 解释器
rem  由 启动.bat / packaging\build.bat 通过 call 调用。
rem  成功后设置 PY（解释器路径）与 PYOPT（附加参数，通常为空）；
rem  失败返回 errorlevel 1。
rem
rem  优先级：
rem    1) 环境变量 DRAGONFETCH_PYTHON（用户显式指定）
rem    2) C:\Python314 ~ C:\Python311（本机固定安装）
rem    3) 用户级安装目录 Python31x
rem    4) py 启动器，参数 -3
rem    5) PATH 里的 python
rem
rem  注意：本文件必须保存为 GBK 编码 + CRLF 换行，
rem  否则 cmd 解析中文注释时会把注释行当成命令执行。
rem ============================================================

set "PYOPT="
set "PY="

if defined DRAGONFETCH_PYTHON set "PY=%DRAGONFETCH_PYTHON%"
if defined PY goto :found

if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if not defined PY if exist "C:\Python311\python.exe" set "PY=C:\Python311\python.exe"
if defined PY goto :found

if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined PY goto :found

if exist "%SystemRoot%\py.exe" (
    set "PY=%SystemRoot%\py.exe"
    set "PYOPT=-3"
    goto :found
)

where python >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
    goto :found
)

echo [x] 未找到 Python 3.11 及以上版本。
echo     请安装 Python，或设置环境变量 DRAGONFETCH_PYTHON 指向 python.exe，例如：
echo     set DRAGONFETCH_PYTHON=C:\Python314\python.exe
exit /b 1

:found
rem 校验解释器真的能跑（避免指向坏掉或过旧的安装）
"%PY%" %PYOPT% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [x] 解释器不可用或版本低于 3.11：%PY% %PYOPT%
    echo     请用 DRAGONFETCH_PYTHON 指定正确的 python.exe。
    exit /b 1
)
exit /b 0
