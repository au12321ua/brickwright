# BrickWright 架构

**Pending confirmation**。此说明依据当前代码整理。

- 工作区中的 `bricklayer-sdk` 通过 `bricklayer` 提供建模 API。
  [`LegoBuild`](../../packages/bricklayer/src/bricklayer/modeling/build.py) 组合零件和子模型，
  提供碰撞、支撑和连通性查询。[LDraw 写出](../../packages/bricklayer/src/bricklayer/io/writer.py)
  将模型转换为可交换的文件。
- [BrickWright CLI](../../packages/brickwright/src/brickwright/cli.py) 有两条入口。
  `generate` 由 [`run_agent`](../../packages/brickwright/src/brickwright/driver/loop.py) 驱动模型与工具，
  `external` 供外部代理直接操作记录。两条路径共用
  [`Record`](../../packages/brickwright/src/brickwright/record.py) 和相同的验证、渲染及导出工具。
- 记录目录保存可编辑的 `build.py`、元数据、验证快照和输出。工具用 `build.py` 创建
  BrickLayer 模型，再将报告、LDraw 或预览写回该记录。修改记录和输出协议时，
  同时检查 CLI 两条入口、`Record` 与工具的调用约定。

验证与导出之间的约束见[现状设计](../design/validation-and-export.md)。

## 代理怎样找到零件和搭法

- [官方零件查询](../../packages/bricklayer/src/bricklayer/parts/official.py)区分指定发布版收录的 ID、能构造为 `Element` 的 ID，以及有标注的端点。端点带局部坐标、方向和来源。现有 `PartSpec` 和 `search_parts` 继续面向建模目录。
- [连接装配](../../packages/bricklayer/src/bricklayer/modeling/assembly.py)把目录中的静态配合带到具体模型。给定宿主和装配要求，可以查候选、试装、读取失败原因，再把选定的零件或子模型放入模型。
- [搭建知识库](../../packages/brickwright/src/brickwright/knowledge/__init__.py)按任务描述、零件号或诊断码索引随包提供的条目。内置工具和 `brickwright knowledge search` / `show` 共用正文、示例和署名。

新旧代理的行为评估使用[配对评测入口](../../packages/brickwright/src/brickwright/evaluation/p001.py)。它冻结任务、推理设置和预算，分别用两版工作树里的原生代理生成模型。运行结果用于检验代理行为；SDK 的装配测试可独立在本地运行。
