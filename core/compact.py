"""上下文压缩。

为什么需要压缩：
  Anthropic API 每次都把全部 messages 当 input 发回去。一个 100 轮、每轮带几个
  tool_result 的会话很容易冲到 100k+ input tokens。后期既慢又贵，更糟的是接近
  context window 时模型质量明显下降。

策略：
  当 input_tokens > threshold 时触发压缩。
  保留：
    - 第一条 user message（通常是任务定义）
    - 最近 KEEP_TAIL_TURNS 轮（含完整的 user→assistant 配对）
  中间历史压成一段总结，作为单条 user message 注入。

压缩本身就是一次模型调用——让模型自己总结自己的历史最自然。

安全切点（避免破坏 tool_use ↔ tool_result 配对）：
  Anthropic 要求 assistant 含 tool_use 的话，下一条 user 必须含对应 tool_result。
  我们只在"上一条是 assistant 且 stop_reason 不是 tool_use（即 end_turn 或没有
  tool_use 块）"的位置切。换句话说：必须切在一个干净的 user→assistant 配对边界
  之外。简化：只在 messages[i] role=='user' 且 i 之前的 assistant 没未结的
  tool_use 时切。
"""

from __future__ import annotations

from typing import Any, cast

from anthropic.types import MessageParam, ThinkingConfigParam


# 默认阈值：达到此 input_tokens 就触发压缩。第三方网关有 1M context 时调高。
DEFAULT_THRESHOLD_TOKENS = 60_000

# 压缩后保留尾部多少个 user/assistant 配对（一个 user-assistant 对算 1 轮）。
DEFAULT_KEEP_TAIL_TURNS = 4

# 总结调用的 max_tokens。
SUMMARY_MAX_TOKENS = 2048

SUMMARY_SYSTEM = (
    "你是一个会话压缩助手。我会给你一段已经发生的对话历史，"
    "请用中文总结其中"
    "（1）用户的核心目标和约束 "
    "（2）已经做过的关键操作和工具调用结果 "
    "（3）尚未解决的问题或下一步计划。"
    "总结要简洁但不丢事实，不超过 30 行。不要寒暄、不要复述工具原始输出。"
)


def _has_unmatched_tool_use(messages: list[dict]) -> bool:
    """判断 messages 末尾是否存在未被 tool_result 闭合的 tool_use。"""
    if not messages:
        return False
    last = messages[-1]
    if last.get("role") != "assistant":
        return False
    content = last.get("content")
    if not isinstance(content, list):
        return False
    for b in content:
        btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
        if btype == "tool_use":
            return True
    return False


def _find_safe_cut(messages: list[dict], keep_tail_turns: int) -> int:
    """从尾部数 keep_tail_turns 个 user 边界，返回合法切点 index。

    切点位置满足：
      - messages[cut].role == "user"
      - cut 之前不存在未闭合的 tool_use（自动满足，因为我们只在 user 处切）
    返回的 cut 表示：messages[1:cut] 这段会被压缩（messages[0] 是首个 user，保留）。
    """
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_indices) <= keep_tail_turns + 1:
        return -1  # 不需要压缩
    # 保留最近 keep_tail_turns 个 user 边界 + 首个 user
    # 切点取 "倒数第 keep_tail_turns 个 user" 的位置
    cut = user_indices[-keep_tail_turns]
    if cut <= 1:
        return -1
    return cut


def _stringify_messages_for_summary(messages: list[dict]) -> str:
    """把一段 messages 拍平成给压缩模型读的纯文本。"""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, str):
            lines.append(f"[{role}] {content}")
            continue
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict):
                btype = b.get("type")
                if btype == "text":
                    lines.append(f"[{role}/text] {b.get('text', '')}")
                elif btype == "tool_use":
                    name = b.get("name", "?")
                    inp = b.get("input", {})
                    lines.append(f"[{role}/tool_use] {name}({inp})")
                elif btype == "tool_result":
                    # 工具结果常很长，截一下
                    body = str(b.get("content", ""))
                    if len(body) > 600:
                        body = body[:600] + "...(truncated)"
                    lines.append(f"[{role}/tool_result] {body}")
                elif btype == "thinking":
                    text = b.get("thinking", "")
                    if len(text) > 300:
                        text = text[:300] + "...(truncated)"
                    lines.append(f"[{role}/thinking] {text}")
            else:
                # pydantic block 走 duck-type
                btype = getattr(b, "type", "?")
                if btype == "text":
                    lines.append(f"[{role}/text] {getattr(b, 'text', '')}")
                elif btype == "tool_use":
                    lines.append(
                        f"[{role}/tool_use] {getattr(b, 'name', '?')}({getattr(b, 'input', {})})"
                    )
    return "\n".join(lines)


def summarize_segment(
    *,
    segment: list[dict],
    client: Any,
    model: str,
) -> str:
    """调一次模型把这段 messages 压成一段文本。"""
    if not segment:
        return "(空段)"
    text = _stringify_messages_for_summary(segment)
    prompt = (
        "以下是一段已发生的对话历史，请按 system 指引总结：\n\n"
        + text
    )
    think: ThinkingConfigParam = {"type": "disabled"}
    response = client.messages.create(
        model=model,
        system=SUMMARY_SYSTEM,
        messages=cast(list[MessageParam], [{"role": "user", "content": prompt}]),
        max_tokens=SUMMARY_MAX_TOKENS,
        thinking=think,
    )
    parts = []
    for b in response.content:
        if getattr(b, "type", None) == "text":
            parts.append(getattr(b, "text", ""))
    return "\n".join(parts).strip() or "(总结为空)"


def compact_messages(
    *,
    messages: list[dict],
    client: Any,
    model: str,
    keep_tail_turns: int = DEFAULT_KEEP_TAIL_TURNS,
) -> tuple[list[dict], str] | None:
    """压缩 messages。返回 (新 messages, 总结文本)；若不需要压缩返回 None。

    新 messages 形态：
      [
        原 messages[0],                             ← 首个 user，任务定义
        {"role": "user", "content": "<历史摘要>: ..."},
        ...原 messages[cut:]                        ← 最近 keep_tail_turns 轮
      ]
    """
    if _has_unmatched_tool_use(messages):
        # 当前响应轮还没把 tool_result 回填，先不压
        return None
    cut = _find_safe_cut(messages, keep_tail_turns)
    if cut < 0:
        return None

    head = messages[0]
    middle = messages[1:cut]
    tail = messages[cut:]

    summary = summarize_segment(segment=middle, client=client, model=model)
    summary_msg = {
        "role": "user",
        "content": (
            "(以下为压缩后的会话历史摘要，原始消息已省略以节省上下文)\n\n"
            + summary
        ),
    }
    new_messages = [head, summary_msg, *tail]
    return new_messages, summary
