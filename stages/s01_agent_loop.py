#!/usr/bin/env python3
"""Stage 1: Agent Loop —— 一个工具 + 一个循环 = 一个 agent。

整个 coding agent 的核心秘密：

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+

后续 stage 都在这个循环上叠加（工具集 / 持久化 / 权限 / 子 agent / 网关），
循环本身永远不变。

运行：
    python stages/s01_agent_loop.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 让本文件可直接 `python stages/s01_agent_loop.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import make_client  # noqa: E402

# Windows 控制台默认 GBK，强制 UTF-8 避免中文乱码
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

client, cfg = make_client()
MODEL = cfg.model_id

SYSTEM = (
    f"你是一个 coding agent，工作目录在 {os.getcwd()}。"
    "使用 bash 工具完成用户的任务。直接动手，不要冗长解释。"
)

# 工具定义：暴露给模型看的 JSON Schema
TOOLS = [
    {
        "name": "bash",
        "description": "在工作目录中执行一条 shell 命令，返回 stdout+stderr。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令",
                }
            },
            "required": ["command"],
        },
    }
]

# 危险命令黑名单（最简版，s07 会替换为完整权限系统）
_DANGEROUS = ("rm -rf /", "sudo ", "shutdown", "reboot", "mkfs", ":(){ :|:& };:")


def run_bash(command: str) -> str:
    """执行一条 shell 命令并返回输出。带最简危险命令拦截。"""
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
        # 截断过长输出，避免吃掉上下文
        return out[:50_000]
    except subprocess.TimeoutExpired:
        return "Error: 命令超时（120s）"
    except (FileNotFoundError, OSError) as exc:
        return f"Error: {exc}"


def agent_loop(messages: list[dict]) -> None:
    """核心循环：调用模型 → 执行工具 → 反馈结果，直到模型停止调用工具。"""
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=4096,
        )
        # 1) 把 assistant 这一轮（可能是文本 + tool_use）追加到历史
        messages.append({"role": "assistant", "content": response.content})

        # 2) 模型不再调工具 → 任务完成
        if response.stop_reason != "tool_use":
            return

        # 3) 把每个 tool_use 都执行一遍，把结果作为下一轮 user 消息回填
        results = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            cmd = block.input.get("command", "")
            print(f"\033[33m$ {cmd}\033[0m")
            output = run_bash(cmd)
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


def _print_assistant_text(content) -> None:
    """打印模型最后一轮的纯文本块（忽略 tool_use 块）。"""
    if not isinstance(content, list):
        return
    for block in content:
        text = getattr(block, "text", None)
        if text:
            print(text)


def main() -> None:
    history: list[dict] = []
    print("PracticeAgent · stage01 (agent loop) — 输入 q / exit / 空行 退出")
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m").strip()
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
