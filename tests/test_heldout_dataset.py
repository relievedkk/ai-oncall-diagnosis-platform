import json
from pathlib import Path

import pandas as pd
import pytest


def grid():
    rows = []
    for s, system in enumerate(("ob", "ss", "tt")):
        for f, fault in enumerate(("cpu", "mem", "disk", "socket", "loss", "delay")):
            for service in range(5):
                for repeat in range(1, 4):
                    t = 10000 + s * 100000 + f * 10000 + service * 1000 + repeat * 100
                    rows.append(
                        {
                            "case": f"re2{system}_svc{service}_{fault}_{repeat}",
                            "suite": "RE2",
                            "system": system,
                            "root_cause_service": f"svc{service}",
                            "fault": fault,
                            "repetition": repeat,
                            "inject_time": t,
                            "time_start": t - 20,
                            "time_end": t + 20,
                            "has_logs": True,
                            "has_traces": system != "ss",
                        }
                    )
    index = pd.DataFrame(rows)
    return index, index[index.root_cause_service.isin(["svc0", "svc1", "svc2"])]


def test_selection_excludes_entire_exposed_groups_and_is_order_independent():
    from app.evaluation.heldout_dataset import select_cases

    index, exposed = grid()
    selected = select_cases(index, exposed.iloc[::3])
    assert len(selected) == 36
    assert {r["root_cause_service"] for r in selected} == {"svc3", "svc4"}
    assert selected == select_cases(index.sample(frac=1, random_state=7), exposed)
    assert len({(r["system"], r["root_cause_service"], r["fault"]) for r in selected}) == 36


def test_same_system_overlapping_window_is_not_selected():
    from app.evaluation.heldout_dataset import select_cases

    index, exposed = grid()
    row = index.index[
        (index.system == "ob")
        & (index.fault == "cpu")
        & (index.root_cause_service == "svc3")
        & (index.repetition == 1)
    ][0]
    index.loc[row, ["inject_time", "time_start", "time_end"]] = exposed.iloc[0][
        ["inject_time", "time_start", "time_end"]
    ].tolist()
    assert index.loc[row, "case"] not in {r["case"] for r in select_cases(index, exposed)}


def test_missing_stratum_stops_instead_of_reusing_a_group():
    from app.evaluation.heldout_dataset import select_cases

    index, exposed = grid()
    index = index[
        ~((index.system == "tt") & (index.fault == "loss") & (index.root_cause_service == "svc4"))
    ]
    with pytest.raises(ValueError, match="incomplete_holdout_grid"):
        select_cases(index, exposed)


def test_selected_groups_cannot_share_a_recording_window():
    from app.evaluation.heldout_dataset import select_cases

    index, exposed = grid()
    new = (
        (index.system == "ob")
        & (index.fault == "cpu")
        & index.root_cause_service.isin(["svc3", "svc4"])
    )
    index.loc[new, ["inject_time", "time_start", "time_end"]] = [999999, 999900, 1000100]
    with pytest.raises(ValueError, match="overlapping_selected_windows"):
        select_cases(index, exposed)


def test_publication_checks_prior_telemetry_and_removes_custom_parquet_metadata(tmp_path):
    import pyarrow.parquet as pq

    from app.evaluation.heldout_dataset import publish_dataset

    directory = source_case(tmp_path / "raw", "source_a", 1)
    source_case(tmp_path / "old", "renamed_old", 1)
    table = pq.read_table(directory / "metrics.parquet")
    pq.write_table(
        table.replace_schema_metadata({b"answer": b"secret-root-disk"}),
        directory / "metrics.parquet",
    )
    selection = [{"case": "source_a", "has_logs": False, "has_traces": False}]
    with pytest.raises(ValueError, match="duplicate_telemetry"):
        publish_dataset(selection, tmp_path / "raw", [tmp_path / "old"], tmp_path / "duplicate")
    publish_dataset(selection, tmp_path / "raw", [], tmp_path / "clean")
    assert not pq.read_schema(tmp_path / "clean/tasks/case-001/metrics.parquet").metadata


def source_case(root, name, value):
    directory = root / name
    directory.mkdir(parents=True)
    pd.DataFrame({"time": [1, 2, 3], "svc_cpu": [value] * 3}).to_parquet(
        directory / "metrics.parquet"
    )
    (directory / "inject_time.txt").write_text("2", encoding="utf-8")
    return directory


def test_publication_rejects_renamed_duplicate_and_path_escape(tmp_path):
    from app.evaluation.heldout_dataset import publish_dataset

    source_case(tmp_path / "raw", "source_a", 1)
    source_case(tmp_path / "raw", "source_b", 1)
    selection = [
        {"case": "source_a", "has_logs": False, "has_traces": False},
        {"case": "source_b", "has_logs": False, "has_traces": False},
    ]
    with pytest.raises(ValueError, match="duplicate_telemetry"):
        publish_dataset(selection, tmp_path / "raw", [], tmp_path / "out")
    assert not (tmp_path / "out" / "tasks").exists()
    with pytest.raises(ValueError, match="invalid_source_case"):
        publish_dataset([{"case": "../escape"}], tmp_path / "raw", [], tmp_path / "out2")


def test_anonymous_publication_keeps_answers_and_source_paths_outside_tasks(tmp_path):
    from app.evaluation.heldout_dataset import publish_dataset

    source_case(tmp_path / "raw", "root_disk_1", 7)
    selection = [
        {
            "case": "root_disk_1",
            "has_logs": False,
            "has_traces": False,
            "root_cause_service": "root",
            "fault": "disk",
            "system": "ob",
        }
    ]
    published = publish_dataset(selection, tmp_path / "raw", [], tmp_path / "out")
    tasks = tmp_path / "out" / "tasks"
    text = (tasks / "manifest.json").read_text("utf-8")
    assert "root_disk" not in text and "root_cause" not in text
    assert json.loads(text)["cases"] == [{"case_id": "case-001", "case": "case-001"}]
    assert Path(published["answers_file"]).is_file()
    assert (tasks / "case-001" / "metrics.parquet").is_file()
    assert "root_disk" not in " ".join(p.as_posix() for p in tasks.rglob("*"))


def test_nested_field_metadata_is_stripped_and_cannot_evade_content_deduplication(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from app.evaluation.heldout_dataset import content_fingerprint, publish_dataset

    a = source_case(tmp_path / "raw", "source_a", 1)
    b = source_case(tmp_path / "raw", "source_b", 1)

    def write(path, label):
        schema = pa.schema(
            [
                pa.field("time", pa.int64()),
                pa.field("svc_cpu", pa.int64(), metadata={b"root_cause": label}),
                pa.field(
                    "nested",
                    pa.list_(
                        pa.field(
                            "item",
                            pa.struct([pa.field("value", pa.int64(), metadata={b"answer": label})]),
                        )
                    ),
                ),
            ]
        )
        pq.write_table(
            pa.Table.from_pydict(
                {"time": [1, 2, 3], "svc_cpu": [1, 1, 1], "nested": [[{"value": 1}]] * 3},
                schema=schema,
            ),
            path / "metrics.parquet",
        )

    write(a, b"a")
    write(b, b"b")
    assert content_fingerprint(a) == content_fingerprint(b)
    publish_dataset([{"case": "source_a"}], tmp_path / "raw", [], tmp_path / "out")
    schema = pq.read_schema(tmp_path / "out/tasks/case-001/metrics.parquet")
    assert schema.field("svc_cpu").metadata is None
    assert schema.field("nested").type.value_type.field("value").metadata is None
