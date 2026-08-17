@echo off
chcp 65001 >nul
title FiguRobot 控制台
cd /d "%~dp0"

echo ============================================
echo   FiguRobot 机器人控制台
echo ============================================
echo.

REM 查找 Python
set PYTHON=
where python >nul 2>nul && set PYTHON=python
if not defined PYTHON (
    if exist "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
        set "PYTHON=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
    )
)
if not defined PYTHON (
    echo [错误] 未找到 Python，请安装 Python 3 或修改本脚本中的路径
    pause
    exit /b 1
)

echo [1/2] 启动桥接服务 (robot_bridge.py) ...
start "FiguRobot Bridge" "%PYTHON%" robot_bridge.py --port 8888

timeout /t 2 /nobreak >nul

echo [2/2] 打开网页控制台 ...
start "" "figurobot-console.html"

echo.
echo 已启动！浏览器打开后，点击左上角「连接桥接」即可控制机器人。
echo 关闭时请同时关闭「FiguRobot Bridge」窗口。
echo.
timeout /t 5 /nobreak >nul
