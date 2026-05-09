"""glob 工具：用通配符在工作目录中查找文件，按修改时间倒序。"""

from __future__ import annotations

from pathlib import Path

from core.tool_registry import Tool


_MAX_RESULTS = 200


def _run(args: dict) -> str:
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        return "Error: 缺少 pattern 参数（如 **/*.py）"
    base = Path(str(args.get("path", "")).strip() or ".")
    if not base.is_absolute():
        base = Path.cwd() / base
    if not base.is_dir():
        return f"Error: 起始目录不存在 {base}"

    try:
        matches = [p for p in base.glob(pattern) if p.is_file()]
    except OSError as exc:
        return f"Error: 通配符匹配失败：{exc}"

    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        return f"(无匹配 pattern={pattern} 起点={base})"
    truncated = matches[:_MAX_RESULTS]
    head = "\n".join(str(p) for p in truncated)
    suffix = (
        f"\n... ({len(matches) - _MAX_RESULTS} 个结果未显示)"
        if len(matches) > _MAX_RESULTS
        else ""
    )
    return head + suffix


TOOL = Tool(
    name="glob",
    description="用 glob 通配符（如 **/*.py）查找文件，按修改时间倒序返回路径。",
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob 模式，如 src/**/*.ts"},
            "path": {"type": "string", "description": "起始目录，默认当前目录"},
        },
        "required": ["pattern"],
    },
    handler=_run,
)
