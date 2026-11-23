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

## 下一阶段：代理搭建流程演示

[总设计](design/agent-build-flow.md)以 OMR36 从零版第二版六题演示理解、规划、搭建、结构检查、外观评审和交付。594effd 基线每题一次，六题通过平台交付，标准目录重读最终 MPD 均零碰撞、零悬空。四题连成一体，小屋 8 个组件、巴士 5 个。六题均尝试题目机位对照，五题打印过独立评审说明，实际派发为零。[基线运行](http://localhost:8420/runs/run_01m834xe645ngv8jjcmc40svmx)

P-004 至 P-006 待评审，分别用小屋与巴士检验结构归因、用喷气机检验独立外观返修、用巴士两组检验模块接口和并行成本。新增的 [P-007 草案](proposals/P-007-official-reference-assemblies.md)已复核 101 个官方轮组文件的基线；等价位姿逐例判定、误接反例、成类修正及全量筛查属于待实施验收。完整范围见[提案索引](proposals/README.md)。

两项验收取舍已确认。事先声明可简化的装饰接口，在具体缺项有证据且两栏结构检查通过时可带限制交付。六题按整组检验交付、平台分和原始条件满足数不退步，喷气机用同机位盲评展示局部改善。[U-09](decisions.md#u-09--有证据的装饰接口缺项可带限制放行) · [U-10](decisions.md#u-10--六题整体不退步并展示局部改善)

## 连接装配、知识查询和零件覆盖

- 从搭建意图查件可用 `bricklayer.find_connection_pairs`。已有模型可用 `find_connection_candidates` 查安装姿态，`trial_connections` 试装，`place_connection` 提交。内置代理和外部代理可收到同一组执行原因码、现场证据和修复建议。[连接驱动装配提案](proposals/P-001-connection-driven-building.md)
- 搭建问题、零件号和诊断码可交给 `brickwright knowledge search`，再用 `brickwright knowledge show` 读取步骤、可执行示例和来源。[知识库验收结果](proposals/archive/P-002-construction-knowledge.md#实现与验收结果)
- `bricklayer.lookup_part` 可离线查询 LDraw 2026-10 的全部 24,985 个官方顶层 ID，返回建模及碰撞能力、端点、已验证安装姿态或数据缺项。[覆盖统计与数据限制](proposals/archive/P-003-official-part-coverage.md#实现与验收结果)

P-001 的 Maestro 外部代理首轮对照未通过连接占比条件。补全反向安装候选，并加入整扇门试装的可执行示例后，原五题每版各做两次，连接放置从 40/235 升至 56/239，两版均完成 10/10，质量均分均为 6/6。附加两题两版均完成 3/4。修复后的四个舱门试次全部采用候选试装，原生代理的整体验收仍待运行。[评测报告](design/p001-paired-evaluation.md#铰链修复复测结果) · [复测快照](http://localhost:8420/snapshots/snp_01m81459cgpjb3nee5gkwjz5tt)

本次集成的 pytest 在启用冻结清单比对后通过 3,069 项，跳过 6 项。用本地 LDraw 库单独复跑其中两项渲染测试均通过，其余为三项需要原始 BrickNet 输入的提取测试和一项真实模型测试。两套发布数据生成器的 `--check`、Ruff 和 ty 均通过。从两个 wheel 在仓库外重查了官方全集，并试装了能通过五个姿态采样的铰链。

铰链修复分支配置本地 LDraw 库后，pytest 通过 3,072 项，跳过 4 项（三项缺原始 BrickNet 输入，一项为按需启用的原生代理实测）。Ruff 检查、格式检查和 ty 均通过。
