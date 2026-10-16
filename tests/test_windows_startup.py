import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CMD startup test")
@pytest.mark.parametrize("health_failures", [0, 2, 99])
def test_startup_reports_success_only_after_health_check(tmp_path, health_failures):
    root = Path.cwd()
    shutil.copy(root / "start-windows.bat", tmp_path / "start-windows.bat")
    app = tmp_path / "app"
    app.mkdir()
    shutil.copy(root / "app/local_env.py", app / "local_env.py")
    (app / "__init__.py").write_text(
        'raise RuntimeError("application must not be imported during bootstrap")', encoding="utf-8"
    )
    (tmp_path / ".env").write_text(
        "DASHSCOPE_API_KEY=test-value\nAPI_KEY=" + "a" * 64 + "\n"
        "ALERTMANAGER_WEBHOOK_TOKEN=" + "b" * 64 + "\n"
        "ADMIN_PASSWORD=test-password-12345\nAUTH_ENABLED=true\n"
        "TENCENTCLOUD_SECRET_ID=fake-test-id\nTENCENTCLOUD_SECRET_KEY=fake-test-key\n",
        encoding="utf-8",
    )
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in ("uv", "npx", "timeout", "curl"):
        (stubs / f"{name}.cmd").write_text("@echo off\nexit /b 0\n", encoding="utf-8")
    (stubs / "timeout.cmd").write_text(
        '@echo off\necho ERROR: Input redirection is not supported 1>&2\nexit /b 1\n',
        encoding="utf-8",
    )
    (stubs / "curl.cmd").write_text(
        '@echo off\nsetlocal enabledelayedexpansion\n'
        'echo %* | findstr /C:"/health" >nul\n'
        'if errorlevel 1 exit /b 0\n'
        'set count=0\n'
        'if exist "%~dp0health-count.txt" set /p count=<"%~dp0health-count.txt"\n'
        'set /a count+=1\n'
        '>"%~dp0health-count.txt" echo !count!\n'
        f'if !count! LEQ {health_failures} exit /b 22\n'
        'exit /b 0\n',
        encoding="utf-8",
    )
    (stubs / "powershell.cmd").write_text(
        '@echo off\necho %* | findstr /C:"Start-Sleep -Seconds 5" >nul\n'
        'if errorlevel 1 exit /b 0\necho wait>>"%~dp0waits.txt"\nexit /b 0\n',
        encoding="utf-8",
    )
    (stubs / "docker.cmd").write_text(
        '@echo off\necho %MCP_CLS_DOCKER_URL%>>"%~dp0endpoints.txt"\nexit /b 0\n', encoding="utf-8"
    )
    (stubs / "netstat.cmd").write_text(
        "@echo off\necho TCP 0.0.0.0:3000 0.0.0.0:0 LISTENING 1\n"
        "echo TCP 0.0.0.0:8004 0.0.0.0:0 LISTENING 1\n"
        "echo TCP 0.0.0.0:9900 0.0.0.0:0 LISTENING 1\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": str(stubs)
        + os.pathsep
        + str(Path(sys.executable).parent)
        + os.pathsep
        + os.environ["PATH"],
    }
    result = subprocess.run(
        ["cmd", "/d", "/c", "start-windows.bat"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=25,
    )
    output = result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    assert "Input redirection is not supported" not in output
    assert (stubs / "health-count.txt").exists(), "Startup exited before its health check: " + output
    attempts = int((stubs / "health-count.txt").read_text().strip())
    waits = (stubs / "waits.txt").read_text().splitlines() if (stubs / "waits.txt").exists() else []
    assert len(waits) == attempts - 1, "Health retries must wait even with redirected stdin"
    if health_failures == 99:
        assert result.returncode != 0, output
        assert "Startup completed" not in output
        assert "All services started" not in output
        assert "[ERROR]" in output
        assert 1 < attempts <= 12
    else:
        assert result.returncode == 0, output
        assert "Startup completed" in output
        assert attempts == health_failures + 1
    assert "http://host.docker.internal:3000/mcp" in (stubs / "endpoints.txt").read_text()
