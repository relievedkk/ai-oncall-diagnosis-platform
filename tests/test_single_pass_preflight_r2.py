import importlib.util
import json

import pytest


def module():
    assert importlib.util.find_spec("app.evaluation.single_pass_preflight") is not None
    from app.evaluation import single_pass_preflight

    return single_pass_preflight


def test_cached_preflight_rejects_changed_source_or_input_and_audit_tampering(tmp_path):
    m = module()
    context = {
        "revision": "r2",
        "source_sha256": "code-one",
        "input_sha256": "data-one",
        "profile": "rcaeval-re2",
    }
    audit = {"audited": 18, "errors": 0, "paid_gate_passed": False}
    package = m.make_package(audit, context, {"case_count": 54, "passed": True})
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(package))
    assert m.load_package(path, context)["audit"] == audit
    for changed in (
        {**context, "source_sha256": "code-two"},
        {**context, "input_sha256": "data-two"},
    ):
        with pytest.raises(ValueError, match="mismatch"):
            m.load_package(path, changed)
    package["audit"]["errors"] = 1
    path.write_text(json.dumps(package))
    with pytest.raises(ValueError, match="hash"):
        m.load_package(path, context)


def test_input_snapshot_changes_when_raw_data_changes(tmp_path):
    m = module()
    root = tmp_path / "tasks"
    root.mkdir()
    (root / "manifest.json").write_text("{}")
    (root / "logs.parquet").write_bytes(b"raw-data-one")
    answer = tmp_path / "answers.parquet"
    answer.write_bytes(b"labels")
    before = m.input_snapshot(root, answer)
    (root / "logs.parquet").write_bytes(b"raw-data-two")
    assert m.input_snapshot(root, answer) != before


def test_smoke_gate_requires_all_selected_cases_and_no_protocol_failures():
    from app.evaluation import single_pass_batch as batch

    assert hasattr(batch, "protocol_gate")
    good = {
        "status": "candidate",
        "model_calls": 1,
        "usage": {"observed": True},
        "evidence_binding_origin": "server_bound_candidate",
    }
    runs = [{**good, "case_id": "case-001"}, {**good, "case_id": "case-002"}]
    assert batch.protocol_gate(runs, {"case-001", "case-002"})
    assert not batch.protocol_gate(runs[:1], {"case-001", "case-002"})
    assert not batch.protocol_gate(
        [runs[0], {**runs[1], "status": "error"}], {"case-001", "case-002"}
    )


def test_tentative_and_confirmed_scores_are_not_pooled():
    from app.evaluation import single_pass_runner as runner

    assert hasattr(runner, "attach_scores")
    result = {
        "service": "api",
        "fault": "unknown",
        "status": "candidate",
        "model_calls": 1,
        "candidate_service": "api",
        "candidate_fault": "mem",
        "proposal": {"service": "api", "fault": "mem"},
        "usage": {"observed": True, "total_tokens": 10},
    }
    runner.attach_scores(result, {"root_cause_service": "api", "fault": "mem"})
    assert result["candidate_score"]["joint_correct"] and result["selection_score"]["joint_correct"]
    assert not result["score"]["joint_correct"]
    report = runner.summarize_runs([result])
    assert report["joint_correct"] == 0 and report["candidate_joint_correct"] == 1
    assert report["service_correct"] == 1 and report["candidate_count"] == 1


@pytest.mark.asyncio
async def test_smoke_subset_and_full_resume_do_not_resend_smoke_requests(tmp_path):
    from types import SimpleNamespace

    from app.agent.aiops.single_pass import SinglePassChoice
    from app.evaluation.single_pass_batch import evaluate_cases
    from app.evaluation.single_pass_journal import AttemptJournal
    from tests.single_pass_helpers import view_of
    from tests.test_single_pass_execution import Model

    models = []

    def open_view(cid):
        v = view_of({"a_cpu": [1.0] * 10 + [20.0] * 21})
        v.case_id = cid
        return v

    def factory(_):
        m = Model(SinglePassChoice(decision="unknown"))
        models.append(m)
        return m

    pilot = SimpleNamespace(
        ids={cid: cid for cid in ("case-001", "case-002", "case-003")},
        open=open_view,
        truth=lambda cid: {"root_cause_service": "a", "fault": "cpu"},
    )
    kwargs = {
        "output_dir": tmp_path,
        "journal": AttemptJournal(tmp_path / "j.sqlite"),
        "experiment_id": "phased",
        "config": {"smoke_case_ids": ["case-001", "case-002"]},
        "model_factory": factory,
        "telemetry_profile": "rcaeval-re2",
        "timeout_seconds": 3,
    }
    smoke = await evaluate_cases(pilot, case_ids={"case-001", "case-002"}, **kwargs)
    assert smoke["case_count"] == 2 and not smoke["complete"] and smoke["smoke_gate_passed"]
    full = await evaluate_cases(pilot, resume=True, **kwargs)
    assert full["case_count"] == 3 and full["complete"] and full["reserved_attempts"] == 3
    assert sum(m.calls for m in models) == 3
    assert full["new_model_calls"] == 1


@pytest.mark.asyncio
async def test_preparation_failure_keeps_safe_detail_without_reading_secret(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.evaluation import single_pass_batch as batch
    from app.evaluation.single_pass_journal import AttemptJournal

    pilot = SimpleNamespace(
        ids={"case-001"},
        open=lambda _: None,
        truth=lambda _: {"root_cause_service": "a", "fault": "mem"},
    )

    def fail(*args, **kwargs):
        raise ValueError("log_timestamps_unusable")

    # Dataset opening itself fails before evidence can be extracted.
    pilot.open = fail
    result = await batch.evaluate_cases(
        pilot,
        output_dir=tmp_path,
        journal=AttemptJournal(tmp_path / "j.sqlite"),
        experiment_id="blocked",
        config={},
        model_factory=lambda _: pytest.fail("paid call"),
        telemetry_profile="rcaeval-re2",
        timeout_seconds=3,
    )
    run = result["runs"][0]
    assert run["model_calls"] == 0
    assert run["failure_detail_code"] == "log_timestamps_unusable"
    assert run["failure_stage"] == "open_dataset"


def test_cached_packet_is_bound_to_case_and_digest_and_reused_without_dataset_open(tmp_path):
    from dataclasses import asdict

    from app.evaluation import single_pass_preflight as preflight
    from tests.test_single_pass_choice import packet

    assert hasattr(preflight, "cached_packets")
    p = packet()
    row = {"case_id": p.case_id, "passed": True, "packet_sha256": p.sha256, "packet": asdict(p)}
    import hashlib

    from app.core.single_pass import canonical

    row["packet_record_sha256"] = hashlib.sha256(canonical(row["packet"]).encode()).hexdigest()
    restored = preflight.cached_packets({"cases": [row]}, {p.case_id})
    assert restored[p.case_id].text == p.text
    assert restored[p.case_id].payload == p.payload
    row["packet"]["text"] += " "
    with pytest.raises(ValueError, match="cached_packet"):
        preflight.cached_packets({"cases": [row]}, {p.case_id})
    row["packet"]["text"] = p.text
    row["packet"]["service_aliases"] = [("s1", "wrong-service")]
    with pytest.raises(ValueError, match="cached_packet"):
        preflight.cached_packets({"cases": [row]}, {p.case_id})


@pytest.mark.asyncio
async def test_preflight_packet_used_without_reextracting_raw_data(tmp_path):
    from types import SimpleNamespace

    from app.agent.aiops.single_pass import SinglePassChoice
    from app.evaluation.single_pass_batch import evaluate_cases
    from app.evaluation.single_pass_journal import AttemptJournal
    from tests.test_single_pass_choice import packet
    from tests.test_single_pass_execution import Model

    p = packet()
    pilot = SimpleNamespace(
        ids={p.case_id},
        open=lambda _: pytest.fail("re-extracted dataset"),
        truth=lambda _: {"root_cause_service": "a", "fault": "cpu"},
    )
    m = Model(SinglePassChoice(decision="unknown"))
    result = await evaluate_cases(
        pilot,
        output_dir=tmp_path,
        journal=AttemptJournal(tmp_path / "j.sqlite"),
        experiment_id="cached-packet",
        config={},
        model_factory=lambda _: m,
        telemetry_profile="rcaeval-re2",
        timeout_seconds=3,
        prepared_packets={p.case_id: p},
    )
    assert result["failed"] == 0 and m.calls == 1


def test_blocked_resume_keeps_original_failure_diagnostics(tmp_path):
    from app.evaluation.single_pass_journal import AttemptJournal
    from app.evaluation.single_pass_runner import blocked_case

    journal = AttemptJournal(tmp_path / "j.sqlite")
    token = journal.reserve("failed", "case-001", "f")
    original = {
        "case_id": "case-001",
        "status": "error",
        "error": "invalid_structured_output",
        "failure_detail_code": "schema_parse_failed",
        "schema_issues": [{"code": "invalid_enum", "path": "decision"}],
        "packet_sha256": "packet-hash",
        "finish_reason": "tool_calls",
        "usage": {"observed": True, "total_tokens": 25},
    }
    journal.finish("failed", "case-001", token, original)
    resumed = blocked_case(journal, "failed", "case-001", "attempt_or_evidence_blocked")
    for key in original:
        assert resumed[key] == original[key]
    assert resumed["new_model_calls"] == 0 and resumed["model_calls"] == 1
