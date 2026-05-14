"""Unit tests for make_permission_override_hook in core/hooks.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core.hooks import HookContext, HookEvent, HookExecutor, make_permission_override_hook


def _pre_ctx(tool: str) -> HookContext:
    return HookContext(event=HookEvent.PRE_TOOL_USE, tool_name=tool, args={})


def _post_ctx(tool: str) -> HookContext:
    return HookContext(event=HookEvent.POST_TOOL_USE, tool_name=tool, args={})


@pytest.mark.unit
def test_exact_match_injects_override() -> None:
    hook = make_permission_override_hook([("bash", "deny")])
    ctx = hook(_pre_ctx("bash"))
    assert ctx.extra["permission_override"] == "deny"
    assert "bash" in ctx.extra["permission_override_reason"]


@pytest.mark.unit
def test_glob_pattern_matches() -> None:
    hook = make_permission_override_hook([("writ*", "allow")])
    ctx = hook(_pre_ctx("write"))
    assert ctx.extra["permission_override"] == "allow"


@pytest.mark.unit
def test_no_match_leaves_extra_clean() -> None:
    hook = make_permission_override_hook([("bash", "deny")])
    ctx = hook(_pre_ctx("read"))
    assert "permission_override" not in ctx.extra


@pytest.mark.unit
def test_first_match_wins() -> None:
    hook = make_permission_override_hook([("bash", "allow"), ("bash", "deny")])
    ctx = hook(_pre_ctx("bash"))
    assert ctx.extra["permission_override"] == "allow"


@pytest.mark.unit
def test_post_event_not_modified() -> None:
    hook = make_permission_override_hook([("bash", "deny")])
    ctx = hook(_post_ctx("bash"))
    assert "permission_override" not in ctx.extra


@pytest.mark.unit
def test_wildcard_matches_all_tools() -> None:
    hook = make_permission_override_hook([("*", "ask")])
    for tool in ("read", "write", "bash", "edit", "unknown"):
        ctx = hook(_pre_ctx(tool))
        assert ctx.extra["permission_override"] == "ask", tool


@pytest.mark.unit
def test_hook_registered_in_executor() -> None:
    executor = HookExecutor()
    hook = make_permission_override_hook([("bash", "deny")])
    executor.register(HookEvent.PRE_TOOL_USE, hook)
    ctx = executor.run(_pre_ctx("bash"))
    assert ctx.extra["permission_override"] == "deny"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
