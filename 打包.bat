@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ============================================================
rem  DragonFetch 打包入口（真正做事的是 packaging\build.bat）
rem   打包.bat                   完整打包，产物在 dist\DragonFetch
rem   打包.bat --no-bin          不复制 bin（yt-dlp / ffmpeg 不进包，体积小）
rem   打包.bat --skip-build      只重新组装 modules / bin / DATA
rem   打包.bat --distpath 目录   指定产物目录，默认 dist
rem
rem  注意：本文件必须保存为 GBK 编码 + CRLF 换行。
rem ============================================================

if not exist "packaging\build.bat" (
    echo [x] 缺少 packaging\build.bat
    pause
    exit /b 1
)

call "%~dp0packaging\build.bat" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [x] 打包失败（退出码 %RC%）
    pause
)

endlocal & exit /b %RC%
