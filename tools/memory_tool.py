"""memory 工具：让 agent 可以跨会话读写记忆。

操作：
  write  — 写入或更新一条记忆
  read   — 读取一条记忆的完整内容
  list   — 列出所有记忆
  delete — 删除一条记忆
"""

from __future__ import annotations

from pathlib import Path

from core.memory import delete_memory, list_memories, read_memory, write_memory
from core.tool_registry import Tool


def _run(args: dict) -> str:
    cwd = Path.cwd()
    action = args.get("action", "").strip()
    if action == "write":
        title = args.get("title", "").strip()
        content = args.get("content", "").strip()
        memory_type = args.get("type", "").strip()
        if not title or not content:
            return "Error: write 需要 title 和 content 参数"
        path = write_memory(cwd, title, content, memory_type)
        return f"记忆已保存：{path}"

    if action == "read":
        name = args.get("name", "").strip()
        if not name:
            return "Error: read 需要 name 参数"
        header = read_memory(cwd, name)
        if header is None:
            return f"Error: 未找到记忆 {name!r}"
        return f"# {header.title}\n**type**: {header.memory_type or '-'}\n**desc**: {header.description}\n\n{header.body}"

    if action == "list":
        headers = list_memories(cwd)
        if not headers:
            return "(暂无跨会话记忆)"
        lines = [f"共 {len(headers)} 条记忆："]
        for h in headers:
            tag = f"[{h.memory_type}] " if h.memory_type else ""
            lines.append(f"  - {h.title} ({tag}{h.description[:60]})")
        return "\n".join(lines)

    if action == "delete":
        name = args.get("name", "").strip()
        if not name:
            return "Error: delete 需要 name 参数"
        ok = delete_memory(cwd, name)
        return f"已删除：{name}" if ok else f"Error: 未找到记忆 {name!r}"

    return f"Error: 未知 action={action!r}，可选：write / read / list / delete"


TOOL = Tool(
    name="memory",
    description=(
        "跨会话记忆工具。用于持久化记住用户偏好、项目约定等，重启后仍有效。\n"
        "action=write：写入 title+content（可选 type: user/feedback/project/reference）\n"
        "action=read：按 name 读取完整内容\n"
        "action=list：列出所有记忆\n"
        "action=delete：删除一条记忆"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["write", "read", "list", "delete"],
                "description": "操作类型",
            },
            "title": {"type": "string", "description": "write 时的标题（唯一键）"},
            "content": {"type": "string", "description": "write 时的内容"},
            "type": {
                "type": "string",
                "enum": ["user", "feedback", "project", "reference", ""],
                "description": "write 时的记忆类型（可选）",
            },
            "name": {"type": "string", "description": "read/delete 时的 title 或 slug"},
        },
        "required": ["action"],
    },
    handler=_run,
)
