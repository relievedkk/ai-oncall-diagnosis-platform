"""Post-freeze development explanations; never used by retrieval or diagnosis."""

from pathlib import Path

from app.evaluation.single_pass_semantics import SOURCES

REQUIREMENTS = {
    "cpu": "核实CPU使用量语义，比较负载、GC/重试与其他资源；未知单位不能计算饱和度。",
    "mem": "区分持续内存增长、缓存和分配/GC行为，并处理CPU与业务量共变。",
    "disk": "核查I/O等待、存储错误及吞吐对照；diskio变化本身不能确认磁盘压力。",
    "socket": "核查连接/句柄资源限制或连接失败与资源对照；连接数变化不能单独确诊。",
    "loss": "核查丢包/丢弃/重传或对应结构化网络观测；timeout和error不是丢包证明。",
    "delay": "区分自身、下游和网络延迟，并核对资源、负载和路径覆盖。",
}


def _dump(h):
    return h if isinstance(h, dict) else h.model_dump()


def attribute_failure(evidence, hypotheses, *, catalog, truth, packet_error=None):
    root, fault = truth["root_cause_service"], truth["fault"]
    frozen = [_dump(h) for h in catalog]
    actual = next((h for h in frozen if h["service"] == root and h["fault"] == fault), None)
    selected = [_dump(h) for h in hypotheses]
    own = [f for f in evidence.facts if f.service == root and f.family == fault]
    metrics = [f for f in own if f.source == "metrics"]
    semantic_known = any(
        f.data.get("semantic_source") in SOURCES
        or (
            f.data.get("unit") not in {None, "unknown"}
            and f.data.get("metric_kind") not in {None, "unknown"}
        )
        for f in metrics
    )
    recalled = any(h["service"] == root and h["fault"] == fault for h in selected)
    gaps = list(actual["gaps"]) if actual else ["hypothesis_not_generated"]
    categories, actions = [], []
    if not recalled:
        categories.append("retrieval_miss")
        actions.append("repair_label_free_top5_retrieval")
    if packet_error:
        categories.append(
            "packet_budget" if packet_error == "evidence_budget_exceeded" else "packet_invalid"
        )
        actions.append("preserve_material_evidence_within_transport")
    if not semantic_known:
        categories.append("unverified_measurement_semantics")
        actions.append("verify_collector_or_published_quantity")
    if not metrics:
        categories.append("extracted_family_absent")
    if any(
        f.data.get("coverage") in {"baseline_missing", "insufficient_samples", "no_finite_samples"}
        for f in metrics
    ):
        categories.append("baseline_gap")
        actions.append("verify_baseline_coverage")
    if "missing_mechanism_specific_observation" in gaps:
        categories.append("mechanism_evidence_gap")
        actions.append("review_original_mechanism_sources")
    if "unresolved_competing_explanation" in gaps:
        categories.append("unresolved_competition")
        actions.append("identify_discriminating_resource_or_path_contrast")
    if "missing_independent_response_contrast" in gaps:
        categories.append("response_contrast_gap")
        actions.append("verify_response_or_path_coverage")
    if not own:
        actions.append("review_original_mechanism_sources")
    columns = (
        "metric",
        "unit",
        "metric_kind",
        "quantity",
        "semantic_source",
        "interpretation",
        "coverage",
        "changed",
        "direction",
        "post_shape",
        "before_samples",
        "after_samples",
        "before_median",
        "after_median",
        "onset_lower",
        "onset_upper",
    )
    return {
        "expected_service": root,
        "expected_fault": fault,
        "axes": {
            "retrieval": "recalled_top5" if recalled else "missing_top5",
            "packet": packet_error or "passed",
            "permission": "eligible" if actual and actual["eligible"] else "not_eligible",
            "semantics": "documented_or_explicit" if semantic_known else "unverified",
            "coverage": sorted({f.data.get("coverage", "unknown") for f in metrics}),
        },
        "categories": sorted(set(categories)),
        "gaps": gaps,
        "actions": sorted(set(actions)),
        "raw_data_absence_proven": False,
        "raw_review_required": bool(not metrics or "mechanism_evidence_gap" in categories),
        "requirement": REQUIREMENTS.get(fault, "核查故障定义与可区分证据。"),
        "observations": [
            {
                "ref": f.ref,
                "source": f.source,
                "family": f.family,
                **{key: f.data[key] for key in columns if key in f.data},
            }
            for f in own
        ],
        "supporting_refs": list(actual["supporting_refs"]) if actual else [],
        "opposing_refs": list(actual["opposing_refs"]) if actual else [],
        "contrast_refs": list(actual["contrast_refs"]) if actual else [],
        "alternatives": list(actual["alternatives"]) if actual else [],
        "source_references": {
            f.data["semantic_source"]: SOURCES[f.data["semantic_source"]]
            for f in metrics
            if f.data.get("semantic_source") in SOURCES
        },
    }


def write_attribution_report(audit, path):
    lines = [
        "# 开发样本失败归因（零模型调用）",
        "",
        "证据与候选先冻结，开发标签仅用于此表。缺少提取特征不能证明原始数据不存在。",
        "",
        "| 案例 | 候选 | 证据资格 | 包体 | 归因 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in audit["cases"]:
        a = row.get("attribution")
        if not a:
            lines.append(
                f"| {row['case_id']} | 审计失败 | 未验证 | 未验证 | {row.get('error', 'not_attributed')} |"
            )
            continue
        axes = a["axes"]
        lines.append(
            f"| {row['case_id']} | {axes['retrieval']} | {axes['permission']} | {axes['packet']} | {', '.join(a['categories']) or '待人工核对'} |"
        )
    for row in audit["cases"]:
        a = row.get("attribution")
        if not a:
            continue
        lines.extend(
            [
                "",
                "## " + row["case_id"],
                "",
                a["requirement"],
                "",
                "缺口：" + ", ".join(a["gaps"]),
                "",
                "后续：" + ", ".join(a["actions"]),
                "",
                "支持 refs：" + ", ".join(a["supporting_refs"]),
                "",
                "反证 refs：" + ", ".join(a["opposing_refs"]),
                "",
                "对照 refs：" + ", ".join(a["contrast_refs"]),
                "",
                "原始数据缺失已证实：否；须核对原始来源：" + str(a["raw_review_required"]),
            ]
        )
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
