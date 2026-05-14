"""Gateway IM 协议定义：Channel、InboundMessage、OutboundMessage。"""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class InboundMessage:
    channel_id: str          # 通道标识，如 "cli" / "weixin"
    from_user_id: str        # 发件人 ID
    text: str                # 消息文本
    context_token: str | None = None  # WeChat 回复必须携带


@dataclass(frozen=True)
class OutboundMessage:
    channel_id: str
    to_user_id: str
    text: str
    context_token: str | None = None


@runtime_checkable
class Channel(Protocol):
    channel_id: str
    inbound_queue: queue.Queue[InboundMessage]

    def start(self) -> None:
        """启动后台线程/资源，开始接收消息。"""
        ...

    def stop(self) -> None:
        """停止后台线程，释放资源。"""
        ...

    def send(self, msg: OutboundMessage) -> None:
        """发送一条回复消息到该通道。"""
        ...
