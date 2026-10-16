"""Tail only new, sanitized JSON event files; acknowledge after CLS acceptance."""

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path


def upload_file(
    path: Path, state: dict[str, int], send_batch, limit: int = 100, max_bytes: int = 512_000
) -> int:
    key = str(path.resolve())
    offset = state.get(key, 0)
    records = []
    batch_bytes = 0
    with path.open("rb") as stream:
        stream.seek(offset)
        while len(records) < limit:
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                break
            if records and batch_bytes + len(line) > max_bytes:
                break
            event = json.loads(line)
            if not all(key in event for key in ("service", "level", "message", "timestamp_us")):
                raise ValueError("Invalid sanitized event schema")
            records.append(event)
            batch_bytes += len(line)
            offset = stream.tell()
    if not records:
        return 0
    send_batch(records)
    state[key] = offset
    return len(records)


def save_checkpoint(path: Path, state: dict[str, int]) -> None:
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(state), encoding="utf-8")
    os.replace(pending, path)


@contextmanager
def instance_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if path.stat().st_size == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    stop = Path("logs/cls-uploader.stop")
    stop.parent.mkdir(parents=True, exist_ok=True)
    if args.stop:
        stop.touch()
        return 0

    # SDK and its protobuf runtime remain outside the application's environment.
    from dotenv import dotenv_values
    from tencentcloud.log.cls_pb2 import LogGroupList
    from tencentcloud.log.logclient import LogClient

    from oncall_logging.redaction import make_redactor

    env = {**dotenv_values(args.env_file), **os.environ}
    secret_id = env.get("CLS_UPLOAD_SECRET_ID")
    secret_key = env.get("CLS_UPLOAD_SECRET_KEY")
    if not secret_id or not secret_key:
        print(
            "CLS uploader requires independent CLS_UPLOAD_SECRET_ID/CLS_UPLOAD_SECRET_KEY",
            flush=True,
        )
        return 2
    catalog = json.loads(
        Path(env.get("SERVICE_CATALOG_PATH", "config/service_catalog.json")).read_text(
            encoding="utf-8"
        )
    )
    services = catalog["services"]
    redact = make_redactor(
        [
            str(v)
            for k, v in env.items()
            if any(word in k.upper() for word in ("SECRET", "PASSWORD", "TOKEN", "API_KEY")) and v
        ]
    )
    clients = {}

    def send_batch(records):
        # Each process writes events for one configured application service.
        name = records[0]["service"]
        if any(record["service"] != name for record in records):
            raise ValueError("Mixed-service upload batch")
        cls = services[name]["cls"]
        endpoint = "https://" + cls["region"] + ".cls.tencentcs.com"
        if endpoint not in clients:
            clients[endpoint] = LogClient(
                endpoint, secret_id, secret_key, source="oncall-application", region=cls["region"]
            )
            clients[endpoint].timeout = 10
        group_list = LogGroupList()
        group = group_list.logGroupList.add()
        group.filename = "oncall-sanitized-events.jsonl"
        group.source = "oncall-application"
        for record in records:
            log = group.logs.add()
            log.time = int(record["timestamp_us"])
            for key in ("service", "level", "message", "process_id"):
                item = log.contents.add()
                item.key = key
                item.value = redact(str(record.get(key, "")))
        response = clients[endpoint].put_log_raw(cls["topic_id"], group_list)
        print(
            f"uploaded={len(records)} service={name} request_id={response.get_request_id()}",
            flush=True,
        )

    checkpoint = Path("logs/cls-uploader-checkpoint.json")
    try:
        with instance_lock(Path("logs/cls-uploader.lock")):
            stop.unlink(missing_ok=True)
            state = (
                json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {}
            )
            delay = 2
            while not stop.exists():
                try:
                    for path in sorted(Path("logs/cls-events").glob("*.jsonl")):
                        if upload_file(path, state, send_batch):
                            save_checkpoint(checkpoint, state)
                    delay = 2
                except Exception as exc:
                    # Never echo SDK response bodies or log contents on failure.
                    print(
                        f"upload_failed={type(exc).__name__}; checkpoint retained; retry_in={delay}s",
                        flush=True,
                    )
                    if args.once:
                        return 1
                    delay = min(delay * 2, 30)
                if args.once:
                    return 0
                for _ in range(delay):
                    if stop.exists():
                        break
                    time.sleep(1)
    except (BlockingIOError, PermissionError, OSError):
        print("CLS uploader unavailable or another instance holds the lock", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
