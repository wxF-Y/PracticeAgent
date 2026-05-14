#!/usr/bin/env python3
"""Stage 7: Permissions & Hooks —— 给工具调用加治理层。

s06 解决"上下文不爆"，s07 解决"敏感动作要确认"。改动只发生在 dispatch
路径上：每次工具调用都过一遍 PermissionChecker + Pre/Post Hooks，主循环
其它部分完全不动。

关键能力（对应 README V5）：
  - 默认模式：write/edit 在 cwd 外或敏感路径 → ask；bash 黑名单 deny、软词 ask
  - --mode plan：只读侦察，写类与 bash 全部 deny
  - --mode accept-edits：信任 agent 在 cwd 内改文件，bash 仍按默认走
  - --mode bypass：演示用，全放行
  - 用户在 ask 中回 y 后，同一会话同一 (tool,key) 不再问
  - 内置 audit_log_hook：每次调用 JSONL 落盘 ~/.practiceagent/audit.log

运行：
    python stages/s07_permissions.py
    python stages/s07_permissions.py --mode plan
    python stages/s07_permissions.py --mode accept-edits
    REPL 中：/mode default|accept-edits|plan|bypass 切换；/compact 同 s06
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
from core.compact import (  # noqa: E402
    DEFAULT_KEEP_TAIL_TURNS,
    DEFAULT_THRESHOLD_TOKENS,
    compact_messages,
)
from core.governance import Governance  # noqa: E402
from core.hooks import (  # noqa: E402
    HookEvent,
    HookExecutor,
    block_write_outside_cwd_hook,
    make_audit_log_hook,
    print_invocation_hook,
)
from core.permissions import PermissionChecker, PermissionMode, cli_prompt  # noqa: E402
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

SUB_REGISTRY = ToolRegistry()
for tool in (READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL):
    SUB_REGISTRY.register(tool)
SUB_REGISTRY.register(make_skill_tool(SKILLS))

REGISTRY = ToolRegistry()
for tool in (READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL):
    REGISTRY.register(tool)
REGISTRY.register(make_skill_tool(SKILLS))
REGISTRY.register(make_agent_tool(client=client, model=MODEL, sub_registry=SUB_REGISTRY))


# ---- governance 拼装 ----

AUDIT_LOG_PATH = Path(os.path.expanduser("~")) / ".practiceagent" / "audit.log"
PERMISSIONS = PermissionChecker(mode=PermissionMode.DEFAULT, cwd=Path.cwd())
HOOKS = HookExecutor()
HOOKS.register(HookEvent.PRE_TOOL_USE, print_invocation_hook)
HOOKS.register(HookEvent.PRE_TOOL_USE, block_write_outside_cwd_hook(Path.cwd()))
HOOKS.register(HookEvent.PRE_TOOL_USE, make_audit_log_hook(AUDIT_LOG_PATH))
HOOKS.register(HookEvent.POST_TOOL_USE, make_audit_log_hook(AUDIT_LOG_PATH))

GOV = Governance(
    registry=REGISTRY,
    permissions=PERMISSIONS,
    hooks=HOOKS,
    prompt=cli_prompt,
)


def _build_system_prompt() -> str:
    lines = [
        f"你是一个 coding agent，工作目录在 {os.getcwd()}。",
        f"可用工具: {', '.join(REGISTRY.names())}.",
        "敏感动作会被权限层拦截或询问用户；被拒绝时请改用更安全的方式或停止。",
        "适时派子 agent；简单事直接做。",
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

COMPACT_THRESHOLD = DEFAULT_THRESHOLD_TOKENS
KEEP_TAIL_TURNS = DEFAULT_KEEP_TAIL_TURNS


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
    inp = block.input if isinstance(block.input, dict) else {}
    return block.id, GOV.dispatch(block.name, inp)


def _maybe_compact(session: Session, last_input_tokens: int) -> None:
    if last_input_tokens < COMPACT_THRESHOLD:
        return
    print(
        f"\033[31m[compact] input_tokens={last_input_tokens} 超过阈值 "
        f"{COMPACT_THRESHOLD}，触发自动压缩\033[0m"
    )
    _do_compact(session)


def _do_compact(session: Session) -> None:
    before = len(session.messages)
    result = compact_messages(
        messages=session.messages,
        client=client,
        model=MODEL,
        keep_tail_turns=KEEP_TAIL_TURNS,
    )
    if result is None:
        print("\033[31m[compact] 当前不适合压缩（消息太短或末尾有未闭合的 tool_use）\033[0m")
        return
    new_messages, summary = result
    session.messages = new_messages
    print(
        f"\033[31m[compact] {before} 条 → {len(new_messages)} 条；"
        f"摘要前 200 字：{summary[:200]}{'...' if len(summary) > 200 else ''}\033[0m"
    )


def agent_loop(session: Session) -> None:
    while True:
        response = _run_turn(session)
        session.append_assistant(response.content, usage=response.usage)
        _print_session_status(session)

        if response.stop_reason != "tool_use":
            usage = getattr(response, "usage", None)
            if usage is not None:
                _maybe_compact(session, int(getattr(usage, "input_tokens", 0) or 0))
            return

        tool_blocks = [b for b in response.content if isinstance(b, ToolUseBlock)]
        agent_blocks = [b for b in tool_blocks if b.name == "agent"]
        other_blocks = [b for b in tool_blocks if b.name != "agent"]
        results_by_id: dict[str, str] = {}

        for block in other_blocks:
            inp = block.input if isinstance(block.input, dict) else {}
            color = "\033[35m" if block.name == "skill" else "\033[33m"
            print(f"{color}! {block.name}({_brief_args(inp)})\033[0m")
            output = GOV.dispatch(block.name, inp)
            preview = output if len(output) <= 400 else output[:400] + "...(truncated)"
            print(preview)
            results_by_id[block.id] = output

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

        results = [
            {"type": "tool_result", "tool_use_id": b.id, "content": results_by_id[b.id]}
            for b in tool_blocks
        ]
        session.append_user(results)

        usage = getattr(response, "usage", None)
        if usage is not None:
            _maybe_compact(session, int(getattr(usage, "input_tokens", 0) or 0))


def _print_subagent_done(block: ToolUseBlock, output: str) -> None:
    preview = output.replace("\n", " ⏎ ")
    if len(preview) > 300:
        preview = preview[:300] + "...(truncated)"
    print(f"\033[36m↳ agent {block.id[-6:]} done: {preview}\033[0m")


def _print_session_status(session: Session) -> None:
    print(
        f"\033[90m[session {session.session_id[:14]} · turn {session.turn} · "
        f"msgs={len(session.messages)} · tokens total in={session.total_input} out={session.total_output} · "
        f"mode={PERMISSIONS.mode.value}]\033[0m"
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


def _set_mode(raw: str) -> str:
    try:
        new_mode = PermissionMode(raw.strip().lower())
    except ValueError:
        valid = ", ".join(m.value for m in PermissionMode)
        return f"未知模式：{raw}（可选：{valid}）"
    PERMISSIONS.mode = new_mode
    PERMISSIONS.always_allow.clear()
    return f"已切换到 {new_mode.value} 模式（已清空 always-allow 缓存）"


def main() -> int:
    global COMPACT_THRESHOLD, KEEP_TAIL_TURNS
    parser = argparse.ArgumentParser(description="PracticeAgent stage07 (permissions & hooks)")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--resume", "-r", metavar="ID", help="按 id 或前缀恢复会话")
    g.add_argument("--continue", "-c", dest="cont", action="store_true", help="接最近一个会话")
    g.add_argument("--list", "-l", dest="list_", action="store_true", help="列最近 20 个会话")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD_TOKENS,
                        help=f"input_tokens 超过此值触发压缩，默认 {DEFAULT_THRESHOLD_TOKENS}")
    parser.add_argument("--keep-tail", type=int, default=DEFAULT_KEEP_TAIL_TURNS,
                        help=f"压缩时保留尾部多少个 user 边界，默认 {DEFAULT_KEEP_TAIL_TURNS}")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=PermissionMode.DEFAULT.value,
        help="权限模式：default / accept-edits / plan / bypass",
    )
    args = parser.parse_args()

    COMPACT_THRESHOLD = args.threshold
    KEEP_TAIL_TURNS = args.keep_tail
    PERMISSIONS.mode = PermissionMode(args.mode)

    if args.list_:
        return _list_command()

    if args.resume:
        session = Session.resume(args.resume)
        print(f"PracticeAgent · stage07 · resumed {session.session_id} ({len(session.messages)} 条历史)")
    elif args.cont:
        recent = Session.list_recent(limit=1)
        if not recent:
            print("(无历史会话，开新的)")
            session = Session.new(model=MODEL)
        else:
            session = Session.resume(recent[0][0])
            print(f"PracticeAgent · stage07 · continued {session.session_id} ({len(session.messages)} 条历史)")
    else:
        session = Session.new(model=MODEL)
        print(f"PracticeAgent · stage07 · new session {session.session_id}")

    print(
        f"tools: {', '.join(REGISTRY.names())} · skills: {len(SKILLS)} · "
        f"mode={PERMISSIONS.mode.value} · audit={AUDIT_LOG_PATH} · "
        f"compact threshold={COMPACT_THRESHOLD} keep_tail={KEEP_TAIL_TURNS} — "
        "/compact 手动压缩 · /mode <name> 切模式 · q/exit 退出"
    )
    while True:
        try:
            query = input("\033[36ms07 >> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if query.lower() in ("q", "exit", ""):
            break
        if query == "/compact":
            _do_compact(session)
            continue
        if query.startswith("/mode"):
            parts = query.split(maxsplit=1)
            if len(parts) != 2:
                print(f"当前模式：{PERMISSIONS.mode.value}（用法：/mode default|accept-edits|plan|bypass）")
            else:
                print(_set_mode(parts[1]))
            continue
        session.append_user(query)
        agent_loop(session)
        if not STREAM and session.messages:
            _print_assistant_text(session.messages[-1].get("content"))
        print()

    print(f"会话已保存：{session.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
