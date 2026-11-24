# BrickWright 交接

P-008 和 P-009 已完成。Codex / Claude Code 加载项目内的 skill，直接编辑记录中的 Python 模型。每条 `external` 命令只输出一个 JSON，宿主按其中的 `next` 推进或修复：执行命令、改文件后执行，或补充题目输入后执行。[CLI 结果与下一步](design/validation-and-export.md#cli-结果与下一步)BrickLayer 提供零件、连接、试装和 LDraw 导出，BrickWright 管理记录、结构快照、渲染、评审和定稿。安装和两块砖的起步任务见[根 README](../README.md)，开发检查见[维护约定](../AGENTS.md#阅读与验证)。

## 单题演示的交付状态

本地 OMR36 蓝色动力喷气机（R-31039）的准备说明、Harbor 脚本、接收端及其测试存放在维护者检出的 `.local/demo/omr36/`。准备脚本把当前检出构建的两个 wheel、宿主 skill 和接收端放入原题。搭建者用原题 JSON 创建流程记录，检查结构并对照同版本渲染图，再定稿。接收端核对原题，并逐字节比较提交的 MPD、成功定稿的导出与已验证快照。真实宿主跑题留在开发完成后。验收时已在构建出的演示镜像中用脚本走完自评两轮用尽、定稿和接收，另起容器评分。提交文件改字节、定稿后改源码和替换题目三种情况均记零分。P-009 后，在重建的镜像中用只依据 `next` 推进的脚本，再次走完漏填回执的修复、返工、轮数用尽、定稿和接收，独立容器评分有效。[P-008 实施结果](proposals/archive/P-008-skill-and-sdk.md) · [U-13](decisions.md#u-13--只交付定稿快照并在开发后实测)

## 接手具体改动

- 扩充搭建知识或调整 API 参考时，修改随包维护的条目、示例或 SDK 生成器，重新安装 skill 即可查看宿主读到的页面。[内容来源](architecture/overview.md#skill-的内容从哪里来)
- 新增或修改流程命令的报错时，抛出带诊断码的 `FlowError`，用参数和占位符描述动作，不要在库代码里拼命令字符串。首选动作的顺序集中在 `flow/guide.py`。[恢复回归](../packages/brickwright/tests/test_recovery.py)
- 修改结构检查和交付条件时，先核对普通记录与流程记录的区别、摘要的失效条件，以及接收端实际读取的文件。[验证与导出](design/validation-and-export.md)
- 查询旧评测时，P-001 的舱门配对、P-004 至 P-006 的六题协议和 P-007 的官方装配覆盖均有独立记录。[舱门复测](design/p001-paired-evaluation.md#铰链修复复测结果) · [六题协议](design/omr36-evaluation/README.md) · [官方装配](design/p007-official-assembly-evaluation.md)

各提案当前状态以[提案索引](proposals/README.md)为准。
