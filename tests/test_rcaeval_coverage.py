import json
import math

import pytest

pd = pytest.importorskip("pandas")
pytest.importorskip("pyarrow")


@pytest.fixture
def telemetry(tmp_path):
    def make(**columns):
        from app.evaluation.rcaeval import CaseView

        times = list(range(20))
        pd.DataFrame({"time": times, **columns}).to_parquet(tmp_path / "metrics.parquet")
        pd.DataFrame(
            {
                "timestamp": [0, 19],
                "container_name": ["orders", "orders"],
                "message": ["normal", "busy"],
            }
        ).to_parquet(tmp_path / "logs.parquet")
        (tmp_path / "inject_time.txt").write_text("5")
        return CaseView(tmp_path, "case-detail")

    return make


def test_missing_baseline_keeps_post_observations_without_claiming_fault(telemetry):
    from app.evaluation.rcaeval_evidence import _profile_from_metrics

    view = telemetry(orders_diskio=[None] * 5 + [20.0] * 15)
    profile = _profile_from_metrics(view, "orders", "disk")
    assert profile.available is True
    assert profile.coverage.status == "baseline_missing"
    assert profile.coverage.before_samples == 0
    assert profile.coverage.after_samples == 15
    assert profile.post_start_mean == 20
    assert profile.strong_change is False and profile.fault_eligible is False
    trend = view.metric_trends("orders", mechanisms=("disk",))[0]
    assert trend["baseline_median"] is None
    assert trend["comparison_available"] is False
    assert trend["after_samples"] == 15


def test_missing_post_is_coverage_gap_not_normal(telemetry):
    from app.evaluation.rcaeval_evidence import _profile_from_metrics

    profile = _profile_from_metrics(telemetry(orders_cpu=[1.0] * 5 + [None] * 15), "orders", "cpu")
    assert profile.coverage.status == "post_missing"
    assert profile.coverage.after_samples == 0
    assert not profile.strong_change and not profile.fault_eligible


@pytest.mark.parametrize(
    "mechanism,suffix",
    [
        ("cpu", "cpu"),
        ("mem", "mem"),
        ("delay", "latency-90"),
        ("disk", "diskio"),
        ("loss", "packet_loss"),
        ("socket", "socket"),
    ],
)
def test_trends_are_scoped_to_requested_mechanism(telemetry, mechanism, suffix):
    columns = {
        f"orders_{s}": [1.0] * 5 + [20.0] * 15
        for s in ("cpu", "mem", "latency-90", "diskio", "packet_loss", "socket")
    }
    rows = telemetry(**columns).metric_trends("orders", mechanisms=(mechanism,))
    assert [row["metric"] for row in rows] == [f"orders_{suffix}"]


def test_nonfinite_values_never_become_evidence_or_break_serialization(telemetry):
    from app.evaluation.rcaeval_evidence import _profile_from_metrics

    view = telemetry(orders_cpu=[1, 1, float("inf"), 1, float("nan")] + [20] * 15)
    profile = _profile_from_metrics(view, "orders", "cpu")
    assert profile.ratio is not None and math.isfinite(profile.ratio)
    rows = view.metric_trends("orders", mechanisms=("cpu",))
    json.dumps(rows, allow_nan=False)


def test_zero_baseline_and_constant_series_are_handled_explicitly(telemetry):
    from app.evaluation.rcaeval_evidence import _profile_from_metrics

    view = telemetry(orders_cpu=[0] * 5 + [10] * 15, orders_mem=[0] * 20)
    changed = _profile_from_metrics(view, "orders", "cpu")
    stable = _profile_from_metrics(view, "orders", "mem")
    assert changed.strong_change and changed.ratio is None
    assert not stable.strong_change


def test_trend_query_rejects_unsupported_mechanism(telemetry):
    with pytest.raises(ValueError):
        telemetry(orders_cpu=[1] * 20).metric_trends("orders", mechanisms=("shell",))
