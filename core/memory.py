"""跨会话记忆：项目隔离存储 + MEMORY.md 索引。

每个项目目录对应 ~/.practiceagent/memory/<name>-<sha1[:12]>/ 下：
  - MEMORY.md       : 索引文件（一行一条 [title](file.md)）
  - <slug>.md       : 单条记忆（带 YAML frontmatter）

记忆写入后立即持久化到磁盘；读取时解析 frontmatter。
提供 write / read / list / delete 四个操作，以及生成 system prompt 片段的工具。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path


_DATA_DIR = Path.home() / ".practiceagent"
_MEMORY_ROOT = _DATA_DIR / "memory"


# ── 路径帮助 ───────────────────────────────────────────────────────────────────

def _project_memory_dir(cwd: Path) -> Path:
    digest = sha1(str(cwd.resolve()).encode()).hexdigest()[:12]
    d = _MEMORY_ROOT / f"{cwd.resolve().name}-{digest}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _memory_entrypoint(cwd: Path) -> Path:
    return _project_memory_dir(cwd) / "MEMORY.md"


_RESERVED = frozenset({"memory"})


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.strip().lower()).strip("_")
    if not slug or slug in _RESERVED:
        slug = "mem_" + re.sub(r"\s+", "_", title.strip())[:20] or "mem_entry"
    return slug


# ── 数据类型 ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MemoryHeader:
    path: Path
    title: str
    description: str
    memory_type: str
    body: str


# ── 解析 ───────────────────────────────────────────────────────────────────────

def _parse(path: Path, text: str) -> MemoryHeader:
    lines = text.splitlines()
    title = path.stem
    description = ""
    memory_type = ""
    body_start = 0

    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                for fm_line in lines[1:i]:
                    key, _, value = fm_line.partition(":")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if not value:
                        continue
                    if key == "name":
                        title = value
                    elif key == "description":
                        description = value
                    elif key == "type":
                        memory_type = value
                body_start = i + 1
                break

    body = "\n".join(lines[body_start:]).strip()
    if not description:
        for line in lines[body_start:]:
            s = line.strip()
            if s and not s.startswith("#"):
                description = s[:200]
                break

    return MemoryHeader(
        path=path,
        title=title,
        description=description,
        memory_type=memory_type,
        body=body,
    )


# ── CRUD ───────────────────────────────────────────────────────────────────────

def _sanitize_inline(value: str) -> str:
    """去除可破坏 YAML 单行的换行符。"""
    return value.replace("\r", " ").replace("\n", " ")


def _safe_child(mem_dir: Path, name: str) -> Path | None:
    """将 name 解析为 mem_dir 的子路径，若逃出则返回 None（防路径穿越）。"""
    candidate = (mem_dir / name).resolve()
    if candidate.parent.resolve() != mem_dir.resolve():
        return None
    return candidate


def write_memory(cwd: Path, title: str, content: str, memory_type: str = "") -> Path:
    """写入一条记忆，返回文件路径。同名会覆盖。"""
    mem_dir = _project_memory_dir(cwd)
    slug_name = _slug(title)
    path = mem_dir / f"{slug_name}.md"

    # 防止覆盖索引文件（Windows 大小写不敏感）
    if path.resolve() == _memory_entrypoint(cwd).resolve():
        slug_name = f"mem_{slug_name}"
        path = mem_dir / f"{slug_name}.md"

    safe_title = _sanitize_inline(title)
    safe_desc = _sanitize_inline(content[:100].splitlines()[0] if content else "")
    safe_type = _sanitize_inline(memory_type)
    fm_type = f"\ntype: {safe_type}" if safe_type else ""
    file_content = (
        f"---\nname: {safe_title}\ndescription: {safe_desc}{fm_type}\n---\n\n"
        + content.strip()
        + "\n"
    )
    path.write_text(file_content, encoding="utf-8")

    _update_index(cwd, safe_title, path)
    return path


def read_memory(cwd: Path, name: str) -> MemoryHeader | None:
    """按 title slug 或文件名读取一条记忆。"""
    mem_dir = _project_memory_dir(cwd)
    slug_name = _slug(name)
    # 优先按 slug 查
    slug_path = mem_dir / f"{slug_name}.md"
    if slug_path.exists():
        return _parse(slug_path, slug_path.read_text(encoding="utf-8"))
    # 按原始文件名查（须在 mem_dir 内）
    fname = name if name.endswith(".md") else f"{name}.md"
    candidate = _safe_child(mem_dir, fname)
    if candidate is not None and candidate.exists():
        return _parse(candidate, candidate.read_text(encoding="utf-8"))
    return None


def list_memories(cwd: Path) -> list[MemoryHeader]:
    """列出所有记忆，按修改时间倒序。"""
    mem_dir = _project_memory_dir(cwd)
    headers: list[MemoryHeader] = []
    for path in sorted(mem_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.name == "MEMORY.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        headers.append(_parse(path, text))
    return headers


def delete_memory(cwd: Path, name: str) -> bool:
    """删除一条记忆，从索引中移除。"""
    mem_dir = _project_memory_dir(cwd)
    slug_name = _slug(name)
    path = mem_dir / f"{slug_name}.md"
    if not path.exists():
        # 按原始文件名查（须在 mem_dir 内，防路径穿越）
        fname = name if name.endswith(".md") else f"{name}.md"
        candidate = _safe_child(mem_dir, fname)
        if candidate is not None and candidate.exists():
            path = candidate
        else:
            return False
    path.unlink()
    _remove_from_index(cwd, path)
    return True


# ── 索引维护 ────────────────────────────────────────────────────────────────────

def _update_index(cwd: Path, title: str, path: Path) -> None:
    entrypoint = _memory_entrypoint(cwd)
    existing = entrypoint.read_text(encoding="utf-8") if entrypoint.exists() else "# Memory\n"
    # 精确匹配文件名，避免子串误判
    if not any(f"]({path.name})" in line for line in existing.splitlines()):
        existing = existing.rstrip() + f"\n- [{title}]({path.name})\n"
        entrypoint.write_text(existing, encoding="utf-8")


def _remove_from_index(cwd: Path, path: Path) -> None:
    entrypoint = _memory_entrypoint(cwd)
    if not entrypoint.exists():
        return
    lines = [l for l in entrypoint.read_text(encoding="utf-8").splitlines() if path.name not in l]
    entrypoint.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ── system prompt 片段 ──────────────────────────────────────────────────────────

def build_memory_section(cwd: Path, max_entries: int = 20) -> str:
    """生成注入 system prompt 的记忆段落；无记忆时返回空串。"""
    headers = list_memories(cwd)[:max_entries]
    if not headers:
        return ""
    lines = ["## 跨会话记忆（请优先遵守）"]
    for h in headers:
        tag = f"[{h.memory_type}] " if h.memory_type else ""
        lines.append(f"- **{h.title}** ({tag}{h.description})")
        if h.body:
            for line in h.body.splitlines()[:5]:
                lines.append(f"  {line}")
    return "\n".join(lines)
