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

## 连接装配、知识查询和零件覆盖

- 从搭建意图查件可用 `bricklayer.find_connection_pairs`。已有模型可用 `find_connection_candidates` 查安装姿态，`trial_connections` 试装，`place_connection` 提交。内置代理和外部代理可收到同一组执行原因码、现场证据和修复建议。[连接驱动装配提案](proposals/P-001-connection-driven-building.md)
- 搭建问题、零件号和诊断码可交给 `brickwright knowledge search`，再用 `brickwright knowledge show` 读取步骤、可执行示例和来源。[知识库验收结果](proposals/archive/P-002-construction-knowledge.md#实现与验收结果)
- `bricklayer.lookup_part` 可离线查询 LDraw 2026-10 的全部 24,985 个官方顶层 ID，返回建模及碰撞能力、端点、已验证安装姿态或数据缺项。[覆盖统计与数据限制](proposals/archive/P-003-official-part-coverage.md#实现与验收结果)

P-001 的 Maestro 外部代理首轮对照未通过连接占比条件。铰链修复已覆盖需要反向安装的完整舱门，知识库的 `hinged-panels` 示例可直接运行。后续配对固定 `52a813a` 和 `6aba106`，分别报告原五题和两道新功能题，每题每版重复两次。原生代理对照另行统计。[配对说明](design/p001-paired-evaluation.md#maestro-外部代理对照)

本次集成的 pytest 在启用冻结清单比对后通过 3,069 项，跳过 6 项。用本地 LDraw 库单独复跑其中两项渲染测试均通过，其余为三项需要原始 BrickNet 输入的提取测试和一项真实模型测试。两套发布数据生成器的 `--check`、Ruff 和 ty 均通过。从两个 wheel 在仓库外重查了官方全集，并试装了能通过五个姿态采样的铰链。
