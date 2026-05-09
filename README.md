# PracticeAgent

> 从 0 开始手写一个 harness agent，逐步扩展为可用的通用 agent。

## 目标

- **从 0 写 agent loop**：先把模型连上现实世界（一个工具 + 一个循环 = 一个 agent）。
- **逐步加入 harness 策略**：工具集、上下文压缩、技能加载、子 agent、权限、记忆、调度、网关。
- **最终交付一个普通人能用的通用 agent**——具体形态见下节。

## 最终产品形态

PracticeAgent 完成 s10 后是一个**个人/小团队可自部署的通用 agent**，命名暂定 `pa`。

### 谁来用

- 写少量代码或不写代码的个人用户：用它整理本机文件、批量改文档、跑脚本、查资料。
- 开发者：用它做日常 coding 助手（读代码、写补丁、跑测试、提 PR），与 Claude Code / OpenHarness 同位但更轻、可改。
- 小团队：把它接到飞书 / Telegram / Discord 里，作为常驻 bot 处理重复任务。

### 怎么用

```bash
pa                      # 进入交互式 REPL（默认）
pa -p "整理桌面 PDF"     # 单条 prompt 模式（脚本/管道友好）
pa --resume <id>        # 恢复历史会话
pa channel start        # 启动聊天通道（飞书/TG/控制台）
pa task list            # 查看后台任务
pa skill add <path>     # 安装一份 .md 技能
```

### 它能做什么（验收场景）

| 场景 | 触发方式 | 涉及 stage |
|---|---|---|
| 给本机一份目录写个 README | `pa` 里自然语言对话 | s01-s04 |
| 把一个 100 文件项目改名/重构 | 长任务，需子 agent 并行 + 上下文压缩 | s02, s05, s06 |
| "每天早上 9 点把昨日 git log 推到飞书" | 定时任务 + 通道 | s09, s10 |
| "你记住我偏好用 pnpm 而不是 npm" | 跨会话生效 | s08 |
| 删除 / 网络请求等敏感动作要确认 | 权限询问 | s07 |
| 在飞书群里 @机器人 让它读代码并回复 | 通道 → agent loop | s10 |

### 与参考产品的差异

| | Claude Code / OpenHarness | **PracticeAgent** |
|---|---|---|
| 体量 | 数万行，几十个工具 | 目标 ≤ 5000 行，10-15 个工具 |
| 受众 | 通用生产工具 | 一个人能从头读完、随手改 |
| 取舍 | 功能全 | 每个特性都来自一个具体 stage 的需求 |
| 通道 | 终端为主 | 终端 + 至少一种聊天通道 |

### 不做什么

- 不做可视化 IDE 插件、不做 web UI、不做训练/微调。
- 不重复实现 OpenHarness 已有的 43+ 工具，只挑必要的。
- 不上云、不收费——本地跑你自己的 API key。

## 核心理念

> Agent = Model（智能） + Harness（工具/记忆/权限/通道/编排）

模型决定"做什么"，harness 决定"怎么安全地做"。本仓库的全部代码都属于 harness 层。

## 技术栈

| 维度 | 选型 |
|---|---|
| 语言 | Python 3.10+ |
| LLM SDK | `anthropic`（Claude / Anthropic-compatible API） |
| 配置 | `python-dotenv` + 环境变量 |
| 启动方式 | 每个 stage 一个独立可运行的 `.py` 文件 |

## 学习路线（参见 [ROADMAP.md](ROADMAP.md)）

10 个阶段，每个阶段在前一阶段基础上叠加一个新概念，全部可独立运行：

```
s01 Agent Loop        -> while + stop_reason，最简循环
s02 Tools             -> 多工具 dispatch 表（read/write/glob/grep/bash）
s03 Sessions          -> 会话持久化与上下文管理
s04 Skills            -> 按需加载 .md 知识
s05 Subagent          -> 隔离子 agent 与并行任务
s06 Context Compact   -> 自动压缩与长会话
s07 Permissions       -> 多级权限、路径规则、Hook
s08 Memory            -> 跨会话长期记忆
s09 Tasks             -> 后台任务、定时调度
s10 Gateway           -> 聊天通道接入（CLI / 可选 IM）
```

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入 ANTHROPIC_AUTH_TOKEN / MODEL_ID 等

# 3. 运行 stage01（最简 agent loop）
python stages/s01_agent_loop.py
```

## 参考项目（位于 `E:\AI`）

| 项目 | 角色 |
|---|---|
| [learn-claude-code](../learn-claude-code) | 12 章渐进式 coding agent harness 教程，本项目的主要参考 |
| [claw0](../claw0) | 10 章 agent gateway 渐进式实现，借鉴 sessions/channels/resilience |
| [OpenHarness](../OpenHarness) | 工程化的成熟 harness（43+ 工具、插件、TUI），后期对照 |
| [openclaw](../openclaw) | 完整 CLI agent，用于研究生产级架构 |
| [claw-code](../claw-code) | 多 agent 协作哲学（Discord 入口、清洁通知路由） |
| [ClawTeam](../ClawTeam) | 多 agent 团队协作 CLI |

## 目录结构

```
PracticeAgent/
├── README.md
├── ROADMAP.md            # 阶段路线图
├── requirements.txt
├── .env.example
├── .gitignore
├── core/                 # 跨 stage 复用的最小内核
│   ├── client.py         # API 客户端封装
│   └── config.py         # 环境变量加载
├── tools/                # 工具实现（s02 起逐步填充）
└── stages/               # 每阶段一个可运行文件
    ├── s01_agent_loop.py
    ├── s02_tools.py
    └── ...
```

## 开发约定

- **每个 stage 必须可独立运行**，不依赖未来章节。
- **代码加中文注释**，便于回顾思路。
- **不引入框架抽象**，只在出现真正复用时才提取到 `core/`。
- **测试方式**：每个 stage 提供 3-5 个示例 prompt 验证关键能力。
