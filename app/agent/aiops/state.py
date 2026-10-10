"""
通用 Plan-Execute-Replan 状态定义
基于 LangGraph 官方教程实现
"""

import operator
from typing import Annotated, Any, NotRequired, TypedDict


class PlanExecuteState(TypedDict):
    """Plan-Execute-Replan 状态"""

    # 用户输入（任务描述）
    input: str

    # 执行计划（步骤列表）
    plan: list[str]

    # 已执行的步骤历史
    # 使用 operator.add 实现追加式更新（而非覆盖）
    past_steps: Annotated[list[tuple], operator.add]

    # 原始工具调用证据，独立于 LLM 对结果的二次总结。
    evidence: Annotated[list[dict[str, Any]], operator.add]

    # 最终响应/报告
    response: str

    # 工作流提前结束时，没有执行且被明确跳过的剩余步骤。
    skipped_steps: NotRequired[list[str]]

    # 工作流结束原因，用于持久化和前端解释诊断边界。
    termination_reason: NotRequired[str]
