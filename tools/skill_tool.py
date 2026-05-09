"""skill 工具：模型按需加载某个 skill 的完整内容。

工厂函数：传入一个 SkillLibrary，返回闭包了 library 的 Tool 实例。
这样不同 stage 可以各自指向不同的 skill 目录。
"""

from __future__ import annotations

from core.skill_loader import SkillLibrary
from core.tool_registry import Tool


def make_skill_tool(library: SkillLibrary) -> Tool:
    def _run(args: dict) -> str:
        name = str(args.get("name", "")).strip()
        if not name:
            return "Error: 缺少 name 参数"
        body = library.load(name)
        if body is None:
            return f"Error: 未知 skill {name!r}（可用: {', '.join(library.names())}）"
        return body

    available = ", ".join(library.names()) or "(无)"
    return Tool(
        name="skill",
        description=(
            f"按名称加载一个技能（skill）的完整指引文档。"
            f"系统已暴露 (name, description) 列表，调用此工具拿到详细 body。"
            f"当前可用：{available}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "skill 名称"},
            },
            "required": ["name"],
        },
        handler=_run,
    )
