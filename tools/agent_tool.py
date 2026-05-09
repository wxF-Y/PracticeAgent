"""Agent 工具：主 agent 可以派子 agent 去做独立子任务。

工厂 make_agent_tool 闭包了 client / model / sub_registry，这样：
  - sub_registry 由调用者决定：通常是主 registry 去掉 Agent 工具本身，
    防止子 agent 继续派子子 agent（无限递归）。
  - 同一个网关 client 复用连接池。

子 agent 的最终 text 报告作为工具结果回填给父 agent。父 agent 的 context
只多一条 tool_result，而不是子 agent 的几十轮历史。
"""

from __future__ import annotations

from typing import Any

from core.subagent import DEFAULT_SUBAGENT_SYSTEM, run_subagent
from core.tool_registry import Tool, ToolRegistry


def make_agent_tool(
    *,
    client: Any,
    model: str,
    sub_registry: ToolRegistry,
    system_prompt: str = DEFAULT_SUBAGENT_SYSTEM,
    max_turns: int = 20,
) -> Tool:
    def _run(args: dict) -> str:
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return "Error: 缺少 prompt 参数"
        return run_subagent(
            prompt=prompt,
            client=client,
            model=model,
            tool_registry=sub_registry,
            system_prompt=system_prompt,
            max_turns=max_turns,
        )

    sub_tools = ", ".join(sub_registry.names()) or "(none)"
    return Tool(
        name="agent",
        description=(
            "派一个独立的子 agent 完成封闭任务（如并行探索、批量分析）。"
            "子 agent 从空历史开始，只返回简短文本结论。"
            f"子 agent 可用工具：{sub_tools}。"
            "适合：多个独立查询可并行；主上下文不想被中间工具调用刷屏。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "给子 agent 的任务描述，具体、封闭、能单独完成",
                },
            },
            "required": ["prompt"],
        },
        handler=_run,
    )
