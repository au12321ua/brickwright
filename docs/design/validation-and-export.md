# 验证、源文件和导出

[`build_and_validate`](../../packages/brickwright/src/brickwright/tools/validation.py)
在一次调用中用 `create_build()` 实例化模型，供默认的无参数构建测试和导出共用。
验证始终检查碰撞和悬空，附加检查由记录的 `run_tests()` 提供。
模型可包含多个独立支撑的组件，单一连通组件可按任务需要作为额外测试。

验证和正式导出都通过[子进程](../../packages/brickwright/src/brickwright/tools/build_execution.py)
执行捕获的源码快照。验证报告用 SHA-256 关联实际检查的源码。
如果写回报告时检测到源码变化，记录 `SourceChanged`，需要对当前源码重新验证。
[`Record.write_build`](../../packages/brickwright/src/brickwright/record.py)
改动源码时会清除旧导出和当前报告，保留历史快照。
`Record.read_meta` 会对照当前源码摘要计算状态，因此外部编辑也会使旧的验证和最终标记失效。

[`finalize`](../../packages/brickwright/src/brickwright/tools/finalization.py)
先验证即将导出的模型。成功时写出 LDraw，并在渲染功能可用时写出预览。
最终失败会清除导出文件，成功时才保存最终标记和导出源码摘要。
相关行为由[验证测试](../../packages/brickwright/tests/tools/test_validation.py)和
[正式导出测试](../../packages/brickwright/tests/tools/test_finalization.py)覆盖。

## 代理收到的诊断

workflow 接口和外部 CLI 在失败报告的 `diagnostics` 中给出原因码、失败阶段、说明、证据及可行的下一步。源码异常指向实际执行快照中的用户代码，静态或运动验证失败可定位到实例、零件对和采样姿态。验证报告的 `report_path` 相对于记录目录，指向此次验证的存档。历史报告缺少诊断字段时按空列表读取。

正式导出从嵌套的 `validation.diagnostics` 传递模型验证问题。模型通过检查后发生的渲染或导出错误出现在正式导出报告自己的 `diagnostics` 中。知识库可用稳定原因码检索，例如 `brickwright knowledge search connector.ambiguous`。[诊断数据结构](../../packages/brickwright/src/brickwright/tools/diagnostics.py) · [执行诊断测试](../../packages/brickwright/tests/tools/test_execution_diagnostics.py) · [从歧义孔位修复到导出](../../packages/brickwright/tests/tools/test_connection_repair.py)

## 连接查询与试装证据

宿主代理的 Python 脚本可以调用同一 SDK，再查看两个证据字典。

```python
query = {}
candidates = build.find_connection_candidates(
    host, content=incoming, target_rotation=aim, evidence=query
)
trials = {}
build.trial_connections(candidates, evidence=trials)
print(query, trials)
```

`query` 包含查询上限、返回数、请求的筛选和双方最多八个端点样例。端点同时给出零件局部和查询坐标系的类型、位置、轴向及侧向。在具体两端均有端点时，空结果可定位到宿主筛选、占用、配合规则或安装筛选等阶段。目录候选按限额查找，证据不足时标为未知。查询上限为零时记录限额。

`trials` 给出试装上限、实际检查数、通过数、各拒因计数和前三个失败候选的原序号。传入有长度的容器时同时给出候选总数，迭代器的总数为空。碰撞样例标出双方端点、拟合位姿及冲突实例。提供目标时，报告以度计的旋转误差和以统一 stud 为单位的端点位移。对照成品时，需另核对零件原点的拟合平移。调用继续返回有序元组。

## 启用流程的记录

创建记录时指定 `--flow --task-json FILE` 可导入原始条件。先通过 `external features` 和 `external plan` 记录特征与事先声明的独立物件、装饰简化项以及模块接口。每次整件验证同时检查记录内模型与干净进程中从相同 MPD 字节重读的标准模型，核对件数、连通组件、具体断口、碰撞和支撑。可检索的报告保留完整实例清单，代理收到结构摘要和报告位置。[结构接口](../../packages/brickwright/src/brickwright/flow/structure.py) · [两种 SDK 的归档复放](p004-structure-evaluation.md)

`external status ID` 显示结构快照、当前评审和阻断条件。模块接口必须实际接合。装饰断口只在有事前声明、同件对与位姿的版本化缺项证据且两栏物理检查均合格时列为交付限制。改动父源码、规划、合并清单或校验数据会使结构证据过期。渲染、评审和定稿绑定已检查的整件。最后一轮需要修改时，只有有效预算用尽、当前版已评且列明遗留项，才允许带遗留项交付。[U-09](../decisions.md#u-09--有证据的装饰接口缺项可带限制放行) · [状态计算](../../packages/brickwright/src/brickwright/flow/state.py)


## demo 交付接收

`brickwright external verify-delivery ID FILE` 读取当前流程记录。成功定稿后，接收端核对提交文件、导出件和已验证的结构快照的 MPD 字节，并返回快照 ID、MPD 摘要及实际评审方式。只做过验证、定稿失败、版本过期或提交文件有改动均拒收。单题评分器先核对原始题目再使用同一接口。[交付校验](../../packages/brickwright/src/brickwright/tools/finalization.py) · [单题接收端](../../demo/omr36/native_record.py)
