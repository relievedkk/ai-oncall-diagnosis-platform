"""Paid continuation guards; these tests never construct a provider model."""

import importlib.util
import zipfile
from pathlib import Path

import pytest


def driver():
    path = Path("scripts/rcaeval_remaining46_20261107.py")
    assert path.is_file(), "reviewable continuation driver is missing"
    spec = importlib.util.spec_from_file_location("remaining46_driver", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sources():
    if not Path(".runtime/rcaeval/v51-r2-all54-live-20261104/source-frozen-1d3bad32.zip").exists():
        pytest.skip("local frozen experiment archive unavailable")
    with zipfile.ZipFile(
        ".runtime/rcaeval/v51-r2-all54-live-20261104/source-frozen-1d3bad32.zip"
    ) as archive:
        before = {n: archive.read(n) for n in archive.namelist()}
    new_archive = Path(
        ".runtime/rcaeval/v51-r2-persistence-remaining46-20261107/source-frozen-871dfa64.zip"
    )
    if not new_archive.exists():
        pytest.skip("repaired frozen source archive unavailable")
    with zipfile.ZipFile(new_archive) as archive:
        after = {n: archive.read(n) for n in archive.namelist()}
    return before, after


def test_continuation_accepts_only_persistence_transition():
    m = driver()
    before, after = sources()
    attested = m.attest_algorithms(before, after)
    assert len(attested["changed_files"]) == 5
    assert attested["diagnosis_contract_unchanged"] is True


def test_changed_candidate_generator_blocks_frozen_packet_reuse():
    m = driver()
    before, after = sources()
    after["app/evaluation/single_pass_hypotheses.py"] += b"\n# changed generator\n"
    with pytest.raises(ValueError, match="source_transition_not_storage_only"):
        m.attest_algorithms(before, after)


def test_changed_choice_binding_blocks_reuse_even_inside_allowed_module():
    m = driver()
    before, after = sources()
    key = "app/agent/aiops/single_pass.py"
    after[key] = after[key].replace(b"unknown_hypothesis_id", b"repaired_unknown_id", 1)
    with pytest.raises(ValueError, match="diagnosis_contract_changed"):
        m.attest_algorithms(before, after)


def test_remaining_scope_cannot_include_historical_unknown_or_complete_case():
    m = driver()
    historical = {
        "case-001",
        "case-012",
        "case-013",
        "case-014",
        "case-019",
        "case-024",
        "case-026",
        "case-032",
    }
    remaining = {f"case-{i:03}" for i in range(1, 55)} - historical
    m.validate_scope(historical, remaining)
    assert len(remaining) == 46
    with pytest.raises(ValueError, match="remaining_scope_invalid"):
        m.validate_scope(historical, remaining | {"case-026"})
    with pytest.raises(ValueError, match="remaining_scope_invalid"):
        m.validate_scope(historical, remaining | {"case-013"})


def test_historical_summary_drift_is_rejected_before_aggregate(tmp_path):
    import hashlib

    m = driver()
    path = tmp_path / "summary.json"
    original = b'{"runs":[]}'
    path.write_bytes(original)
    expected = hashlib.sha256(original).hexdigest()
    assert m.verified_parent_summary(path, expected) == {"runs": []}
    path.write_bytes(b'{"runs":[{"score":"changed"}]}')
    with pytest.raises(ValueError, match="historical_summary_changed"):
        m.verified_parent_summary(path, expected)
