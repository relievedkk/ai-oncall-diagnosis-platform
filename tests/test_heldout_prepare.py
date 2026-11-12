import pandas as pd
import pytest

from tests.test_heldout_dataset import grid, source_case


def test_history_registry_includes_named_downloads_and_all_old_answer_cases(tmp_path):
    from app.evaluation.heldout_prepare import history_records

    index, old = grid()
    old = old.iloc[[0]].copy()
    old["case_id"] = "case-001"
    old.to_parquet(tmp_path / "old.parquet")
    source_case(tmp_path / "history", "case-001", 1)
    extra = index[index.root_cause_service.eq("svc4")].iloc[0]["case"]
    source_case(tmp_path / "history", extra, 2)
    records = history_records(index, [tmp_path / "old.parquet"], tmp_path / "history")
    assert set(records["case"]) == {old.iloc[0]["case"], extra}


def test_unknown_anonymous_history_stops_instead_of_guessing_mapping(tmp_path):
    from app.evaluation.heldout_prepare import history_records

    index, _ = grid()
    source_case(tmp_path / "history", "case-999", 1)
    with pytest.raises(ValueError, match="unmapped_historical_telemetry"):
        history_records(index, [], tmp_path / "history")


def test_downloaded_files_must_match_pinned_index_before_publication(tmp_path):
    from app.evaluation.heldout_prepare import verify_source_files

    directory = source_case(tmp_path, "source_a", 1)
    row = {
        "inject_time": 2,
        "time_start": 1,
        "time_end": 3,
        "n_metrics": 1,
        "n_timesteps": 3,
        "has_logs": False,
        "has_traces": False,
    }
    verify_source_files(directory, row)
    (directory / "inject_time.txt").write_text("1", encoding="utf-8")
    with pytest.raises(ValueError, match="source_metadata_mismatch"):
        verify_source_files(directory, row)


def test_preparation_does_not_mistake_its_own_staging_for_historical_exposure(
    tmp_path, monkeypatch
):
    import huggingface_hub

    from app.evaluation import heldout_prepare as prepare
    from app.evaluation.heldout_dataset import file_sha, select_cases

    index, old = grid()
    old = old.iloc[::3].copy()
    old["case_id"] = [f"case-{i:03}" for i in range(1, len(old) + 1)]
    index.to_parquet(tmp_path / "index.parquet")
    old.to_parquet(tmp_path / "answers.parquet")
    source_case(tmp_path / "history", "case-001", 99)
    selected = select_cases(index, old)
    for row in selected:
        directory = tmp_path / "remote" / row["case"]
        directory.mkdir(parents=True)
        pd.DataFrame(
            {
                "time": [row["time_start"], row["inject_time"], row["time_end"]],
                "service_cpu": [1, 2, 3],
            }
        ).to_parquet(directory / "metrics.parquet")
        (directory / "inject_time.txt").write_text(str(row["inject_time"]), encoding="utf-8")
        pd.DataFrame({"timestamp": [], "container_name": [], "message": []}).to_parquet(
            directory / "logs.parquet"
        )
        if row["has_traces"]:
            pd.DataFrame({"traceID": []}).to_parquet(directory / "traces.parquet")

    def fetch(**kwargs):
        assert kwargs["revision"] == "afeacb11bcc94dadfd1c8f483ee4377b2b8b614e"
        return str(tmp_path / "remote" / kwargs["filename"])

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fetch)
    monkeypatch.setattr(prepare, "INDEX_SHA256", file_sha(tmp_path / "index.parquet"))
    result = prepare.prepare_grouped(
        tmp_path / "index.parquet",
        [tmp_path / "answers.parquet"],
        tmp_path / "history",
        tmp_path / "history/new-holdout",
    )
    assert result["case_count"] == 36
