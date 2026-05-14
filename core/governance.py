"""治理层：把权限检查 + hook 串到 ToolRegistry.dispatch 之上。

调用顺序（核心约束）：
  1. PreToolUse hooks（可改 args、可直接 deny）
  2. PermissionChecker.check：allow → 继续；deny → 直接返回错误文本；
     ask → 调 prompt_fn 让用户决定；用户 yes 可选 remember。
  3. ToolRegistry.dispatch 执行真正逻辑
  4. PostToolUse hooks（可改 result）
  5. 把字符串结果返回，由 agent loop 当作 tool_result 回填给模型

之所以单独抽一层而不是塞进 ToolRegistry，是为了让 registry 在 s05 子 agent 那
边可以"裸用"——子 agent 通常被信任、不需要再走一遍 governance。这里 governance
只装到主 loop 的 dispatch 上。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.hooks import HookContext, HookEvent, HookExecutor
from core.permissions import PermissionChecker, PermissionDecision
from core.tool_registry import ToolRegistry

_VALID_OVERRIDE_ACTIONS: frozenset[str] = frozenset({"allow", "deny", "ask"})


PromptFn = Callable[[str], bool]


@dataclass
class Governance:
    """聚合权限、hook、prompt 实现。dispatch 是入口。"""

    registry: ToolRegistry
    permissions: PermissionChecker
    hooks: HookExecutor
    prompt: PromptFn

    def dispatch(self, name: str, args: dict) -> str:
        """治理版 dispatch：返回字符串结果，与 ToolRegistry.dispatch 同型。"""

        # 1) Pre hooks（可改 args / 直接拒）
        pre_ctx = HookContext(event=HookEvent.PRE_TOOL_USE, tool_name=name, args=dict(args))
        pre_ctx = self.hooks.run(pre_ctx)
        if pre_ctx.deny:
            return f"Error: {pre_ctx.deny_reason or 'hook 拒绝执行'}"
        args = pre_ctx.args  # 用 hook 修改后的 args

        # 2) Permission check（hook 可通过 extra["permission_override"] 覆盖默认决策）
        override = pre_ctx.extra.get("permission_override")
        if override is not None:
            if override not in _VALID_OVERRIDE_ACTIONS:
                return f"Error: 无效的 permission_override 值 {override!r}，必须为 allow/deny/ask"
            reason = pre_ctx.extra.get("permission_override_reason", "hook override")
            decision = PermissionDecision(override, reason)
        else:
            decision = self.permissions.check(name, args)
        if decision.action == "deny":
            return f"Error: 权限拒绝 — {decision.reason}"
        if decision.action == "ask":
            ok = self.prompt(decision.reason)
            if not ok:
                return f"Error: 用户拒绝执行（{decision.reason}）"
            self.permissions.remember_allow(name, args)

        # 3) 真正执行
        result = self.registry.dispatch(name, args)

        # 4) Post hooks（可改 result / 标记为 deny 但已执行——仅作为审计标志）
        post_ctx = HookContext(
            event=HookEvent.POST_TOOL_USE,
            tool_name=name,
            args=args,
            result=result,
        )
        post_ctx = self.hooks.run(post_ctx)
        return post_ctx.result if post_ctx.result is not None else result
