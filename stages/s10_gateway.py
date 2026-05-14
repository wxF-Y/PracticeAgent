#!/usr/bin/env python3
"""Stage 10: Gateway —— 终端 + 微信ClawBot 双通道，解锁 V3/V6。

在 s09 基础上新增三件事：
  1. gateway/im.py      — Channel 协议、InboundMessage / OutboundMessage
  2. gateway/cli.py     — 终端通道（stdin→queue→stdout）
  3. gateway/weixin.py  — 微信ClawBot 通道（long-poll inbound + sendmessage outbound）

运行（仅终端）：
    python stages/s10_gateway.py

运行（同时接入微信）：
    WEIXIN_BOT_TOKEN=<your_token> python stages/s10_gateway.py --weixin

REPL 内置命令（终端模式）：
    /tasks [cancel|run <name>]  /memory [title]  /compact  /mode <name>  q/exit
"""

from __future__ import annotations

import argparse
import logging
import os
import queue
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
from gateway.cli import CLIChannel  # noqa: E402
from gateway.im import InboundMessage, OutboundMessage  # noqa: E402
from gateway.weixin import WeixinChannel  # noqa: E402
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

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
for _t in (READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL):
    SUB_REGISTRY.register(_t)
SUB_REGISTRY.register(make_skill_tool(SKILLS))

REGISTRY = ToolRegistry()
for _t in (READ_TOOL, WRITE_TOOL, EDIT_TOOL, GLOB_TOOL, GREP_TOOL, BASH_TOOL, MEMORY_TOOL, TASK_TOOL):
    REGISTRY.register(_t)
REGISTRY.register(make_skill_tool(SKILLS))
REGISTRY.register(make_agent_tool(client=client, model=MODEL, sub_registry=SUB_REGISTRY))

AUDIT_LOG_PATH = Path.home() / ".practiceagent" / "audit.log"
PERMISSIONS = PermissionChecker(mode=PermissionMode.DEFAULT, cwd=CWD)
HOOKS = HookExecutor()
HOOKS.register(HookEvent.PRE_TOOL_USE, print_invocation_hook)
HOOKS.register(HookEvent.PRE_TOOL_USE, block_write_outside_cwd_hook(CWD))
HOOKS.register(HookEvent.PRE_TOOL_USE, make_audit_log_hook(AUDIT_LOG_PATH))
HOOKS.register(HookEvent.POST_TOOL_USE, make_audit_log_hook(AUDIT_LOG_PATH))

GOV = Governance(registry=REGISTRY, permissions=PERMISSIONS, hooks=HOOKS, prompt=cli_prompt)

_SCHED_PERMISSIONS = PermissionChecker(mode=PermissionMode.BYPASS, cwd=CWD)
_SCHED_HOOKS = HookExecutor()
_SCHED_HOOKS.register(HookEvent.PRE_TOOL_USE, make_audit_log_hook(AUDIT_LOG_PATH))
_SCHED_HOOKS.register(HookEvent.POST_TOOL_USE, make_audit_log_hook(AUDIT_LOG_PATH))
_SCHED_GOV = Governance(
    registry=SUB_REGISTRY,
    permissions=_SCHED_PERMISSIONS,
    hooks=_SCHED_HOOKS,
    prompt=cli_prompt,
)

# 微信通道使用 BYPASS 权限（非交互场景，不阻塞 stdin）
_WEIXIN_PERMISSIONS = PermissionChecker(mode=PermissionMode.BYPASS, cwd=CWD)
_WEIXIN_HOOKS = HookExecutor()
_WEIXIN_HOOKS.register(HookEvent.PRE_TOOL_USE, make_audit_log_hook(AUDIT_LOG_PATH))
_WEIXIN_HOOKS.register(HookEvent.POST_TOOL_USE, make_audit_log_hook(AUDIT_LOG_PATH))
_WEIXIN_GOV = Governance(
    registry=REGISTRY,  # 微信用户可使用完整工具集
    permissions=_WEIXIN_PERMISSIONS,
    hooks=_WEIXIN_HOOKS,
    prompt=lambda _: False,  # 非交互：敏感操作自动拒绝
)

THINKING: ThinkingConfigParam = {"type": "disabled"}
STREAM = True
MAX_PARALLEL_AGENTS = 3
COMPACT_THRESHOLD = DEFAULT_THRESHOLD_TOKENS
KEEP_TAIL_TURNS = DEFAULT_KEEP_TAIL_TURNS
_claim_lock = threading.Lock()


# ── system prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    lines = [
        f"你是一个 coding agent，工作目录在 {os.getcwd()}。",
        f"可用工具: {', '.join(REGISTRY.names())}.",
        "敏感动作会被权限层拦截或询问用户；被拒绝时请改用更安全的方式或停止。",
        "适时派子 agent；简单事直接做。",
    ]
    brief = SKILLS.list_brief()
    if brief:
        lines += ["", "可用 skills（相关任务先 skill(name=...) 加载详细指引）："]
        for name, desc in brief:
            lines.append(f"  - {name}: {desc}")
    mem_section = build_memory_section(CWD)
    if mem_section:
        lines += ["", mem_section]
    task_count = len([t for t in list_tasks(CWD) if t.status not in ("cancelled", "done", "failed")])
    if task_count:
        lines += ["", f"当前有 {task_count} 个活跃后台任务（用 task(action=list) 查看）。"]
    lines += ["", "当用户提到偏好或规范，主动用 memory(action=write) 记住。需要定时任务时用 task(action=schedule)。"]
    lines.append("直接动手，不要冗长解释。")
    return "\n".join(lines)


SYSTEM = _build_system_prompt()


# ── agent loop ────────────────────────────────────────────────────────────────

def _run_turn(session: Session, governance: Governance = GOV) -> Message:
    msgs = cast(list[MessageParam], session.messages)
    tools = cast(list[ToolParam], REGISTRY.list_schemas())
    if STREAM:
        print("\033[35m[stream]\033[0m ", end="", flush=True)
        with client.messages.stream(
            model=MODEL, system=SYSTEM, messages=msgs, tools=tools,
            max_tokens=4096, thinking=THINKING,
        ) as stream:
            for chunk in stream.text_stream:
                print(chunk, end="", flush=True)
            print()
            return stream.get_final_message()
    return cast(Message, client.messages.create(
        model=MODEL, system=SYSTEM, messages=msgs, tools=tools,
        max_tokens=4096, thinking=THINKING,
    ))


def _dispatch_block(block: ToolUseBlock, governance: Governance = GOV) -> tuple[str, str]:
    inp = block.input if isinstance(block.input, dict) else {}
    return block.id, governance.dispatch(block.name, inp)


def _brief_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = repr(v)
        if len(s) > 80:
            s = s[:77] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _maybe_compact(session: Session, last_input_tokens: int) -> None:
    if last_input_tokens < COMPACT_THRESHOLD:
        return
    print(f"\033[31m[compact] input_tokens={last_input_tokens} 超过阈值，触发自动压缩\033[0m")
    result = compact_messages(messages=session.messages, client=client, model=MODEL, keep_tail_turns=KEEP_TAIL_TURNS)
    if result is None:
        return
    new_messages, summary = result
    session.messages = new_messages
    print(f"\033[31m[compact] 压缩完成，摘要: {summary[:200]}\033[0m")


def agent_loop(session: Session, governance: Governance = GOV) -> None:
    while True:
        response = _run_turn(session, governance)
        session.append_assistant(response.content, usage=response.usage)
        print(
            f"\033[90m[session {session.session_id[:14]} · turn {session.turn} · "
            f"tokens in={session.total_input} out={session.total_output}]\033[0m"
        )

        if response.stop_reason != "tool_use":
            usage = getattr(response, "usage", None)
            if usage:
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
            print(output[:400] + ("..." if len(output) > 400 else ""))
            results_by_id[block.id] = output

        if agent_blocks:
            for b in agent_blocks:
                inp = b.input if isinstance(b.input, dict) else {}
                print(f"\033[36m↳ spawn agent {b.id[-6:]}({_brief_args(inp)})\033[0m")
            if len(agent_blocks) == 1:
                tid, out = _dispatch_block(agent_blocks[0], governance)
                results_by_id[tid] = out
            else:
                with ThreadPoolExecutor(max_workers=MAX_PARALLEL_AGENTS) as pool:
                    futures = {pool.submit(_dispatch_block, b, governance): b for b in agent_blocks}
                    for fut in futures:
                        tid, out = fut.result()
                        results_by_id[futures[fut].id] = out

        session.append_user([
            {"type": "tool_result", "tool_use_id": b.id, "content": results_by_id[b.id]}
            for b in tool_blocks
        ])
        usage = getattr(response, "usage", None)
        if usage:
            _maybe_compact(session, int(getattr(usage, "input_tokens", 0) or 0))


# ── 任务调度 ──────────────────────────────────────────────────────────────────

def _execute_task(task_id: str) -> str:
    record = next((t for t in list_tasks(CWD) if t.task_id == task_id), None)
    if record is None:
        return f"Error: task_id={task_id} 不存在"
    print(f"\033[33m[scheduler] 触发任务：{record.name}\033[0m")
    mark_task_running(CWD, task_id)
    session = Session.new(model=MODEL)
    session.append_user(record.prompt)
    try:
        agent_loop(session, governance=_SCHED_GOV)
        update_task_after_run(CWD, task_id, success=True)
        summary = f"任务 {record.name!r} 完成"
        print(f"\033[33m[scheduler] {summary}\033[0m")
        return summary
    except Exception as e:
        err = str(e)[:200]
        update_task_after_run(CWD, task_id, success=False, error=err)
        print(f"\033[31m[scheduler] 任务 {record.name!r} 失败：{err}\033[0m")
        return f"Error: {err}"


def _scheduler_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            due = get_due_tasks(CWD)
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


# ── 多通道消息处理 ─────────────────────────────────────────────────────────────

def _extract_last_text(content) -> str:
    """从 assistant content 中提取最后一段文本。"""
    if not isinstance(content, list):
        return ""
    texts = []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            texts.append(text)
    return "\n".join(texts)


def _handle_weixin_message(msg: InboundMessage, channel: WeixinChannel,
                           sessions: dict[str, Session]) -> None:
    """在独立线程中处理一条微信入站消息，运行 agent loop 并回复。
    每个用户维护独立 Session，保持多轮对话上下文。
    """
    import logging as _logging
    _log = _logging.getLogger("gateway.weixin.handler")
    _log.info("收到来自 %s 的消息: %s", msg.from_user_id, msg.text[:80])

    # 按 from_user_id 维护独立会话（此函数由 ThreadPoolExecutor 调用，同一用户串行执行）
    session = sessions.setdefault(msg.from_user_id, Session.new(model=MODEL))
    session.append_user(msg.text)
    try:
        agent_loop(session, governance=_WEIXIN_GOV)
        reply_text = _extract_last_text(
            session.messages[-1].get("content") if session.messages else []
        )
        if reply_text:
            channel.send(OutboundMessage(
                channel_id="weixin",
                to_user_id=msg.from_user_id,
                text=reply_text,
                context_token=msg.context_token,
            ))
            _log.info("已回复 %s", msg.from_user_id)
    except Exception as exc:
        _log.error("处理消息失败: %s", exc, exc_info=True)


def _weixin_processor_loop(weixin_ch: WeixinChannel, stop_event: threading.Event) -> None:
    """持续从 weixin_ch.inbound_queue 取消息并派发到线程池处理。
    独立于 CLI 主循环，保证微信消息实时响应。
    """
    # 每个用户一个 Session（在本线程内串行访问，无需额外锁）
    user_sessions: dict[str, Session] = {}
    # 同一用户最多 1 个并发请求（避免乱序回复）
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="wx-handler")
    user_futures: dict[str, object] = {}
    try:
        while not stop_event.is_set():
            try:
                wx_msg = weixin_ch.inbound_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            # 若同一用户上一条消息还在处理，等待完成后再处理新消息（保证顺序）
            prev = user_futures.get(wx_msg.from_user_id)
            if prev is not None:
                try:
                    prev.result(timeout=120)
                except Exception:
                    pass
            fut = executor.submit(
                _handle_weixin_message, wx_msg, weixin_ch, user_sessions
            )
            user_futures[wx_msg.from_user_id] = fut
    finally:
        executor.shutdown(wait=False)


# ── REPL 命令 ─────────────────────────────────────────────────────────────────

def _set_mode(raw: str) -> str:
    try:
        new_mode = PermissionMode(raw.strip().lower())
    except ValueError:
        valid = ", ".join(m.value for m in PermissionMode)
        return f"未知模式：{raw}（可选：{valid}）"
    PERMISSIONS.mode = new_mode
    PERMISSIONS.always_allow.clear()
    return f"已切换到 {new_mode.value} 模式"


def _cmd_memory(args_str: str) -> None:
    from core.memory import delete_memory
    headers = list_memories(CWD)
    if args_str.startswith("delete "):
        title = args_str[len("delete "):].strip()
        ok = delete_memory(CWD, title)
        print(f"已删除：{title}" if ok else f"未找到：{title!r}")
        return
    if not headers:
        print("(暂无跨会话记忆)")
        return
    if not args_str.strip() or args_str.strip() == "list":
        for h in headers:
            tag = f"[{h.memory_type}] " if h.memory_type else ""
            print(f"\033[33m■ {h.title}\033[0m  {tag}{h.description}")
            if h.body:
                for line in h.body.splitlines():
                    print(f"  {line}")
            print()
    else:
        header = read_memory(CWD, args_str.strip())
        if header is None:
            print(f"未找到记忆：{args_str.strip()!r}")
        else:
            print(f"\033[33m■ {header.title}\033[0m\n{header.body}")


def _cmd_tasks(args_str: str) -> None:
    from core.tasks import cancel_task as _cancel
    if args_str.startswith("cancel "):
        name = args_str[len("cancel "):].strip()
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
        print(_execute_task(rec.task_id))
        return
    records = list_tasks(CWD)
    if not records:
        print("(暂无任务)")
        return
    active = [r for r in records if r.status not in ("cancelled", "done", "failed")]
    done = [r for r in records if r.status in ("cancelled", "done", "failed")]
    if active:
        print(f"\033[33m活跃任务（{len(active)}）：\033[0m")
        for r in active:
            cron_info = f"cron={r.cron}" if r.cron else "one-shot"
            nxt = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.next_run_at)) if r.next_run_at else "-"
            print(f"  [{r.status}] {r.name}  {cron_info}  next={nxt}  runs={r.run_count}")
    if done:
        print(f"\033[90m已结束（{len(done)}）：" + "  ".join(f"[{r.status}] {r.name}" for r in done) + "\033[0m")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    global COMPACT_THRESHOLD, KEEP_TAIL_TURNS, SYSTEM
    parser = argparse.ArgumentParser(description="PracticeAgent stage10 (gateway)")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--resume", "-r", metavar="ID")
    g.add_argument("--continue", "-c", dest="cont", action="store_true")
    g.add_argument("--list", "-l", dest="list_", action="store_true")
    parser.add_argument("--weixin", action="store_true", help="启用微信ClawBot通道")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD_TOKENS)
    parser.add_argument("--keep-tail", type=int, default=DEFAULT_KEEP_TAIL_TURNS)
    parser.add_argument("--mode", choices=[m.value for m in PermissionMode], default=PermissionMode.DEFAULT.value)
    args = parser.parse_args()

    COMPACT_THRESHOLD = args.threshold
    KEEP_TAIL_TURNS = args.keep_tail
    PERMISSIONS.mode = PermissionMode(args.mode)

    if args.list_:
        items = Session.list_recent()
        if not items:
            print("(无历史会话)")
        else:
            for sid, preview in items:
                print(f"{sid:<28}  {preview}")
        return 0

    set_run_task_callback(_execute_task)

    # 启动调度线程
    _stop_event = threading.Event()
    threading.Thread(target=_scheduler_loop, args=(_stop_event,), name="scheduler", daemon=True).start()
    print("\033[33m[scheduler] 后台调度线程已启动\033[0m")

    # 启动微信通道（可选）— 独立处理线程，不依赖 CLI input()
    weixin_ch: WeixinChannel | None = None
    _weixin_proc_thread: threading.Thread | None = None
    if args.weixin:
        weixin_ch = WeixinChannel()
        weixin_ch.start()
        _weixin_proc_thread = threading.Thread(
            target=_weixin_processor_loop,
            args=(weixin_ch, _stop_event),
            name="weixin-processor",
            daemon=True,
        )
        _weixin_proc_thread.start()
        print(f"\033[36m[gateway] 微信ClawBot 通道已启动（account={weixin_ch._account_id}）\033[0m")

    # 恢复/新建 CLI 会话
    if args.resume:
        session = Session.resume(args.resume)
        print(f"PracticeAgent · stage10 · resumed {session.session_id}")
    elif args.cont:
        recent = Session.list_recent(limit=1)
        if not recent:
            session = Session.new(model=MODEL)
        else:
            session = Session.resume(recent[0][0])
            print(f"PracticeAgent · stage10 · continued {session.session_id}")
    else:
        session = Session.new(model=MODEL)
        print(f"PracticeAgent · stage10 · new session {session.session_id}")

    channels = ["cli"]
    if weixin_ch:
        channels.append("weixin")
    print(
        f"tools: {', '.join(REGISTRY.names())} · channels: {', '.join(channels)} · "
        "q/exit 退出"
    )

    try:
        while True:
            # 非阻塞检查微信入站队列
            # CLI 输入
            try:
                query = input("\033[36ms10 >> \033[0m").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if query.lower() in ("q", "exit", ""):
                break
            if query == "/compact":
                result = compact_messages(messages=session.messages, client=client, model=MODEL,
                                          keep_tail_turns=KEEP_TAIL_TURNS)
                if result:
                    session.messages, summary = result
                    print(f"\033[31m[compact] 完成: {summary[:200]}\033[0m")
                continue
            if query.startswith("/mode"):
                parts = query.split(maxsplit=1)
                print(_set_mode(parts[1]) if len(parts) == 2 else f"当前模式：{PERMISSIONS.mode.value}")
                continue
            if query.startswith("/memory"):
                _cmd_memory(query[len("/memory"):].strip())
                continue
            if query.startswith("/tasks"):
                _cmd_tasks(query[len("/tasks"):].strip())
                continue

            session.append_user(query)
            agent_loop(session)
            if not STREAM and session.messages:
                print(_extract_last_text(session.messages[-1].get("content")))
            print()

    finally:
        _stop_event.set()
        if weixin_ch:
            weixin_ch.stop()

    print(f"会话已保存：{session.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
