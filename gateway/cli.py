"""CLI 通道：把终端 stdin/stdout 包装成 Channel 接口。

消息由 _reader_thread 阻塞读 stdin，放入 inbound_queue；
send() 直接 print 到 stdout。
"""

from __future__ import annotations

import queue
import sys
import threading

from gateway.im import Channel, InboundMessage, OutboundMessage

_CHANNEL_ID = "cli"
_SENTINEL = object()


class CLIChannel:
    """将标准输入封装为 Channel，支持与其他通道共享 inbound_queue。"""

    channel_id: str = _CHANNEL_ID

    def __init__(self, prompt: str = "s10 >> ") -> None:
        self._prompt = prompt
        self.inbound_queue: queue.Queue[InboundMessage] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._reader_loop,
            name="cli-reader",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def send(self, msg: OutboundMessage) -> None:
        print(f"\n\033[32m[bot→{msg.to_user_id}]\033[0m {msg.text}\n", flush=True)

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                sys.stdout.write(f"\033[36m{self._prompt}\033[0m")
                sys.stdout.flush()
                line = sys.stdin.readline()
                if not line:  # EOF
                    break
                text = line.rstrip("\n")
                if text:
                    self.inbound_queue.put(
                        InboundMessage(
                            channel_id=_CHANNEL_ID,
                            from_user_id="local",
                            text=text,
                        )
                    )
            except (EOFError, KeyboardInterrupt):
                break
