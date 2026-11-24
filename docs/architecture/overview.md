# BrickWright 架构

**Pending confirmation**。此说明依据当前代码整理。

- 工作区中的 `bricklayer-sdk` 通过 `bricklayer` 提供建模 API。
  [`LegoBuild`](../../packages/bricklayer/src/bricklayer/modeling/build.py) 组合零件和子模型，
  提供碰撞、支撑和连通性查询。[LDraw 写出](../../packages/bricklayer/src/bricklayer/io/writer.py)
  将模型转换为可交换的文件。
- 宿主代理加载 skill 后，通过 `brickwright external` 操作记录，通过 `bricklayer` 选件、搭建与试装。宿主提供推理和会话，workflow 库管理记录、结构检查、比较与交付。[BrickWright CLI](../../packages/brickwright/src/brickwright/cli.py) · [Record](../../packages/brickwright/src/brickwright/record.py)
- 记录保存可直接编辑的 `build.py`、元数据、验证快照和输出。正式交付绑定到已验证的整版 MPD，接收端检查实际提交文件。

验证与导出之间的约束见[现状设计](../design/validation-and-export.md)。

## 代理怎样找到零件和搭法

- [官方零件查询](../../packages/bricklayer/src/bricklayer/parts/official.py)区分指定发布版收录的 ID、能构造为 `Element` 的 ID，以及有标注的端点。端点带局部坐标、方向和来源。现有 `PartSpec` 和 `search_parts` 继续面向建模目录。
- [连接装配](../../packages/bricklayer/src/bricklayer/modeling/assembly.py)把目录中的静态配合带到具体模型。给定宿主和装配要求，可以查候选、试装、读取失败原因，再把选定的零件或子模型放入模型。
- [搭建知识库](../../packages/brickwright/src/brickwright/knowledge/__init__.py)按任务描述、零件号或诊断码索引随包提供的条目。skill 页面与 `brickwright knowledge search` / `show` 共用条目数据、示例和署名。

## 分阶段搭建

新建记录可选择流程版本。记录保存原始题目以及可独立更新的特征与规划，再按父源码、目录数据、子快照清单和 MPD 字节冻结整件。CLI 调用同一套 workflow 库。模块子记录封存在父记录下，结构检查在整件中复核契约指定的端点和运动范围。[流程层](../../packages/brickwright/src/brickwright/flow/) · [模块层](../../packages/brickwright/src/brickwright/modules/)

固定机位评审读取待交付的 MPD，由主代理自评或在独立会话中对照目标图和特征。评审批次保存图像、输入摘要、会话凭据和逐项结论；模型或比对输入变化后需要重新取得适用于当前版本的评审。[评审实现](../../packages/brickwright/src/brickwright/aligned_review.py) · [流程放行](../design/validation-and-export.md)
