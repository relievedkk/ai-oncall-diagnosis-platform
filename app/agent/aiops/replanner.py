"""
Replanner 节点：重新规划或生成最终响应
基于 LangGraph 官方教程实现
"""

import json
from textwrap import dedent
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from loguru import logger
from pydantic import BaseModel, Field

from app.agent.mcp_client import get_mcp_client_with_retry
from app.core.llm_factory import llm_factory
from app.tools import DEFAULT_LOCAL_AGENT_TOOLS

from .state import PlanExecuteState
from .utils import format_tools_description


class Response(BaseModel):
    """最终响应的格式"""

    response: str = Field(description="对用户的最终响应")


class Act(BaseModel):
    """重新规划的输出格式"""

    action: str = Field(
        description="""下一步的行动，必须是以下三种之一：
        - 'continue': 当前计划合理，继续执行下一个步骤
        - 'replan': 当前计划需要调整，提供新的步骤列表
        - 'respond': 计划已完成且信息充足，生成最终响应"""
    )
    # action 为 'replan' 时，新的步骤列表（会替换当前剩余计划）
    new_steps: list[str] = Field(
        default_factory=list,
        description="新的步骤列表（如果 action 是 'replan'，这些步骤会替换剩余计划）",
    )


# Replanner 提示词
replanner_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                作为一个重新规划专家，你需要根据已执行的步骤决定下一步行动。

                可用工具列表（用于制定计划时参考）：

                {tools_description}

                注意：你的职责是制定或调整计划，实际的工具调用由 Executor 负责执行。

                你有三个选择（按优先级排序）：

                **1. 'respond' - 信息充足，立即生成最终响应** 【最高优先级】
                   - 使用场景：当前信息已经足够回答用户问题
                   - 决策标准：
                     * 已执行步骤 >= 3 且获取了关键信息
                     * 或者已执行步骤 >= 5（无论结果如何）
                     * 或者当前信息完全满足任务需求
                   - ⚠️ 不要等到"完美"才响应，"足够好"就应该立即 respond

                **2. 'continue' - 当前计划合理，继续执行** 【次优先级】
                   - 使用场景：剩余计划合理且必要
                   - 决策标准：剩余步骤确实能提供关键信息
                   - ⚠️ 如果剩余步骤不是"必需"的，应选择 respond

                **3. 'replan' - 当前计划有严重问题** 【最低优先级，谨慎使用】
                   - 使用场景：原计划明显错误或遗漏关键步骤
                   - ⚠️ **严格限制**：
                     * 新步骤数量必须 <= 当前剩余步骤数
                     * 优先简化计划，不要添加不必要的步骤
                     * 总步骤数已执行 >= 5 次时，禁止 replan，只能 respond

                评估标准：
                - 当前信息是否已经足够解决用户问题？【最关键】
                - 已执行步骤是否成功获取了核心信息？
                - 剩余步骤是否真的"必需"？
                - 已执行步骤数是否过多（>= 5）？如果是，立即 respond

                **决策优先级口诀：**
                "优先结束 > 保持不变 > 调整计划"
                "信息足够就响应，不要追求完美"
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)

# 最终响应生成提示词
response_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                根据原始任务和已执行步骤的结果，生成一个全面的最终响应。

                响应要求：
                - 清晰、结构化
                - 基于实际数据，不要编造
                - 如果某些步骤失败，要诚实说明
                - 使用 Markdown 格式
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)


def _find_empty_alert_scan(evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the latest successful Prometheus alert scan that found no alerts."""
    for item in reversed(evidence):
        if str(item.get("source", "")) != "query_prometheus_alerts":
            continue
        result = item.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, json.JSONDecodeError):
                continue
        if not isinstance(result, dict) or result.get("success") is not True:
            continue
        alerts = result.get("alerts")
        total = result.get("total")
        if alerts == [] and total == 0:
            return result
    return None


def _build_no_alert_report(scan: dict[str, Any]) -> str:
    """Build a deterministic report that does not overstate the available evidence."""
    state_counts = scan.get("state_counts") or {}
    state_summary = ", ".join(
        f"{name}: {count}" for name, count in sorted(state_counts.items())
    ) or "无"
    return dedent(f"""
        # 告警扫描报告

        ## 查询结果

        - **数据来源**：Prometheus 当前告警接口（`query_prometheus_alerts`）
        - **当前活跃告警数**：0
        - **告警状态分布**：{state_summary}

        ## 结论

        本次查询未发现 Prometheus 当前处于活跃状态的告警，因此没有进入针对具体告警的指标、日志和根因排查流程。

        ## 证据边界

        本次结果只说明“当前告警查询返回 0 条”。本次诊断**未检查**各服务的 CPU、内存、延迟、错误率、存活状态或日志内容，因此不能据此断言所有服务正常、所有监控指标正常，或系统不存在潜在故障。

        ## 后续建议

        - 若预期此时应有告警，请检查 Prometheus 抓取目标、告警规则和 Alertmanager 链路。
        - 若需要主动健康检查，请单独发起包含关键指标、服务存活和日志查询的诊断任务。
    """).strip()


async def _generate_response_and_close_plan(
    state: PlanExecuteState, llm: ChatOpenAI, reason: str
) -> dict[str, Any]:
    """Generate a final response and make every unexecuted step explicit."""
    response = await _generate_response(state, llm)
    remaining_steps = list(state.get("plan", []))
    return {
        **response,
        "plan": [],
        "skipped_steps": remaining_steps,
        "termination_reason": reason,
    }


async def replanner(state: PlanExecuteState) -> dict[str, Any]:
    """
    重新规划节点：决定是继续、调整计划还是生成最终响应

    三种决策：
    1. continue - 继续执行当前计划
    2. replan - 调整计划（替换剩余步骤）
    3. respond - 生成最终响应
    """
    logger.info("=== Replanner：重新规划 ===")

    input_text = state.get("input", "")
    plan = state.get("plan", [])
    past_steps = state.get("past_steps", [])
    evidence = state.get("evidence", [])

    logger.info(f"剩余计划步骤: {len(plan)}")
    logger.info(f"已执行步骤: {len(past_steps)}")

    # “没有活跃告警”是一个明确的终止条件，不交给 LLM 自由发挥。
    # 这样既避免无意义地继续查询，也避免把“无告警”夸大为“系统健康”。
    empty_alert_scan = _find_empty_alert_scan(evidence)
    if empty_alert_scan is not None:
        logger.info("Prometheus 未返回活跃告警，生成范围受限的确定性报告")
        return {
            "plan": [],
            "skipped_steps": list(plan),
            "termination_reason": "no_active_alerts",
            "response": _build_no_alert_report(empty_alert_scan),
        }

    # ⚠️ 强制限制：如果已执行步骤过多，直接生成响应
    MAX_STEPS = 8
    if len(past_steps) >= MAX_STEPS:
        logger.warning(
            f"已执行 {len(past_steps)} 个步骤，超过最大限制 {MAX_STEPS}，强制生成最终响应"
        )
        llm = llm_factory.create_chat_model(temperature=0, streaming=False)
        return await _generate_response_and_close_plan(state, llm, "max_steps_reached")

    # 获取可用工具列表
    try:
        # 获取本地工具
        local_tools = list(DEFAULT_LOCAL_AGENT_TOOLS)

        # 获取 MCP 工具
        mcp_client = await get_mcp_client_with_retry()
        mcp_tools = await mcp_client.get_tools()

        # 合并所有工具
        all_tools = local_tools + mcp_tools
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        # 格式化工具描述
        tools_description = format_tools_description(all_tools)
    except Exception as e:
        logger.warning(f"获取工具列表失败: {e}")
        tools_description = "无法获取工具列表"

    # 创建 LLM
    llm = llm_factory.create_chat_model(temperature=0, streaming=False)

    # 格式化已执行的步骤
    steps_summary = "\n".join(
        [f"步骤: {step}\n结果: {result[:300]}..." for step, result in past_steps]
    )

    # 如果还有剩余计划，进行决策
    if plan:
        logger.info("还有剩余计划，评估下一步行动")

        replanner_chain = replanner_prompt | llm.with_structured_output(
            Act, method="function_calling"
        )

        try:
            messages = [
                ("user", f"原始任务: {input_text}"),
                ("user", f"已执行的步骤:\n{steps_summary}"),
                ("user", f"剩余计划: {', '.join(plan)}"),
                (
                    "user",
                    f"⚠️ 重要提示：已执行 {len(past_steps)} 个步骤，请优先考虑是否信息已足够生成响应（respond）",
                ),
            ]

            act = await replanner_chain.ainvoke(
                {"messages": messages, "tools_description": tools_description}
            )

            # 处理返回结果
            if isinstance(act, Act):
                action = act.action
                new_steps = act.new_steps
            else:
                # 如果返回的是字典
                action = act.get("action", "continue")  # type: ignore
                new_steps = act.get("new_steps", [])  # type: ignore

            logger.info(f"Replanner 决策: {action}")

            if action == "respond":
                logger.info("决定生成最终响应")
                return await _generate_response_and_close_plan(
                    state, llm, "sufficient_evidence"
                )

            elif action == "replan":
                # ⚠️ 强制限制：新步骤数不能超过当前剩余步骤数
                if len(new_steps) > len(plan):
                    logger.warning(
                        f"新步骤数 {len(new_steps)} > 剩余步骤数 {len(plan)}，"
                        f"强制截断为 {len(plan)} 个步骤"
                    )
                    new_steps = new_steps[: len(plan)]

                # ⚠️ 二次检查：如果已执行步骤 >= 5，禁止 replan
                if len(past_steps) >= 5:
                    logger.warning(f"已执行 {len(past_steps)} 个步骤，禁止重新规划，强制生成响应")
                    return await _generate_response_and_close_plan(
                        state, llm, "max_steps_reached"
                    )

                logger.info(f"决定调整计划，新步骤数量: {len(new_steps)}")
                if new_steps:
                    # 替换剩余计划
                    return {"plan": new_steps}
                else:
                    logger.warning("replan 但未提供新步骤，继续执行原计划")
                    return {}

            else:  # action == "continue"
                logger.info("决定继续执行当前计划")
                return {}  # 不修改状态，继续执行

        except Exception as e:
            logger.error(f"重新规划失败: {e}, 继续执行剩余计划")
            return {}

    else:
        # 没有剩余计划，生成最终响应
        logger.info("计划已执行完毕，生成最终响应")
        return await _generate_response(state, llm)


async def _generate_response(state: PlanExecuteState, llm: ChatOpenAI) -> dict[str, Any]:
    """生成最终响应"""
    logger.info("生成最终响应...")

    input_text = state.get("input", "")
    past_steps = state.get("past_steps", [])

    # 格式化执行历史
    execution_history = "\n\n".join(
        [f"### 步骤: {step}\n**结果:**\n{result}" for step, result in past_steps]
    )

    response_gen = response_prompt | llm.with_structured_output(
        Response, method="function_calling"
    )

    try:
        messages = [
            ("user", f"原始任务: {input_text}"),
            ("user", f"执行历史:\n{execution_history}"),
            ("user", "请基于以上信息生成全面的最终响应"),
        ]

        response_obj = await response_gen.ainvoke({"messages": messages})

        # 处理返回结果
        if isinstance(response_obj, Response):
            final_response = response_obj.response
        else:
            # 如果返回的是字典
            final_response = response_obj.get("response", "")  # type: ignore

        logger.info(f"最终响应生成完成，长度: {len(final_response)}")

        return {"response": final_response}

    except Exception as e:
        logger.error(f"生成响应失败: {e}")
        # 生成简单的后备响应
        fallback_response = f"""# 任务执行结果

## 原始任务
{input_text}

## 执行的步骤
{_format_simple_steps(past_steps)}

## 说明
由于系统异常，无法生成完整响应。以上是已收集的信息。
"""
        return {"response": fallback_response}


def _format_simple_steps(past_steps: list) -> str:
    """格式化步骤列表（简单版）"""
    if not past_steps:
        return "无"

    formatted = []
    for i, (step, result) in enumerate(past_steps, 1):
        result_preview = result[:200] + "..." if len(result) > 200 else result
        formatted.append(f"{i}. **{step}**\n   {result_preview}\n")

    return "\n".join(formatted)
