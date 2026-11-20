# 用户决策

## U-01 — 在 fork 内采用提案流程

- Source: 2026-11-20，用户在讨论 `project-docs` L3、长期保留 `develop` 和从 `upstream/main` 创建投稿分支后确认「那我懂了，就这么来配置吧」。
- Supersedes: none
- Superseded by: none

在 fork 中采用 L3 提案流程。长期在 `develop` 版本化维护 `docs/`，包括已归档的提案。
`main` 跟随上游。投稿时从 `upstream/main` 创建 PR 分支，带上对应的代码、测试和适合上游的文档。
个人笔记和过程记录放在 `.local/`，用本地 Git 排除规则忽略。
