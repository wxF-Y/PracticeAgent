"""read 工具：读取文本文件，按行返回（带行号），支持 offset/limit。"""

from __future__ import annotations

from pathlib import Path

from core.tool_registry import Tool


_DEFAULT_LIMIT = 2000
_MAX_BYTES = 200_000  # 单次读取上限


def _run(args: dict) -> str:
    path_str = str(args.get("path", "")).strip()
    if not path_str:
        return "Error: 缺少 path 参数"
    p = Path(path_str)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        return f"Error: 文件不存在 {p}"
    if not p.is_file():
        return f"Error: 不是文件 {p}"

    offset = int(args.get("offset", 0) or 0)
    limit = int(args.get("limit", _DEFAULT_LIMIT) or _DEFAULT_LIMIT)

    try:
        # 限制总读取字节数防止超大文件吃光内存
        data = p.read_bytes()[:_MAX_BYTES]
        text = data.decode("utf-8", errors="replace")
    except OSError as exc:
        return f"Error: 无法读取 {p}：{exc}"

    lines = text.splitlines()
    end = offset + limit
    selected = lines[offset:end]
    if not selected:
        return f"(文件共 {len(lines)} 行，offset={offset} 已超出范围)"

    width = len(str(end))
    numbered = "\n".join(f"{i + 1:>{width}} | {ln}" for i, ln in enumerate(selected, start=offset))
    suffix = f"\n... ({len(lines) - end} 行未显示)" if end < len(lines) else ""
    return numbered + suffix


TOOL = Tool(
    name="read",
    description="读取文本文件并按行返回（含行号）。可用 offset/limit 翻页。",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径，相对或绝对"},
            "offset": {"type": "integer", "description": "起始行号（0-based），默认 0"},
            "limit": {"type": "integer", "description": "最多返回行数，默认 2000"},
        },
        "required": ["path"],
    },
    handler=_run,
)
