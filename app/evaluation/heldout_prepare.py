"""Label-aware data preparation; never part of a diagnostic process."""

import shutil
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from app.evaluation.heldout_dataset import (
    SOURCE_REVISION,
    file_sha,
    publish_dataset,
    read_json,
    safe_child,
    select_cases,
)
from app.evaluation.heldout_protocol import source_identity
from app.evaluation.single_pass_storage import atomic_json_write

INDEX_SHA256 = "c49a288920dbba2e8e724679a14636d5c7eb2b45426bba14007ef79a6c0ab1bb"


def history_records(index, answer_files, history_root):
    history_root = Path(history_root).resolve(strict=True)
    exposed, aliases = set(), {}
    known = set(index["case"])
    for path in answer_files:
        frame = pd.read_parquet(path)
        if not {"case", "case_id"} <= set(frame.columns):
            raise ValueError("historical_mapping_missing")
        for row in frame.to_dict("records"):
            previous = aliases.setdefault(row["case_id"], row["case"])
            if previous != row["case"]:
                raise ValueError("historical_alias_ambiguous")
            exposed.add(row["case"])
    for path in history_root.rglob("metrics.parquet"):
        if not path.resolve(strict=True).is_relative_to(history_root):
            raise ValueError("history_path_escape")
        name = path.parent.name
        if name in known:
            exposed.add(name)
        elif name in aliases:
            exposed.add(aliases[name])
        else:
            raise ValueError("unmapped_historical_telemetry")
    if not exposed or not exposed <= known:
        raise ValueError("historical_mapping_incomplete")
    return index[index["case"].isin(exposed)].copy()


def verify_source_files(directory, row):
    directory = Path(directory).resolve(strict=True)
    metrics = pq.read_table(safe_child(directory, "metrics.parquet"))
    times = metrics.column("time").to_pylist()
    if (
        int(safe_child(directory, "inject_time.txt").read_text("utf-8").strip())
        != row["inject_time"]
        or min(times) != row["time_start"]
        or max(times) != row["time_end"]
        or ("n_metrics" in row and metrics.num_columns - 1 != row["n_metrics"])
        or ("n_timesteps" in row and metrics.num_rows != row["n_timesteps"])
    ):
        raise ValueError("source_metadata_mismatch")
    for kind in ("logs", "traces"):
        path = directory / (kind + ".parquet")
        if bool(row.get("has_" + kind)) != path.exists():
            raise ValueError("source_availability_mismatch")
        if path.exists() and "n_" + kind in row:
            if pq.read_metadata(safe_child(directory, path.name)).num_rows != row["n_" + kind]:
                raise ValueError("source_row_count_mismatch")


def prepare_grouped(index_path, answer_files, history_root, destination, *, progress=None):
    """Fixed-source selection and download; writes source mapping only in sealed staging."""
    from huggingface_hub import hf_hub_download

    index_path = Path(index_path).resolve(strict=True)
    destination = Path(destination).resolve()
    history_root = Path(history_root).resolve(strict=True)
    if destination.exists():
        raise ValueError("dataset_destination_exists")
    if file_sha(index_path) != INDEX_SHA256:
        raise ValueError("pinned_index_mismatch")
    before = source_identity()
    index = pd.read_parquet(index_path)
    exposed = history_records(index, answer_files, history_root)
    exposed_roots = sorted({path.parent for path in history_root.rglob("metrics.parquet")})
    selection = select_cases(index, exposed)
    # Separate location: an interrupted download must never appear as a published task set.
    staging = destination.with_name(destination.name + "-sealed-download")
    staging.mkdir(parents=True, exist_ok=False)
    atomic_json_write(
        staging / "preparation.json",
        {
            "source_identity": before,
            "source_revision": SOURCE_REVISION,
            "index_sha256": INDEX_SHA256,
            "history_answers": {str(Path(p).resolve()): file_sha(p) for p in answer_files},
            "exposed_source_cases": sorted(exposed["case"]),
            "selection": selection,
        },
    )
    raw = staging / "raw"
    for number, record in enumerate(selection, 1):
        if progress:
            progress(number, len(selection))
        names = ["metrics.parquet", "inject_time.txt"] + [
            k + ".parquet" for k in ("logs", "traces") if record.get("has_" + k)
        ]
        target = raw / record["case"]
        target.mkdir(parents=True)
        for name in names:
            cached = hf_hub_download(
                repo_id="phamquiluan/RCAEval",
                repo_type="dataset",
                revision=SOURCE_REVISION,
                filename=f"{record['case']}/{name}",
            )
            shutil.copyfile(cached, target / name)
        verify_source_files(target, record)
    if source_identity() != before:
        raise ValueError("source_changed_during_preparation")
    result = publish_dataset(
        selection, raw, exposed_roots, destination, preparation_identity=before
    )
    atomic_json_write(
        destination / "sealed/preparation.json",
        {
            **read_json(staging / "preparation.json"),
            "dataset_id": result["dataset_id"],
            "staging": str(staging),
        },
    )
    return {
        **result,
        "exposed_cases": len(exposed),
        "excluded_groups": len(
            exposed[["suite", "system", "root_cause_service", "fault"]].drop_duplicates()
        ),
        "model_calls": 0,
    }
