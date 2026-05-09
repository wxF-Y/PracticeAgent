"""Skill 加载器：把 markdown 知识做成"按需加载"的资源。

约定：每个 skill 一个目录
    skills/<name>/SKILL.md

SKILL.md 起手 frontmatter（YAML-like，简单 KV）：
    ---
    name: write-readme
    description: 给一个目录写一份贴合内容的 README.md
    ---
    # 写 README 的步骤
    1. ...

模型只看到 (name, description) 列表注入 system prompt；
真正使用时调 skill(name="...") 工具拿 body。这样：
  - system prompt 不被几十个 skill 撑爆
  - 加载是模型主动决策（按需），不是预加载
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """简易 YAML frontmatter 解析（仅支持顶层 key: value）。"""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    # 容忍 \r\n
    norm = text.replace("\r\n", "\n")
    end = norm.find("\n---\n", 4)
    if end < 0:
        return {}, text
    front = norm[4:end]
    body = norm[end + 5:]
    meta: dict[str, str] = {}
    for line in front.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    return meta, body


class SkillLibrary:
    """按目录扫描 SKILL.md，提供按名加载。"""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self._skills: dict[str, Skill] = {}
        if base_dir.is_dir():
            self._scan(base_dir)

    def _scan(self, base: Path) -> None:
        for skill_md in base.glob("*/SKILL.md"):
            try:
                raw = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name") or skill_md.parent.name
            description = meta.get("description") or "(no description)"
            if name in self._skills:
                # 同名冲突，跳过后扫到的（保留先发现的）
                continue
            self._skills[name] = Skill(
                name=name, description=description, body=body.strip(), path=skill_md
            )

    def names(self) -> list[str]:
        return sorted(self._skills)

    def list_brief(self) -> list[tuple[str, str]]:
        """返回 [(name, description), ...]，用于 system prompt 注入。"""
        return [(s.name, s.description) for s in sorted(self._skills.values(), key=lambda x: x.name)]

    def load(self, name: str) -> str | None:
        """返回 skill 的 body；不存在返回 None。"""
        s = self._skills.get(name)
        return s.body if s else None

    def __len__(self) -> int:
        return len(self._skills)
