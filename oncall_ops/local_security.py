"""Transactional local credential rotation without secret-bearing output."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from urllib.request import Request

from app.local_env import _set, protect_env_file
from oncall_ops.common import read_env
from oncall_ops.docker import DockerRunner
from oncall_ops.native_health import open_http


class CredentialRotationError(RuntimeError):
    """A local rotation failed; details are deliberately withheld from public output."""


class RotationBackend(Protocol):
    def change_postgres_password(self, username: str, password: str) -> None: ...

    def change_rabbitmq_password(
        self, username: str, current: str, password: str
    ) -> None: ...

    def recreate(self) -> None: ...

    def verify(self) -> None: ...

    def rotate_admin_password(self, username: str, password: str) -> None: ...


def _atomic_write_protected(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        protect_env_file(temporary_path)
        os.replace(temporary_path, path)
        protect_env_file(path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _rotated_content(original: str) -> tuple[str, dict[str, str], dict[str, str]]:
    lines = original.splitlines()
    old = read_env_from_text(original)
    current = {
        "POSTGRES_DB": old.get("POSTGRES_DB", "oncall"),
        "POSTGRES_USER": old.get("POSTGRES_USER", "oncall"),
        "POSTGRES_PASSWORD": old.get("POSTGRES_PASSWORD", "oncall"),
        "POSTGRES_PORT": old.get("POSTGRES_PORT", "5432"),
        "RABBITMQ_DEFAULT_USER": old.get("RABBITMQ_DEFAULT_USER", "oncall"),
        "RABBITMQ_DEFAULT_PASS": old.get("RABBITMQ_DEFAULT_PASS", "oncall"),
        "RABBITMQ_PORT": old.get("RABBITMQ_PORT", "5672"),
        "MINIO_ACCESS_KEY": old.get("MINIO_ACCESS_KEY", "minioadmin"),
        "MINIO_SECRET_KEY": old.get("MINIO_SECRET_KEY", "minioadmin"),
        "ADMIN_USERNAME": old.get("ADMIN_USERNAME", "admin"),
        "ADMIN_PASSWORD": old.get("ADMIN_PASSWORD", ""),
    }
    new = {
        **current,
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "RABBITMQ_DEFAULT_PASS": secrets.token_hex(32),
        "MINIO_ACCESS_KEY": "oncall" + secrets.token_hex(7),
        "MINIO_SECRET_KEY": secrets.token_hex(20),
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
    }
    for key in (
        "POSTGRES_PASSWORD",
        "RABBITMQ_DEFAULT_PASS",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "ADMIN_PASSWORD",
    ):
        lines = _set(lines, key, new[key])
    lines = _set(lines, "ALLOW_WEAK_ADMIN_PASSWORD", "false")
    lines = _set(
        lines,
        "DATABASE_URL",
        "postgresql+asyncpg://"
        f"{quote(new['POSTGRES_USER'], safe='')}:{quote(new['POSTGRES_PASSWORD'], safe='')}"
        f"@127.0.0.1:{new['POSTGRES_PORT']}/{quote(new['POSTGRES_DB'], safe='')}",
    )
    lines = _set(
        lines,
        "CELERY_BROKER_URL",
        "amqp://"
        f"{quote(new['RABBITMQ_DEFAULT_USER'], safe='')}:{quote(new['RABBITMQ_DEFAULT_PASS'], safe='')}"
        f"@127.0.0.1:{new['RABBITMQ_PORT']}//",
    )
    return "\n".join(lines) + "\n", current, new


def read_env_from_text(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value
    return values


def rotate_local_credentials(path: Path, backend: RotationBackend) -> None:
    """Rotate all local credentials, restoring configuration and services on failure."""
    path = Path(path)
    original = path.read_text(encoding="utf-8-sig")
    rendered, old, new = _rotated_content(original)
    postgres_changed = False
    rabbitmq_changed = False
    try:
        backend.change_postgres_password(new["POSTGRES_USER"], new["POSTGRES_PASSWORD"])
        postgres_changed = True
        backend.change_rabbitmq_password(
            new["RABBITMQ_DEFAULT_USER"], old["RABBITMQ_DEFAULT_PASS"], new["RABBITMQ_DEFAULT_PASS"]
        )
        rabbitmq_changed = True
        _atomic_write_protected(path, rendered)
        backend.recreate()
        backend.verify()
        backend.rotate_admin_password(new["ADMIN_USERNAME"], new["ADMIN_PASSWORD"])
    except Exception:
        rollback_failed = False
        config_restored = False
        try:
            _atomic_write_protected(path, original)
            config_restored = True
        except Exception:
            rollback_failed = True
        if rabbitmq_changed:
            try:
                backend.change_rabbitmq_password(
                    old["RABBITMQ_DEFAULT_USER"],
                    new["RABBITMQ_DEFAULT_PASS"],
                    old["RABBITMQ_DEFAULT_PASS"],
                )
            except Exception:
                rollback_failed = True
        if postgres_changed:
            try:
                backend.change_postgres_password(old["POSTGRES_USER"], old["POSTGRES_PASSWORD"])
            except Exception:
                rollback_failed = True
        if config_restored and (postgres_changed or rabbitmq_changed):
            try:
                backend.recreate()
            except Exception:
                rollback_failed = True
        if rollback_failed:
            raise CredentialRotationError(
                "Local credential rotation failed and automatic rollback was incomplete"
            ) from None
        raise CredentialRotationError(
            "Local credential rotation failed; configuration and service credentials were restored"
        ) from None


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class DockerLocalSecurityBackend:
    """Rotate the currently running single-host Compose stack."""

    def __init__(
        self,
        root: Path,
        env_file: Path,
        *,
        docker_executable: str = "docker",
        project_name: str = "oncallagent",
    ) -> None:
        self.root = Path(root).resolve()
        self.env_file = Path(env_file).resolve()
        self.project_name = project_name
        self.docker = DockerRunner(
            docker_executable,
            environment=read_env(self.env_file),
            env_file=self.env_file,
        )

    def _postgres_sql(self, username: str, sql: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", username):
            raise CredentialRotationError("Configured PostgreSQL username is invalid")
        database = read_env(self.env_file).get("POSTGRES_DB", "oncall")
        return self.docker.run(
            [
                "exec",
                "-i",
                "oncall-postgres",
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-U",
                username,
                "-d",
                database,
            ],
            input_text=sql,
            timeout=30,
        )

    def change_postgres_password(self, username: str, password: str) -> None:
        quoted_identifier = '"' + username.replace('"', '""') + '"'
        self._postgres_sql(
            username,
            f"ALTER ROLE {quoted_identifier} WITH PASSWORD {_sql_literal(password)};\n",
        )

    def change_rabbitmq_password(self, username: str, current: str, password: str) -> None:
        environment = read_env(self.env_file)
        port = environment.get("RABBITMQ_MANAGEMENT_PORT", "15672")
        token = base64.b64encode(f"{username}:{current}".encode()).decode("ascii")
        request = Request(
            f"http://127.0.0.1:{port}/api/users/{quote(username, safe='')}",
            data=json.dumps({"password": password, "tags": "administrator"}).encode("utf-8"),
            method="PUT",
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        )
        with open_http(request, timeout=10) as response:
            if response.status not in {201, 204}:
                raise CredentialRotationError("RabbitMQ credential update was rejected")

    def _compose(self, *arguments: str, timeout: int = 600) -> str:
        self.docker.environment = read_env(self.env_file)
        return self.docker.run(
            [
                "compose",
                "-p",
                self.project_name,
                "-f",
                str(self.root / "vector-database.yml"),
                *arguments,
            ],
            cwd=self.root,
            timeout=timeout,
        )

    def recreate(self) -> None:
        self._compose(
            "up",
            "-d",
            "--force-recreate",
            "--wait",
            "postgres",
            "rabbitmq",
            "etcd",
            "minio",
            "standalone",
            "attu",
        )
        self._compose(
            "up",
            "-d",
            "--force-recreate",
            "diagnosis-critical-worker",
            "diagnosis-default-worker",
            "remediation-worker",
            "outbox-publisher",
        )

    def verify(self) -> None:
        environment = read_env(self.env_file)
        database_url = environment["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
        import psycopg

        with psycopg.connect(database_url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise CredentialRotationError("PostgreSQL verification failed")

        rabbit_user = environment.get("RABBITMQ_DEFAULT_USER", "oncall")
        rabbit_password = environment["RABBITMQ_DEFAULT_PASS"]
        token = base64.b64encode(f"{rabbit_user}:{rabbit_password}".encode()).decode("ascii")
        rabbit_request = Request(
            f"http://127.0.0.1:{environment.get('RABBITMQ_MANAGEMENT_PORT', '15672')}/api/overview",
            headers={"Authorization": f"Basic {token}"},
        )
        with open_http(rabbit_request, timeout=10) as response:
            if response.status != 200:
                raise CredentialRotationError("RabbitMQ verification failed")
        for url in ("http://127.0.0.1:9000/minio/health/live", "http://127.0.0.1:9091/healthz"):
            with open_http(Request(url), timeout=10) as response:
                if response.status != 200:
                    raise CredentialRotationError("Storage verification failed")

    def rotate_admin_password(self, username: str, password: str) -> None:
        from pwdlib import PasswordHash

        password_hash = PasswordHash.recommended().hash(password)
        # Inputs are SQL-escaped before the statement is delivered over stdin.
        sql = (
            "WITH changed AS ("  # nosec B608
            "UPDATE users SET password_hash="
            f"{_sql_literal(password_hash)}, failed_login_count=0, locked_until=NULL "
            f"WHERE username={_sql_literal(username)} RETURNING id), "
            "cleared AS (DELETE FROM web_sessions USING changed "
            "WHERE web_sessions.user_id=changed.id RETURNING 1) "
            "SELECT count(*) FROM changed;\n"
        )
        result = self._postgres_sql(read_env(self.env_file).get("POSTGRES_USER", "oncall"), sql)
        if result.strip().splitlines()[-1:] != ["1"]:
            raise CredentialRotationError("Configured administrator account was not found")
