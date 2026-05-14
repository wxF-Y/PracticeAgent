"""Unit tests for core/governance.py — permission_override integration."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from core.governance import Governance
from core.hooks import HookContext, HookEvent, HookExecutor, make_permission_override_hook
from core.permissions import PermissionChecker, PermissionMode
from core.tool_registry import Tool, ToolRegistry


# ---- minimal fixtures ----

def _make_registry(tool_name: str = "write") -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(
        name=tool_name,
        description="stub",
        input_schema={"type": "object", "properties": {}},
        handler=lambda args: "ok",
    ))
    return reg


def _always_yes(_reason: str) -> bool:
    return True


def _always_no(_reason: str) -> bool:
    return False


def _make_gov(
    mode: PermissionMode = PermissionMode.DEFAULT,
    prompt=_always_yes,
    tool_name: str = "write",
    extra_hooks: list | None = None,
    cwd: Path | None = None,
) -> Governance:
    registry = _make_registry(tool_name)
    cwd = cwd or Path.cwd()
    checker = PermissionChecker(mode=mode, cwd=cwd)
    executor = HookExecutor()
    for hook in (extra_hooks or []):
        executor.register(HookEvent.PRE_TOOL_USE, hook)
    return Governance(registry=registry, permissions=checker, hooks=executor, prompt=prompt)


# ---- tests ----

@pytest.mark.unit
def test_override_allow_bypasses_default_ask(tmp_path: Path) -> None:
    """hook override=allow 让 cwd 外的写入直接通过，无需用户确认。"""
    outside = tmp_path.parent / "outside" / "x.txt"
    override_hook = make_permission_override_hook([("write", "allow")])
    gov = _make_gov(
        tool_name="write",
        extra_hooks=[override_hook],
        prompt=_always_no,   # 若走到 ask 就会拒绝
        cwd=tmp_path,
    )
    result = gov.dispatch("write", {"path": str(outside), "content": "hi"})
    assert result == "ok"


@pytest.mark.unit
def test_override_deny_blocks_normally_allowed_tool(tmp_path: Path) -> None:
    """hook override=deny 阻断 cwd 内本应放行的写入。"""
    inside = tmp_path / "a.txt"
    override_hook = make_permission_override_hook([("write", "deny")])
    gov = _make_gov(
        tool_name="write",
        extra_hooks=[override_hook],
        prompt=_always_yes,
        cwd=tmp_path,
    )
    result = gov.dispatch("write", {"path": str(inside), "content": "hi"})
    assert "权限拒绝" in result


@pytest.mark.unit
def test_override_ask_prompts_user(tmp_path: Path) -> None:
    """hook override=ask 强制走到 prompt，用户 yes → 执行成功。"""
    inside = tmp_path / "b.txt"
    override_hook = make_permission_override_hook([("write", "ask")])
    gov = _make_gov(
        tool_name="write",
        extra_hooks=[override_hook],
        prompt=_always_yes,
        cwd=tmp_path,
    )
    result = gov.dispatch("write", {"path": str(inside), "content": "x"})
    assert result == "ok"


@pytest.mark.unit
def test_override_ask_user_no_blocks(tmp_path: Path) -> None:
    inside = tmp_path / "c.txt"
    override_hook = make_permission_override_hook([("write", "ask")])
    gov = _make_gov(
        tool_name="write",
        extra_hooks=[override_hook],
        prompt=_always_no,
        cwd=tmp_path,
    )
    result = gov.dispatch("write", {"path": str(inside), "content": "x"})
    assert "用户拒绝" in result


@pytest.mark.unit
def test_no_override_falls_through_to_checker(tmp_path: Path) -> None:
    """无 override hook 时，走原始 PermissionChecker 逻辑（PLAN 模式 deny）。"""
    gov = _make_gov(mode=PermissionMode.PLAN, cwd=tmp_path, tool_name="write")
    result = gov.dispatch("write", {"path": str(tmp_path / "x.txt"), "content": "y"})
    assert "权限拒绝" in result


@pytest.mark.unit
def test_glob_pattern_targets_specific_tool(tmp_path: Path) -> None:
    """glob 仅命中 write，bash 工具不受影响（bash 未在 registry 里，触发 KeyError 返回 Error）。"""
    override_hook = make_permission_override_hook([("write", "deny")])
    reg = ToolRegistry()
    reg.register(Tool("write", "stub", {"type": "object", "properties": {}}, lambda a: "ok"))
    reg.register(Tool("bash", "stub", {"type": "object", "properties": {}}, lambda a: "executed"))
    checker = PermissionChecker(mode=PermissionMode.DEFAULT, cwd=tmp_path)
    executor = HookExecutor()
    executor.register(HookEvent.PRE_TOOL_USE, override_hook)
    gov = Governance(registry=reg, permissions=checker, hooks=executor, prompt=_always_yes)

    write_result = gov.dispatch("write", {"path": str(tmp_path / "f.txt"), "content": "hi"})
    bash_result = gov.dispatch("bash", {"command": "ls"})

    assert "权限拒绝" in write_result
    assert bash_result == "executed"


@pytest.mark.unit
def test_invalid_override_value_is_rejected(tmp_path: Path) -> None:
    """非法 override 字符串必须被拒绝，不能悄悄 allow。"""
    def bad_hook(ctx: HookContext) -> HookContext:
        ctx.extra["permission_override"] = "ALLOW"  # 大写 / 错误值
        return ctx

    reg = ToolRegistry()
    reg.register(Tool("write", "stub", {"type": "object", "properties": {}}, lambda a: "ok"))
    checker = PermissionChecker(mode=PermissionMode.DEFAULT, cwd=tmp_path)
    executor = HookExecutor()
    executor.register(HookEvent.PRE_TOOL_USE, bad_hook)
    gov = Governance(registry=reg, permissions=checker, hooks=executor, prompt=_always_yes)

    result = gov.dispatch("write", {"path": str(tmp_path / "x.txt"), "content": "y"})
    assert "Error" in result
    assert "permission_override" in result or "无效" in result


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
