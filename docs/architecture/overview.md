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
