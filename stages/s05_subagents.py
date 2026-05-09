#!/usr/bin/env python3
"""Stage 5: Subagent —— 隔离上下文 + 同轮并行。

主 agent 的 context 宝贵。当要做的事情是：
  - 独立、封闭、不需要全部历史
  - 可能要跑很多轮工具调用（子任务复杂）
  - 或者多个类似任务可并行
就派一个或多个 *子 agent*，让它们从空白 messages 开始跑完整个 tool-use loop，
只把一行文本报告交回来。主 context 每次只多一条 tool_result，不被刷屏。

关键实现点：
  - 子 agent 工具集 = 主工具集减去 Agent 本身（天然限深度）
  - 同一轮响应里多个 Agent 工具调用 → ThreadPoolExecutor 并行
  - 普通工具仍串行（file I/O 本地快，串行更可读）

运行：
    python stages/s05_subagents.py
    python stages/s05_subagents.py --continue
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import make_client  # noqa: E402
from core.session import Session  # noqa: E402
from core.skill_loader import SkillLibrary  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402
from tools.agent_tool import make_agent_tool  # noqa: E402
from tools.bash_tool import TOOL as BASH_TOOL  # noqa: E402
from tools.edit_tool import TOOL as EDIT_TOOL  # noqa: E402
from tools.glob_tool import TOOL as GLOB_TOOL  # noqa: E402
from tools.grep_tool import TOOL as GREP_TOOL  # noqa: E402
from tools.read_tool import TOOL as READ_TOOL  # noqa: E402
from tools.skill_tool import make_skill_tool  # noqa: E402
from tools.write_tool import TOOL as WRITE_TOOL  # noqa: E402
from anthropic.types import Message, MessageParam, ThinkingConfigParam, ToolParam, ToolUseBlock  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

client, cfg = make_client()
MODEL = cfg.model_id

SKILLS = SkillLibrary(Path(__file__).resolve().parent.parent / "skills")

# 子 agent 用的工具集：read/write/edit/glob/grep/bash/skill，但不含 agent
SUB_REGISTRY = ToolRegistry()
for tool in (READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL):
    SUB_REGISTRY.register(tool)
SUB_REGISTRY.register(make_skill_tool(SKILLS))

# 主 agent 的工具集：在子集基础上多一个 agent 工具
REGISTRY = ToolRegistry()
for tool in (READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL):
    REGISTRY.register(tool)
REGISTRY.register(make_skill_tool(SKILLS))
REGISTRY.register(make_agent_tool(client=client, model=MODEL, sub_registry=SUB_REGISTRY))


def _build_system_prompt() -> str:
    lines = [
        f"你是一个 coding agent，工作目录在 {os.getcwd()}。",
        f"可用工具: {', '.join(REGISTRY.names())}.",
        "适时派子 agent：封闭任务、可并行探索、或想保持主上下文简洁时，调用 agent(prompt=...)。",
        "简单改动/读写用直接工具即可；不要为小事派子 agent。",
    ]
    brief = SKILLS.list_brief()
    if brief:
        lines.append("")
        lines.append("可用 skills（相关任务先 skill(name=...) 加载详细指引）：")
        for name, desc in brief:
            lines.append(f"  - {name}: {desc}")
    lines.append("直接动手，不要冗长解释。")
    return "\n".join(lines)


SYSTEM = _build_system_prompt()
THINKING: ThinkingConfigParam = {"type": "disabled"}
STREAM = True
MAX_PARALLEL_AGENTS = 3


def _run_turn(session: Session) -> Message:
    msgs = cast(list[MessageParam], session.messages)
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


def _dispatch_block(block: ToolUseBlock) -> tuple[str, str]:
    """返回 (tool_use_id, output)，供主循环按顺序回填。"""
    inp = block.input if isinstance(block.input, dict) else {}
    out = REGISTRY.dispatch(block.name, inp)
    return block.id, out


def agent_loop(session: Session) -> None:
    while True:
        response = _run_turn(session)
        session.append_assistant(response.content, usage=response.usage)
        _print_session_status(session)

        if response.stop_reason != "tool_use":
            return

        tool_blocks = [b for b in response.content if isinstance(b, ToolUseBlock)]
        agent_blocks = [b for b in tool_blocks if b.name == "agent"]
        other_blocks = [b for b in tool_blocks if b.name != "agent"]

        results_by_id: dict[str, str] = {}

        # 普通工具：串行
        for block in other_blocks:
            inp = block.input if isinstance(block.input, dict) else {}
            color = "\033[35m" if block.name == "skill" else "\033[33m"
            print(f"{color}! {block.name}({_brief_args(inp)})\033[0m")
            output = REGISTRY.dispatch(block.name, inp)
            preview = output if len(output) <= 400 else output[:400] + "...(truncated)"
            print(preview)
            results_by_id[block.id] = output

        # agent 工具：并行（>=2 个时才开线程池）
        if agent_blocks:
            for b in agent_blocks:
                inp = b.input if isinstance(b.input, dict) else {}
                print(f"\033[36m↳ spawn agent {b.id[-6:]}({_brief_args(inp)})\033[0m")
            if len(agent_blocks) == 1:
                tid, out = _dispatch_block(agent_blocks[0])
                results_by_id[tid] = out
                _print_subagent_done(agent_blocks[0], out)
            else:
                with ThreadPoolExecutor(max_workers=MAX_PARALLEL_AGENTS) as pool:
                    futures = {pool.submit(_dispatch_block, b): b for b in agent_blocks}
                    for fut in futures:
                        b = futures[fut]
                        tid, out = fut.result()
                        results_by_id[tid] = out
                        _print_subagent_done(b, out)

        # 按原顺序回填
        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": results_by_id[b.id]}
            for b in tool_blocks
        ]
        session.append_user(results)


def _print_subagent_done(block: ToolUseBlock, output: str) -> None:
    preview = output.replace("\n", " ⏎ ")
    if len(preview) > 300:
        preview = preview[:300] + "...(truncated)"
    print(f"\033[36m↳ agent {block.id[-6:]} done: {preview}\033[0m")


def _print_session_status(session: Session) -> None:
    print(
        f"\033[90m[session {session.session_id[:14]} · turn {session.turn} · "
        f"tokens total in={session.total_input} out={session.total_output}]\033[0m"
    )


def _brief_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = repr(v)
        if len(s) > 80:
            s = s[:77] + "...'"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _print_response_brief(resp: Message, brief_only: bool = False) -> None:
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
        elif btype == "thinking":
            text = (getattr(block, "thinking", "") or "").strip()
            preview = text[:200] + ("..." if len(text) > 200 else "")
            print(f"\033[90m  [{i}] thinking: {preview}\033[0m")
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
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            print(text)


def _list_command() -> int:
    items = Session.list_recent()
    if not items:
        print("(无历史会话)")
        return 0
    print(f"{'session_id':<28}  first user message")
    print("-" * 80)
    for sid, preview in items:
        print(f"{sid:<28}  {preview}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PracticeAgent stage05 (subagents)")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--resume", "-r", metavar="ID", help="按 id 或前缀恢复会话")
    g.add_argument("--continue", "-c", dest="cont", action="store_true", help="接最近一个会话")
    g.add_argument("--list", "-l", dest="list_", action="store_true", help="列最近 20 个会话")
    args = parser.parse_args()

    if args.list_:
        return _list_command()

    if args.resume:
        session = Session.resume(args.resume)
        print(f"PracticeAgent · stage05 · resumed {session.session_id} ({len(session.messages)} 条历史)")
    elif args.cont:
        recent = Session.list_recent(limit=1)
        if not recent:
            print("(无历史会话，开新的)")
            session = Session.new(model=MODEL)
        else:
            session = Session.resume(recent[0][0])
            print(f"PracticeAgent · stage05 · continued {session.session_id} ({len(session.messages)} 条历史)")
    else:
        session = Session.new(model=MODEL)
        print(f"PracticeAgent · stage05 · new session {session.session_id}")

    print(
        f"tools: {', '.join(REGISTRY.names())} · skills: {len(SKILLS)} · "
        f"sub_tools: {', '.join(SUB_REGISTRY.names())} — q/exit 退出"
    )
    while True:
        try:
            query = input("\033[36ms05 >> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if query.lower() in ("q", "exit", ""):
            break
        session.append_user(query)
        agent_loop(session)
        if not STREAM and session.messages:
            _print_assistant_text(session.messages[-1].get("content"))
        print()

    print(f"会话已保存：{session.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
