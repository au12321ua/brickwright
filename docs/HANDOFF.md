# BrickWright 交接

- [BrickLayer](../packages/bricklayer/README.md) 提供零件、建模、几何校验和 LDraw 导出。
- [BrickWright](../packages/brickwright/README.md) 提供代理生成和命令行工作流。
  内置代理用 `brickwright generate`，外部代理按[搭建指南](../packages/brickwright/EXTERNAL_AGENT_INSTRUCTIONS.md)操作。

在仓库根目录执行 `uv sync --locked`，用 `uv run brickwright --help` 核对入口。
开发检查见 [AGENTS.md](../AGENTS.md#阅读与验证)。默认 pytest 套件跳过标记为 `real_agent` 的测试。
真实模型测试通过 `--run-real-agent` 启用，会调用模型 API。
本地 SOCKS 代理因缺少 `socksio` 导致 LiteLLM 加载价格表失败时，可用
`LITELLM_LOCAL_MODEL_COST_MAP=True uv run pytest` 让测试使用 LiteLLM 自带的价格表。

视觉预览需要 LDraw 零件库。设置 `LDRAW_LIBRARY_PATH` 后可以运行渲染功能。
无此库时，仍可验证结构和导出 LDraw。默认生成记录保存在仓库忽略的 `records/`。

修改模型校验或产物时，同时查看 [验证与导出设计](design/validation-and-export.md)。
连接驱动装配与执行错误反馈、搭建知识库、LDraw 2026-10 零件覆盖已写成三份草案，
见[提案索引与并行开发约定](proposals/README.md)。索引列出三条线的接口和共享的开发前可行性检查。
