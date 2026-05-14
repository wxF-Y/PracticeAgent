"""权限检查：在工具真正执行前给每次调用打一个 allow/deny/ask 决策。

s07 把"危险动作要确认"这件事从 bash_tool 的黑名单升级成统一治理：

  - 任何工具调用都过一遍 PermissionChecker
  - 决策结果 (allow / deny / ask) 与执行解耦：执行端拿到 ask 时再去问用户
  - 用户回答"是"后，同一会话内可缓存为 always allow，避免狂刷确认

规则按"模式 + 工具类型"两个维度展开：

  PermissionMode.DEFAULT       常规：读类直放；写类落到 cwd 内放过、否则 ask；
                                bash 黑名单拦截、软危险词触发 ask。
  PermissionMode.ACCEPT_EDITS  写类全部直放（信任 agent 自己改 cwd 内文件），
                                bash 仍按 DEFAULT 规则。
  PermissionMode.PLAN          只读模式：写类与 bash 全部 deny，agent 只能侦察。
  PermissionMode.BYPASS        开发/演示用：全部直放，不做任何检查。

PermissionChecker 不直接和 IO 打交道；prompt 由 governance 层注入（CLI 实现一份、
测试塞一份固定答案），保证它本身可纯单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal, NamedTuple


class PermissionMode(str, Enum):
    """权限运行模式。值用字符串方便从 CLI 参数解析。"""

    DEFAULT = "default"
    ACCEPT_EDITS = "accept-edits"
    PLAN = "plan"
    BYPASS = "bypass"


Action = Literal["allow", "deny", "ask"]


class PermissionDecision(NamedTuple):
    """单次工具调用的判定。reason 既用于日志也用于 ask 时给用户看的提示。"""

    action: Action
    reason: str


# bash 命令中包含这些字串 → 直接 deny（兜底，模式无关）
HARD_DENY_BASH: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "shutdown",
    "reboot",
    "halt ",
    "poweroff",
    ":(){ :|:& };:",  # fork bomb
    "dd if=",
    "> /dev/sda",
)

# bash 命令中包含这些 → ask（DEFAULT 模式）。可写但需用户点头。
SOFT_ASK_BASH: tuple[str, ...] = (
    "rm ",
    "rm -",
    "mv ",
    "git push",
    "git reset --hard",
    "git clean",
    "curl ",
    "wget ",
    "pip install",
    "npm install",
    "pnpm install",
    "uv add",
    "sudo ",
    "chmod ",
    "chown ",
    "kill ",
    "pkill",
)

# 写到匹配以下 glob 模式的路径一律 ask，即便在 cwd 内。
# 使用 fnmatch 在规范化的 POSIX 路径上匹配，防止 ../../.ssh/ 等路径遍历绕过。
SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    "*/.env",
    "*/.env.*",
    "*/.ssh/*",
    "*/.aws/*",
    "*/.gnupg/*",
    "*/.kube/config",
    "*/.git/*",
    "*/id_rsa*",
    "*/id_ed25519*",
    "*/.netrc",
    "*/credentials",
)


@dataclass
class PermissionChecker:
    """根据当前 mode + 配置，给一次工具调用打决策。"""

    mode: PermissionMode = PermissionMode.DEFAULT
    cwd: Path = field(default_factory=Path.cwd)
    hard_deny_bash: tuple[str, ...] = HARD_DENY_BASH
    soft_ask_bash: tuple[str, ...] = SOFT_ASK_BASH
    sensitive_patterns: tuple[str, ...] = SENSITIVE_PATH_PATTERNS
    # 会话内"已经点过 yes"的 (tool, key) 集合：再次遇到同样的调用直接放行
    always_allow: set[tuple[str, str]] = field(default_factory=set)

    # ---- 主入口 ----

    def check(self, name: str, args: dict) -> PermissionDecision:
        if self.mode is PermissionMode.BYPASS:
            return PermissionDecision("allow", "bypass mode")

        key = self._cache_key(name, args)
        if key is not None and (name, key) in self.always_allow:
            return PermissionDecision("allow", "user previously approved")

        if name in {"read", "glob", "grep", "skill", "agent"}:
            return PermissionDecision("allow", f"{name} 是只读/探查类工具")

        if name in {"write", "edit"}:
            return self._check_write(name, args)

        if name == "bash":
            return self._check_bash(args)

        # 未识别的工具默认要问一下
        return PermissionDecision("ask", f"未登记规则的工具 {name!r}")

    def remember_allow(self, name: str, args: dict) -> None:
        """记住"这种调用以后别再问了"。key 与 _cache_key 保持一致。"""
        key = self._cache_key(name, args)
        if key is not None:
            self.always_allow.add((name, key))

    # ---- 内部规则 ----

    def _check_write(self, name: str, args: dict) -> PermissionDecision:
        path_str = str(args.get("path", "")).strip()
        if not path_str:
            return PermissionDecision("deny", f"{name} 缺少 path 参数")
        p = Path(path_str)
        if not p.is_absolute():
            p = self.cwd / p
        p_norm = p.resolve() if p.exists() else p

        # PLAN 模式：写类全锁
        if self.mode is PermissionMode.PLAN:
            return PermissionDecision("deny", f"PLAN 模式禁止 {name} 到 {p_norm}")

        # 敏感路径 glob 匹配（规范化为 POSIX 路径后用 fnmatch）
        posix_norm = p_norm.as_posix()
        for pat in self.sensitive_patterns:
            if fnmatch(posix_norm, pat):
                return PermissionDecision("ask", f"敏感路径写入：{p_norm}（命中 {pat!r}）")

        # cwd 外要 ask
        if not _is_within(p_norm, self.cwd):
            return PermissionDecision("ask", f"目标在工作目录外：{p_norm}")

        if self.mode is PermissionMode.ACCEPT_EDITS:
            return PermissionDecision("allow", "ACCEPT_EDITS 模式直放 cwd 内写入")
        return PermissionDecision("allow", f"cwd 内安全写入 {p_norm}")

    def _check_bash(self, args: dict) -> PermissionDecision:
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return PermissionDecision("deny", "bash 缺少 command 参数")

        low = cmd.lower()
        for bad in self.hard_deny_bash:
            if bad in low:
                return PermissionDecision("deny", f"命中硬黑名单：{bad!r}")

        if self.mode is PermissionMode.PLAN:
            return PermissionDecision("deny", "PLAN 模式禁止任何 bash")

        for soft in self.soft_ask_bash:
            if soft in low:
                return PermissionDecision("ask", f"命中软危险词 {soft!r}：{cmd[:80]}")

        return PermissionDecision("allow", "bash 命令未命中风险词")

    def _cache_key(self, name: str, args: dict) -> str | None:
        if name in {"write", "edit"}:
            p = str(args.get("path", "")).strip()
            return p or None
        if name == "bash":
            # 同一条命令再次出现就别再问。截断防止超长。
            cmd = str(args.get("command", "")).strip()
            return cmd[:200] or None
        return None


def _is_within(path: Path, root: Path) -> bool:
    """path 是否在 root 之内（含等于）。两个都尽量 resolve。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def cli_prompt(reason: str) -> bool:
    """终端版 ask 实现：默认 N，回车即拒。"""
    try:
        ans = input(f"\033[33m[permission] {reason} — 允许? [y/N] \033[0m").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in {"y", "yes"}
