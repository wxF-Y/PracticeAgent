"""core/memory.py 单元测试。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.memory import (
    build_memory_section,
    delete_memory,
    list_memories,
    read_memory,
    write_memory,
)


@pytest.fixture()
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


def test_write_and_read(tmpdir):
    path = write_memory(tmpdir, "pnpm preference", "Always use pnpm instead of npm.", "feedback")
    assert path.exists()
    header = read_memory(tmpdir, "pnpm preference")
    assert header is not None
    assert header.title == "pnpm preference"
    assert "pnpm" in header.body
    assert header.memory_type == "feedback"


def test_list_returns_all(tmpdir):
    write_memory(tmpdir, "mem A", "content A")
    write_memory(tmpdir, "mem B", "content B")
    headers = list_memories(tmpdir)
    titles = {h.title for h in headers}
    assert "mem A" in titles
    assert "mem B" in titles


def test_delete_removes_file(tmpdir):
    write_memory(tmpdir, "to delete", "bye bye")
    assert delete_memory(tmpdir, "to delete")
    headers = list_memories(tmpdir)
    assert all(h.title != "to delete" for h in headers)


def test_delete_returns_false_when_not_found(tmpdir):
    assert not delete_memory(tmpdir, "nonexistent")


def test_index_updated_on_write(tmpdir):
    write_memory(tmpdir, "index test", "some content")
    from core.memory import _memory_entrypoint
    idx = _memory_entrypoint(tmpdir)
    assert idx.exists()
    text = idx.read_text(encoding="utf-8")
    assert "index test" in text


def test_index_cleaned_on_delete(tmpdir):
    write_memory(tmpdir, "clean me", "content")
    delete_memory(tmpdir, "clean me")
    from core.memory import _memory_entrypoint
    idx = _memory_entrypoint(tmpdir)
    if idx.exists():
        assert "clean me" not in idx.read_text(encoding="utf-8")


def test_build_memory_section_empty(tmpdir):
    section = build_memory_section(tmpdir)
    assert section == ""


def test_build_memory_section_with_entries(tmpdir):
    write_memory(tmpdir, "pnpm", "use pnpm not npm", "feedback")
    section = build_memory_section(tmpdir)
    assert "pnpm" in section
    assert "跨会话记忆" in section


def test_overwrite_same_title(tmpdir):
    write_memory(tmpdir, "key", "old content")
    write_memory(tmpdir, "key", "new content")
    header = read_memory(tmpdir, "key")
    assert header is not None
    assert "new content" in header.body
    # 索引只有一条
    from core.memory import _memory_entrypoint
    idx_text = _memory_entrypoint(tmpdir).read_text(encoding="utf-8")
    assert idx_text.count("key.md") == 1


def test_read_nonexistent_returns_none(tmpdir):
    assert read_memory(tmpdir, "ghost") is None


def test_chinese_title_does_not_overwrite_index(tmpdir):
    """纯中文 title slug 为空时，不能覆盖 MEMORY.md 索引文件。"""
    from core.memory import _memory_entrypoint
    path = write_memory(tmpdir, "测试文件清理规范", "测试完成后需删除测试文件")
    idx = _memory_entrypoint(tmpdir)
    # 内容文件不能是 MEMORY.md
    assert path.resolve() != idx.resolve()
    # 索引文件不含 frontmatter（不被内容覆盖）
    if idx.exists():
        assert "---" not in idx.read_text(encoding="utf-8").splitlines()[0]
    """../etc/passwd 之类的路径不应读取到 mem_dir 外的文件。"""
    result = read_memory(tmpdir, "../outside")
    assert result is None


def test_path_traversal_delete_blocked(tmpdir):
    result = delete_memory(tmpdir, "../outside")
    assert result is False


def test_frontmatter_newline_in_title_sanitized(tmpdir):
    """title 中含换行符时不应产生独立的 YAML key（防注入）。"""
    path = write_memory(tmpdir, "foo\ntype: injected", "body")
    raw = path.read_text(encoding="utf-8")
    fm = raw.split("---")[1]
    # 换行被替换为空格，"type: injected" 不应作为独立行出现
    assert not any(line.strip().startswith("type: injected") for line in fm.splitlines())
