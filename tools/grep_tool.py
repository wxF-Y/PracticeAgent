"""grep 工具：用 Python 正则在文件内容里搜索。

默认输出模式 files_with_matches（只返回文件路径）；
output_mode=content 时返回带行号的匹配行（含截断）。
"""

from __future__ import annotations

import re
from pathlib import Path

from core.tool_registry import Tool


_MAX_FILES = 500
_MAX_LINES = 200
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


def _iter_files(base: Path, glob: str | None) -> list[Path]:
    if glob:
        return [p for p in base.glob(glob) if p.is_file()]
    out: list[Path] = []
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        out.append(p)
    return out


def _run(args: dict) -> str:
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        return "Error: 缺少 pattern 参数"
    base = Path(str(args.get("path", "")).strip() or ".")
    if not base.is_absolute():
        base = Path.cwd() / base
    glob = args.get("glob") or None
    output_mode = str(args.get("output_mode", "files_with_matches")).strip()

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: 正则编译失败：{exc}"

    files = _iter_files(base, glob if isinstance(glob, str) else None)[:_MAX_FILES]
    matched_files: list[Path] = []
    matched_lines: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = [(i, ln) for i, ln in enumerate(text.splitlines(), 1) if regex.search(ln)]
        if not hits:
            continue
        matched_files.append(f)
        if output_mode == "content":
            for i, ln in hits[:50]:  # 单文件限 50 行
                matched_lines.append(f"{f}:{i}: {ln}")
                if len(matched_lines) >= _MAX_LINES:
                    break
        if len(matched_lines) >= _MAX_LINES:
            break

    if output_mode == "content":
        if not matched_lines:
            return f"(无匹配 pattern={pattern})"
        return "\n".join(matched_lines)

    if not matched_files:
        return f"(无匹配文件 pattern={pattern})"
    return "\n".join(str(p) for p in matched_files)


TOOL = Tool(
    name="grep",
    description=(
        "在文件内容中用 Python 正则搜索。output_mode=files_with_matches（默认）"
        "只返文件路径；output_mode=content 返带行号的匹配行。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则表达式"},
            "path": {"type": "string", "description": "起始目录，默认当前目录"},
            "glob": {"type": "string", "description": "可选 glob 过滤（如 *.py）"},
            "output_mode": {
                "type": "string",
                "enum": ["files_with_matches", "content"],
                "description": "默认 files_with_matches",
            },
        },
        "required": ["pattern"],
    },
    handler=_run,
)
