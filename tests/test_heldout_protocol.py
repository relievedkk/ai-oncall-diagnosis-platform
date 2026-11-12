import json

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    from app.evaluation import heldout_registry

    monkeypatch.setattr(heldout_registry, "REGISTRY_ROOT", tmp_path / "shared-registry")


def dataset(tmp_path):
    from app.evaluation.heldout_dataset import publish_dataset

    source = tmp_path / "raw/source_a"
    source.mkdir(parents=True)
    pd.DataFrame(
        {
            "time": list(range(20)),
            "orders_cpu": [1] * 10 + [10] * 10,
            "orders_workload": [1] * 20,
            "orders_latency-90": [1] * 10 + [4] * 10,
        }
    ).to_parquet(source / "metrics.parquet")
    (source / "inject_time.txt").write_text("10", encoding="utf-8")
    result = publish_dataset(
        [
            {
                "case": "source_a",
                "root_cause_service": "orders",
                "fault": "cpu",
                "system": "ob",
                "has_logs": False,
                "has_traces": False,
            }
        ],
        tmp_path / "raw",
        [],
        tmp_path / "data",
    )
    return result


def test_preflight_creates_unscored_baseline_without_reading_answers(tmp_path, monkeypatch):
    from app.evaluation.heldout_protocol import prepare_run
    from app.evaluation.rcaeval import Pilot

    data = dataset(tmp_path)

    def forbidden(*args):
        pytest.fail("preflight must not access truth")

    monkeypatch.setattr(Pilot, "truth", forbidden)
    result = prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    assert result["case_count"] == 1 and result["model_calls"] == 0
    baseline = json.loads((tmp_path / "run/case-001-baseline.json").read_text("utf-8"))
    assert baseline["proposal"]["service"] == "orders"
    assert "ground_truth" not in baseline and "scores" not in baseline
    assert (tmp_path / "run/case-001-packet.json").exists()
    assert not (tmp_path / "run/finalized.json").exists()


def test_copying_anonymous_tasks_cannot_bypass_retirement(tmp_path):
    import shutil

    from app.evaluation.heldout_protocol import finalize_run, prepare_run

    data = dataset(tmp_path)
    prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    finalize_run(tmp_path / "run", mode="offline-only")
    shutil.copytree(data["tasks"], tmp_path / "copy/tasks")
    with pytest.raises(ValueError, match="already_bound|retired"):
        prepare_run(tmp_path / "copy/tasks", tmp_path / "second-run", expected_count=1)


def test_source_freeze_from_before_data_publication_is_enforced(tmp_path, monkeypatch):
    from app.evaluation import heldout_protocol as protocol

    data = dataset(tmp_path)
    monkeypatch.setattr(protocol, "source_identity", lambda: {"changed": "after opening telemetry"})
    with pytest.raises(ValueError, match="preparation_source_changed"):
        protocol.prepare_run(data["tasks"], tmp_path / "run", expected_count=1)


@pytest.mark.asyncio
async def test_scoring_is_blocked_while_live_execution_owns_batch(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.evaluation.heldout_live import run_live
    from app.evaluation.heldout_protocol import prepare_run
    from app.evaluation.heldout_score import score_run

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-not-a-real-key")
    data = dataset(tmp_path)
    run = prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    blocked = []

    class Model:
        max_retries = 0

        def with_structured_output(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            try:
                score_run(tmp_path / "run", data["answers_file"], offline_only=True)
            except ValueError as exc:
                blocked.append(str(exc))
            return {
                "parsed": {"hypothesis_id": None, "decision": "unknown", "reason": "uncertain"},
                "raw": SimpleNamespace(usage_metadata={"total_tokens": 30}, response_metadata={}),
            }

    await run_live(
        tmp_path / "run",
        model_factory=lambda _: Model(),
        model_config={"model": "fake"},
        allow_live=True,
        reviewed_sha=run["run_sha256"],
    )
    assert blocked == ["evaluation_batch_busy"]
    assert not (tmp_path / "run/finalized.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_prediction", [False, True])
async def test_resume_preserves_error_receipt_and_usage_without_resending(
    tmp_path, monkeypatch, saved_prediction
):
    from types import SimpleNamespace

    from app.evaluation import heldout_live as live
    from app.evaluation.heldout_protocol import prepare_run

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-not-a-real-key")
    data = dataset(tmp_path)
    run = prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    calls = []

    class Model:
        max_retries = 0

        def with_structured_output(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            calls.append(1)
            return {
                "parsed": None,
                "raw": SimpleNamespace(usage_metadata={"total_tokens": 40}, response_metadata={}),
            }

    options = {
        "model_factory": lambda _: Model(),
        "model_config": {"model": "fake"},
        "allow_live": True,
        "reviewed_sha": run["run_sha256"],
    }
    write = live.atomic_json_write

    def crash(path, value):
        if path.name.endswith("-prediction.json"):
            if saved_prediction:
                write(path, value)
            raise OSError("simulated local publication interruption")
        write(path, value)

    monkeypatch.setattr(live, "atomic_json_write", crash)
    with pytest.raises(OSError):
        await live.run_live(tmp_path / "run", **options)
    monkeypatch.setattr(live, "atomic_json_write", write)
    await live.run_live(tmp_path / "run", resume=True, **options)
    prediction = json.loads((tmp_path / "run/case-001-prediction.json").read_text("utf-8"))
    assert prediction["usage"]["total_tokens"] == 40
    assert prediction["error"] == "invalid_structured_output"
    assert len(calls) == 1
    assert (tmp_path / "run/finalized.json").exists()


@pytest.mark.parametrize("kind", ["input", "packet", "source"])
def test_frozen_run_rejects_changed_inputs_or_code(tmp_path, monkeypatch, kind):
    from app.evaluation import heldout_protocol as protocol

    data = dataset(tmp_path)
    protocol.prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    if kind == "input":
        path = tmp_path / "data/tasks/case-001/inject_time.txt"
        path.write_text("11", encoding="utf-8")
    elif kind == "packet":
        (tmp_path / "run/case-001-packet.json").write_text("{}", encoding="utf-8")
    else:
        monkeypatch.setattr(protocol, "source_identity", lambda: {"source": "changed"})
    with pytest.raises(ValueError, match="changed|mismatch"):
        protocol.verify_run(tmp_path / "run")


def test_unknown_file_in_tasks_is_rejected_before_opening_telemetry(tmp_path):
    from app.evaluation.heldout_protocol import prepare_run

    data = dataset(tmp_path)
    (tmp_path / "data/tasks/answers.json").write_text('{"answer":"orders"}', encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected_task_files"):
        prepare_run(data["tasks"], tmp_path / "run", expected_count=1)


def test_offline_finalization_retires_dataset_and_cannot_be_reused(tmp_path):
    from app.evaluation.heldout_protocol import finalize_run, prepare_run, verify_run

    data = dataset(tmp_path)
    prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    final = finalize_run(tmp_path / "run", mode="offline-only")
    assert final["mode"] == "offline-only"
    assert verify_run(tmp_path / "run")["case_count"] == 1
    with pytest.raises(ValueError, match="dataset_already_bound|retired"):
        prepare_run(data["tasks"], tmp_path / "second", expected_count=1)


@pytest.mark.asyncio
async def test_live_requires_explicit_choice_and_never_resends_saved_attempts(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from app.evaluation.heldout_live import run_live
    from app.evaluation.heldout_protocol import prepare_run

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-not-a-real-key")
    data = dataset(tmp_path)
    run = prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    calls = []

    class Model:
        max_retries = 0

        def with_structured_output(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            calls.append(1)
            return {
                "parsed": {"hypothesis_id": None, "decision": "unknown", "reason": "insufficient"},
                "raw": SimpleNamespace(
                    usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                    response_metadata={},
                ),
            }

    options = {
        "model_factory": lambda packet: Model(),
        "model_config": {"model": "fake"},
        "reviewed_sha": run["run_sha256"],
    }
    with pytest.raises(ValueError, match="explicit_live_required"):
        await run_live(tmp_path / "run", **options)
    await run_live(tmp_path / "run", allow_live=True, **options)
    await run_live(tmp_path / "run", allow_live=True, resume=True, phase="full", **options)
    assert len(calls) == 1
    prediction = json.loads((tmp_path / "run/case-001-prediction.json").read_text("utf-8"))
    assert prediction["status"] == "insufficient_evidence"
    assert not any(k in prediction for k in ("ground_truth", "scores"))
    assert (tmp_path / "run/case-001-timing.json").exists()
    from app.evaluation.heldout_score import score_run

    scores = score_run(tmp_path / "run", data["answers_file"])
    assert scores["model"]["observed_tokens"] == 30
    assert scores["model"]["confirmed_joint_accuracy"] == 0
    assert scores["timing"]["observed_attempts"] == 1


@pytest.mark.asyncio
async def test_failed_live_attempt_stops_batch_and_cannot_retry(tmp_path, monkeypatch):
    from app.evaluation.heldout_live import run_live
    from app.evaluation.heldout_protocol import prepare_run

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-not-a-real-key")
    data = dataset(tmp_path)
    run = prepare_run(data["tasks"], tmp_path / "run", expected_count=1)
    calls = []

    class Model:
        max_retries = 0

        def with_structured_output(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            calls.append(1)
            raise TimeoutError()

    options = {
        "model_factory": lambda packet: Model(),
        "model_config": {"model": "fake"},
        "allow_live": True,
        "reviewed_sha": run["run_sha256"],
    }
    await run_live(tmp_path / "run", **options)
    with pytest.raises(ValueError, match="finalized|retired"):
        await run_live(tmp_path / "run", resume=True, **options)
    assert len(calls) == 1


def test_inventory_cli_is_usable_without_provider_configuration(tmp_path, monkeypatch):
    from app.evaluation.heldout_cli import main

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    data = dataset(tmp_path)
    assert (
        main(["inventory", "--tasks", data["tasks"], "--output", str(tmp_path / "inventory.json")])
        == 0
    )
    result = json.loads((tmp_path / "inventory.json").read_text("utf-8"))
    assert result["case_count"] == 1 and result["model_calls"] == 0
    assert len(result["input_sha256"]) == 64
    assert "case-001/metrics.parquet" in result["input_files"]
