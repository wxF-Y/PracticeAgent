"""Anthropic 客户端工厂。

通过 core.config.load_config() 获取配置，返回可直接调用的 Anthropic 客户端。
"""

from __future__ import annotations

from anthropic import Anthropic

from .config import Config, load_config


def make_client(config: Config | None = None) -> tuple[Anthropic, Config]:
    """创建 Anthropic 客户端，并返回 (client, config)。"""
    cfg = config or load_config()
    client = Anthropic(api_key=cfg.api_key, base_url=cfg.base_url)
    return client, cfg
