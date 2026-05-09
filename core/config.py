"""环境配置加载。

支持两种鉴权方式（择一）：
  1) ANTHROPIC_API_KEY：官方 Anthropic
  2) ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN：第三方兼容网关
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    """运行时配置。"""

    api_key: str
    base_url: str | None
    model_id: str


def load_config() -> Config:
    """读取 .env 与环境变量，返回 Config。缺失关键项时抛 RuntimeError。

    .env 优先级高于系统环境变量（override=True），与 learn-claude-code / claw0 一致：
    本地开发时改 .env 立即生效，避免被 shell 里的同名变量静默覆盖。
    """
    load_dotenv(override=True)

    # 优先使用兼容网关 token；否则回退到官方 API key
    api_key = (
        os.getenv("ANTHROPIC_AUTH_TOKEN")
        or os.getenv("ANTHROPIC_API_KEY")
        or ""
    )
    base_url = os.getenv("ANTHROPIC_BASE_URL") or None
    model_id = os.getenv("MODEL_ID") or ""

    if not api_key:
        raise RuntimeError(
            "未找到 API 鉴权信息。请在 .env 中设置 ANTHROPIC_API_KEY "
            "或 ANTHROPIC_AUTH_TOKEN（配合 ANTHROPIC_BASE_URL）。"
        )
    if not model_id:
        raise RuntimeError("未找到 MODEL_ID。请在 .env 中设置，例如 claude-sonnet-4-6。")

    return Config(api_key=api_key, base_url=base_url, model_id=model_id)
