"""Evidence-bounded, read-only inspection of registered application services."""

import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Any

from app.agent.mcp_client import get_mcp_client_with_retry
from app.services.service_catalog import service_catalog
from app.tools.service_observability import query_service_logs

METRICS = {
    "up": "存活状态",
    "cpu": "CPU",
    "memory": "内存",
    "latency_p95": "P95 延迟",
    "error_rate": "错误率",
}


def is_full_health_request(question: str) -> bool:
    """Recognize explicit requests for the complete registered-service checklist."""
    normalized = re.sub(r"\s+", "", question).lower()
    return bool(
        re.match(r"^(?:请帮我|帮我|请)?(?:主动检查|主动巡检|检查)(?:所有|全部)服务", normalized)
        and all(word in normalized for word in ("存活", "cpu", "内存", "延迟", "错误率", "日志"))
    )


def _decode_metric(blocks: Any) -> dict[str, Any]:
    if isinstance(blocks, dict) and "success" in blocks:
        return blocks
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            payload = json.loads(block["text"])
            if isinstance(payload, dict) and "success" in payload:
                return payload
    raise ValueError("Monitor returned no readable metric response")


def _cell(value: Any) -> str:
    """Keep error and source data from breaking the Markdown table structure."""
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _metric_summary(metric: str, evidence: dict[str, Any]) -> str:
    if evidence.get("success") is not True:
        return "查询失败：" + _cell(evidence.get("error", "未知错误"))
    if metric == "error_rate" and evidence.get("data_status") == "no_traffic":
        return "最新请求速率为 0，当前无流量；错误率不适用（不能当作 0）"
    points = evidence.get("data_points", [])
    if not points:
        return (
            "缺少有效数据；不能判断错误率为 0"
            if metric == "error_rate"
            else "缺少有效数据；不能判断健康状态"
        )
    latest = points[-1]["value"]
    unit = {
        "percent_of_one_cpu_core": "%（单核）",
        "bytes": "bytes",
        "seconds": "秒",
        "ratio": "（比例）",
    }.get(evidence.get("unit"), "")
    if metric == "up":
        state = "在线" if latest == 1 else "抓取失败/不在线" if latest == 0 else "状态未知"
        return f"{state}；最近采样 up={_cell(latest)}（不代表窗口内始终在线）"
    summary = f"最近采样 {_cell(latest)} {unit}"
    maximum = evidence.get("statistics", {}).get("max")
    if maximum is not None:
        summary += f"；窗口最大 {_cell(maximum)} {unit}"
    return summary


async def inspect_registered_services() -> str:
    """Collect all six checks and render findings without model reinterpretation."""
    services = service_catalog.list_services()
    end = datetime.now().astimezone()
    start = end - timedelta(minutes=15)
    lines = [
        "# 主动健康检查报告",
        "",
        "检查范围：已登记应用服务 "
        + ("、".join(services) or "（无）")
        + "；不包含未登记应用及基础设施。",
        f"查询时间窗口：{start.isoformat()} 至 {end.isoformat()}（最近 15 分钟）。",
        "",
    ]
    if not services:
        return "\n".join(lines + ["服务目录为空，未执行检查。"])
    metric_tool = None
    monitor_error = ""
    try:
        client = await get_mcp_client_with_retry()
        tools = await client.get_tools(server_name="monitor")
        metric_tool = next(tool for tool in tools if tool.name == "query_service_metric")
    except Exception as exc:
        monitor_error = f"{type(exc).__name__}: {exc}"

    async def metric_query(name: str, metric: str) -> dict[str, Any]:
        if metric_tool is None:
            return {"success": False, "error": monitor_error}
        try:
            return _decode_metric(
                await metric_tool.ainvoke(
                    {
                        "service_name": name,
                        "metric": metric,
                        "start_time": start.isoformat(),
                        "end_time": end.isoformat(),
                        "interval": "1m",
                    }
                )
            )
        except Exception as exc:
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    async def log_query(name: str) -> dict[str, Any]:
        try:
            return await query_service_logs.ainvoke({"service_name": name, "lookback_minutes": 15})
        except Exception as exc:
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    # Bound fan-out per service to its five metrics and one log query.
    for name in services:
        results = await asyncio.gather(
            *(metric_query(name, metric) for metric in METRICS), log_query(name)
        )
        lines += [f"## {_cell(name)}", "", "| 检查项 | 实际结果 |", "| --- | --- |"]
        for (metric, label), result in zip(METRICS.items(), results[:-1], strict=True):
            lines.append(f"| {label} | {_metric_summary(metric, result)} |")
        logs = results[-1]
        if logs.get("success") is True:
            log_text = f"返回 {logs['returned_count']} 条匹配记录"
            if logs.get("possibly_truncated"):
                log_text += "；达到返回上限，可能被截断"
        else:
            log_text = "查询失败：" + _cell(logs.get("error", "未知错误"))
        lines += [
            f"| 近期异常日志 | {log_text} |",
            "",
            "指标来源：Prometheus `query_service_metric`。",
        ]
        for (_metric, label), result in zip(METRICS.items(), results[:-1], strict=True):
            if result.get("promql"):
                lines.append(f"- {label} PromQL：`{_cell(result['promql'])}`")
        if logs.get("query"):
            query = logs["query"]
            lines.append(
                f"日志来源：CLS SearchLog；地域 `{query['Region']}`，主题 `{query['TopicId']}`，查询 `{query['Query']}`。"
            )
        lines.append("")
    lines += [
        "证据边界：指标缺失或查询失败时不能判定健康；未配置判定阈值，因此不生成‘整体正常’结论。",
        "日志仅覆盖已接入 CLS 的记录，未验证采集完整性；零条匹配不能据此判断应用没有异常。",
    ]
    return "\n".join(lines)
