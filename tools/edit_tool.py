"""edit 工具：在已有文件中做精确字符串替换。

要求 old_string 在文件内**唯一**出现（除非 replace_all=True），避免误改。
"""

from __future__ import annotations

from pathlib import Path

from core.tool_registry import Tool


def _run(args: dict) -> str:
    path_str = str(args.get("path", "")).strip()
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))

    if not path_str:
        return "Error: 缺少 path 参数"
    if not isinstance(old, str) or not isinstance(new, str):
        return "Error: old_string/new_string 必须是字符串"
    if old == "":
        return "Error: old_string 不能为空"
    if old == new:
        return "Error: old_string 与 new_string 相同，无变更"

    p = Path(path_str)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        return f"Error: 文件不存在或不是文件 {p}"

    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        return f"Error: 文件中未找到 old_string"
    if count > 1 and not replace_all:
        return (
            f"Error: old_string 出现 {count} 次，需要扩大上下文使其唯一，"
            f"或将 replace_all 设为 true"
        )

    new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        p.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return f"Error: 写回失败：{exc}"
    return f"已替换 {count if replace_all else 1} 处 → {p}"


TOOL = Tool(
    name="edit",
    description=(
        "对已有文件做精确字符串替换。old_string 必须唯一（或开 replace_all）。"
        "用于小范围改动；整文件覆盖请用 write。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径"},
            "old_string": {"type": "string", "description": "要被替换的原文"},
            "new_string": {"type": "string", "description": "替换后的内容"},
            "replace_all": {
                "type": "boolean",
                "description": "若为 true 则替换全部出现，默认 false",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
    handler=_run,
)
