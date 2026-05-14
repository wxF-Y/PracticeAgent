"""Hook 调度：让外部回调在工具调用前后挂钩，可观测、可改输入、可拦截。

设计原则：
  - Hook 是一个普通 Python callable：fn(HookContext) -> HookContext
  - 返回的 ctx 可能：
      · 修改 args（PreToolUse 时改输入，例如把相对路径规整为绝对路径）
      · 设置 ctx.deny=True 直接短路（governance 层不再执行 tool）
      · 把 result 改写（PostToolUse 时截断、脱敏）
  - 任何 hook 抛异常不应让 agent loop 崩；HookExecutor 捕获并打错误日志。

事件：
  - PreToolUse  ：权限通过、即将 dispatch 之前
  - PostToolUse ：dispatch 完成、即将把结果回填给模型之前

s07 内置两个示例：
  - audit_log_hook        ：把每次调用追加到 .practiceagent/audit.log
  - print_invocation_hook ：Pre 打印一行简表，Post 不动
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from core.permissions import Action


class HookEvent(str, Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"


@dataclass
class HookContext:
    """挂在事件总线上传的可变上下文。Hook 通过修改字段影响后续步骤。"""

    event: HookEvent
    tool_name: str
    args: dict
    result: str | None = None  # 仅 Post 有
    deny: bool = False
    deny_reason: str = ""
    # 任意 hook 之间可以塞数据
    extra: dict = field(default_factory=dict)


Hook = Callable[[HookContext], HookContext]


@dataclass
class HookExecutor:
    """按事件分发的 hook 容器。"""

    pre: list[Hook] = field(default_factory=list)
    post: list[Hook] = field(default_factory=list)

    def register(self, event: HookEvent, hook: Hook) -> None:
        bucket = self.pre if event is HookEvent.PRE_TOOL_USE else self.post
        bucket.append(hook)

    def run(self, ctx: HookContext) -> HookContext:
        bucket = self.pre if ctx.event is HookEvent.PRE_TOOL_USE else self.post
        for fn in bucket:
            try:
                out = fn(ctx)
                if isinstance(out, HookContext):
                    ctx = out
            except Exception as exc:  # noqa: BLE001
                # hook 出错不让 agent 崩，仅记到 stderr 风格的输出
                print(
                    f"\033[31m[hook error] {fn.__name__} on {ctx.event.value}: {exc}\033[0m"
                )
                traceback.print_exc()
            if ctx.deny:
                break
        return ctx


# ---- 内置 hook ----


def make_audit_log_hook(log_path: Path) -> Hook:
    """生成把调用按 JSONL 追加到 log_path 的 hook。Pre/Post 都能用。"""

    log_path.parent.mkdir(parents=True, exist_ok=True)

    def _hook(ctx: HookContext) -> HookContext:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": ctx.event.value,
            "tool": ctx.tool_name,
            "args": _shrink(ctx.args),
        }
        if ctx.event is HookEvent.POST_TOOL_USE:
            rec["result_preview"] = (ctx.result or "")[:200]
            rec["denied"] = ctx.deny
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return ctx

    _hook.__name__ = "audit_log_hook"
    return _hook


def print_invocation_hook(ctx: HookContext) -> HookContext:
    """Pre 时打印一行简表，方便人盯 agent 做了什么。"""
    if ctx.event is HookEvent.PRE_TOOL_USE:
        args_preview = _shrink(ctx.args)
        print(f"\033[34m[hook] → {ctx.tool_name}({args_preview})\033[0m")
    return ctx


def block_write_outside_cwd_hook(cwd: Path) -> Hook:
    """示例硬规则 hook：写到 cwd 外直接 deny（与 PermissionChecker 形成双保险）。"""

    cwd_resolved = cwd.resolve()

    def _hook(ctx: HookContext) -> HookContext:
        if ctx.event is not HookEvent.PRE_TOOL_USE:
            return ctx
        if ctx.tool_name not in {"write", "edit"}:
            return ctx
        path_str = str(ctx.args.get("path", "")).strip()
        if not path_str:
            return ctx
        p = Path(path_str)
        if not p.is_absolute():
            p = cwd_resolved / p
        try:
            p.resolve().relative_to(cwd_resolved)
        except ValueError:
            ctx.deny = True
            ctx.deny_reason = f"hook 拒绝：写入路径 {p} 不在 {cwd_resolved} 内"
        return ctx

    _hook.__name__ = "block_write_outside_cwd_hook"
    return _hook


def make_permission_override_hook(rules: list[tuple[str, Action]]) -> Hook:
    """按 (tool_glob_pattern, action) 规则列表注入 permission_override 到 ctx.extra。

    governance 层在 PermissionChecker.check 之前读取此字段，若存在则直接用它覆盖
    权限决策，跳过默认规则。rules 按顺序首匹配即止。

    用例：
      make_permission_override_hook([("read", "allow"), ("bash", "deny")])
      make_permission_override_hook([("write", "ask")])
    """

    def _hook(ctx: HookContext) -> HookContext:
        if ctx.event is not HookEvent.PRE_TOOL_USE:
            return ctx
        for pattern, action in rules:
            if fnmatch(ctx.tool_name, pattern):
                ctx.extra["permission_override"] = action
                ctx.extra["permission_override_reason"] = (
                    f"hook rule {pattern!r} → {action}"
                )
                break
        return ctx

    _hook.__name__ = "permission_override_hook"
    return _hook


def _shrink(args: dict) -> dict:
    """把过长字段截短，避免审计日志爆炸。"""
    out: dict = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:200] + f"...(+{len(v) - 200} chars)"
        else:
            out[k] = v
    return out
