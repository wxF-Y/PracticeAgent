---
name: write-readme
description: 为一个目录生成贴合实际内容的 README.md（V1 场景）
---

# 写 README 的标准流程

目标：给一个普通用户能直接读懂、能照着上手的 README，3 段以上。

## 步骤

1. **侦查目录结构**
   - 用 `glob(pattern="*")` 看顶层有哪些文件/目录
   - 关注：入口文件（main.py / index.ts / src/lib.rs / package.json / pyproject.toml / Cargo.toml）、配置文件、已有的 README/CHANGELOG

2. **读关键文件**
   - 读 1-2 个最像入口的源文件的开头 30 行（用 `read` 工具）
   - 读包描述类文件（package.json / pyproject.toml）的前 30 行
   - 读已有 README（如有）以便保留事实，仅补全和优化

3. **判定项目类型**
   - 命令行工具？库？Web 服务？教学项目？
   - 用什么技术栈？
   - 主要入口是什么？

4. **输出 README，至少包含三段**
   - **简介**（2-3 句话说明这个项目是干什么、解决了什么问题）
   - **快速开始**（几条 shell 命令能跑起来）
   - **目录结构**或**关键文件**（让读者知道往哪看）

5. **写入** 用 `write(path="README.md", ...)` 完成。如果原 README 存在且不空，先读一下避免覆盖重要事实。

## 反模式

- ❌ 没看代码就泛泛写"这是一个 Python 项目"
- ❌ 抄项目根的注释当摘要而不组织成段落
- ❌ 用 emoji 海报风格但没说清能干嘛

## 风格

- 中文优先（除非项目本身英文）
- 短句，避免冗长形容词
- 命令用 ```bash 代码块
