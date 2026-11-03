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
