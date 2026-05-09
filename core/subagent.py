"""子 agent 循环：跑完一轮完整的 tool-use 直到 stop，只返回最终文本。

为什么要有子 agent：
  - 主 agent 的 context 有限。做"收集信息+总结"这类独立任务时，派子 agent 跑完，
    只把结论回填给主 agent，主 context 只多一条 tool_result。
  - 多个独立任务可以并行（见 stages/s05_subagents.py）。

设计约束：
  - 子 agent 用非流式（不需要实时打字，只要最终结论）
  - 子 agent 的 tool_registry 不含 Agent 工具本身（天然限深度）
  - max_turns 兜底，避免死循环烧 token
"""

from __future__ import annotations

from typing import Any, cast

from anthropic.types import (
    MessageParam,
    ThinkingConfigParam,
    ToolParam,
    ToolUseBlock,
)

from core.tool_registry import ToolRegistry


DEFAULT_SUBAGENT_SYSTEM = (
    "你是一个专注的子 agent。用工具收集信息或完成任务，"
    "最后输出一份简短报告（中文，不超过 6 行）。"
    "报告只写结论与关键事实，不要复述工具原始输出，也不要寒暄。"
)


def run_subagent(
    *,
    prompt: str,
    client: Any,
    model: str,
    tool_registry: ToolRegistry,
    system_prompt: str = DEFAULT_SUBAGENT_SYSTEM,
    max_turns: int = 20,
    max_tokens: int = 4096,
    thinking: ThinkingConfigParam | None = None,
) -> str:
    """跑一个独立的 agent 循环，返回最终 assistant 文本拼接。"""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    think: ThinkingConfigParam = thinking or {"type": "disabled"}

    for _ in range(max_turns):
        response = client.messages.create(
            model=model,
            system=system_prompt,
            messages=cast(list[MessageParam], messages),
            tools=cast(list[ToolParam], tool_registry.list_schemas()),
            max_tokens=max_tokens,
            thinking=think,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return _extract_text(response.content)

        results = []
        for block in response.content:
            if not isinstance(block, ToolUseBlock):
                continue
            inp = block.input if isinstance(block.input, dict) else {}
            output = tool_registry.dispatch(block.name, inp)
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": output}
            )
        messages.append({"role": "user", "content": results})

    return f"(子 agent 超过 max_turns={max_turns} 未完成，主动中断)"


def _extract_text(content: Any) -> str:
    """从 assistant content 里提取 text 块拼接；忽略 tool_use / thinking 等。"""
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p).strip() or "(子 agent 未产出 text)"
