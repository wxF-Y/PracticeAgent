"""task 工具：让 agent 可以调度、查看、取消后台任务。

操作：
  schedule — 创建一次性或 cron 定时任务
  list     — 列出所有任务及状态
  cancel   — 取消一个任务
  run_now  — 立即触发一次（发送信号给调度线程）
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from core.tasks import (
    cancel_task,
    get_task_by_name,
    list_tasks,
    schedule_task,
    update_task_after_run,
)
from core.tool_registry import Tool

# 由 s09_tasks.py 在启动时注入，允许 run_now 直接触发
_run_task_callback: Callable[[str], str] | None = None


def set_run_task_callback(cb) -> None:
    global _run_task_callback
    _run_task_callback = cb


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_task(r) -> str:
    cron_info = f"cron={r.cron}" if r.cron else "one-shot"
    next_info = f"next={_fmt_ts(r.next_run_at)}" if r.next_run_at else ""
    last_info = f"last={_fmt_ts(r.last_run_at)}" if r.last_run_at else ""
    parts = [f"[{r.status}]", r.name, cron_info]
    if next_info:
        parts.append(next_info)
    if last_info:
        parts.append(last_info)
    if r.run_count:
        parts.append(f"runs={r.run_count}")
    if r.last_error:
        parts.append(f"err={r.last_error[:60]}")
    return "  " + "  ".join(parts)


def _run(args: dict) -> str:
    cwd = Path.cwd()
    action = args.get("action", "").strip()

    if action == "schedule":
        name = args.get("name", "").strip()
        prompt = args.get("prompt", "").strip()
        cron = args.get("cron", "").strip() or None
        delay = args.get("delay_seconds")
        if not name or not prompt:
            return "Error: schedule 需要 name 和 prompt 参数"
        try:
            delay_f = float(delay) if delay is not None else None
            record = schedule_task(cwd, name, prompt, cron=cron, delay_seconds=delay_f)
        except ValueError as e:
            return f"Error: {e}"
        cron_desc = f"cron={record.cron}" if record.cron else "one-shot"
        return (
            f"任务已调度：{record.name} ({cron_desc})\n"
            f"  task_id={record.task_id[:12]}\n"
            f"  下次触发：{_fmt_ts(record.next_run_at)}"
        )

    if action == "list":
        records = list_tasks(cwd)
        if not records:
            return "(暂无任务)"
        lines = [f"共 {len(records)} 个任务："]
        for r in records:
            lines.append(_fmt_task(r))
        return "\n".join(lines)

    if action == "cancel":
        name = args.get("name", "").strip()
        if not name:
            return "Error: cancel 需要 name 参数"
        ok = cancel_task(cwd, name)
        return f"已取消：{name}" if ok else f"Error: 未找到任务 {name!r}"

    if action == "run_now":
        name = args.get("name", "").strip()
        if not name:
            return "Error: run_now 需要 name 参数"
        record = get_task_by_name(cwd, name)
        if record is None:
            return f"Error: 未找到任务 {name!r}"
        if record.status == "cancelled":
            return f"Error: 任务 {name!r} 已取消，无法触发"
        if _run_task_callback is not None:
            result = _run_task_callback(record.task_id)
            return f"任务 {name!r} 已触发：{result}"
        return f"Error: 调度器未就绪（run_task_callback 未注册）"

    return f"Error: 未知 action={action!r}，可选：schedule / list / cancel / run_now"


TOOL = Tool(
    name="task",
    description=(
        "后台任务调度工具。可创建一次性或 cron 定时任务，任务触发时 agent 将执行 prompt。\n"
        "action=schedule：调度任务（name + prompt，可选 cron='0 9 * * *' 或 delay_seconds=60）\n"
        "action=list：列出所有任务\n"
        "action=cancel：取消任务（by name）\n"
        "action=run_now：立即触发一次"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["schedule", "list", "cancel", "run_now"],
                "description": "操作类型",
            },
            "name": {"type": "string", "description": "任务名称（唯一键）"},
            "prompt": {"type": "string", "description": "schedule 时 agent 收到的 prompt"},
            "cron": {
                "type": "string",
                "description": "5 字段 cron 表达式，如 '0 9 * * *'（每天 9 点），留空为一次性任务",
            },
            "delay_seconds": {
                "type": "number",
                "description": "一次性任务：N 秒后触发（与 cron 互斥）",
            },
        },
        "required": ["action"],
    },
    handler=_run,
)
