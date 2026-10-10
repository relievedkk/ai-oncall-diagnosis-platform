"""
AIOps Agent 通用工具函数
"""


def format_tools_description(tools: list) -> str:
    """格式化工具列表为描述文本"""
    tool_descriptions = []
    for tool in tools:
        if hasattr(tool, "name") and hasattr(tool, "description"):
            tool_descriptions.append(f"- {tool.name}: {tool.description}")
    return "\n".join(tool_descriptions)
