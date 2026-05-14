"""Unit tests for core/permissions.py — pure logic, no API."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core.permissions import PermissionChecker, PermissionMode


@pytest.fixture
def cwd(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def checker(cwd: Path) -> PermissionChecker:
    return PermissionChecker(mode=PermissionMode.DEFAULT, cwd=cwd)


@pytest.mark.unit
def test_read_tools_always_allow(checker: PermissionChecker) -> None:
    for name in ("read", "glob", "grep", "skill", "agent"):
        d = checker.check(name, {"path": "anywhere"})
        assert d.action == "allow", (name, d)


@pytest.mark.unit
def test_write_inside_cwd_allowed(checker: PermissionChecker, cwd: Path) -> None:
    d = checker.check("write", {"path": str(cwd / "a.txt"), "content": "hi"})
    assert d.action == "allow"


@pytest.mark.unit
def test_write_outside_cwd_asks(checker: PermissionChecker, tmp_path: Path) -> None:
    other = tmp_path.parent / "elsewhere"
    d = checker.check("write", {"path": str(other / "x.txt"), "content": "x"})
    assert d.action == "ask"
    assert "工作目录外" in d.reason


@pytest.mark.unit
def test_sensitive_path_asks_even_in_cwd(checker: PermissionChecker, cwd: Path) -> None:
    d = checker.check("edit", {"path": str(cwd / ".env"), "old_string": "a", "new_string": "b"})
    assert d.action == "ask"
    assert ".env" in d.reason


@pytest.mark.unit
def test_bash_hard_deny() -> None:
    chk = PermissionChecker(mode=PermissionMode.DEFAULT, cwd=Path.cwd())
    d = chk.check("bash", {"command": "rm -rf /"})
    assert d.action == "deny"


@pytest.mark.unit
def test_bash_soft_ask() -> None:
    chk = PermissionChecker(mode=PermissionMode.DEFAULT, cwd=Path.cwd())
    d = chk.check("bash", {"command": "git push origin main"})
    assert d.action == "ask"


@pytest.mark.unit
def test_bash_plain_allow() -> None:
    chk = PermissionChecker(mode=PermissionMode.DEFAULT, cwd=Path.cwd())
    d = chk.check("bash", {"command": "ls -la"})
    assert d.action == "allow"


@pytest.mark.unit
def test_plan_mode_blocks_writes_and_bash(cwd: Path) -> None:
    chk = PermissionChecker(mode=PermissionMode.PLAN, cwd=cwd)
    assert chk.check("write", {"path": str(cwd / "a.txt"), "content": "x"}).action == "deny"
    assert chk.check("bash", {"command": "ls"}).action == "deny"
    assert chk.check("read", {"path": "x"}).action == "allow"


@pytest.mark.unit
def test_accept_edits_skips_ask_for_cwd_writes(cwd: Path) -> None:
    chk = PermissionChecker(mode=PermissionMode.ACCEPT_EDITS, cwd=cwd)
    d = chk.check("write", {"path": str(cwd / "deep/nested.txt"), "content": "y"})
    assert d.action == "allow"


@pytest.mark.unit
def test_bypass_mode_allows_everything() -> None:
    chk = PermissionChecker(mode=PermissionMode.BYPASS, cwd=Path.cwd())
    assert chk.check("bash", {"command": "rm -rf /"}).action == "allow"
    assert chk.check("write", {"path": "/etc/passwd", "content": "x"}).action == "allow"


@pytest.mark.unit
def test_remember_allow_skips_future_ask(tmp_path: Path) -> None:
    chk = PermissionChecker(mode=PermissionMode.DEFAULT, cwd=tmp_path)
    cmd = {"command": "git push origin main"}
    assert chk.check("bash", cmd).action == "ask"
    chk.remember_allow("bash", cmd)
    assert chk.check("bash", cmd).action == "allow"


@pytest.mark.unit
def test_unknown_tool_asks() -> None:
    chk = PermissionChecker(mode=PermissionMode.DEFAULT, cwd=Path.cwd())
    d = chk.check("brand_new_tool", {"x": 1})
    assert d.action == "ask"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
