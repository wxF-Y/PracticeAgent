"""bash 工具：执行 shell 命令。从 s01 迁移，结构与 stage 1 完全一致。"""

from __future__ import annotations

import os
import subprocess

from core.tool_registry import Tool


_DANGEROUS = ("rm -rf /", "sudo ", "shutdown", "reboot", "mkfs", ":(){ :|:& };:")


def _run(args: dict) -> str:
    command = str(args.get("command", "")).strip()
    if not command:
        return "Error: 缺少 command 参数"
    if any(d in command for d in _DANGEROUS):
        return "Error: 危险命令已被拦截"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if not out:
            return "(命令无输出)"
        return out[:50_000]
    except subprocess.TimeoutExpired:
        return "Error: 命令超时（120s）"
    except (FileNotFoundError, OSError) as exc:
        return f"Error: {exc}"


TOOL = Tool(
    name="bash",
    description="在工作目录中执行一条 shell 命令，返回 stdout+stderr。仅限非交互命令。",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
        },
        "required": ["command"],
    },
    handler=_run,
)
