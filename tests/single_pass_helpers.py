from types import SimpleNamespace

import pytest

pd = pytest.importorskip("pandas")


def view_of(columns, *, incident=10, logs=None, traces=None, times=None):
    n = len(next(iter(columns.values())))
    metrics = pd.DataFrame({"time": list(range(n)) if times is None else times, **columns})
    return SimpleNamespace(
        case_id="case-test",
        m=metrics,
        incident=incident,
        start=int(metrics.time.min()),
        end=int(metrics.time.max()),
        services=sorted({c.split("_", 1)[0] for c in columns}),
        l=pd.DataFrame(logs or [], columns=["timestamp", "container_name", "message"]),
        t=pd.DataFrame(
            traces or [],
            columns=[
                "traceID",
                "spanID",
                "parentSpanID",
                "serviceName",
                "operationName",
                "startTimeMillis",
                "duration",
                "statusCode",
            ],
        ),
        has_traces=bool(traces),
    )
