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

内置工具和外部 CLI 在失败报告的 `diagnostics` 中给出原因码、失败阶段、说明、证据及可行的下一步。源码异常指向实际执行快照中的用户代码，静态或运动验证失败可定位到实例、零件对和采样姿态。验证报告的 `report_path` 相对于记录目录，指向此次验证的存档。历史报告缺少诊断字段时按空列表读取。

正式导出从嵌套的 `validation.diagnostics` 传递模型验证问题。模型通过检查后发生的渲染或导出错误出现在正式导出报告自己的 `diagnostics` 中。知识库可用稳定原因码检索，例如 `brickwright knowledge search connector.ambiguous`。[诊断数据结构](../../packages/brickwright/src/brickwright/tools/diagnostics.py) · [执行入口一致性测试](../../packages/brickwright/tests/tools/test_execution_diagnostics.py) · [从歧义孔位修复到导出](../../packages/brickwright/tests/tools/test_connection_repair.py)
