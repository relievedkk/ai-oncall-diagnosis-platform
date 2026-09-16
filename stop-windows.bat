@echo off
setlocal enabledelayedexpansion
set MONITORING_PROJECT=oncallagent-monitoring
echo ====================================
echo  Stop SuperBizAgent Services
echo ====================================
echo.

REM Stop FastAPI service
echo [1/4] Stopping FastAPI service...
taskkill /FI "WINDOWTITLE eq SuperBizAgent API*" /F >nul 2>&1
if errorlevel 1 (
    echo [INFO] FastAPI service was not running, skipped.
) else (
    echo [OK] FastAPI service stopped.
)
call :stop_listener 9900 FastAPI
echo.

REM Stop CLS MCP service
echo [2/4] Stopping CLS MCP service...
taskkill /FI "WINDOWTITLE eq CLS MCP Server*" /F >nul 2>&1
if errorlevel 1 (
    echo [INFO] CLS MCP service was not running, skipped.
) else (
    echo [OK] CLS MCP service stopped.
)
call :stop_listener 3000 "Official CLS MCP"
call :stop_listener 8003 "Local CLS mock"
echo.

REM Stop Monitor MCP service
echo [3/5] Stopping Monitor MCP service...
taskkill /FI "WINDOWTITLE eq Monitor MCP Server*" /F >nul 2>&1
if errorlevel 1 (
    echo [INFO] Monitor MCP service was not running, skipped.
) else (
    echo [OK] Monitor MCP service stopped.
)
call :stop_listener 8004 "Monitor MCP"
echo.

REM Stop Prometheus stack
echo [4/5] Stopping Prometheus stack...
docker ps --format "{{.Names}}" | findstr "prometheus" >nul 2>&1
if not errorlevel 1 (
    docker compose -p %MONITORING_PROJECT% -f monitoring.yml down
    if errorlevel 1 (
        echo [ERROR] Docker stop failed.
    ) else (
        echo [OK] Prometheus container stopped.
    )
) else (
    echo [INFO] Prometheus container was not running.
)
echo.

REM Stop Docker (Milvus)
echo [5/5] Stopping Milvus container...
docker ps --format "{{.Names}}" | findstr "milvus" >nul 2>&1
if not errorlevel 1 (
    docker compose -f vector-database.yml down
    if errorlevel 1 (
        echo [ERROR] Docker stop failed.
    ) else (
        echo [OK] Milvus container stopped.
    )
) else (
    echo [INFO] Milvus container was not running.
)
echo.

echo ====================================
echo  All services stopped.
echo ====================================
echo.
echo Tip:
echo   - Service data volumes were preserved.
echo   - To remove all Milvus Docker data volumes, run:
echo     docker compose -f vector-database.yml down -v
echo.
pause
endlocal
exit /b 0

:stop_listener
REM Stop a process only when it is listening on one of this project's fixed ports.
set "PORT=%~1"
set "SERVICE_NAME=%~2"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    echo [INFO] Stopping !SERVICE_NAME! listener on port !PORT! ^(PID %%P^)...
    taskkill /F /PID %%P >nul 2>&1
)
exit /b 0
