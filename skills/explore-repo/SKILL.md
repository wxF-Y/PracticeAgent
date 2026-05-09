---
name: explore-repo
description: 系统化探索一个陌生仓库，输出"是什么/怎么跑/关键文件在哪"
---

# 探索陌生仓库的最短路径

目的：在 5 步以内对一个陌生项目形成有效心智模型。

## 步骤

1. **顶层文件清单** `glob(pattern="*")`，看根目录文件名
2. **读 README**（如有）：通常已经回答了"是什么/怎么跑"
3. **找入口**：根据语言生态找入口文件
   - Python: `main.py` / `__main__.py` / `pyproject.toml` 的 `[project.scripts]`
   - Node: `package.json` 的 `main` / `bin` / `scripts.start`
   - Go: `cmd/*/main.go`
   - Rust: `src/main.rs` / `src/lib.rs`
4. **看依赖**：`requirements.txt` / `package.json.dependencies` / `Cargo.toml`，这一步告诉你"它依赖什么世界观"
5. **抽样关键源文件 1-2 个**：读入口的前 50 行，读最大模块的开头

## 输出建议

回答用户时按这个结构：

- **是什么**（1-2 句）
- **怎么跑**（命令行示例）
- **关键文件位置**（3-5 个最值得读的路径）
- **风险/疑问**（哪些地方文档没说清）
