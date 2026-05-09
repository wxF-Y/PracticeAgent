"""跨 stage 复用的最小内核。

不在 s01 中使用，从 s02/s03 起按需引入：
- config.py: 环境变量加载（API key / model）
- client.py: Anthropic 客户端创建
"""
