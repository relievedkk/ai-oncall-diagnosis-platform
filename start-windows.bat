@echo off
setlocal enabledelayedexpansion

echo ====================================
echo  Start SuperBizAgent Services
echo ====================================
echo.

REM Check if uv is installed (optional, fall back to pip if not)
echo [1/8] Checking environment...
where uv >nul 2>&1
if errorlevel 1 (
    echo [INFO] uv not installed, using traditional pip mode
    echo [TIP] Install uv for faster setup: pip install uv
    set USE_UV=0
) else (
    echo [OK] uv detected, will use it
    set USE_UV=1
)
echo.

REM Ensure correct Python version
echo [2/8] Checking Python version...
if exist .python-version (
    set /p PYTHON_VERSION=<.python-version
    echo [INFO] Current pinned version: !PYTHON_VERSION!

    REM Check if it is 3.10 (incompatible)
    echo !PYTHON_VERSION! | findstr /C:"3.10" >nul
    if not errorlevel 1 (
        echo [WARN] Python 3.10 is incompatible, auto-upgrading to 3.13...
        echo 3.13> .python-version
        echo [OK] Updated to Python 3.13
    )
) else (
    echo [INFO] Creating .python-version file...
    echo 3.13> .python-version
)
echo.

REM Create or sync virtual environment
echo [3/8] Creating/syncing virtual environment...
if exist .venv\Scripts\python.exe (
    echo [INFO] Virtual environment exists, syncing...

    REM If uv is available, use uv sync
    if "%USE_UV%"=="1" (
        uv sync 2>nul
        if errorlevel 1 (
            echo [WARN] uv sync failed, retrying with pip...
            .venv\Scripts\python.exe -m pip install -e . -q
        ) else (
            echo [OK] Dependencies synced with uv
        )
    ) else (
        echo [INFO] Using pip to update dependencies...
        .venv\Scripts\python.exe -m pip install -e . -q
    )
) else (
    echo [INFO] Creating new virtual environment...

    REM If uv is available, use uv sync
    if "%USE_UV%"=="1" (
        echo [INFO] Trying uv sync to create venv...
        uv sync 2>nul
        if not errorlevel 1 (
            echo [OK] Venv created with uv
            goto :venv_created
        )
        echo [WARN] uv sync failed, falling back to traditional mode...
    )

    REM Use traditional Python venv
    echo [INFO] Using python -m venv to create...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Virtual environment creation failed
        echo [TIP] Make sure Python 3.11+ is installed
        pause
        exit /b 1
    )

    REM Install dependencies
    echo [INFO] Installing project dependencies ^(may take a while^)...
    .venv\Scripts\python.exe -m pip install --upgrade pip -q
    .venv\Scripts\python.exe -m pip install -e . -q
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created
)

:venv_created
echo [OK] Virtual environment ready
echo.

REM Set Python command
set PYTHON_CMD=.venv\Scripts\python.exe

REM Start Docker Compose (Milvus)
echo [4/8] Starting Milvus vector database...
docker ps --format "{{.Names}}" | findstr "milvus-standalone" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Milvus is already running
) else (
    docker compose -f vector-database.yml up -d
    if errorlevel 1 (
        echo [ERROR] Docker startup failed, make sure Docker Desktop is running
        pause
        exit /b 1
    )
    echo [INFO] Waiting for Milvus to be ready ^(10s^)...
    timeout /t 10 /nobreak >nul
)
echo [OK] Milvus database ready
echo.

REM Start CLS MCP service
echo [5/8] Starting CLS MCP service...
REM Read Tencent Cloud CLS credentials from .env
set CLS_SECRET_ID=
set CLS_SECRET_KEY=
for /f "usebackq tokens=1,2 delims==" %%a in (".env") do (
    if "%%a"=="TENCENTCLOUD_SECRET_ID" set CLS_SECRET_ID=%%b
    if "%%a"=="TENCENTCLOUD_SECRET_KEY" set CLS_SECRET_KEY=%%b
)
REM Check Node.js and start official CLS MCP Server (or fallback to local mock)
where npx >nul 2>&1
if errorlevel 1 (
    echo [WARNING] Node.js/npx not found, falling back to local mock CLS
    start "CLS MCP Server" /min %PYTHON_CMD% mcp_servers/cls_server.py
    timeout /t 2 /nobreak >nul
) else if "!CLS_SECRET_ID!"=="" (
    echo [WARNING] TENCENTCLOUD_SECRET_ID not in .env, falling back to local mock CLS
    start "CLS MCP Server" /min %PYTHON_CMD% mcp_servers/cls_server.py
    timeout /t 2 /nobreak >nul
) else (
    start "CLS MCP Server" /min cmd /c "set TRANSPORT=http&&set TENCENTCLOUD_SECRET_ID=!CLS_SECRET_ID!&&set TENCENTCLOUD_SECRET_KEY=!CLS_SECRET_KEY!&&set PORT=3000&&set TZ=Asia/Shanghai&&npx -y cls-mcp-server@latest"
    echo [INFO] Waiting for official CLS MCP Server ^(first run downloads package, ~15-20s^)...
    timeout /t 15 /nobreak >nul
)
echo [OK] CLS MCP service started
echo.

REM Start Monitor MCP service
echo [6/8] Starting Monitor MCP service...
start "Monitor MCP Server" /min %PYTHON_CMD% mcp_servers/monitor_server.py
timeout /t 2 /nobreak >nul
echo [OK] Monitor MCP service started
echo.

REM Start FastAPI service
echo [7/8] Starting FastAPI service...
start "SuperBizAgent API" %PYTHON_CMD% -m uvicorn app.main:app --host 0.0.0.0 --port 9900
echo [INFO] Waiting for service to start (15s)...
timeout /t 15 /nobreak >nul
echo.

REM Check service status and upload documents
echo.
echo [INFO] Checking service status...
curl -s http://localhost:9900/health >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Service may not be fully started, please wait a moment
) else (
    echo [OK] FastAPI service is running
    echo.

    REM Use API to upload aiops-docs documents into vector database
    echo [8/8] Uploading documents to vector database...
    for %%f in (aiops-docs\*.md) do (
        echo   Uploading: %%~nxf
        curl -s -X POST http://localhost:9900/api/upload -F "file=@%%f" >nul 2>&1
    )
    echo [OK] Document upload complete
)

echo.
echo ====================================
echo  All services started!
echo ====================================
echo Web UI:   http://localhost:9900
echo API docs: http://localhost:9900/docs
echo.
echo View logs:
echo   - FastAPI: logs\app_*.log (Loguru, auto-rotated)
echo   - CLS MCP: type mcp_cls.log
echo   - Monitor: type mcp_monitor.log
echo Stop services: stop-windows.bat
echo ====================================
pause
