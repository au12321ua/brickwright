# BrickWright 维护约定

Documentation level: L3
Branch model: integration
Integration branch: develop
Release branch: main

## 阅读与验证

先读[交接](docs/HANDOFF.md)了解本分支的实现和验收状态。使用者从[根 README](README.md)安装并调用 skill。

| 改动 | 从哪里读起 |
| --- | --- |
| 零件、连接、试装或 SDK 参考 | [BrickLayer SDK](packages/bricklayer/README.md) · [架构](docs/architecture/overview.md) |
| 宿主指引、知识条目或按主题生成的页面 | [skill 和知识库的来源](docs/architecture/overview.md#skill-的内容从哪里来) |
| 记录、结构验证、评审、定稿或 demo 接收端 | [验证与导出](docs/design/validation-and-export.md) · [单题演示](demo/omr36/README.md) |
| 跨模块方案、进行中的提案及历史结果 | [提案索引](docs/proposals/README.md) |

宿主负责模型调用。BrickWright 维护 skill、workflow 库和 CLI，BrickLayer 维护建模 SDK。知识按任务检索，API 按动作拆页，允许按批扩充。单题 demo 允许调整数据集和接口；若旧接口兼容性妨碍演示，以演示为先。主代理可自评。只接收成功定稿的版本，并核对提交 MPD 与已验证快照逐字节一致。真实宿主代理实测留到开发完成后。[U-11](docs/decisions.md#u-11--在-fork-内精简为-skill-与-sdk) · [U-12](docs/decisions.md#u-12--演示从简并允许调整数据集与接口) · [U-13](docs/decisions.md#u-13--只交付定稿快照并在开发后实测)

代码改动采用 TDD。新工作树先用 `uv sync --locked` 安装依赖，提交前在该工作树运行全部 CI 检查。

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
```

## 提案和决策

跨模块或需要先确认方案的改动，按[提案索引](docs/proposals/README.md)写草案，确认后实施。
相关代码、测试和文档放在同一任务分支，按用途分提交，提交正文引用相关 P/U 编号。
用法变化更新对应 README，实现和约束更新架构或设计文档。结项时同步交接和提案索引，再归档提案。
过程笔记放在主工作树的 `.local/`。每个克隆在本地 Git 排除文件中配置 `/.local/`，
可用 `git rev-parse --git-path info/exclude` 查找文件。

只把用户明确作出且会约束后续工作的决定追加到 `docs/decisions.md`，记录稳定的 U 编号和来源。
保留原条目内容。替代决策时新旧条目双向链接，旧条目只补替代标记，同时更新受影响的摘要。
约束冲突、来源有疑问、变更约束或审计时，查[决策原文](docs/decisions.md)及其替代条目。以有效的明确决定、代码与运行证据、其他文档的顺序判断冲突，分别记录已实现行为和待实现约束。

## 分支和投稿

主工作树留在 `develop`，在其中维护提案和开发文档。`main` 跟随上游。[U-01](docs/decisions.md#u-01--在-fork-内采用提案流程)

代码任务从 `develop` 开短期 `feature/` 或 `fix/` 分支，
用单独的相邻工作树，例如 `git worktree add -b feature/P-001-topic ../brickwright-P-001-topic develop`。
仅文档改动可在主工作树的短期 `docs/` 分支处理。按授权用 `--no-ff` 合回 `develop`，
并在合并提交中概括改动。确认合并和工作树干净后，用 `git worktree remove` 移除已完成的任务工作树，
以 `git branch -d` 删除已合并的本地任务分支，再运行 `git worktree prune`。
安全删除失败时检查未集成提交或其他工作树的使用情况。

同步上游时先 `git fetch upstream main`，再将 `upstream/main` 合入现有 `develop`。
获授权同步本地 `main` 时，在该分支上用 `git merge --ff-only upstream/main`。
投稿时以 `upstream/main` 为基线另开 PR 分支，摘取对应代码、测试和适合上游的文档提交，
并用 `git diff upstream/main...HEAD` 核对范围。合入 `main`、推送、打标签和删除远端分支均按用户授权执行。

## 发布

两个包的版本分别以各自的 `pyproject.toml` 为准。发布按累计改动对公开 Python API、CLI、
配置和已有数据的最高兼容性影响选用 X.Y.Z。X 表示不兼容改动，Y 表示兼容功能新增，
Z 表示兼容修复或维护。升级相应位时将其后各位归零。
[上游发布流程](.github/workflows/release.yml)由 `v*` 标签触发。
