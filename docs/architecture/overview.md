# BrickWright 架构

- 工作区中的 `bricklayer-sdk` 通过 `bricklayer` 提供建模 API。
  [`LegoBuild`](../../packages/bricklayer/src/bricklayer/modeling/build.py) 组合零件和子模型，
  提供碰撞、支撑和连通性查询。[LDraw 写出](../../packages/bricklayer/src/bricklayer/io/writer.py)
  将模型转换为可交换的文件。
- 宿主代理加载 skill 后，通过 `brickwright external` 操作记录，通过 `bricklayer` 选件、搭建与试装。宿主提供推理和会话，workflow 库管理记录、结构检查、比较与交付。[BrickWright CLI](../../packages/brickwright/src/brickwright/cli.py) · [Record](../../packages/brickwright/src/brickwright/record.py)
- 记录保存可直接编辑的 `build.py`、元数据、验证快照和输出。正式交付绑定到已验证的整版 MPD，接收端检查实际提交文件。

验证与导出之间的约束见[现状设计](../design/validation-and-export.md)。

## 代理怎样找到零件和搭法

- [官方零件查询](../../packages/bricklayer/src/bricklayer/parts/official.py)区分指定发布版收录的 ID、能构造为 `Element` 的 ID，以及有标注的端点。端点带局部坐标、方向和来源。现有 `PartSpec` 和 `search_parts` 继续面向建模目录。
- [连接装配](../../packages/bricklayer/src/bricklayer/modeling/assembly.py)把目录中的静态配合带到具体模型。给定已放置的零件和装配要求，可以查候选、试装、读取失败原因，再把选定的零件或子模型放入模型。

## skill 的内容从哪里来

`brickwright skill install --target DIR` 将手写说明和按当前两个包生成的参考页安装到同一目录，并在入口写入实际的 Python 路径。维护时按内容选择源文件。

| 宿主读到的内容 | 维护位置 |
| --- | --- |
| 起步示例、流程、模块和评审说明 | [skill 模板和安装器](../../packages/brickwright/src/brickwright/skill/) |
| 可按任务、零件号和诊断码查到的案例 | [条目 JSON](../../packages/brickwright/src/brickwright/knowledge/entries/) 和 [Python 示例](../../packages/brickwright/src/brickwright/knowledge/examples/)。检索和安装器都按目录发现条目，可一次添加多个案例，页面保留来源与许可。 |
| 选件、放置、坐标、连接、运动、验证与导出六个 API 主题 | [BrickLayer 参考生成器](../../packages/bricklayer/src/bricklayer/docs/compilation.py)。签名和说明取自 SDK，颜色表和工厂参数另页展开。 |

例如，`brickwright knowledge search '舱门翻开'` 返回 `hinged-panels` 和安装目录内的相对路径，`brickwright knowledge show hinged-panels` 返回案例及示例。安装后的 API 索引、知识页和示例由[安装测试](../../packages/brickwright/tests/test_skill.py)检查链接。

## 分阶段搭建

用 `external init --flow --task-json FILE` 创建流程记录。记录保存原始题目以及可独立更新的特征与规划，再按父源码、目录数据、子快照清单和 MPD 字节冻结整件。CLI 调用同一套 workflow 库。模块子记录封存在父记录下，结构检查在整件中复核契约指定的端点和运动范围。[流程层](../../packages/brickwright/src/brickwright/flow/) · [模块层](../../packages/brickwright/src/brickwright/modules/)

固定机位评审读取待交付的 MPD，由主代理自评或在独立会话中对照目标图和特征。评审记录保存图像、输入摘要、评审方式和逐项结论。模型或比对输入变化后，需要重新评审当前版本。[评审实现](../../packages/brickwright/src/brickwright/aligned_review.py) · [流程放行](../design/validation-and-export.md)
