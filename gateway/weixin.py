"""微信 ClawBot 通道。

认证：iLink bot token（WEIXIN_BOT_TOKEN 环境变量）。
接收：POST /ilink/bot/getupdates  长轮询（约 35s 超时），持久化 get_updates_buf。
发送：POST /ilink/bot/sendmessage  纯文本，携带 context_token。
持久化：~/.practiceagent/weixin/{account_id}.sync.json
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gateway.im import Channel, InboundMessage, OutboundMessage

logger = logging.getLogger("gateway.weixin")

_CHANNEL_VERSION = "1.0.3"
_CHANNEL_ID = "weixin"
_MSG_TYPE_USER = 1
_MSG_TYPE_BOT = 2
_ITEM_TYPE_TEXT = 1
_STATE_FINISH = 2
_ERR_SESSION_EXPIRED = -14

_STORE_DIR = Path.home() / ".practiceagent" / "weixin"


def _load_sync(account_id: str) -> dict[str, Any]:
    path = _STORE_DIR / f"{account_id}.sync.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"get_updates_buf": "", "context_tokens": {}}


def _save_sync(account_id: str, data: dict[str, Any]) -> None:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    path = _STORE_DIR / f"{account_id}.sync.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _md_to_plain(text: str) -> str:
    """把 Markdown 粗略转为纯文本，适合微信聊天窗口。"""
    text = re.sub(r"```[\s\S]*?```", lambda m: m.group().strip("`").strip(), text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"!\[.*?\]\(.*?\)", "[图片]", text)
    text = re.sub(r"\[(.+?)\]\(.*?\)", r"\1", text)
    return text.strip()


def _post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 40) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:200]}") from e


class WeixinChannel:
    """微信 ClawBot 通道（长轮询接收 + 文本发送）。

    env vars:
        WEIXIN_BOT_TOKEN   iLink bot token（必填）
        WEIXIN_ACCOUNT_ID  账户标识，用于持久化（可选，默认 "default"）
        WEIXIN_BASE_URL    接口根路径（可选，默认 https://ilinkai.weixin.qq.com）
    """

    channel_id: str = _CHANNEL_ID

    def __init__(self) -> None:
        self._token = os.environ.get("WEIXIN_BOT_TOKEN", "")
        # 净化 account_id，防止路径遍历
        raw_id = os.environ.get("WEIXIN_ACCOUNT_ID", "default")
        self._account_id = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_id) or "default"
        self._base_url = os.environ.get("WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com").rstrip("/")
        self.inbound_queue: queue.Queue[InboundMessage] = queue.Queue()
        self._sync = _load_sync(self._account_id)
        self._sync_lock = threading.Lock()  # 保护 _sync 读写
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
            "AuthorizationType": "ilink_bot_token",
        }

    def _base_info(self) -> dict[str, str]:
        return {"channel_version": _CHANNEL_VERSION}

    def start(self) -> None:
        if not self._token:
            logger.warning("WEIXIN_BOT_TOKEN 未设置，微信通道不启动")
            return
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="weixin-poll",
            daemon=True,
        )
        self._thread.start()
        logger.info("微信通道已启动（account=%s）", self._account_id)

    def stop(self) -> None:
        self._stop_event.set()

    def send(self, msg: OutboundMessage) -> None:
        """发送纯文本回复。context_token 来自入站消息。"""
        text = _md_to_plain(msg.text)
        with self._sync_lock:
            ctx_token = msg.context_token or self._sync["context_tokens"].get(msg.to_user_id, "")
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": msg.to_user_id,
                "client_id": f"practiceagent-{uuid.uuid4().hex[:16]}",
                "message_type": _MSG_TYPE_BOT,
                "message_state": _STATE_FINISH,
                "context_token": ctx_token,
                "item_list": [{"type": _ITEM_TYPE_TEXT, "text_item": {"text": text}}],
            },
            "base_info": self._base_info(),
        }
        url = f"{self._base_url}/ilink/bot/sendmessage"
        try:
            result = _post(url, payload, self._headers)
            if result.get("ret") != 0:
                logger.error("sendmessage 失败: %s", result)
        except Exception as exc:
            logger.error("sendmessage 异常: %s", exc)

    def _poll_loop(self) -> None:
        url = f"{self._base_url}/ilink/bot/getupdates"
        backoff = 1
        while not self._stop_event.is_set():
            try:
                with self._sync_lock:
                    buf_val = self._sync.get("get_updates_buf", "")
                payload = {"get_updates_buf": buf_val, "base_info": self._base_info()}
                result = _post(url, payload, self._headers, timeout=45)
                ret = result.get("ret", -1)
                if ret == _ERR_SESSION_EXPIRED:
                    logger.error("微信会话过期，请重新登录并更新 WEIXIN_BOT_TOKEN")
                    self._stop_event.wait(300)
                    continue
                if ret != 0:
                    logger.warning("getupdates 非零返回: %s", result)
                    self._stop_event.wait(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 1
                new_buf = result.get("get_updates_buf")
                msgs = result.get("msgs") or []

                # 在锁内统一更新 _sync，一次性持久化
                with self._sync_lock:
                    if new_buf:
                        self._sync["get_updates_buf"] = new_buf
                    for raw_msg in msgs:
                        if raw_msg.get("message_type") != _MSG_TYPE_USER:
                            continue
                        ctx_token: str = raw_msg.get("context_token", "")
                        from_uid: str = raw_msg.get("from_user_id", "")
                        if ctx_token and from_uid:
                            self._sync["context_tokens"][from_uid] = ctx_token
                    if new_buf or any(
                        m.get("context_token") for m in msgs if m.get("message_type") == _MSG_TYPE_USER
                    ):
                        _save_sync(self._account_id, self._sync)

                for raw_msg in msgs:
                    if raw_msg.get("message_type") != _MSG_TYPE_USER:
                        continue
                    from_uid = raw_msg.get("from_user_id", "")
                    ctx_token = raw_msg.get("context_token", "")
                    text = ""
                    for item in raw_msg.get("item_list") or []:
                        if item.get("type") == _ITEM_TYPE_TEXT:
                            text += (item.get("text_item") or {}).get("text", "")
                    if text.strip():
                        self.inbound_queue.put(
                            InboundMessage(
                                channel_id=_CHANNEL_ID,
                                from_user_id=from_uid,
                                text=text.strip(),
                                context_token=ctx_token or None,
                            )
                        )
            except URLError as exc:
                logger.warning("getupdates 网络超时，重试: %s", exc)
                self._stop_event.wait(2)
            except Exception as exc:
                logger.error("getupdates 异常: %s", exc)
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
