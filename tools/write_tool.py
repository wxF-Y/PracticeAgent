"""write 工具：把内容写入文件。父目录不存在则自动创建。"""

from __future__ import annotations

from pathlib import Path

from core.tool_registry import Tool


def _run(args: dict) -> str:
    path_str = str(args.get("path", "")).strip()
    if not path_str:
        return "Error: 缺少 path 参数"
    content = args.get("content", "")
    if not isinstance(content, str):
        return "Error: content 必须是字符串"

    p = Path(path_str)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"Error: 写入失败 {p}：{exc}"
    return f"已写入 {p}（{len(content)} 字符）"


TOOL = Tool(
    name="write",
    description="把内容写入文件。已存在会被覆盖；父目录不存在会自动创建。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "要写入的完整内容"},
        },
        "required": ["path", "content"],
    },
    handler=_run,
)
