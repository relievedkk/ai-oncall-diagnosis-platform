"""Deterministic group holdout provisioning. This is the only label-aware preparation step."""

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from app.core.single_pass import FAULTS, canonical
from app.evaluation.single_pass_storage import atomic_json_write

SOURCE_REVISION = "afeacb11bcc94dadfd1c8f483ee4377b2b8b614e"
GROUP = ("suite", "system", "root_cause_service", "fault")
SYSTEMS = ("ob", "ss", "tt")
DATA_FILES = ("metrics.parquet", "logs.parquet", "traces.parquet", "inject_time.txt")


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text("utf-8"))


def safe_child(root, name):
    root = Path(root).resolve(strict=True)
    candidate = (root / name).resolve(strict=True)
    if not candidate.is_relative_to(root):
        raise ValueError("data_path_escape")
    return candidate


def select_cases(index, exposed, *, seed=20261112):
    """One repeat in each of exactly two unseen groups per system/fault; never backfill."""
    required = {*GROUP, "case", "repetition", "inject_time", "time_start", "time_end"}
    if not required <= set(index.columns) or not required <= set(exposed.columns):
        raise ValueError("incomplete_source_index")
    if index[list(required)].isna().any().any() or not index["case"].is_unique:
        raise ValueError("invalid_source_index")
    old_groups = {tuple(row) for row in exposed[list(GROUP)].itertuples(index=False, name=None)}
    windows = {}
    for row in exposed.to_dict("records"):
        windows.setdefault(row["system"], []).append((row["time_start"], row["time_end"]))
    candidates = {}
    for row in index[index.suite.eq("RE2")].to_dict("records"):
        group = tuple(row[k] for k in GROUP)
        if group in old_groups or any(
            start <= row["time_end"] and end >= row["time_start"]
            for start, end in windows.get(row["system"], [])
        ):
            continue
        candidates.setdefault(group, []).append(row)
    selected = []
    for system in SYSTEMS:
        for fault in FAULTS:
            groups = sorted(g for g in candidates if g[1] == system and g[3] == fault)
            if len(groups) != 2:
                raise ValueError("incomplete_holdout_grid")
            for group in groups:
                selected.append(min(candidates[group], key=lambda row: digest([seed, row["case"]])))
    for number, row in enumerate(selected):
        if any(
            other["system"] == row["system"]
            and other["time_start"] <= row["time_end"]
            and other["time_end"] >= row["time_start"]
            for other in selected[:number]
        ):
            raise ValueError("overlapping_selected_windows")
    return sorted(selected, key=lambda row: digest([seed, "anonymous_order", row["case"]]))


def clean_type(kind):
    if pa.types.is_struct(kind):
        return pa.struct([clean_field(field) for field in kind])
    if pa.types.is_list(kind):
        return pa.list_(clean_field(kind.value_field))
    if pa.types.is_large_list(kind):
        return pa.large_list(clean_field(kind.value_field))
    if pa.types.is_fixed_size_list(kind):
        return pa.list_(clean_field(kind.value_field), kind.list_size)
    if pa.types.is_map(kind):
        return pa.map_(
            clean_field(kind.key_field), clean_field(kind.item_field), keys_sorted=kind.keys_sorted
        )
    if pa.types.is_dictionary(kind):
        return pa.dictionary(kind.index_type, clean_type(kind.value_type), ordered=kind.ordered)
    return kind


def clean_field(field):
    return pa.field(field.name, clean_type(field.type), nullable=field.nullable)


def clean_table(table):
    return table.cast(pa.schema([clean_field(field) for field in table.schema]))


def content_fingerprint(directory):
    """Hash telemetry values, independent of source alias, Parquet metadata and row groups."""
    values = {}
    for name in DATA_FILES:
        path = Path(directory) / name
        if not path.exists():
            continue
        path = safe_child(directory, name)
        if name.endswith(".parquet"):
            table = pq.read_table(path)
            table = clean_table(table.select(sorted(table.column_names)).combine_chunks())
            sink = pa.BufferOutputStream()
            with pa.ipc.new_stream(sink, table.schema) as writer:
                writer.write_table(table)
            values[name] = hashlib.sha256(sink.getvalue()).hexdigest()
        else:
            values[name] = digest(int(path.read_text("utf-8").strip()))
    return digest(values)


def publish_dataset(
    selection, source_root, exposed_roots, destination, *, preparation_identity=None
):
    """Publish only after every source and whole-case duplicate check succeeds."""
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("dataset_destination_exists")
    source_root = Path(source_root).resolve(strict=True)
    seen = set()
    for root in exposed_roots:
        root = Path(root).resolve(strict=True)
        for metric in sorted(root.rglob("metrics.parquet")):
            if not metric.resolve(strict=True).is_relative_to(root):
                raise ValueError("data_path_escape")
            seen.add(content_fingerprint(metric.parent))
    sources, answers = [], []
    for number, record in enumerate(selection, 1):
        name = record["case"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise ValueError("invalid_source_case")
        source = safe_child(source_root, name)
        required = {"metrics.parquet", "inject_time.txt"}
        for kind in ("logs", "traces"):
            if record.get("has_" + kind):
                required.add(kind + ".parquet")
        if not required <= {p.name for p in source.iterdir() if p.is_file()}:
            raise ValueError("missing_required_telemetry")
        fingerprint = content_fingerprint(source)
        if fingerprint in seen:
            raise ValueError("duplicate_telemetry")
        seen.add(fingerprint)
        case_id = f"case-{number:03}"
        files = {n: file_sha(safe_child(source, n)) for n in DATA_FILES if (source / n).exists()}
        sources.append((case_id, source, files))
        answers.append(
            {
                **record,
                "case_id": case_id,
                "content_sha256": fingerprint,
                "source_file_sha256": canonical(files),
            }
        )
    if not sources:
        raise ValueError("empty_holdout")
    dataset_id = digest([SOURCE_REVISION, sorted(a["content_sha256"] for a in answers)])
    if preparation_identity is None:
        from app.evaluation.heldout_protocol import source_identity

        preparation_identity = source_identity()
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".preparing-" + uuid4().hex)
    tasks, sealed = staging / "tasks", staging / "sealed"
    tasks.mkdir(parents=True)
    sealed.mkdir()
    public_files = {}
    for case_id, source, files in sources:
        target = tasks / case_id
        target.mkdir()
        for name, expected in files.items():
            original = safe_child(source, name)
            if name.endswith(".parquet"):
                # Untrusted source provenance in Parquet metadata must not disclose labels.
                table = clean_table(pq.read_table(original))
                pq.write_table(table, target / name, compression="zstd")
            else:
                (target / name).write_bytes(original.read_bytes())
            if file_sha(original) != expected:
                raise ValueError("source_changed_during_publication")
            public_files[f"{case_id}/{name}"] = file_sha(target / name)
    pd.DataFrame(answers).to_parquet(sealed / "answers.parquet", index=False)
    manifest = {
        "schema_version": 1,
        "dataset": "RCAEval RE2 grouped holdout",
        "dataset_id": dataset_id,
        "source_revision": SOURCE_REVISION,
        "split": "group_holdout_unscored",
        "files": public_files,
        "answers_sha256": file_sha(sealed / "answers.parquet"),
        "content_sha256": {a["case_id"]: a["content_sha256"] for a in answers},
        "preparation_source_identity": preparation_identity,
        "cases": [{"case_id": cid, "case": cid} for cid, _, _ in sources],
    }
    atomic_json_write(tasks / "manifest.json", manifest)
    atomic_json_write(
        sealed / "provenance.json",
        {
            "dataset_id": dataset_id,
            "selection_sha256": digest(selection),
            "source_revision": SOURCE_REVISION,
            "excluded_content_count": len(seen) - len(sources),
            "answers_sha256": file_sha(sealed / "answers.parquet"),
            "manifest_sha256": file_sha(tasks / "manifest.json"),
        },
    )
    staging.rename(destination)
    return {
        "dataset_id": dataset_id,
        "case_count": len(sources),
        "tasks": str(destination / "tasks"),
        "answers_file": str(destination / "sealed/answers.parquet"),
    }
