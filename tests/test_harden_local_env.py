from pathlib import Path

from app.local_env import harden_env


def test_harden_env_generates_secrets_without_replacing_valid_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEBUG=true\nAUTH_ENABLED=false\nAPI_KEY=\nALERTMANAGER_WEBHOOK_TOKEN=\n",
        encoding="utf-8",
    )

    generated = harden_env(env_file)
    content = env_file.read_text(encoding="utf-8")

    assert generated == ["API_KEY", "ALERTMANAGER_WEBHOOK_TOKEN", "ADMIN_PASSWORD"]
    assert "DEBUG=false" in content
    assert "AUTH_ENABLED=true" in content
    assert "WEB_LOGIN_ENABLED=true" in content
    assert "HOST=0.0.0.0" in content
    assert "replace-with" not in content

    values = dict(
        line.split("=", 1)
        for line in content.splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    first_api_key = values["API_KEY"]
    assert len(first_api_key) == 64
    assert len(values["ADMIN_PASSWORD"]) >= 32

    assert harden_env(env_file) == []
    second_content = env_file.read_text(encoding="utf-8")
    assert f"API_KEY={first_api_key}" in second_content


def test_harden_env_preserves_explicit_local_demo_password(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "API_KEY=" + "a" * 64 + "\n"
        "ALERTMANAGER_WEBHOOK_TOKEN=" + "b" * 64 + "\n"
        "ADMIN_PASSWORD=123456\n"
        "ALLOW_WEAK_ADMIN_PASSWORD=true\n",
        encoding="utf-8",
    )

    assert harden_env(env_file) == []
    assert "ADMIN_PASSWORD=123456" in env_file.read_text(encoding="utf-8")
