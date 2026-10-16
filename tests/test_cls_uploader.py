import json

import pytest

from oncall_logging.uploader import upload_file


def test_checkpoint_advances_only_after_successful_batch(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"service": "orders", "level": "INFO", "message": "started", "timestamp_us": 10})
        + "\n",
        encoding="utf-8",
    )
    state = {}

    def failed(records):
        raise ConnectionError("upload unavailable")

    with pytest.raises(ConnectionError):
        upload_file(path, state, failed)
    assert state == {}
    sent = []
    count = upload_file(path, state, lambda records: sent.extend(records))
    assert count == 1
    assert sent[0]["message"] == "started"
    assert state[str(path.resolve())] == path.stat().st_size
    assert upload_file(path, state, lambda records: pytest.fail("already acknowledged")) == 0


def test_partial_final_line_is_not_acknowledged(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"service":"orders","message":"partial"}')
    state = {}
    assert upload_file(path, state, lambda records: pytest.fail("incomplete line")) == 0
    assert state == {}


def test_batch_byte_limit_preserves_the_next_record(tmp_path):
    path = tmp_path / "events.jsonl"
    line = (
        json.dumps({"service": "orders", "level": "INFO", "message": "x" * 100, "timestamp_us": 10})
        + "\n"
    )
    path.write_bytes(line.encode() * 2)
    state = {}
    sent = []
    assert (
        upload_file(path, state, lambda records: sent.extend(records), max_bytes=len(line.encode()))
        == 1
    )
    assert state[str(path.resolve())] == len(line.encode())
    assert (
        upload_file(path, state, lambda records: sent.extend(records), max_bytes=len(line.encode()))
        == 1
    )
    assert len(sent) == 2
