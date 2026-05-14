#!/usr/bin/env python3
"""Stage 9: Tasks & Cron —— 后台任务调度，解锁 V3（定时推 git log）。

在 s08 的基础上新增三件事：
  1. core/tasks.py  — 任务数据模型、持久化（JSONL）、cron 解析
  2. tools/task_tool.py — agent 可调度任务的工具
  3. 后台调度线程（每 30 秒 tick）— 触发到期任务，各跑独立子 session

运行：
    python stages/s09_tasks.py
    python stages/s09_tasks.py --mode accept-edits
    REPL 中：/tasks · /tasks cancel <name> · /tasks run <name>
             /memory [title] · /compact · /mode <name> · q/exit 退出
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
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
from core.memory import build_memory_section, list_memories, read_memory  # noqa: E402
from core.permissions import PermissionChecker, PermissionMode, cli_prompt  # noqa: E402
from core.session import Session  # noqa: E402
from core.skill_loader import SkillLibrary  # noqa: E402
from core.tasks import (  # noqa: E402
    get_due_tasks,
    get_task_by_name,
    list_tasks,
    mark_task_running,
    schedule_task,
    update_task_after_run,
)
from core.tool_registry import ToolRegistry  # noqa: E402
from tools.agent_tool import make_agent_tool  # noqa: E402
from tools.bash_tool import TOOL as BASH_TOOL  # noqa: E402
from tools.edit_tool import TOOL as EDIT_TOOL  # noqa: E402
from tools.glob_tool import TOOL as GLOB_TOOL  # noqa: E402
from tools.grep_tool import TOOL as GREP_TOOL  # noqa: E402
from tools.memory_tool import TOOL as MEMORY_TOOL  # noqa: E402
from tools.read_tool import TOOL as READ_TOOL  # noqa: E402
from tools.skill_tool import make_skill_tool  # noqa: E402
from tools.task_tool import TOOL as TASK_TOOL, set_run_task_callback  # noqa: E402
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
CWD = Path.cwd()

SKILLS = SkillLibrary(Path(__file__).resolve().parent.parent / "skills")

SUB_REGISTRY = ToolRegistry()
for tool in (READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL):
    SUB_REGISTRY.register(tool)
SUB_REGISTRY.register(make_skill_tool(SKILLS))

REGISTRY = ToolRegistry()
for tool in (READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL, MEMORY_TOOL, TASK_TOOL):
    REGISTRY.register(tool)
REGISTRY.register(make_skill_tool(SKILLS))
REGISTRY.register(make_agent_tool(client=client, model=MODEL, sub_registry=SUB_REGISTRY))

# ── governance ────────────────────────────────────────────────────────────────

AUDIT_LOG_PATH = Path.home() / ".practiceagent" / "audit.log"
PERMISSIONS = PermissionChecker(mode=PermissionMode.DEFAULT, cwd=CWD)
HOOKS = HookExecutor()
HOOKS.register(HookEvent.PRE_TOOL_USE, print_invocation_hook)
HOOKS.register(HookEvent.PRE_TOOL_USE, block_write_outside_cwd_hook(CWD))
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

    mem_section = build_memory_section(CWD)
    if mem_section:
        lines.append("")
        lines.append(mem_section)

    task_count = len([t for t in list_tasks(CWD) if t.status not in ("cancelled", "done", "failed")])
    if task_count:
        lines.append("")
        lines.append(f"当前有 {task_count} 个活跃后台任务（用 task(action=list) 查看）。")

    lines.append("")
    lines.append(
        "当用户提到偏好或规范，主动用 memory(action=write) 记住。"
        "需要定时任务时用 task(action=schedule)。"
    )
    lines.append("直接动手，不要冗长解释。")
    return "\n".join(lines)


SYSTEM = _build_system_prompt()
THINKING: ThinkingConfigParam = {"type": "disabled"}
STREAM = True
MAX_PARALLEL_AGENTS = 3

COMPACT_THRESHOLD = DEFAULT_THRESHOLD_TOKENS
KEEP_TAIL_TURNS = DEFAULT_KEEP_TAIL_TURNS

# ── 调度器使用的独立 Governance（bypass 模式，不弹权限弹窗）─────────────────
# 安全假设：任务 prompt 来自本地 JSONL 文件，只有当前用户可写。
# 若任务文件被恶意篡改，BYPASS 模式无法提供防护。
# 使用与子 agent 相同的 SUB_REGISTRY（不含 task/memory 工具），限制调度任务的权限范围。

_SCHED_PERMISSIONS = PermissionChecker(mode=PermissionMode.BYPASS, cwd=CWD)
_SCHED_HOOKS = HookExecutor()
_SCHED_HOOKS.register(HookEvent.PRE_TOOL_USE, make_audit_log_hook(AUDIT_LOG_PATH))
_SCHED_HOOKS.register(HookEvent.POST_TOOL_USE, make_audit_log_hook(AUDIT_LOG_PATH))

_SCHED_GOV = Governance(
    registry=SUB_REGISTRY,      # 限制调度任务只能使用文件/bash 工具
    permissions=_SCHED_PERMISSIONS,
    hooks=_SCHED_HOOKS,
    prompt=cli_prompt,
)


# ── agent loop ────────────────────────────────────────────────────────────────

def _run_turn(session: Session, governance: Governance = GOV) -> Message:
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


def _dispatch_block(block: ToolUseBlock, governance: Governance = GOV) -> tuple[str, str]:
    inp = block.input if isinstance(block.input, dict) else {}
    return block.id, governance.dispatch(block.name, inp)


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


def agent_loop(session: Session, governance: Governance = GOV) -> None:
    while True:
        response = _run_turn(session, governance)
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
            color = "\033[35m" if block.name in ("skill", "memory", "task") else "\033[33m"
            print(f"{color}! {block.name}({_brief_args(inp)})\033[0m")
            output = governance.dispatch(block.name, inp)
            preview = output if len(output) <= 400 else output[:400] + "...(truncated)"
            print(preview)
            results_by_id[block.id] = output

        if agent_blocks:
            for b in agent_blocks:
                inp = b.input if isinstance(b.input, dict) else {}
                print(f"\033[36m↳ spawn agent {b.id[-6:]}({_brief_args(inp)})\033[0m")
            if len(agent_blocks) == 1:
                tid, out = _dispatch_block(agent_blocks[0], governance)
                results_by_id[tid] = out
                _print_subagent_done(agent_blocks[0], out)
            else:
                with ThreadPoolExecutor(max_workers=MAX_PARALLEL_AGENTS) as pool:
                    futures = {pool.submit(_dispatch_block, b, governance): b for b in agent_blocks}
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


# ── 后台任务执行 ──────────────────────────────────────────────────────────────

# 仅用于"领取任务"的原子操作，执行本身在锁外进行，避免长时间持锁。
_claim_lock = threading.Lock()


def _execute_task(task_id: str) -> str:
    """在独立 session 中执行一个任务；返回结果摘要。"""
    record = next((t for t in list_tasks(CWD) if t.task_id == task_id), None)
    if record is None:
        return f"Error: task_id={task_id} 不存在"

    print(f"\033[33m[scheduler] 触发任务：{record.name} ({record.task_id[:12]})\033[0m")
    mark_task_running(CWD, task_id)
    session = Session.new(model=MODEL)
    session.append_user(record.prompt)
    try:
        agent_loop(session, governance=_SCHED_GOV)
        update_task_after_run(CWD, task_id, success=True)
        summary = f"任务 {record.name!r} 完成（session={session.session_id[:12]}）"
        print(f"\033[33m[scheduler] {summary}\033[0m")
        return summary
    except Exception as e:
        err = str(e)[:200]
        update_task_after_run(CWD, task_id, success=False, error=err)
        print(f"\033[31m[scheduler] 任务 {record.name!r} 失败：{err}\033[0m")
        return f"Error: {err}"


def _scheduler_loop(stop_event: threading.Event) -> None:
    """每 30 秒检查一次到期任务并触发执行。"""
    while not stop_event.is_set():
        try:
            due = get_due_tasks(CWD)
            # 在锁内原子领取（mark_task_running），锁外执行，避免长时间阻塞
            to_run: list[str] = []
            with _claim_lock:
                for task in due:
                    to_run.append(task.task_id)
                    mark_task_running(CWD, task.task_id)
            for task_id in to_run:
                _execute_task(task_id)
        except Exception as e:
            print(f"\033[31m[scheduler] 调度器异常：{e}\033[0m")
        stop_event.wait(30)


# ── REPL 辅助 ─────────────────────────────────────────────────────────────────

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


def _list_sessions_command() -> int:
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


def _cmd_memory(args_str: str) -> None:
    args_str = args_str.strip()
    from core.memory import delete_memory
    headers = list_memories(CWD)

    if args_str.startswith("delete "):
        title = args_str[len("delete "):].strip()
        if not title:
            print("用法：/memory delete <title>")
            return
        ok = delete_memory(CWD, title)
        print(f"已删除：{title}" if ok else f"未找到：{title!r}（已有：{', '.join(h.title for h in headers)}）")
        return

    if not headers:
        print("(暂无跨会话记忆)")
        return

    if not args_str or args_str == "list":
        for h in headers:
            tag = f"[{h.memory_type}] " if h.memory_type else ""
            print(f"\033[33m■ {h.title}\033[0m  {tag}{h.description}")
            if h.body:
                for line in h.body.splitlines():
                    print(f"  {line}")
            print()
    else:
        header = read_memory(CWD, args_str)
        if header is None:
            print(f"未找到记忆：{args_str!r}")
            print("已有记忆：" + ", ".join(h.title for h in headers))
        else:
            print(f"\033[33m■ {header.title}\033[0m")
            if header.memory_type:
                print(f"  type: {header.memory_type}")
            print(f"  desc: {header.description}")
            print()
            print(header.body)


def _cmd_tasks(args_str: str) -> None:
    args_str = args_str.strip()

    if args_str.startswith("cancel "):
        name = args_str[len("cancel "):].strip()
        from core.tasks import cancel_task as _cancel
        ok = _cancel(CWD, name)
        print(f"已取消：{name}" if ok else f"未找到任务：{name!r}")
        return

    if args_str.startswith("run "):
        name = args_str[len("run "):].strip()
        rec = get_task_by_name(CWD, name)
        if rec is None:
            print(f"未找到任务：{name!r}")
            return
        with _claim_lock:
            mark_task_running(CWD, rec.task_id)
        result = _execute_task(rec.task_id)
        print(result)
        return

    # 默认 list
    records = list_tasks(CWD)
    if not records:
        print("(暂无任务)")
        return
    active = [r for r in records if r.status not in ("cancelled", "done", "failed")]
    done   = [r for r in records if r.status in ("cancelled", "done", "failed")]
    if active:
        print(f"\033[33m活跃任务（{len(active)}）：\033[0m")
        for r in active:
            cron_info = f"cron={r.cron}" if r.cron else "one-shot"
            nxt = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.next_run_at)) if r.next_run_at else "-"
            print(f"  [{r.status}] {r.name}  {cron_info}  next={nxt}  runs={r.run_count}")
    if done:
        print(f"\033[90m已结束任务（{len(done)}）：\033[0m")
        for r in done:
            print(f"  [{r.status}] {r.name}  runs={r.run_count}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    global COMPACT_THRESHOLD, KEEP_TAIL_TURNS, SYSTEM
    parser = argparse.ArgumentParser(description="PracticeAgent stage09 (tasks & cron)")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--resume", "-r", metavar="ID", help="按 id 或前缀恢复会话")
    g.add_argument("--continue", "-c", dest="cont", action="store_true", help="接最近一个会话")
    g.add_argument("--list", "-l", dest="list_", action="store_true", help="列最近 20 个会话")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD_TOKENS)
    parser.add_argument("--keep-tail", type=int, default=DEFAULT_KEEP_TAIL_TURNS)
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=PermissionMode.DEFAULT.value,
    )
    args = parser.parse_args()

    COMPACT_THRESHOLD = args.threshold
    KEEP_TAIL_TURNS = args.keep_tail
    PERMISSIONS.mode = PermissionMode(args.mode)

    if args.list_:
        return _list_sessions_command()

    # 注册 run_now 回调
    set_run_task_callback(_execute_task)

    # 启动后台调度线程
    _stop_event = threading.Event()
    _sched_thread = threading.Thread(
        target=_scheduler_loop,
        args=(_stop_event,),
        name="scheduler",
        daemon=True,
    )
    _sched_thread.start()
    print("\033[33m[scheduler] 后台调度线程已启动（tick=30s）\033[0m")

    if args.resume:
        session = Session.resume(args.resume)
        print(f"PracticeAgent · stage09 · resumed {session.session_id} ({len(session.messages)} 条历史)")
    elif args.cont:
        recent = Session.list_recent(limit=1)
        if not recent:
            print("(无历史会话，开新的)")
            session = Session.new(model=MODEL)
        else:
            session = Session.resume(recent[0][0])
            print(f"PracticeAgent · stage09 · continued {session.session_id} ({len(session.messages)} 条历史)")
    else:
        session = Session.new(model=MODEL)
        print(f"PracticeAgent · stage09 · new session {session.session_id}")

    mem_count = len(list_memories(CWD))
    task_count = len([t for t in list_tasks(CWD) if t.status not in ("cancelled", "done", "failed")])
    print(
        f"tools: {', '.join(REGISTRY.names())} · skills: {len(SKILLS)} · "
        f"memories: {mem_count} · tasks: {task_count} · mode={PERMISSIONS.mode.value} · "
        "compact threshold={} keep_tail={} — "
        "/tasks [cancel|run <name>] · /memory [title] · /compact · /mode <name> · q/exit 退出".format(
            COMPACT_THRESHOLD, KEEP_TAIL_TURNS
        )
    )

    try:
        while True:
            try:
                query = input("\033[36ms09 >> \033[0m").strip()
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
            if query.startswith("/memory"):
                _cmd_memory(query[len("/memory"):])
                continue
            if query.startswith("/tasks"):
                _cmd_tasks(query[len("/tasks"):])
                continue

            session.append_user(query)
            agent_loop(session)
            if not STREAM and session.messages:
                _print_assistant_text(session.messages[-1].get("content"))
            print()
    finally:
        _stop_event.set()
        _sched_thread.join(timeout=2)

    print(f"会话已保存：{session.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
