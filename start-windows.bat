@echo off
setlocal enabledelayedexpansion
set MONITORING_PROJECT=oncallagent-monitoring
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo ====================================
echo  Start AI OnCall Diagnosis Platform
echo ====================================
echo.

REM Check if uv is installed (optional, fall back to pip if not)
echo [1/9] Checking environment and configuration...
where uv >nul 2>&1
if errorlevel 1 (
    echo [INFO] uv not installed, using traditional pip mode
    echo [TIP] Install uv for faster setup: pip install uv
    set USE_UV=0
) else (
    echo [OK] uv detected, will use it
    set USE_UV=1
)

if not exist .env (
    if exist .env.example (
        copy /Y .env.example .env >nul
        echo [ACTION REQUIRED] Created .env from .env.example.
        echo [ACTION REQUIRED] Edit .env, set DASHSCOPE_API_KEY, then run this script again.
        exit /b 1
    ) else (
        echo [ERROR] .env and .env.example are both missing.
        exit /b 1
    )
)

python -m app.local_env --env-file .env
if errorlevel 1 (
    echo [ERROR] Failed to generate secure local API credentials.
    exit /b 1
)

set DASH_SCOPE_KEY=
set APP_API_KEY=
set APP_AUTH_ENABLED=
set ALERT_WEBHOOK_TOKEN=
set CLS_SECRET_ID=
set CLS_SECRET_KEY=
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if "%%a"=="DASHSCOPE_API_KEY" set DASH_SCOPE_KEY=%%b
    if "%%a"=="API_KEY" set APP_API_KEY=%%b
    if "%%a"=="AUTH_ENABLED" set APP_AUTH_ENABLED=%%b
    if "%%a"=="ALERTMANAGER_WEBHOOK_TOKEN" set ALERT_WEBHOOK_TOKEN=%%b
    if "%%a"=="TENCENTCLOUD_SECRET_ID" set CLS_SECRET_ID=%%b
    if "%%a"=="TENCENTCLOUD_SECRET_KEY" set CLS_SECRET_KEY=%%b
)
if "!DASH_SCOPE_KEY!"=="" (
    echo [ERROR] DASHSCOPE_API_KEY is empty. Edit .env before starting.
    exit /b 1
)
if "!DASH_SCOPE_KEY!"=="replace-with-your-dashscope-api-key" (
    echo [ERROR] DASHSCOPE_API_KEY still contains the placeholder value. Edit .env before starting.
    exit /b 1
)
if /I not "!APP_AUTH_ENABLED!"=="true" (
    echo [ERROR] AUTH_ENABLED must be true for the full local stack.
    exit /b 1
)
if "!APP_API_KEY!"=="" (
    echo [ERROR] AUTH_ENABLED=true requires a non-empty API_KEY in .env.
    exit /b 1
)
if "!APP_API_KEY!"=="replace-with-random-64-character-api-key" (
    echo [ERROR] Replace the API_KEY placeholder with a strong random value.
    exit /b 1
)
if "!ALERT_WEBHOOK_TOKEN!"=="" (
    echo [ERROR] ALERTMANAGER_WEBHOOK_TOKEN is required in .env.
    exit /b 1
)
if "!ALERT_WEBHOOK_TOKEN!"=="replace-with-random-64-character-token" (
    echo [ERROR] Replace the ALERTMANAGER_WEBHOOK_TOKEN placeholder with a strong random value.
    exit /b 1
)
echo.

REM Ensure correct Python version
echo [2/9] Checking Python version...
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
echo [3/9] Creating/syncing virtual environment...
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

REM Start Docker Compose (PostgreSQL + RabbitMQ + Milvus + diagnosis workers)
echo [4/9] Starting databases, RabbitMQ and diagnosis workers...
docker compose -f vector-database.yml up -d --build --remove-orphans
if errorlevel 1 (
    echo [ERROR] Docker startup failed, make sure Docker Desktop is running
    pause
    exit /b 1
)
echo [INFO] Waiting for infrastructure and workers to be ready ^(10s^)...
timeout /t 10 /nobreak >nul
echo [OK] PostgreSQL, RabbitMQ, Milvus and diagnosis workers ready
echo.

REM Start Prometheus stack
echo [5/9] Starting Prometheus stack...
docker compose -p !MONITORING_PROJECT! -f monitoring.yml up -d --build
if errorlevel 1 (
    echo [ERROR] Monitoring stack startup failed, make sure Docker Desktop is running
    pause
    exit /b 1
)
echo [INFO] Waiting for Prometheus and Alertmanager to be ready ^(10s^)...
timeout /t 10 /nobreak >nul
echo [OK] Monitoring stack ready ^(Prometheus: http://localhost:9090, Alertmanager: http://localhost:9093^)
echo.

REM Start CLS MCP service
echo [6/9] Starting CLS MCP service...
REM Check Node.js and start official CLS MCP Server (or fallback to local mock)
where npx >nul 2>&1
if errorlevel 1 (
    echo [WARNING] Node.js/npx not found, falling back to local mock CLS on port 8003
    set MCP_CLS_TRANSPORT=streamable-http
    set MCP_CLS_URL=http://127.0.0.1:8003/mcp
    netstat -ano | findstr /R /C:":8003 .*LISTENING" >nul 2>&1
    if not errorlevel 1 (
        echo [INFO] Local CLS mock is already running on port 8003
    ) else (
        start "CLS MCP Server" /min %PYTHON_CMD% mcp_servers/cls_server.py
        timeout /t 2 /nobreak >nul
    )
) else if "!CLS_SECRET_ID!"=="" (
    echo [WARNING] TENCENTCLOUD_SECRET_ID not in .env, falling back to local mock CLS on port 8003
    set MCP_CLS_TRANSPORT=streamable-http
    set MCP_CLS_URL=http://127.0.0.1:8003/mcp
    netstat -ano | findstr /R /C:":8003 .*LISTENING" >nul 2>&1
    if not errorlevel 1 (
        echo [INFO] Local CLS mock is already running on port 8003
    ) else (
        start "CLS MCP Server" /min %PYTHON_CMD% mcp_servers/cls_server.py
        timeout /t 2 /nobreak >nul
    )
) else (
    set MCP_CLS_TRANSPORT=streamable-http
    set MCP_CLS_URL=http://127.0.0.1:3000/mcp
    netstat -ano | findstr /R /C:":3000 .*LISTENING" >nul 2>&1
    if not errorlevel 1 (
        echo [INFO] Official CLS MCP Server is already running on port 3000
    ) else (
        start "CLS MCP Server" /min cmd /c "set TRANSPORT=http&&set TENCENTCLOUD_SECRET_ID=!CLS_SECRET_ID!&&set TENCENTCLOUD_SECRET_KEY=!CLS_SECRET_KEY!&&set PORT=3000&&set TZ=Asia/Shanghai&&npx -y cls-mcp-server@latest"
        echo [INFO] Waiting for official CLS MCP Server ^(first run downloads package, ~15-20s^)...
        timeout /t 15 /nobreak >nul
    )
)
echo [OK] CLS MCP service started
echo.

REM Start Monitor MCP service
echo [7/9] Starting Monitor MCP service...
netstat -ano | findstr /R /C:":8004 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Monitor MCP service is already running on port 8004
) else (
    start "Monitor MCP Server" /min %PYTHON_CMD% mcp_servers/monitor_server.py
    timeout /t 2 /nobreak >nul
)
echo [OK] Monitor MCP service started
echo.

REM Start FastAPI service
echo [8/9] Starting FastAPI service...
netstat -ano | findstr /R /C:":9900 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] FastAPI service is already running on port 9900
) else (
    start "AI OnCall API" /min %PYTHON_CMD% -m app.server
    echo [INFO] Waiting for service to start (15s)...
    timeout /t 15 /nobreak >nul
)
echo.

REM Check service status and upload documents
echo.
echo [INFO] Checking service status...
curl -s http://localhost:9900/health -H "X-API-Key: !APP_API_KEY!" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Service may not be fully started, please wait a moment
) else (
    echo [OK] FastAPI service is running
    echo.

    REM Use API to upload aiops-docs documents into vector database
    echo [9/9] Uploading documents to vector database...
    for %%f in (aiops-docs\*.md) do (
        echo   Uploading: %%~nxf
        curl -s -X POST http://localhost:9900/api/upload -H "X-API-Key: !APP_API_KEY!" -F "file=@%%f" >nul 2>&1
    )
    echo [OK] Document upload complete
)

echo.
echo ====================================
echo  All services started!
echo ====================================
echo Web UI:   http://localhost:9900
echo API docs: disabled by default ^(set EXPOSE_API_DOCS=true only for local debugging^)
echo RabbitMQ: http://localhost:15672 ^(oncall/oncall for local development^)
echo.
echo View logs:
echo   - FastAPI: logs\app_*.log (Loguru, auto-rotated)
echo   - CLS MCP: type mcp_cls.log
echo   - Monitor: type mcp_monitor.log
echo Stop services: stop-windows.bat
echo ====================================
pause
