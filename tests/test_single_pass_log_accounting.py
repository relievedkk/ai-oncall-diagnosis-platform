from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.core.single_pass import ObservationWindow
from app.evaluation.single_pass_events import log_facts


def view(frame):
    return SimpleNamespace(case_id="case-test", services=["api"], l=frame)


def frame(timestamps):
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "container_name": ["api"] * len(timestamps),
            "message": ['{"errno":"ENOMEM"}'] * len(timestamps),
        }
    )


def coverage(facts):
    return next(f.data for f in facts if f.family == "coverage")


def test_nullable_parquet_timestamps_are_processed_and_accounted(tmp_path):
    data = frame(pd.Series([90, 101, 107, pd.NA], dtype="Int64"))
    file = tmp_path / "logs.parquet"
    data.to_parquet(file)
    facts = log_facts(
        view(pd.read_parquet(file)), ObservationWindow(start=90, incident=100, end=110)
    )
    events = [f for f in facts if f.family != "coverage"]
    assert len(events) == 1
    assert events[0].data["before_count"] == 1 and events[0].data["after_count"] == 2
    c = coverage(facts)
    assert c["raw_rows"] == 4 and c["invalid_timestamp_rows"] == 1
    assert c["window_rows"] == c["visible_rows"] == c["processed_rows"] == 3
    assert c["generated_facts"] == 1


def test_boolean_nonfinite_and_non_numeric_timestamps_are_not_accepted():
    data = frame(
        pd.Series(
            [np.int64(101), np.float64(107), True, np.bool_(False), float("inf"), "bad"],
            dtype=object,
        )
    )
    c = coverage(log_facts(view(data), ObservationWindow(start=90, incident=100, end=110)))
    assert c["window_rows"] == 2 and c["invalid_timestamp_rows"] == 4


def test_declared_millisecond_timestamps_normalize_without_guessing():
    facts = log_facts(
        view(frame([90000, 101000, 107000])),
        ObservationWindow(start=90, incident=100, end=110),
        timestamp_unit="milliseconds",
    )
    assert coverage(facts)["window_rows"] == 3
    assert next(f for f in facts if f.family != "coverage").data["after_count"] == 2


def test_fully_invalid_timestamps_fail_explicitly():
    with pytest.raises(ValueError, match="log_timestamps_unusable"):
        log_facts(
            view(frame([None, True, float("nan")])),
            ObservationWindow(start=90, incident=100, end=110),
        )


def test_unmatched_and_control_rows_are_explicitly_accounted():
    data = frame([90, 101, 107, 108])
    data.loc[1, "container_name"] = "other"
    data.loc[2, "message"] = "fault injection chaos-mesh"
    c = coverage(log_facts(view(data), ObservationWindow(start=90, incident=100, end=110)))
    assert (
        c["window_rows"]
        == c["processed_rows"] + c["unmatched_services"] + c["excluded_control_rows"]
    )
    assert c["processed_rows"] == 2 and c["unmatched_services"] == c["excluded_control_rows"] == 1
