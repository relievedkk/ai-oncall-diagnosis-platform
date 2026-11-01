import subprocess
from pathlib import Path

import pytest

from app import local_env
from app.local_env import harden_env, protect_env_file, provision_infrastructure_credentials


def test_harden_env_generates_secrets_without_replacing_valid_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    protected: list[Path] = []
    monkeypatch.setattr(local_env, "protect_env_file", lambda path: protected.append(path))
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEBUG=true\nAUTH_ENABLED=false\nAPI_KEY=\nALERTMANAGER_WEBHOOK_TOKEN=\n",
        encoding="utf-8",
    )

    generated = harden_env(env_file)
    content = env_file.read_text(encoding="utf-8")

    assert generated == ["API_KEY", "ALERTMANAGER_WEBHOOK_TOKEN", "MCP_CLS_TOKEN", "ADMIN_PASSWORD"]
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
    assert values["ALLOW_WEAK_ADMIN_PASSWORD"] == "false"
    assert protected == [env_file]

    assert harden_env(env_file) == []
    second_content = env_file.read_text(encoding="utf-8")
    assert f"API_KEY={first_api_key}" in second_content


def test_harden_env_rotates_explicit_weak_demo_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_env, "protect_env_file", lambda _path: None)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "API_KEY=" + "a" * 64 + "\n"
        "ALERTMANAGER_WEBHOOK_TOKEN=" + "b" * 64 + "\n"
        "MCP_CLS_TOKEN=" + "c" * 64 + "\n"
        "ADMIN_PASSWORD=123456\n"
        "ALLOW_WEAK_ADMIN_PASSWORD=true\n",
        encoding="utf-8",
    )

    assert harden_env(env_file) == ["ADMIN_PASSWORD"]
    values = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert values["ADMIN_PASSWORD"] != "123456"
    assert len(values["ADMIN_PASSWORD"]) >= 32
    assert values["ALLOW_WEAK_ADMIN_PASSWORD"] == "false"


def test_provision_infrastructure_credentials_updates_urls_and_uses_independent_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_env, "protect_env_file", lambda _path: None)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_USER=oncall\n"
        "POSTGRES_PASSWORD=replace-with-random-postgres-password\n"
        "DATABASE_URL=postgresql+asyncpg://oncall:placeholder@127.0.0.1:5432/oncall\n"
        "RABBITMQ_DEFAULT_USER=oncall\n"
        "RABBITMQ_DEFAULT_PASS=replace-with-random-rabbitmq-password\n"
        "CELERY_BROKER_URL=amqp://oncall:placeholder@127.0.0.1:5672//\n"
        "MINIO_ACCESS_KEY=replace-with-random-minio-access-key\n"
        "MINIO_SECRET_KEY=replace-with-random-minio-secret-key\n",
        encoding="utf-8",
    )

    generated = provision_infrastructure_credentials(env_file)
    values = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )

    assert generated == [
        "POSTGRES_PASSWORD",
        "RABBITMQ_DEFAULT_PASS",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
    ]
    assert len(values["POSTGRES_PASSWORD"]) >= 32
    assert len(values["RABBITMQ_DEFAULT_PASS"]) >= 32
    assert 16 <= len(values["MINIO_ACCESS_KEY"]) <= 20
    assert len(values["MINIO_SECRET_KEY"]) >= 32
    assert len({values[key] for key in generated}) == len(generated)
    assert values["DATABASE_URL"] == (
        f"postgresql+asyncpg://oncall:{values['POSTGRES_PASSWORD']}@127.0.0.1:5432/oncall"
    )
    assert values["CELERY_BROKER_URL"] == (
        f"amqp://oncall:{values['RABBITMQ_DEFAULT_PASS']}@127.0.0.1:5672//"
    )


def test_provision_refuses_missing_or_live_default_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_env, "protect_env_file", lambda _path: None)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "POSTGRES_USER=oncall\nPOSTGRES_PASSWORD=oncall\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="live credential rotation"):
        provision_infrastructure_credentials(env_file)


@pytest.mark.parametrize(
    "overrides",
    [
        {"POSTGRES_PASSWORD": "custom-but-short"},
        {"RABBITMQ_DEFAULT_PASS": "p" * 32},
    ],
)
def test_provision_refuses_short_or_reused_existing_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
) -> None:
    monkeypatch.setattr(local_env, "protect_env_file", lambda _path: None)
    values = {
        "POSTGRES_PASSWORD": "p" * 32,
        "RABBITMQ_DEFAULT_PASS": "r" * 32,
        "MINIO_ACCESS_KEY": "oncall-test-access",
        "MINIO_SECRET_KEY": "m" * 40,
        **overrides,
    }
    env_file = tmp_path / ".env"
    original = "".join(f"{key}={value}\n" for key, value in values.items())
    env_file.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="independent strong"):
        provision_infrastructure_credentials(env_file)
    assert env_file.read_text(encoding="utf-8") == original


def test_protect_env_file_uses_constant_script_and_never_passes_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=top-secret-value\n", encoding="utf-8")
    calls: list[tuple[list[str], dict]] = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setattr(local_env.subprocess, "run", fake_run)
    protect_env_file(env_file)

    assert len(calls) == 1
    arguments, kwargs = calls[0]
    assert "top-secret-value" not in " ".join(arguments)
    assert kwargs["env"]["ONCALL_PRIVATE_ENV_FILE"] == str(env_file.resolve())
    assert kwargs["capture_output"] is True
