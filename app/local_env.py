"""Generate local authentication secrets without printing or committing them."""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

PLACEHOLDERS = {
    "",
    "replace-with-random-64-character-api-key",
    "replace-with-random-64-character-token",
    "replace-with-random-admin-password",
}


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


def harden_env(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Environment file not found: {path}")

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    values = _load(lines)
    generated: list[str] = []

    for key in ("API_KEY", "ALERTMANAGER_WEBHOOK_TOKEN"):
        current = values.get(key, "")
        if current in PLACEHOLDERS or len(current) < 32:
            lines = _set(lines, key, secrets.token_hex(32))
            generated.append(key)

    admin_password = values.get("ADMIN_PASSWORD", "")
    allow_weak_password = values.get("ALLOW_WEAK_ADMIN_PASSWORD", "").lower() == "true"
    if not allow_weak_password and (admin_password in PLACEHOLDERS or len(admin_password) < 12):
        lines = _set(lines, "ADMIN_PASSWORD", secrets.token_urlsafe(24))
        generated.append("ADMIN_PASSWORD")

    # Required only because monitoring containers access the authenticated host API.
    container_bind_host = "0.0.0.0"  # nosec B104
    for key, value in {
        "AUTH_ENABLED": "true",
        "DEBUG": "false",
        "EXPOSE_API_DOCS": "false",
        "WEB_LOGIN_ENABLED": "true",
        # The application refuses this bind unless strong authentication is enabled.
        "HOST": container_bind_host,
    }.items():
        lines = _set(lines, key, value)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return generated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    generated = harden_env(Path(args.env_file))
    if generated:
        print("Generated local secrets: " + ", ".join(generated))
    else:
        print("Local security settings already hardened")


if __name__ == "__main__":
    main()
