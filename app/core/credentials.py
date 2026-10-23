"""Constant-time credential comparison for untrusted HTTP text."""

import secrets


def secrets_equal(supplied: str, expected: str) -> bool:
    # compare_digest(str, str) raises TypeError for non-ASCII header/cookie text.
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
