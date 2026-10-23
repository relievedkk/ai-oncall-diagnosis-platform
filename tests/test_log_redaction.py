import io
import time

from loguru import logger

from oncall_logging.redaction import make_redactor, patch_record


def test_long_non_url_text_does_not_cause_repeated_scheme_backtracking():
    text = "X" * 70_000
    started = time.perf_counter()
    assert make_redactor([])(text) == text
    assert time.perf_counter() - started < 1.0


def test_redacts_known_secrets_and_authorization_values():
    redact = make_redactor(["known-secret-value-123"])
    text = redact(
        'password="another-password" Authorization: Bearer abc.def.ghi key=known-secret-value-123'
    )
    assert "known-secret-value-123" not in text
    assert "another-password" not in text
    assert "abc.def.ghi" not in text
    assert "[REDACTED]" in text


def test_exception_output_never_expands_locals_or_discloses_secret():
    output = io.StringIO()
    handler = logger.add(output, format="{message}", diagnose=False)
    secret = "known-secret-value-123"
    try:
        try:
            raise RuntimeError("token=" + secret)
        except RuntimeError:
            logger.patch(lambda record: patch_record(record, make_redactor([secret]))).exception(
                "failed"
            )
    finally:
        logger.remove(handler)
    assert secret not in output.getvalue()
    assert "RuntimeError" in output.getvalue()
