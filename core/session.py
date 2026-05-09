"""会话持久化与上下文累积。

每个会话一个 JSONL 文件：
    ~/.practiceagent/sessions/<id>.jsonl

    line 0: {"_meta": true, "session_id": ..., "created_at": ..., "model": ..., "cwd": ...}
    line 1: {"role": "user", "content": "..."}
    line 2: {"role": "assistant", "content": [{"type": "text", "text": "..."}, ...]}
    line 3: {"role": "user", "content": [{"type": "tool_result", ...}]}
    ...

写盘时立即 flush，每轮可恢复（即使 agent 崩溃也最多丢最后一条）。

Session 同时维护 token 累计，supports both Anthropic Usage 对象和 dict。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


def _default_base_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".practiceagent" / "sessions"


def _block_to_dict(block: Any) -> Any:
    """把 anthropic ContentBlock（pydantic Model）转 dict；已是 dict/str 则原样。"""
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return block


def _serialize_content(content: Any) -> Any:
    """messages 里 content 可能是 str / list[block]。归一化为可 JSON 序列化形式。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return [_block_to_dict(b) for b in content]
    return _block_to_dict(content)


@dataclass
class Session:
    session_id: str
    path: Path
    messages: list[dict] = field(default_factory=list)
    total_input: int = 0
    total_output: int = 0
    turn: int = 0  # assistant turn 数

    # ---- 工厂 ----

    @classmethod
    def new(cls, *, model: str, base_dir: Path | None = None) -> "Session":
        """开一个新会话，立即写 meta。"""
        base = base_dir or _default_base_dir()
        base.mkdir(parents=True, exist_ok=True)
        sid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        path = base / f"{sid}.jsonl"
        sess = cls(session_id=sid, path=path)
        sess._write_line(
            {
                "_meta": True,
                "session_id": sid,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "model": model,
                "cwd": str(Path.cwd()),
            }
        )
        return sess

    @classmethod
    def resume(cls, session_id: str, *, base_dir: Path | None = None) -> "Session":
        """加载已有会话。session_id 可以是完整 id 或前缀。"""
        base = base_dir or _default_base_dir()
        path = cls._resolve_path(base, session_id)
        sess = cls(session_id=path.stem, path=path)
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("_meta"):
                    continue
                sess.messages.append(rec)
                if rec.get("role") == "assistant":
                    sess.turn += 1
                # token 计数无法从历史精确恢复（usage 没存）——重新计费从 0 开始
        return sess

    @classmethod
    def list_recent(
        cls, *, base_dir: Path | None = None, limit: int = 20
    ) -> list[tuple[str, str]]:
        """返回 [(session_id, first_user_message_preview), ...]，按文件 mtime 倒序。"""
        base = base_dir or _default_base_dir()
        if not base.is_dir():
            return []
        files = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        out: list[tuple[str, str]] = []
        for p in files[:limit]:
            preview = ""
            try:
                with p.open("r", encoding="utf-8") as f:
                    for line in f:
                        rec = json.loads(line)
                        if rec.get("role") == "user":
                            c = rec.get("content")
                            preview = c if isinstance(c, str) else "(non-text)"
                            break
            except (OSError, json.JSONDecodeError):
                preview = "(read error)"
            out.append((p.stem, (preview or "")[:60]))
        return out

    @staticmethod
    def _resolve_path(base: Path, sid: str) -> Path:
        """支持完整 session_id 或前缀匹配。"""
        exact = base / f"{sid}.jsonl"
        if exact.is_file():
            return exact
        candidates = sorted(base.glob(f"{sid}*.jsonl"))
        if not candidates:
            raise FileNotFoundError(f"未找到会话 {sid}（base={base}）")
        if len(candidates) > 1:
            raise ValueError(f"前缀 {sid} 匹配多个会话：{[c.stem for c in candidates]}")
        return candidates[0]

    # ---- 追加 ----

    def append_user(self, content: Any) -> None:
        msg = {"role": "user", "content": _serialize_content(content)}
        self.messages.append(msg)
        self._write_line(msg)

    def append_assistant(self, content: Any, usage: Any = None) -> None:
        msg = {"role": "assistant", "content": _serialize_content(content)}
        self.messages.append(msg)
        self._write_line(msg)
        self.turn += 1
        if usage is not None:
            self.total_input += int(getattr(usage, "input_tokens", 0) or 0)
            self.total_output += int(getattr(usage, "output_tokens", 0) or 0)

    # ---- 内部 ----

    def _write_line(self, obj: Any) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()

    def __iter__(self) -> Iterator[dict]:
        return iter(self.messages)
