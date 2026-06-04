@echo off
chcp 65001 >nul
echo ================================================
echo   半导体晶圆厂设备预防性维护平台 - 启动脚本
echo ================================================
echo.
echo  [1] 演示模式 (SQLite, 无需Docker)
echo  [2] 生产模式 (TimescaleDB + gRPC)
echo.
set /p MODE="请选择模式 (1/2): "

if "%MODE%"=="1" goto demo_mode
if "%MODE%"=="2" goto prod_mode
echo 无效选择，默认进入演示模式
goto demo_mode

:demo_mode
echo.
echo [演示模式] 使用 SQLite，无需外部依赖
echo.
echo [1/3] 安装 Python 依赖...
pip install fastapi uvicorn -q
echo [2/3] 初始化演示数据库...
cd /d %~dp0backend
python init_demo_db.py
echo [3/3] 启动演示服务器...
start "Demo Server" cmd /k "cd /d %~dp0backend && python demo_server.py"
cd /d %~dp0
echo.
echo [完成] 打开浏览器访问: http://localhost:8000
echo.
pause
exit /b 0

:prod_mode
where docker >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Docker，请先安装 Docker Desktop
    echo 或使用演示模式(1)
    pause
    exit /b 1
)

echo [1/4] 启动 TimescaleDB 容器...
docker compose up -d timescaledb

echo [2/4] 等待数据库就绪...
timeout /t 8 /nobreak >nul

echo [3/4] 安装 Python 依赖...
pip install -r backend\requirements.txt -q

echo [4/4] 初始化数据库...
cd /d %~dp0backend
python init_db.py
cd /d %~dp0

echo.
echo ================================================
echo   启动服务...
echo ================================================
echo.
echo   将打开3个窗口：
echo   - gRPC 服务端 (端口 50051)
echo   - FastAPI 服务 (端口 8000)
echo   - gRPC 模拟器 (200台设备)
echo.

start "gRPC Server" cmd /k "cd /d %~dp0backend && python grpc_server.py"
timeout /t 2 /nobreak >nul
start "FastAPI Server" cmd /k "cd /d %~dp0backend && python main.py"
timeout /t 2 /nobreak >nul
start "Simulator" cmd /k "cd /d %~dp0backend && python simulator.py"

echo.
echo [完成] 打开浏览器访问: http://localhost:8000
echo.
pause
