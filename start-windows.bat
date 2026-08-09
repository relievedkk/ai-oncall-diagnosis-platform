@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ====================================
echo ���� SuperBizAgent ����
echo ====================================
echo.

REM ��� uv �Ƿ�װ����ѡ�����û�л�ʹ�� pip��
echo [1/6] ����������...
where uv >nul 2>&1
if errorlevel 1 (
    echo [��Ϣ] uv δ��װ����ʹ�ô�ͳ pip ��ʽ
    echo [��ʾ] ��װ uv �������ٶȣ�pip install uv
    set USE_UV=0
) else (
    echo [�ɹ�] ��⵽ uv ��������
    set USE_UV=1
)
echo.

REM ȷ�� Python �汾��ȷ
echo [2/6] ���� Python �汾...
if exist .python-version (
    set /p PYTHON_VERSION=<.python-version
    echo [��Ϣ] ��ǰ���ð汾: !PYTHON_VERSION!
    
    REM ����Ƿ�Ϊ 3.10�������ݣ�
    echo !PYTHON_VERSION! | findstr /C:"3.10" >nul
    if not errorlevel 1 (
        echo [����] Python 3.10 �����ݣ��Զ����µ� 3.13...
        echo 3.13> .python-version
        echo [�ɹ�] �Ѹ��µ� Python 3.13
    )
) else (
    echo [��Ϣ] ���� .python-version �ļ�...
    echo 3.13> .python-version
)
echo.

REM ������ͬ�����⻷��
echo [3/6] ����/ͬ�����⻷��...
if exist .venv\Scripts\python.exe (
    echo [��Ϣ] ���⻷���Ѵ��ڣ�������...
    
    REM ����� uv������ʹ�� uv sync
    if "%USE_UV%"=="1" (
        uv sync 2>nul
        if errorlevel 1 (
            echo [����] uv sync ʧ�ܣ�ʹ�� pip ����...
            .venv\Scripts\python.exe -m pip install -e . -q
        ) else (
            echo [�ɹ�] ʹ�� uv ͬ�����
        )
    ) else (
        echo [��Ϣ] ʹ�� pip ��������...
        .venv\Scripts\python.exe -m pip install -e . -q
    )
) else (
    echo [��Ϣ] �����µ����⻷��...
    
    REM ����� uv������ʹ�� uv sync
    if "%USE_UV%"=="1" (
        echo [��Ϣ] ����ʹ�� uv sync ����...
        uv sync 2>nul
        if not errorlevel 1 (
            echo [�ɹ�] ʹ�� uv �������
            goto :venv_created
        )
        echo [����] uv sync ʧ�ܣ����˵���ͳ��ʽ...
    )
    
    REM ʹ�ô�ͳ Python venv ����
    echo [��Ϣ] ʹ�� python -m venv ����...
    python -m venv .venv
    if errorlevel 1 (
        echo [����] ���⻷������ʧ��
        echo [��ʾ] ��ȷ���Ѱ�װ Python 3.11+
        pause
        exit /b 1
    )
    
    REM ��װ����
    echo [��Ϣ] ��װ��Ŀ�������������Ҫ�����ӣ�...
    .venv\Scripts\python.exe -m pip install --upgrade pip -q
    .venv\Scripts\python.exe -m pip install -e . -q
    if errorlevel 1 (
        echo [����] ������װʧ��
        pause
        exit /b 1
    )
    echo [�ɹ�] ���⻷���������
)

:venv_created
echo [�ɹ�] ���⻷������
echo.

REM ���� Python ����
set PYTHON_CMD=.venv\Scripts\python.exe

REM ���� Docker Compose
echo [4/6] ���� Milvus �������ݿ�...
docker ps --format "{{.Names}}" | findstr "milvus-standalone" >nul 2>&1
if not errorlevel 1 (
    echo [��Ϣ] Milvus ������������
) else (
    docker compose -f vector-database.yml up -d
    if errorlevel 1 (
        echo [����] Docker ����ʧ�ܣ���ȷ�� Docker Desktop ������
        pause
        exit /b 1
    )
    echo [��Ϣ] �ȴ� Milvus ������10�룩...
    timeout /t 10 /nobreak >nul
)
echo [�ɹ�] Milvus ���ݿ����
echo.

REM ���� CLS MCP ����
echo [5/6] ���� CLS MCP ����...
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
    echo [INFO] Waiting for official CLS MCP Server (first run downloads package, ~15-20s)...
    timeout /t 15 /nobreak >nul
)
echo [�ɹ�] CLS MCP ����������
echo.

REM ���� Monitor MCP ����
echo [6/6] ���� Monitor MCP ����...
start "Monitor MCP Server" /min %PYTHON_CMD% mcp_servers/monitor_server.py
timeout /t 2 /nobreak >nul
echo [�ɹ�] Monitor MCP ����������
echo.

REM ���� FastAPI ����
echo [7/8] ���� FastAPI ����...
start "SuperBizAgent API" %PYTHON_CMD% -m uvicorn app.main:app --host 0.0.0.0 --port 9900
echo [��Ϣ] �ȴ�����������15�룩...
timeout /t 15 /nobreak >nul
echo.

REM ������״̬���ϴ��ĵ�
echo.
echo [��Ϣ] ������״̬...
curl -s http://localhost:9900/health >nul 2>&1
if errorlevel 1 (
    echo [����] ������ܻ�δ��ȫ���������Ե�Ƭ��
) else (
    echo [�ɹ�] FastAPI ������������
    echo.
    
    REM ���� API �ϴ� aiops-docs �ĵ����������ݿ�
    echo [8/8] �ϴ��ĵ����������ݿ�...
    for %%f in (aiops-docs\*.md) do (
        echo   �ϴ�: %%~nxf
        curl -s -X POST http://localhost:9900/api/upload -F "file=@%%f" >nul 2>&1
    )
    echo [�ɹ�] �ĵ��ϴ����
)

echo.
echo ====================================
echo ����������ɣ�
echo ====================================
echo Web ����: http://localhost:9900
echo API �ĵ�: http://localhost:9900/docs
echo.
echo �鿴��־:
echo   - FastAPI: logs\app_*.log��Loguru ��־��������ת��
echo   - CLS MCP: type mcp_cls.log
echo   - Monitor: type mcp_monitor.log
echo ֹͣ����: stop-windows.bat
echo ====================================
pause
