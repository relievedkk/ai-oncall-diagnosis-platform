from pathlib import Path

import pytest

from oncall_ops import local_security


class FakeBackend:
    def __init__(
        self, *, fail_verify: bool = False, fail_rabbitmq_rollback: bool = False
    ) -> None:
        self.calls: list[str] = []
        self.fail_verify = fail_verify
        self.fail_rabbitmq_rollback = fail_rabbitmq_rollback

    def change_postgres_password(self, username: str, password: str) -> None:
        assert username == "oncall"
        assert password
        self.calls.append("postgres")

    def change_rabbitmq_password(self, username: str, current: str, password: str) -> None:
        assert username == "oncall"
        assert current and password
        self.calls.append("rabbitmq")
        if self.fail_rabbitmq_rollback and self.calls.count("rabbitmq") == 2:
            raise RuntimeError("rollback failed")

    def recreate(self) -> None:
        self.calls.append("recreate")

    def verify(self) -> None:
        self.calls.append("verify")
        if self.fail_verify:
            raise RuntimeError("provider leaked-value-should-not-escape")

    def rotate_admin_password(self, username: str, password: str) -> None:
        assert username == "admin"
        assert len(password) >= 32
        self.calls.append("admin")


def _write_default_env(path: Path) -> str:
    content = (
        "POSTGRES_DB=oncall\n"
        "POSTGRES_USER=oncall\n"
        "DATABASE_URL=postgresql+asyncpg://oncall:oncall@127.0.0.1:5432/oncall\n"
        "RABBITMQ_DEFAULT_USER=oncall\n"
        "CELERY_BROKER_URL=amqp://oncall:oncall@127.0.0.1:5672//\n"
        "ADMIN_USERNAME=admin\n"
        "ADMIN_PASSWORD=123456\n"
        "ALLOW_WEAK_ADMIN_PASSWORD=true\n"
    )
    path.write_text(content, encoding="utf-8")
    return content


def _values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def test_rotate_local_credentials_updates_services_before_admin_and_protects_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    _write_default_env(env_file)
    protected: list[Path] = []
    monkeypatch.setattr(
        local_security, "protect_env_file", lambda path: protected.append(path)
    )
    backend = FakeBackend()

    local_security.rotate_local_credentials(env_file, backend)

    values = _values(env_file)
    assert backend.calls == ["postgres", "rabbitmq", "recreate", "verify", "admin"]
    assert protected[-1] == env_file
    assert len(values["POSTGRES_PASSWORD"]) >= 32
    assert len(values["RABBITMQ_DEFAULT_PASS"]) >= 32
    assert 16 <= len(values["MINIO_ACCESS_KEY"]) <= 20
    assert len(values["MINIO_SECRET_KEY"]) >= 32
    assert len(values["ADMIN_PASSWORD"]) >= 32
    assert values["ALLOW_WEAK_ADMIN_PASSWORD"] == "false"
    assert len(
        {
            values["POSTGRES_PASSWORD"],
            values["RABBITMQ_DEFAULT_PASS"],
            values["MINIO_ACCESS_KEY"],
            values["MINIO_SECRET_KEY"],
            values["ADMIN_PASSWORD"],
        }
    ) == 5
    assert values["DATABASE_URL"] == (
        f"postgresql+asyncpg://oncall:{values['POSTGRES_PASSWORD']}@127.0.0.1:5432/oncall"
    )
    assert values["CELERY_BROKER_URL"] == (
        f"amqp://oncall:{values['RABBITMQ_DEFAULT_PASS']}@127.0.0.1:5672//"
    )


def test_rotate_local_credentials_restores_env_and_old_service_passwords_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    original = _write_default_env(env_file)
    monkeypatch.setattr(local_security, "protect_env_file", lambda _path: None)
    backend = FakeBackend(fail_verify=True)

    with pytest.raises(local_security.CredentialRotationError) as exc_info:
        local_security.rotate_local_credentials(env_file, backend)

    assert "leaked-value-should-not-escape" not in str(exc_info.value)
    assert env_file.read_text(encoding="utf-8") == original
    assert backend.calls == [
        "postgres",
        "rabbitmq",
        "recreate",
        "verify",
        "rabbitmq",
        "postgres",
        "recreate",
    ]


def test_cli_exposes_explicit_rotation_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from oncall_ops import cli

    env_file = tmp_path / ".env"
    _write_default_env(env_file)
    backend = FakeBackend()
    calls: list[tuple[Path, object]] = []

    monkeypatch.setattr(
        local_security,
        "DockerLocalSecurityBackend",
        lambda root, path, **_kwargs: backend,
    )
    monkeypatch.setattr(
        local_security,
        "rotate_local_credentials",
        lambda path, selected: calls.append((path, selected)),
    )

    assert cli.main(
        [
            "--root",
            str(tmp_path),
            "--config-root",
            str(tmp_path),
            "rotate-credentials",
        ]
    ) == 0
    assert calls == [(env_file, backend)]
    output = capsys.readouterr().out
    assert '"status": "rotated"' in output
    assert "123456" not in output


def test_rollback_attempts_postgres_and_recreate_even_if_rabbitmq_rollback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    _write_default_env(env_file)
    monkeypatch.setattr(local_security, "protect_env_file", lambda _path: None)
    backend = FakeBackend(fail_verify=True, fail_rabbitmq_rollback=True)

    with pytest.raises(
        local_security.CredentialRotationError, match="rollback was incomplete"
    ):
        local_security.rotate_local_credentials(env_file, backend)

    assert backend.calls[-3:] == ["rabbitmq", "postgres", "recreate"]
