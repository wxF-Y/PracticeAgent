"""微信 ClawBot 通道。

登录：python stages/s10_gateway.py --login  （扫码后凭证自动持久化）
认证：iLink bot token，优先读 ~/.practiceagent/weixin/{id}.account.json，
      其次读 WEIXIN_BOT_TOKEN 环境变量。
接收：POST /ilink/bot/getupdates  长轮询（约 35s 超时），持久化 get_updates_buf。
发送：POST /ilink/bot/sendmessage  纯文本，携带 context_token。
持久化：~/.practiceagent/weixin/{account_id}.{sync,account}.json
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


def _load_account(account_id: str) -> dict[str, Any] | None:
    path = _STORE_DIR / f"{account_id}.account.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _save_account(account_id: str, data: dict[str, Any]) -> None:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    path = _STORE_DIR / f"{account_id}.account.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get(url: str, headers: dict[str, str], timeout: int = 15) -> dict[str, Any]:
    req = Request(url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:300]}") from e


def login_with_qrcode(
    account_id: str = "default",
    base_url: str = "https://ilinkai.weixin.qq.com",
    poll_interval: float = 3.0,
    timeout_sec: float = 120.0,
) -> dict[str, Any]:
    """交互式扫码登录微信 ClawBot，成功后将凭证写入磁盘并返回账户信息。

    凭证保存路径：~/.practiceagent/weixin/{account_id}.account.json
    """
    base_url = base_url.rstrip("/")
    headers: dict[str, str] = {"iLink-App-ClientVersion": "1"}

    # ── 1. 获取二维码 ─────────────────────────────────────────────────────────
    print("正在获取微信登录二维码...", flush=True)
    result = _get(f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3", headers)
    qrcode_str: str = result.get("qrcode", "")
    qrcode_img_url: str = result.get("qrcode_img_content", "")

    if not qrcode_str:
        raise RuntimeError(f"获取二维码失败：{result}")
    logger.info("qrcode token=%s  scan_url=%s", qrcode_str, qrcode_img_url)

    # ── 2. 展示二维码 ─────────────────────────────────────────────────────────
    # qrcode_img_url 是 WeChat 识别的扫码 URL，将其编进 QR 码
    qr_data = qrcode_img_url if qrcode_img_url.startswith("http") else qrcode_str
    try:
        import qrcode as _qrcode  # type: ignore[import]
        qr = _qrcode.QRCode(border=1)
        qr.add_data(qr_data)
        qr.make(fit=True)
        print("\n请用微信扫描下方二维码：", flush=True)
        qr.print_ascii(invert=True)
        print(flush=True)
    except ImportError:
        print("提示：pip install qrcode 可在终端展示二维码", flush=True)
        print(f"二维码内容：{qr_data}", flush=True)
    except Exception as exc:
        logger.warning("ASCII 二维码渲染失败：%s", exc)
        print(f"二维码内容：{qr_data}", flush=True)
    print("请用微信扫描上方二维码...", flush=True)

    # ── 3. 轮询确认结果 ───────────────────────────────────────────────────────
    poll_url = f"{base_url}/ilink/bot/get_qrcode_status?qrcode={qrcode_str}"
    deadline = time.monotonic() + timeout_sec
    last_status = ""
    print("等待扫码确认...", flush=True)

    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        try:
            sr = _get(poll_url, headers, timeout=15)
        except Exception as exc:
            logger.warning("轮询登录状态失败：%s", exc)
            continue

        status: str = sr.get("status", "")
        if status != last_status:
            last_status = status
            status_zh = {"wait": "等待扫码", "scaned": "已扫码，等待确认", "confirmed": "确认成功", "expired": "已过期"}.get(status, status)
            print(f"  [{status_zh}]", flush=True)

        if status == "confirmed":
            token: str = sr.get("bot_token", "")
            bot_id: str = sr.get("ilink_bot_id", "")
            srv_base_url: str = sr.get("baseurl", base_url).rstrip("/")
            user_id: str = sr.get("ilink_user_id", "")
            if not token:
                raise RuntimeError(f"confirmed 但 bot_token 为空：{sr}")
            account: dict[str, Any] = {
                "account_id": account_id,
                "token": token,
                "bot_id": bot_id,
                "base_url": srv_base_url,
                "user_id": user_id,
            }
            _save_account(account_id, account)
            print(f"\n登录成功！  bot_id={bot_id}  user_id={user_id}", flush=True)
            print(f"凭证已保存：{_STORE_DIR / f'{account_id}.account.json'}", flush=True)
            return account

        if status == "expired":
            raise RuntimeError("二维码已过期，请重新运行 --login")

    raise RuntimeError(f"登录超时（{timeout_sec:.0f}s），请重新尝试")


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

    凭证加载优先级：
      1. ~/.practiceagent/weixin/{account_id}.account.json（扫码登录后自动写入）
      2. 环境变量 WEIXIN_BOT_TOKEN / WEIXIN_BASE_URL

    env vars（均可选，扫码登录后可省略）:
        WEIXIN_BOT_TOKEN   iLink bot token
        WEIXIN_ACCOUNT_ID  账户标识，用于持久化（默认 "default"）
        WEIXIN_BASE_URL    接口根路径（默认 https://ilinkai.weixin.qq.com）
    """

    channel_id: str = _CHANNEL_ID

    def __init__(self) -> None:
        # 净化 account_id，防止路径遍历
        raw_id = os.environ.get("WEIXIN_ACCOUNT_ID", "default")
        self._account_id = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_id) or "default"

        # 优先从持久化凭证文件读取 token/base_url
        account = _load_account(self._account_id)
        if account:
            self._token = account.get("token", "")
            self._base_url = account.get("base_url", "https://ilinkai.weixin.qq.com").rstrip("/")
            logger.debug("已从磁盘加载账户凭证（account=%s）", self._account_id)
        else:
            self._token = os.environ.get("WEIXIN_BOT_TOKEN", "")
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
