"""Finite, window-bounded metric evidence. No labels, unit guessing or model calls."""

import math

from app.core.single_pass import fact_for, family_for


def metric_facts(view, window, *, semantics=None):
    frame = view.m.loc[view.m.time.between(window.start, window.end)].sort_values("time")
    facts = []
    for service in sorted(view.services):
        for column in sorted(frame.columns):
            family = family_for(column, service)
            if family is None:
                continue
            meta = (semantics or {}).get(column, {})
            kind, unit = meta.get("kind", "unknown"), meta.get("unit", "unknown")
            if kind not in {"unknown", "gauge", "counter"}:
                raise ValueError("invalid_metric_semantics")
            try:
                series = frame[["time", column]].copy()
                series[column] = series[column].astype(float)
                series = series.loc[series[column].map(math.isfinite)]
            except (TypeError, ValueError):
                facts.append(
                    fact_for(
                        view.case_id,
                        window,
                        service,
                        "metrics",
                        family,
                        {
                            "metric": column,
                            "coverage": "invalid_numeric",
                            "changed": False,
                            "before_samples": 0,
                            "after_samples": 0,
                            "metric_kind": kind,
                            "unit": unit,
                        },
                    )
                )
                continue
            duplicates = bool(series.time.duplicated().any())
            series = series.drop_duplicates("time", keep=False)
            resets = 0
            if kind == "counter":
                delta, elapsed = series[column].diff(), series.time.diff()
                resets = int(delta.lt(0).sum())
                series[column] = (delta / elapsed).where(delta.ge(0) & elapsed.gt(0))
                series = series.dropna(subset=[column])
            before = series.loc[series.time.lt(window.incident), column]
            after = series.loc[series.time.ge(window.incident)]
            values = after[column]
            coverage = (
                "no_finite_samples"
                if before.empty and values.empty
                else "baseline_missing"
                if before.empty
                else "post_missing"
                if values.empty
                else "insufficient_samples"
                if len(before) < 3 or len(values) < 3
                else "complete"
            )
            pre = float(before.median()) if len(before) else None
            post = float(values.median()) if len(values) else None
            data = {
                "metric": column,
                "metric_kind": "counter_rate" if kind == "counter" else kind,
                "unit": (unit + "/second") if kind == "counter" else unit,
                "reset_count": resets,
                "coverage": coverage,
                "before_samples": len(before),
                "after_samples": len(values),
                "before_median": pre,
                "after_median": post,
                "ratio": post / pre if pre not in (None, 0) and post is not None else None,
                "changed": False,
                "onset_lower": None,
                "onset_upper": None,
                "persistence": 0.0,
                "direction": "unknown",
                "sampling_gap": duplicates,
                "post_shape": "unavailable",
                "scales": {},
            }
            if coverage == "complete":
                mad = float((before - pre).abs().median())
                threshold = max(6 * mad, abs(pre) * 0.5, 1e-9)
                cadence = float(series.time.diff().dropna().median())
                data["sampling_gap"] |= bool(
                    series.time.diff().dropna().gt(max(3 * cadence, 1)).any()
                )
                rows = list(after[["time", column]].itertuples(index=False, name=None))
                abnormal = [abs(v - pre) > threshold for _, v in rows]
                for i in range(len(rows) - 2):
                    if all(abnormal[i : i + 3]) and rows[i + 2][0] - rows[i][0] <= max(
                        3 * cadence, 1
                    ):
                        normal = series.loc[
                            (series.time < rows[i][0])
                            & (series[column].sub(pre).abs() <= threshold),
                            "time",
                        ]
                        if len(normal):
                            data.update(
                                onset_lower=int(normal.max()),
                                onset_upper=int(rows[i][0]),
                                persistence=sum(abnormal[i:]) / len(abnormal[i:]),
                            )
                        break
                data["changed"] = bool(
                    data["onset_upper"] is not None
                    and abs(post - pre) > threshold
                    and not data["sampling_gap"]
                )
                data["direction"] = "up" if post > pre else "down" if post < pre else "stable"
                split = max(1, len(values) // 3)
                early, late = float(values.iloc[:split].mean()), float(values.iloc[-split:].mean())
                data["post_shape"] = (
                    "stable"
                    if not data["changed"]
                    else "decrease"
                    if post < pre
                    else "growth"
                    if late - early > threshold
                    else "sustained"
                )
                for seconds in (30, 60, 120, 300):
                    section = after.loc[after.time.lt(window.incident + seconds), column]
                    data["scales"][str(seconds)] = float(section.median()) if len(section) else None
            facts.append(fact_for(view.case_id, window, service, "metrics", family, data))
    return tuple(facts)
