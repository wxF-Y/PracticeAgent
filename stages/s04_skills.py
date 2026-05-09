#!/usr/bin/env python3
"""Stage 4: Skills —— 按需加载 .md 知识。

s03 之前，所有"怎么做"的规则都硬塞在 system prompt 里。Skills 让：
  1) system prompt 只看到 (name, description) 列表（不占 context）
  2) 模型判断需要时调 skill(name="...") 工具加载完整 body
  3) body 里可以写详细步骤、反模式、风格要求等

技能来源：./skills/<name>/SKILL.md（frontmatter 定义 name/description）

agent loop 主体与 s03 一致，变化：
  - 启动时扫 ./skills/ 加载 SkillLibrary
  - REGISTRY 额外注册 skill 工具
  - system prompt 追加"可用 skills"列表

运行：
    python stages/s04_skills.py
    python stages/s04_skills.py --list
    python stages/s04_skills.py --resume <id>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import make_client  # noqa: E402
from core.session import Session  # noqa: E402
from core.skill_loader import SkillLibrary  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402
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

REGISTRY = ToolRegistry()
for tool in (READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL):
    REGISTRY.register(tool)
REGISTRY.register(make_skill_tool(SKILLS))


def _build_system_prompt() -> str:
    lines = [
        f"你是一个 coding agent，工作目录在 {os.getcwd()}。",
        f"可用工具: {', '.join(REGISTRY.names())}.",
        "按需选择工具完成任务。优先 read/edit/write 等结构化工具；shell 操作用 bash。",
    ]
    brief = SKILLS.list_brief()
    if brief:
        lines.append("")
        lines.append("可用 skills（遇到相关任务时先 skill(name=...) 加载详细指引再动手）：")
        for name, desc in brief:
            lines.append(f"  - {name}: {desc}")
    lines.append("直接动手，不要冗长解释。")
    return "\n".join(lines)


SYSTEM = _build_system_prompt()
THINKING: ThinkingConfigParam = {"type": "disabled"}
STREAM = True


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


def agent_loop(session: Session) -> None:
    while True:
        response = _run_turn(session)
        session.append_assistant(response.content, usage=response.usage)
        _print_session_status(session)

        if response.stop_reason != "tool_use":
            return

        results = []
        for block in response.content:
            if not isinstance(block, ToolUseBlock):
                continue
            inp = block.input if isinstance(block.input, dict) else {}
            # skill 工具高亮成紫色（与 stream 区分，标记"加载了知识"）
            color = "\033[35m" if block.name == "skill" else "\033[33m"
            print(f"{color}! {block.name}({_brief_args(inp)})\033[0m")
            output = REGISTRY.dispatch(block.name, inp)
            preview = output if len(output) <= 400 else output[:400] + "...(truncated)"
            print(preview)
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": output}
            )
        session.append_user(results)


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
    parser = argparse.ArgumentParser(description="PracticeAgent stage04 (skills)")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--resume", "-r", metavar="ID", help="按 id 或前缀恢复会话")
    g.add_argument("--continue", "-c", dest="cont", action="store_true", help="接最近一个会话")
    g.add_argument("--list", "-l", dest="list_", action="store_true", help="列最近 20 个会话")
    args = parser.parse_args()

    if args.list_:
        return _list_command()

    if args.resume:
        session = Session.resume(args.resume)
        print(f"PracticeAgent · stage04 · resumed {session.session_id} ({len(session.messages)} 条历史)")
    elif args.cont:
        recent = Session.list_recent(limit=1)
        if not recent:
            print("(无历史会话，开新的)")
            session = Session.new(model=MODEL)
        else:
            session = Session.resume(recent[0][0])
            print(f"PracticeAgent · stage04 · continued {session.session_id} ({len(session.messages)} 条历史)")
    else:
        session = Session.new(model=MODEL)
        print(f"PracticeAgent · stage04 · new session {session.session_id}")

    print(f"tools: {', '.join(REGISTRY.names())} · skills: {len(SKILLS)} — q/exit 退出")
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m").strip()
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
