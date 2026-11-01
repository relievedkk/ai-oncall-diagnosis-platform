"""Generate local authentication secrets without printing or committing them."""

from __future__ import annotations

import argparse
import os
import secrets
import string

# subprocess is limited to a constant PowerShell ACL script; the path is passed via the environment.
import subprocess  # nosec B404
import tempfile
from pathlib import Path
from urllib.parse import quote

PLACEHOLDERS = {
    "",
    "replace-with-random-64-character-api-key",
    "replace-with-random-64-character-token",
    "replace-with-random-admin-password",
}

INFRASTRUCTURE_PLACEHOLDERS = {
    "POSTGRES_PASSWORD": "replace-with-random-postgres-password",  # nosec B105
    "RABBITMQ_DEFAULT_PASS": "replace-with-random-rabbitmq-password",  # nosec B105
    "MINIO_ACCESS_KEY": "replace-with-random-minio-access-key",
    "MINIO_SECRET_KEY": "replace-with-random-minio-secret-key",  # nosec B105
}

INSECURE_INFRASTRUCTURE_VALUES = {
    "POSTGRES_PASSWORD": {"", "oncall", "postgres"},
    "RABBITMQ_DEFAULT_PASS": {"", "oncall", "guest"},
    "MINIO_ACCESS_KEY": {"", "minioadmin"},
    "MINIO_SECRET_KEY": {"", "minioadmin"},
}

INFRASTRUCTURE_MINIMUM_LENGTHS = {
    "POSTGRES_PASSWORD": 32,  # nosec B105
    "RABBITMQ_DEFAULT_PASS": 32,  # nosec B105
    "MINIO_ACCESS_KEY": 16,
    "MINIO_SECRET_KEY": 32,  # nosec B105
}

MANAGED_CREDENTIAL_MINIMUM_LENGTHS = {
    "API_KEY": 32,
    "ALERTMANAGER_WEBHOOK_TOKEN": 32,  # nosec B105
    "MCP_CLS_TOKEN": 32,  # nosec B105
    **INFRASTRUCTURE_MINIMUM_LENGTHS,
}

URI_SAFE_CREDENTIAL_KEYS = {"POSTGRES_PASSWORD", "RABBITMQ_DEFAULT_PASS"}
URI_SAFE_CREDENTIAL_CHARACTERS = frozenset(
    string.ascii_letters + string.digits + "._~-"
)


def validate_strong_credentials(
    values: dict[str, str], keys: tuple[str, ...] | None = None
) -> None:
    """Reject defaults, placeholders, low-diversity values, and credential reuse."""
    selected = keys or tuple(MANAGED_CREDENTIAL_MINIMUM_LENGTHS)
    credentials: list[str] = []
    for key in selected:
        value = values.get(key, "")
        minimum = MANAGED_CREDENTIAL_MINIMUM_LENGTHS[key]
        insecure = INSECURE_INFRASTRUCTURE_VALUES.get(key, set())
        if (
            len(value) < minimum
            or value.lower() in insecure
            or value.startswith("replace-with-")
            or len(set(value)) < 8
            or (
                key in URI_SAFE_CREDENTIAL_KEYS
                and not set(value) <= URI_SAFE_CREDENTIAL_CHARACTERS
            )
        ):
            raise ValueError("Configuration requires independent strong credentials")
        credentials.append(value)
    if len(set(credentials)) != len(credentials):
        raise ValueError("Configuration requires independent strong credentials")


def _load(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def _set(lines: list[str], key: str, value: str) -> list[str]:
    prefix = f"{key}="
    replaced = False
    result: list[str] = []
    for line in lines:
        if line.startswith(prefix):
            if not replaced:
                result.append(f"{prefix}{value}")
                replaced = True
            continue
        result.append(line)
    if not replaced:
        result.append(f"{prefix}{value}")
    return result


def protect_env_file(path: Path) -> None:
    """Restrict the secret-bearing environment file to local administrators."""
    path = path.resolve()
    if os.name != "nt":
        path.chmod(0o600)
        return

    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    script = (
        "$ErrorActionPreference='Stop';"
        "$path=$env:ONCALL_PRIVATE_ENV_FILE;"
        "$owner=[Security.Principal.WindowsIdentity]::GetCurrent().User;"
        "$acl=[Security.AccessControl.FileSecurity]::new();"
        "$acl.SetOwner($owner);"
        "$acl.SetAccessRuleProtection($true,$false);"
        "$sids=@($owner,[Security.Principal.SecurityIdentifier]'S-1-5-18',"
        "[Security.Principal.SecurityIdentifier]'S-1-5-32-544');"
        "foreach($sid in $sids){"
        "$rule=[Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','Allow');"
        "$acl.AddAccessRule($rule)};"
        "([IO.FileInfo]::new($path)).SetAccessControl($acl)"
    )
    result = subprocess.run(  # nosec B603
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "ONCALL_PRIVATE_ENV_FILE": str(path)},
        capture_output=True,
        timeout=15,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Could not protect environment file permissions")


def _write_protected(path: Path, lines: list[str]) -> None:
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        # Tighten the empty file first. No secret bytes exist if this fails.
        protect_env_file(temporary_path)
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(lines) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        protect_env_file(path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def provision_infrastructure_credentials(path: Path) -> list[str]:
    """Provision a new stack; existing defaults require a separate offline migration."""
    if not path.exists():
        raise FileNotFoundError(f"Environment file not found: {path}")

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    values = _load(lines)
    generated: list[str] = []
    generators = {
        "POSTGRES_PASSWORD": lambda: secrets.token_hex(32),
        "RABBITMQ_DEFAULT_PASS": lambda: secrets.token_hex(32),
        "MINIO_ACCESS_KEY": lambda: "oncall" + secrets.token_hex(7),
        "MINIO_SECRET_KEY": lambda: secrets.token_hex(20),
    }

    for key, placeholder in INFRASTRUCTURE_PLACEHOLDERS.items():
        current = values.get(key, "")
        if current == placeholder:
            current = generators[key]()
            values[key] = current
            lines = _set(lines, key, current)
            generated.append(key)
        elif current in INSECURE_INFRASTRUCTURE_VALUES[key]:
            raise ValueError(
                "Existing or missing default credentials require an explicit offline migration"
            )

    validate_strong_credentials(values, tuple(INFRASTRUCTURE_MINIMUM_LENGTHS))

    postgres_user = values.get("POSTGRES_USER", "oncall")
    postgres_db = values.get("POSTGRES_DB", "oncall")
    postgres_port = values.get("POSTGRES_PORT", "5432")
    rabbit_user = values.get("RABBITMQ_DEFAULT_USER", "oncall")
    rabbit_port = values.get("RABBITMQ_PORT", "5672")
    lines = _set(
        lines,
        "DATABASE_URL",
        "postgresql+asyncpg://"
        f"{quote(postgres_user, safe='')}:{quote(values['POSTGRES_PASSWORD'], safe='')}"
        f"@127.0.0.1:{postgres_port}/{quote(postgres_db, safe='')}",
    )
    lines = _set(
        lines,
        "CELERY_BROKER_URL",
        "amqp://"
        f"{quote(rabbit_user, safe='')}:{quote(values['RABBITMQ_DEFAULT_PASS'], safe='')}"
        f"@127.0.0.1:{rabbit_port}//",
    )
    _write_protected(path, lines)
    return generated


def harden_env(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Environment file not found: {path}")

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    values = _load(lines)
    generated: list[str] = []

    for key in ("API_KEY", "ALERTMANAGER_WEBHOOK_TOKEN", "MCP_CLS_TOKEN"):
        current = values.get(key, "")
        if current in PLACEHOLDERS or len(current) < 32:
            lines = _set(lines, key, secrets.token_hex(32))
            generated.append(key)

    admin_password = values.get("ADMIN_PASSWORD", "")
    if admin_password in PLACEHOLDERS or len(admin_password) < 12:
        lines = _set(lines, "ADMIN_PASSWORD", secrets.token_urlsafe(24))
        generated.append("ADMIN_PASSWORD")
    lines = _set(lines, "ALLOW_WEAK_ADMIN_PASSWORD", "false")

    # Required only because monitoring containers access the authenticated host API.
    container_bind_host = "0.0.0.0"  # nosec B104
    for key, value in {
        "AUTH_ENABLED": "true",
        "DEBUG": "false",
        "EXPOSE_API_DOCS": "false",
        "WEB_LOGIN_ENABLED": "true",
        # The application refuses this bind unless strong authentication is enabled.
        "HOST": container_bind_host,
        "MCP_CLS_GATEWAY_HOST": container_bind_host,
    }.items():
        lines = _set(lines, key, value)

    _write_protected(path, lines)
    return generated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--provision-infrastructure",
        action="store_true",
        help="generate credentials only for a new placeholder-based local stack",
    )
    args = parser.parse_args()
    generated = harden_env(Path(args.env_file))
    if args.provision_infrastructure:
        generated.extend(provision_infrastructure_credentials(Path(args.env_file)))
    if generated:
        print("Generated local secrets: " + ", ".join(generated))
    else:
        print("Local security settings already hardened")


if __name__ == "__main__":
    main()
