#!/usr/bin/env python3
"""Stage 2: Tool Use —— 多工具 dispatch。

s01 用单一 bash 工具，模型只能"打 shell"。s02 把工具集中管理：
  registry.list_schemas() ────► LLM 看到 6 个工具
                                model 选其一发 tool_use(name, input)
  registry.dispatch(name, input)── 执行并返回字符串

agent loop 主体与 s01 完全相同，只是工具数据源换了：
  - tools= 参数从 [BASH_TOOL_DICT] 变成 registry.list_schemas()
  - 执行从 run_bash(cmd) 变成 registry.dispatch(name, args)

新工具：read / write / edit / glob / grep / bash（详见 tools/）。

运行：
    python stages/s02_tools.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import make_client  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402
from tools.bash_tool import TOOL as BASH_TOOL  # noqa: E402
from tools.edit_tool import TOOL as EDIT_TOOL  # noqa: E402
from tools.glob_tool import TOOL as GLOB_TOOL  # noqa: E402
from tools.grep_tool import TOOL as GREP_TOOL  # noqa: E402
from tools.read_tool import TOOL as READ_TOOL  # noqa: E402
from tools.write_tool import TOOL as WRITE_TOOL  # noqa: E402
from anthropic.types import Message, MessageParam, ToolParam, ToolUseBlock  # noqa: E402
from anthropic.types.thinking_config_disabled_param import (  # noqa: E402
    ThinkingConfigDisabledParam,
)

# Windows 控制台 UTF-8
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

client, cfg = make_client()
MODEL = cfg.model_id

# 注册所有工具
REGISTRY = ToolRegistry()
for tool in (READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL):
    REGISTRY.register(tool)

SYSTEM = (
    f"你是一个 coding agent，工作目录在 {os.getcwd()}。\n"
    f"可用工具: {', '.join(REGISTRY.names())}.\n"
    "按需选择工具完成任务。优先 read/edit/write 等结构化工具；"
    "shell 操作用 bash。直接动手，不要冗长解释。"
)

THINKING: ThinkingConfigDisabledParam = {"type": "disabled"}
STREAM = True


def _run_turn(messages: list[dict]) -> Message:
    """与 s01 相同的接口。流式时实时打印 text deltas。"""
    msgs = cast(list[MessageParam], messages)
    tools = cast(list[ToolParam], REGISTRY.list_schemas())
    if STREAM:
        print("\033[35m[stream]\033[0m ", end="", flush=True)
        with client.messages.stream(
            model=MODEL,
            system=SYSTEM,
            messages=msgs,
            tools=tools,
            max_tokens=4096,
            thinking=THINKING,
        ) as stream:
            for chunk in stream.text_stream:
                print(chunk, end="", flush=True)
            print()
            final = stream.get_final_message()
        _print_response_brief(final, brief_only=True)
        return final
    response = client.messages.create(
        model=MODEL,
        system=SYSTEM,
        messages=msgs,
        tools=tools,
        max_tokens=4096,
        thinking=THINKING,
    )
    _print_response_brief(response)
    return response


def agent_loop(messages: list[dict]) -> None:
    """循环结构与 s01 完全一致，只是工具执行换成 registry.dispatch。"""
    while True:
        response = _run_turn(messages)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if not isinstance(block, ToolUseBlock):
                continue
            inp = block.input if isinstance(block.input, dict) else {}
            print(f"\033[33m! {block.name}({_brief_args(inp)})\033[0m")
            output = REGISTRY.dispatch(block.name, inp)
            preview = output if len(output) <= 400 else output[:400] + "...(truncated)"
            print(preview)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
            )
        messages.append({"role": "user", "content": results})


def _brief_args(args: dict) -> str:
    """tool 入参的简短预览（避免长 content 刷屏）。"""
    parts = []
    for k, v in args.items():
        s = repr(v)
        if len(s) > 80:
            s = s[:77] + "...'"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _print_response_brief(resp: Message, brief_only: bool = False) -> None:
    """打印一次模型响应的结构概要。

    brief_only=True（流式模式）：跳过 text 块（已实时打字过避免重复），
    但 tool_use 等结构化块仍显示——它们没出现在流式输出里，
    需要从这里观察到模型选了什么工具、参数是什么。
    """
    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", "?") if usage else "?"
    out_tok = getattr(usage, "output_tokens", "?") if usage else "?"
    print(
        f"\033[90m[← response] stop={resp.stop_reason} "
        f"tokens(in/out)={in_tok}/{out_tok}\033[0m"
    )
    for i, block in enumerate(resp.content or []):
        btype = getattr(block, "type", "?")
        if brief_only and btype == "text":
            continue
        if btype == "text":
            text = (getattr(block, "text", "") or "").strip()
            preview = text[:200] + ("..." if len(text) > 200 else "")
            print(f"\033[90m  [{i}] text: {preview}\033[0m")
        elif btype == "tool_use":
            name = getattr(block, "name", "?")
            inp = getattr(block, "input", {})
            print(f"\033[90m  [{i}] tool_use: {name}({_brief_args(inp if isinstance(inp, dict) else {})})\033[0m")
        else:
            print(f"\033[90m  [{i}] {btype}\033[0m")


def _print_assistant_text(content) -> None:
    if not isinstance(content, list):
        return
    for block in content:
        text = getattr(block, "text", None)
        if text:
            print(text)


def main() -> None:
    history: list[dict] = []
    print(f"PracticeAgent · stage02 (tool use, {len(REGISTRY.names())} tools) — q/exit 退出")
    while True:
        try:
            query = input("\033[36ms02 >> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if query.lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        _print_assistant_text(history[-1]["content"])
        print()


if __name__ == "__main__":
    main()
