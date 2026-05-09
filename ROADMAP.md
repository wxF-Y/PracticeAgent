# 路线图

每个阶段一个独立可运行的 Python 文件，叠加一个新机制。学完即可读懂 OpenHarness / openclaw 的生产代码。

## Stage 概览

每个 stage 末尾标注它**为最终产品贡献了哪个能力**——对应 [README.md 验收场景表](README.md#它能做什么验收场景)（编号 V1-V6）。

| Stage | 主题 | 核心概念 | 新增产物 | 解锁场景 |
|---|---|---|---|---|
| s01 | Agent Loop | `while stop_reason == "tool_use"` | `stages/s01_agent_loop.py` + 1 个 bash 工具 | V1（雏形） |
| s02 | Tool Use | 多工具 dispatch 表、JSON Schema | `tools/{read,write,edit,glob,grep,bash}.py` | V1, V2 |
| s03 | Sessions & Context | JSONL 持久化、上下文窗口 | `core/session.py` | 支撑 V2/V6 长任务 |
| s04 | Skills | 按需加载 `.md` 知识 | `skills/` 目录 + 加载器 | 完整 V1（写出像样的 README） |
| s05 | Subagent | 子 agent 隔离上下文 | `Agent` 工具，可并行 | 完整 V2（100 文件重构） |
| s06 | Context Compact | 自动压缩长上下文 | `core/compact.py` | 支撑 V2/V6 不爆上下文 |
| s07 | Permissions & Hooks | 多级权限、Pre/Post 钩子 | `core/permissions.py`, `hooks/` | **V5**（敏感动作确认） |
| s08 | Memory | 跨会话 `MEMORY.md` | `core/memory.py` | **V4**（记住 pnpm 偏好） |
| s09 | Tasks & Cron | 后台任务、定时触发 | `core/tasks.py` | **V3**（定时推 git log） |
| s10 | Gateway | 终端 / 可选 IM 通道 | `gateway/cli.py`, `gateway/im.py` | **V3, V6**（飞书 @ 机器人） |

> 验收场景速记：V1=本机目录写 README · V2=100 文件重构 · V3=定时推送 · V4=跨会话偏好 · V5=敏感动作确认 · V6=IM 通道接入

## 阶段依赖

```
s01 → s02 → s03 → s04 → s05
              │          │
              ▼          ▼
             s06        s07
              │          │
              ▼          ▼
             s08 ─────► s09 → s10
```

## Stage 1 — Agent Loop

**目标**：实现"模型自主调用工具直到任务完成"的最小循环。

**交付**：
- `stages/s01_agent_loop.py`，30 行内的 agent loop
- 一个 `bash` 工具（含基本危险命令拦截）
- REPL 命令行入口

**验收 prompt**：
1. `创建一个名为 hello.py 的文件，输出 Hello, World!`
2. `列出当前目录所有 .py 文件`
3. `当前 git 分支是什么？`（无 git 时应能优雅报错）

**贡献能力**：V1 雏形（能在本机做"读+写文件"类任务，但还很糙）。

## Stage 2 — Tool Use

**目标**：从单一 bash 扩展为多工具集，引入工具注册表和 dispatch 表。

**交付**：
- `tools/{read,write,edit,glob,grep,bash}.py`
- `core/tool_registry.py` 提供 `register / list_schemas / dispatch`
- `stages/s02_tools.py` 复用 s01 循环但替换工具源

**验收 prompt**：
1. `读 README.md 并把 README 改成 README2`（Read + Edit）
2. `找到所有包含 TODO 的代码文件`（Grep + Read）

**贡献能力**：V1 完整可用 + V2 启动条件（Edit/Glob/Grep 是 100 文件重构的前提）。

## Stage 3 — Sessions & Context

**目标**：会话可持久化、可恢复；监控 token 消耗。

**交付**：
- `~/.practiceagent/sessions/<id>.jsonl` 写入历史
- `--resume <id>` 加载上次会话
- 简单 token 计数（基于 Anthropic 返回的 usage）

**贡献能力**：支撑 V2 / V6 的长任务——会话不丢、能续跑。

## Stage 4-10

完成 s01-s03 后再细化每章设计文档。原则：

- 每章新增代码 ≤ 300 行
- 每章必须能独立 demo
- 每章必须明确**解锁/完成了哪个 V 场景**，否则不做

## 退出条件

完成 s10 后，PracticeAgent 应能逐项通过 [README 中的 6 个验收场景](README.md#它能做什么验收场景)：

| # | 场景 | 通过判据 |
|---|---|---|
| V1 | 给本机一份目录写个 README | 单条 prompt 无人工干预生成≥3 段、内容贴合目录的 README |
| V2 | 100 文件重命名/重构 | 子 agent 并行处理，长会话不爆上下文，最终 diff 一致 |
| V3 | 每天 9 点把昨日 git log 推到飞书 | cron 触发，消息正确送达通道 |
| V4 | 记住"用 pnpm 而不是 npm" | 重启进程、新会话仍生效 |
| V5 | 删除/网络等敏感动作要确认 | 默认拦截 + 询问，用户拒绝则不执行 |
| V6 | 在飞书群 @ 机器人读代码并回复 | 通道 inbound→agent loop→outbound 闭环 |

附加约束：
- 代码量 ≤ 5000 行，可被一个人完整阅读
- 全部测试在本地一条命令跑完
- 文档（README + ROADMAP + 各 stage 设计稿）≤ 3000 字
