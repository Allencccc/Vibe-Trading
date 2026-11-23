# AGENTS.md — Local Fork Development Guidelines

本仓库为 `HKUDS/Vibe-Trading` 的个人 Fork（`Allencccc/Vibe-Trading`）。所有开发者及 AI Agent 在本仓库中工作时，必须严格遵守以下 Git 分支与提交规范。

## 1. 远程仓库约定

- `origin`：`https://github.com/Allencccc/Vibe-Trading.git`（个人 Fork，**唯一允许 push 的远程仓库**）
- `upstream`：`https://github.com/HKUDS/Vibe-Trading.git`（上游原仓库，**只读**，已通过 `git remote set-url --push upstream DISABLED` 禁用推送）

## 2. 分支职责与硬性约束

- **`main` 分支（只读上游同步分支）**：
  - 追踪 `upstream/main`，仅用于拉取上游原仓库的最新代码。
  - **禁止**在 `main` 分支上进行任何本地开发、修改代码或直接 `git commit`。
  - **禁止**对 `main` 分支执行 `git push`。
- **`local-dev` 分支（个人默认开发分支）**：
  - 追踪 `origin/local-dev`。
  - 所有本地修改、功能开发、配置调整及提交都在 `local-dev`（或基于 `local-dev` 切出的特性分支）上进行。
  - 推送代码时**仅推送到 `origin`**（如 `git push origin local-dev`）。

## 3. AI Agent 执行规范

1. **修改代码或提交前必须先检查当前分支**（`git branch --show-current`）：
   - 如果当前位于 `main` 分支，**必须先切换到 `local-dev`**（`git checkout local-dev`），再进行任何文件修改或提交。
2. **严禁未经明确要求执行 `git push`**：完成本地修改或 `git commit` 后，**必须等待用户明确要求 push** 才能执行 `git push` 到远程仓库（且仅允许推送到 `origin`）。
3. **严禁修改 `upstream` 的推送限制**：不要移除或覆盖 `upstream` 的 `DISABLED` push URL，也不要尝试向 `HKUDS/Vibe-Trading` 推送任何分支或提交。
4. **禁止未经确认向原仓库发起 PR**：所有提交仅保留在个人 Fork（`origin`）中。

## 4. 本地开发环境约定

- **默认 Python 环境**：统一使用 conda 虚拟环境 **`sl-dev-313`**（`/Users/allen/anaconda3/envs/sl-dev-313/bin/python`，Python 3.13.5）。
- **模块路径**：运行项目脚本、因子评测、回测或 `pytest` 时，指定 `PYTHONPATH=agent`（例如 `PYTHONPATH=agent /Users/allen/anaconda3/envs/sl-dev-313/bin/pytest agent/tests/...`）。
- **依赖保护**：`sl-dev-313` 为共用开发环境（已安装 `pandas`、`numpy`、`polars`、`bottleneck`、`fastapi`、`uvicorn`、`pytest` 等），**不要**直接执行全量 `pip install -e .` 强行覆盖升级该环境中已有的 `fastmcp` / `mcp` 等包；若需补充缺失包，应按需单独安装并确认不破坏现有版本。

## 5. 常用工作流参考

### 日常开发与提交（在 `local-dev` 分支）
```bash
git checkout local-dev
git add <files>
git commit -m "<commit message>"
git push origin local-dev
```

### 同步上游仓库更新
```bash
git checkout main
git pull                 # 从 upstream/main 拉取最新代码
git checkout local-dev
git merge main           # 将上游更新合并到 local-dev
```
