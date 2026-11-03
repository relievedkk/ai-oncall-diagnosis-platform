import math

import pytest

from tests.single_pass_helpers import view_of


def test_metrics_never_read_future_outside_fixed_window():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_metrics import metric_facts

    view = view_of({"orders_cpu": [1.0] * 310 + [20.0] * 300 + [9999.0] * 30}, incident=310)
    window = make_window(view)
    assert (window.start, window.end) == (10, 610)
    before = metric_facts(view, window)[0]
    view.m.loc[view.m.time > 610, "orders_cpu"] = 1e30
    after = metric_facts(view, window)[0]
    assert before.ref == after.ref
    assert after.data["after_median"] == 20


@pytest.mark.parametrize(
    "family,suffix",
    [
        ("cpu", "cpu"),
        ("mem", "mem"),
        ("disk", "diskio"),
        ("delay", "latency-90"),
        ("loss", "packet_loss"),
        ("socket", "socket"),
        ("workload", "workload"),
        ("error", "error"),
    ],
)
def test_all_families_and_context_are_extracted(family, suffix):
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_metrics import metric_facts

    view = view_of({"orders_" + suffix: [1] * 10 + [20] * 10})
    fact = metric_facts(view, make_window(view))[0]
    assert fact.family == family and fact.service == "orders"
    assert fact.data["changed"] is True
    assert fact.data["onset_upper"] == 10


def test_partial_baseline_preserves_observation_but_not_change():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_metrics import metric_facts

    view = view_of({"orders_diskio": [None] * 10 + [20] * 10})
    data = metric_facts(view, make_window(view))[0].data
    assert data["coverage"] == "baseline_missing"
    assert data["after_samples"] == 10 and data["after_median"] == 20
    assert data["changed"] is False


def test_nonfinite_values_zero_baseline_and_missing_post_are_explicit():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_metrics import metric_facts

    view = view_of(
        {"orders_cpu": [0] * 9 + [math.inf] + [10] * 10, "orders_mem": [1] * 10 + [math.nan] * 10}
    )
    cpu, mem = metric_facts(view, make_window(view))
    assert cpu.data["ratio"] is None and cpu.data["changed"]
    assert mem.data["coverage"] == "post_missing"


def test_counter_conversion_requires_explicit_semantics_and_handles_reset():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_metrics import metric_facts

    view = view_of({"orders_cpu": [0, 1, 2, 3, 4, 0, 1, 2, 3, 4]}, incident=5)
    raw = metric_facts(view, make_window(view))[0]
    rate = metric_facts(
        view, make_window(view), semantics={"orders_cpu": {"kind": "counter", "unit": "seconds"}}
    )[0]
    assert raw.data["metric_kind"] == "unknown"
    assert rate.data["metric_kind"] == "counter_rate"
    assert rate.data["reset_count"] == 1
    assert rate.data["after_median"] == 1


def test_single_transient_is_not_sustained_onset():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_metrics import metric_facts

    view = view_of({"orders_cpu": [1] * 10 + [20] + [1] * 9})
    data = metric_facts(view, make_window(view))[0].data
    assert data["onset_upper"] is None
    assert data["changed"] is False
