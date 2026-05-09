"""工具注册表：把工具集中管理，agent loop 通过它喂 schema 给模型、按名称分发执行。

Tool 抽象：
    name + description + JSON Schema + handler(dict) -> str

Registry 三个职责：
    1) register(tool)        ：登记
    2) list_schemas()         ：返回模型看到的 [{name, description, input_schema}, ...]
    3) dispatch(name, args)   ：按名称执行，捕获异常返回错误文本（不抛）

s07 会在 dispatch 前后插入权限检查 / 钩子。s02 保持最简版本。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


ToolHandler = Callable[[dict], str]


@dataclass(frozen=True)
class Tool:
    """单个工具的不可变描述符。"""

    name: str
    description: str
    input_schema: dict
    handler: ToolHandler


class ToolRegistry:
    """工具注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具 {tool.name!r} 已存在")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def list_schemas(self) -> list[dict]:
        """返回喂给模型的工具定义列表（不含 handler）。"""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def dispatch(self, name: str, args: dict) -> str:
        """按名称执行工具。捕获所有异常，返回字符串结果。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: 未知工具 {name!r}（可用: {', '.join(self._tools)}）"
        try:
            return tool.handler(args)
        except Exception as exc:  # noqa: BLE001
            # 工具内部错误也作为字符串回填给模型，让它自己决定下一步
            return f"Error: 工具 {name} 执行失败：{exc}"
