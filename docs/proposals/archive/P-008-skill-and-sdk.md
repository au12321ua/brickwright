# P-008 — 精简为 skill 与 SDK

Status: completed

## 问题与目标

OMR36 的 40 次搭建尝试留下了 2,405 条 shell 命令。35 次尝试读过包源码，合计 405 次。36 次尝试遇到过 Python 找不到模块，合计 88 次。代理会打开源码查连接端点的字段，也会为了提交一份评审回执去读流程实现。固定机位渲染有了直接命令后，基线和候选中读取渲染工具源码的次数分别为 91 和 1。样本包含不同任务、重试和预算，这些观察用于定位搭建中的障碍。统计来源为 2026-11-23 的本地轨迹普查，见主工作树 `.local/agent-build-flow/census/`。

提案前的外部入口每次打印指引都附上完整 API 参考，基线版共 1,667 行。搭建知识已经支持按任务、零件号和诊断码检索。可以利用这个入口，让代理在遇到具体问题时拿到一页参考和一个可运行的例子，也让作者能按主题成批添加案例。[现有知识查询](P-002-construction-knowledge.md#查询及代理接入)

演示面向 Codex、Claude Code 等宿主代理。代理决定选件、搭法、是否拆模块、何时试装和返工。例如，主代理可以直接在 `build.py` 里试搭，到需要把活动舱盖拆成子模型时再查接口。BrickLayer 提供建模与检查，BrickWright 提供可调用的 workflow 库和命令行，保留记录、快照、整版结构检查、渲染、评审绑定与导出。宿主负责模型调用，移除内置代理和 LiteLLM。[U-11](../../decisions.md#u-11--在-fork-内精简为-skill-与-sdk) · [U-12](../../decisions.md#u-12--演示从简并允许调整数据集与接口)

## skill 怎么读、怎么扩充

skill 随 fork 的 `brickwright` 包提供。用 `brickwright skill install --target DIR` 把同版本内容放入宿主可读的目录。演示任务准备环境时调用一次，并把实际可导入两个包的 Python 路径交给代理。首期从当前仓库安装即可。

| 代理当前要做什么 | 读取什么 |
| --- | --- |
| 开始搭建 | 根 `SKILL.md` 给出环境入口、一个能创建记录并放好两块砖的起步示例，以及校验和导出的最短路径。页尾给出知识查询、API 目录和工作流参考的入口。 |
| 找搭法或修复错误 | 用已有的 `knowledge search` 查询自然语言、零件号或诊断码。匹配项给出适用条件和 `references/knowledge/<id>.md` 路径，打开后可读步骤、预期结果和来源，按需取用对应的 Python 示例。诊断已有条目 ID 时可直接跳到该页。 |
| 查一个 SDK 调用 | `references/api/index.md` 按搭建问题指向下表的主题页。知道符号名时也可直接在这个目录搜索。 |
| 需要分块、评审或处理交付阻断 | 分别读 `references/modules.md`、`review.md` 或 `flow.md`。页面给出此时所需的命令、输入和结果。 |

初版沿用现有 17 个条目、16 个示例。作者按主题提交一批条目和示例，每个条目沿用稳定 ID、别名、零件号、诊断码和来源字段。用同一份条目数据生成 skill 页面和查询结果，按目录发现新增内容。新增一批车轮或铰链案例，只需加入这批资料，根说明和既有条目无需跟着编辑。每条的署名及许可随页面分发。[条目与示例](../../../packages/brickwright/src/brickwright/knowledge/) · [内容许可](../../../packages/brickwright/src/brickwright/knowledge/CONTENT-LICENSE.md)

### API 按搭建动作拆页

建议先用六个主题。一个方法的签名和返回值只在所属页展开，同一类的方法可按用途分到不同页。完整颜色表和构件工厂的合法参数组合单独生成，查到具体颜色或具体工厂时再打开。现有生成器已能从 docstring、方法清单和注册表生成这些内容，实施时按组输出 Markdown 文件。[API 生成器](../../../packages/bricklayer/src/bricklayer/docs/compilation.py)

| 主题页 | 回答的问题与代表接口 |
| --- | --- |
| `parts.md` 选件 | 已知编号能否使用、未知编号怎么搜、怎样实例化并选颜色。含 `lookup_part`、`search_parts`、`Element`、`Color`，并链接颜色表与各工厂的参数页。 |
| `placement.md` 放置与组合 | 两块砖怎么叠、怎样重复构件或嵌入子模型。含 `LegoBuild.add`、`stack`、`Placement` 和填充助手。 |
| `geometry.md` 坐标与变换 | stud 和 plate 如何换算、位置处于哪个坐标系、旋转的对象是什么。含 `Position`、`Pose`、`Rotation` 和模型变换。 |
| `connections.md` 连接与试装 | 从配对、筛选端点到试装和提交。将 `Connector.position/axis`、`find_connection_pairs`、`find_connection_candidates`、`trial_connections`、`place_connection` 放在同页，展示失败时从哪里取证据。 |
| `motion.md` 运动 | 怎样声明铰链行程、检查开启方向并定位干涉。含 `MotionRange`、`set_motion`、`posed`、`check_motion` 及返回的采样结果。 |
| `validation-export.md` 检查与导出 | 碰撞、悬空、断开分别查哪里，如何写出模型。含 `colliding_placements`、`floating_placements`、`connected_components` 和 `write_ldraw`。整件交付命令链接到 `flow.md`。 |

搭活动舱盖时，搜“舱门翻开”可命中 `hinged-panels`，直接取用整扇舱盖试装的例子。需要解释某个失败姿态时再打开运动页。遇到 `connector.ambiguous` 则可直接读孔位歧义案例，再按需查端点字段。[舱盖案例](../../../packages/brickwright/src/brickwright/knowledge/entries/hinged-panels.json) · [孔位案例](../../../packages/brickwright/src/brickwright/knowledge/entries/diagnose-ambiguous-ports.json)

## demo 的评审与数据集

主代理可以在同一会话直接对照目标图和当前成品图自评，也可以交给独立的宿主子代理。两种方式都能完成演示，报告标明实际评审方式、所看版本、结论和遗留项。图上看不到的内部连接从该版本的结构检查取证。可复用 `fix/flow-review-friction` 中的解释器提示与结构证据处理。[现有评审与结构检查的分工](../P-005-aligned-review.md#评审代理)

选一道有目标图和机位的 OMR36 题。数据集中的任务说明、附件安装、旧命令及验证器如果妨碍新的入口，直接适配本题，让代理从已安装的 skill 开始，交付题目要求的模型文件。正式六题配对及其冻结环境属于此前的评测。[原评测口径](../../design/omr36-evaluation/README.md)

demo 只接收 `finalize` 成功的那一版模型，接收时逐字节核对提交的 MPD 与该版本已验证快照中的 MPD 一致。[U-13](../../decisions.md#u-13--只交付定稿快照并在开发后实测)

## 实施范围与验收

1. 在干净的任务分支复用能直接服务演示的 SDK 和 workflow 代码。接口、记录格式和任务入口按目标选择，发生冲突时可以直接改。优先沿用已有可用指令，兼容别名、旧记录迁移和旧评测复放均可省去。
2. 建立 skill、知识条目页和分主题 API 页。复用已有知识检索、示例测试及文档生成器。用一个小回归检查同批新增的条目能被检索，且返回的页面和示例可打开。
3. 移出 `driver/external_critique.py` 中仍需的说明，删除内置循环、工具注册表、仅供内置代理的提示与配置、`generate`、`gallery` 和 LiteLLM。随原生评测一起清理 `p001.py`、`p001_native.py` 及直接依赖前者的 `p001_review.py`。P-001 的原生运行不列入本次验收，已有外部结果可在原版本查阅。实施时同步更新 CLI 用法与交接。
4. 开发中运行本地测试、lint 和类型检查，覆盖成功交付及拒收错误版本。真实宿主代理验收安排在开发完成之后，只跑上述一道题。宿主加载 skill，按需取用参考，完成搭建、结构检查、读图评审与导出，留下一份 MPD 和注明评审方式的交付记录。

复用已有装配、结构检查和渲染能缩短实现时间。自评由搭建者判断自己的成品，单题结果只说明这条演示路径是否跑通。

## 开发交付

宿主现在可安装 skill，按搭建动作打开六个 API 主题，或按诊断码、零件号和意图取得知识条目及其示例。内置代理和 LiteLLM 已移除。自评与独立评审共用冻结图像，交付回执记录评审方式、快照 ID 和 MPD 的 SHA-256。[使用入口](../../../packages/brickwright/README.md)

`external verify-delivery ID FILE` 已接入蓝色动力喷气机单题的接收端。离线集成测试用两块砖走通结构验证、自评、定稿和接收，并覆盖缺少定稿、文件字节改变和评审依据过期时的拒收。工作区测试通过 2,737 项，跳过 7 项，Ruff 检查、格式检查和 ty 通过。两份 wheel 和 Harbor 任务可在本地准备，真实宿主代理验收按 U-13 留到开发后。[单题准备](../../../demo/omr36/README.md) · [交付回归](../../../packages/brickwright/tests/test_flow.py)
