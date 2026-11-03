import pytest

from tests.single_pass_helpers import view_of


def test_log_events_count_buckets_not_repeated_prints_and_redact_secrets():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_events import log_facts

    logs = [
        {
            "timestamp": 12,
            "container_name": "orders_1",
            "message": "ERROR timeout Authorization: Bearer sk-private-token",
        }
        for _ in range(100)
    ]
    logs += [{"timestamp": 999, "container_name": "orders_1", "message": "ERROR timeout"}]
    view = view_of({"orders_cpu": [1] * 20}, logs=logs)
    event = next(f for f in log_facts(view, make_window(view)) if f.family == "timeout")
    assert event.data["after_count"] == 100
    assert event.data["after_buckets"] == 1
    assert event.data["specific"] is False
    assert "sk-private-token" not in event.model_dump_json()


def test_log_metadata_and_ambiguous_container_never_become_evidence():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_events import log_facts

    logs = [
        {
            "timestamp": 12,
            "container_name": "orders_1",
            "message": "fault injection root_cause_service=orders",
        },
        {"timestamp": 12, "container_name": "orders_payments", "message": "ERROR timeout"},
    ]
    view = view_of({"orders_cpu": [1] * 20, "payments_cpu": [1] * 20}, logs=logs)
    facts = log_facts(view, make_window(view))
    assert not any(f.family == "timeout" for f in facts)
    assert "root_cause_service" not in "".join(f.model_dump_json() for f in facts)


def span(identifier, parent, service, start, duration, *, trace="t", status=None):
    return {
        "traceID": trace,
        "spanID": identifier,
        "parentSpanID": parent,
        "serviceName": service,
        "operationName": "op",
        "startTimeMillis": start * 1000,
        "duration": duration,
        "statusCode": status,
    }


def test_parallel_children_are_subtracted_as_union_not_sum():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_traces import trace_facts

    view = view_of(
        {"front_cpu": [1] * 20, "a_cpu": [1] * 20, "b_cpu": [1] * 20},
        traces=[
            span("p", "", "front", 12, 5_000_000),
            span("a", "p", "a", 12.1, 3_000_000),
            span("b", "p", "b", 13, 3_000_000),
        ],
    )
    facts = trace_facts(view, make_window(view))
    operation = next(f for f in facts if f.service == "front" and f.family == "operation")
    assert operation.data["after_self_p90_us"] == pytest.approx(1_100_000)
    assert {(f.service, f.data["callee"]) for f in facts if f.family == "edge"} == {
        ("front", "a"),
        ("front", "b"),
    }


def test_parent_ids_never_join_across_traces_or_duplicate_ids():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_traces import trace_facts

    view = view_of(
        {"a_cpu": [1] * 20, "b_cpu": [1] * 20},
        traces=[
            span("p", "", "a", 12, 1_000_000, trace="one"),
            span("c", "p", "b", 12, 100_000, trace="two"),
            span("x", "", "a", 12, 1_000_000, trace="three"),
            span("x", "", "a", 12, 1_000_000, trace="three"),
            span("y", "x", "b", 12, 100_000, trace="three"),
        ],
    )
    assert not any(f.family == "edge" for f in trace_facts(view, make_window(view)))


def test_status_code_zero_and_missing_are_not_assumed_success():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_traces import trace_facts

    view = view_of(
        {"orders_cpu": [1] * 20},
        traces=[
            span("a", "", "orders", 12, 100, status=0),
            span("b", "", "orders", 13, 100, status=None),
        ],
    )
    fact = next(f for f in trace_facts(view, make_window(view)) if f.family == "operation")
    assert fact.data["after_error_count"] is None
    assert fact.data["status_semantics"] == "unknown"


def test_explicit_status_mapping_and_empty_sources():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_traces import trace_facts

    view = view_of({"orders_cpu": [1] * 20}, traces=[span("a", "", "orders", 12, 100, status=2)])
    fact = next(
        f
        for f in trace_facts(view, make_window(view), status_semantics="otel")
        if f.family == "operation"
    )
    assert fact.data["after_error_count"] == 1
    empty = view_of({"orders_cpu": [1] * 20})
    assert trace_facts(empty, make_window(empty))[0].data["status"] == "not_available"


def test_incomplete_or_duplicate_children_do_not_fabricate_parent_self_time():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_traces import trace_facts

    view = view_of(
        {"a_cpu": [1] * 20, "b_cpu": [1] * 20},
        traces=[
            span("p", "", "a", 12, 1_000_000),
            span("c", "p", "b", 12.1, 500_000),
            span("c", "p", "b", 12.1, 500_000),
        ],
    )
    facts = trace_facts(view, make_window(view))
    parent = next(f for f in facts if f.family == "operation" and f.service == "a")
    assert parent.data["after_self_p90_us"] is None
    assert parent.data["after_self_spans"] == 0


def test_nullable_operation_and_invalid_numeric_trace_values_are_quality_gaps():
    import pandas as pd

    from app.core.single_pass import make_window
    from app.evaluation.single_pass_traces import trace_facts

    rows = [span("a", "", "orders", 12, 100), span("bad", "", "orders", 12, 100)]
    rows[0]["operationName"] = pd.NA
    rows[1]["duration"] = "invalid"
    view = view_of({"orders_cpu": [1] * 20}, traces=rows)
    facts = trace_facts(view, make_window(view))
    assert next(f for f in facts if f.family == "operation").data["operation"] == "unknown"
    assert next(f for f in facts if f.family == "coverage").data["invalid_numeric_rows"] == 1


def test_many_templates_never_hide_a_competing_specific_event():
    from app.core.single_pass import make_window
    from app.evaluation.single_pass_events import log_facts

    logs = [
        {
            "timestamp": 12,
            "container_name": "orders_1",
            "message": '{"errno":"ENOMEM","site":"' + chr(65 + i) + '"}',
        }
        for i in range(20)
    ]
    logs += [{"timestamp": 16, "container_name": "orders_1", "message": '{"errno":"EMFILE"}'}]
    view = view_of({"orders_cpu": [1] * 20}, logs=logs)
    facts = log_facts(view, make_window(view))
    assert any(f.family == "socket_limit" and f.data["specific"] for f in facts)


@pytest.mark.parametrize("failure", ["missing_start", "duplicate_bad_duration"])
def test_malformed_spans_cannot_disappear_before_topology_validation(failure):
    import math

    from app.core.single_pass import make_window
    from app.evaluation.single_pass_traces import trace_facts

    rows = [span("p", "", "a", 12, 1_000_000), span("c", "p", "b", 12.1, 100_000)]
    if failure == "missing_start":
        rows[1]["startTimeMillis"] = math.nan
    else:
        rows.append({**rows[0], "duration": "bad"})
    view = view_of({"a_cpu": [1] * 20, "b_cpu": [1] * 20}, traces=rows)
    facts = trace_facts(view, make_window(view))
    assert not any(f.family == "edge" for f in facts)
    if failure == "missing_start":
        parent = next(f for f in facts if f.family == "operation" and f.service == "a")
        assert parent.data["after_self_p90_us"] is None
    coverage = next(f for f in facts if f.family == "coverage").data
    assert coverage["incomplete_traces"] >= 1
    assert coverage["invalid_numeric_rows"] >= 1
    if failure == "duplicate_bad_duration":
        assert coverage["duplicate_rows"] == 2


def test_template_prefix_collision_cannot_upgrade_keyword_to_structured_buckets():
    import json

    from app.core.single_pass import make_window
    from app.evaluation.single_pass_events import log_facts

    prefix = {"message": "EIO " + "x" * 300}
    logs = [
        {
            "timestamp": t,
            "container_name": "orders_1",
            "message": json.dumps({**prefix, "errno": code}),
        }
        for t, code in ((12, "EIO"), (17, "OTHER"))
    ]
    view = view_of({"orders_cpu": [1] * 20}, logs=logs)
    facts = log_facts(view, make_window(view))
    structured = [f for f in facts if f.family == "disk_error" and f.data["specific"]]
    assert len(structured) == 1 and structured[0].data["after_buckets"] == 1
    assert any(f.family == "disk_error" and not f.data["specific"] for f in facts)
