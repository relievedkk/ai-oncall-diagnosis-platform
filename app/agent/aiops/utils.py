"""
AIOps Agent 通用工具函数
"""

import json


def format_tools_description(tools: list) -> str:
    """格式化工具列表为描述文本"""
    tool_descriptions = []
    for tool in tools:
        if hasattr(tool, "name") and hasattr(tool, "description"):
            tool_descriptions.append(f"- {tool.name}: {tool.description}")
            schema = getattr(tool, "args_schema", None)
            if schema is not None:
                if hasattr(schema, "model_json_schema"):
                    schema = schema.model_json_schema()
                if isinstance(schema, dict):
                    tool_descriptions.append(
                        "  parameters: " + json.dumps(schema, ensure_ascii=False)
                    )
    return "\n".join(tool_descriptions)
