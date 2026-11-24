# P-009 — 流程命令的可执行报错

Status: draft

## 问题与目标

宿主代理靠命令输出决定下一步。`validate`、`finalize` 和 `status` 已返回诊断码、证据和 `next_action`，代理照着做即可。其余流程命令出错时没有这一层：

- 记录、产物、评审、渲染和交付几块合计约 80 处直接抛 `ValueError`。CLI 包装层把它们统一记为 `flow.invalid_input`，`next_action` 为空。很多消息只说出了什么问题，没给命令；有些用的是内部说法，例如 “frozen review manifest bindings changed”。
- 同一段消息在 `error`、`message` 和 `evidence.exception_message` 里重复三遍。评审回执缺字段时，pydantic 的 8 条报错会膨胀到约 4 KB。
- `features`、`plan`、`review --set` 校验失败时，`typer.Exit` 被包装层当作错误再包一次，标准输出出现两段 JSON，代理无法解析。
- 子进程抛异常时只返回 “exited with code 1”，原因散落在 stderr。未安装 LDraw 库时，`render` 就只给出这一句，`next_action` 还让代理去看 worker 的 stderr。
- 先 `compare` 后 `render` 时返回原始的 `FileNotFoundError` 路径；先请求评审说明后验证时，只说缺结构快照，不给命令。`verify-delivery` 拒收时没有下一步。

P-008 验收中，参考图命名错误导致对比失败，随后评审说明又转去了视角匹配，已在 P-008 修复。本提案把同样的处理推广到全部流程命令：每次出错只输出一个 JSON 对象，给出稳定的诊断码和能直接执行的下一步。用户 2026-11-23 确认不做轨迹统计，直接整体修改。

## 方案

### 统一的错误类型

复用 BrickLayer 已有的 `DiagnosticError(ValueError)`：它带 `code`、`evidence` 和 `next_action`，CLI 包装层和 `exception_diagnostic` 已能识别，模块流程的 `ModuleError` 就是它的子类。BrickWright 增加 `FlowError` 子类。各报错点按场景选码，`next_action` 写出带记录 ID 的完整命令。

| 场景 | 诊断码 | 下一步 |
| --- | --- | --- |
| 跳过了前置步骤：缺特征或规划、结构未验证或未通过、未渲染、未对比、尚未定稿 | 沿用 `flow.features`、`flow.plan`、`flow.structure`；新增 `review.render_missing`、`review.comparison_missing`、`delivery.not_finalized` | 对应的 `features`、`plan`、`validate`、`render`、`compare`、`finalize` 命令 |
| 源码或输入在上一步之后改变 | `flow.changed`（定稿已在用） | 重新验证或渲染，或查看 `status` |
| 评审回执内容不合格 | `review.report_invalid`，证据列出出错的字段或编号 | 修正后重新提交的命令 |
| 回执对应的轮次已过期、已提交过另一份或会话冲突 | `review.round_stale`、`review.session` | 重新请求评审说明 |
| 冻结评审包被改动 | `review.packet_changed` | 重新请求评审说明 |
| 参考图、机位文件、题目 JSON 等输入不合格 | `review.references_missing`、`flow.invalid_input` | 说明期望的格式或文件名 |
| 找不到记录 | `record.not_found` | `brickwright ls --records-dir DIR`，并提示 `BRICKWRIGHT_RECORDS_DIR` |
| 渲染缺 LDraw 库 | `render.ldraw_missing` | 设置 `LDRAW_LIBRARY_PATH` |

没有专门下一步的流程错误，由包装层补上 `brickwright external status ID`，前提是记录可读、当前命令不是 `status` 本身。`status` 会列出全部阻断项及其命令。

### 输出形状

- 每次调用只输出一个 JSON 对象。`typer.Exit` 不再被包装层捕获。
- `error` 与 `diagnostics[0].message` 保留同一句话。`evidence` 只在异常信息与消息不同时才附上原文。
- pydantic 校验错误压缩为一句摘要，例如 “Report has 8 invalid fields: conditions (missing), …”。证据里按 `path`、`message` 逐条列出，链接和输入回显去掉。`features`、`plan` 已有的逐字段诊断不变。
- `verify-delivery` 的结果增加 `code` 和 `next_action`，拒收原因保持现有措辞。

### 子进程失败

worker 捕获普通异常，把异常类型和消息交回父进程，父进程抛出 `ChildFailed`（`ChildExited` 的子类），消息形如 `LDrawLibraryNotFound: Could not find an LDraw parts library …`。stderr 不再出现 traceback，已有的 `ChildExited` 判断和失败报告路径不变。真正的崩溃或 `SystemExit` 仍报退出码。`render` 和 `compare` 渲染前先检查 LDraw 库。

### 其他使用摩擦

`knowledge search` 接受多个词，按空格合并后查询，例如 `knowledge search hinge door` 不再报参数多余。

## 备选与取舍

- **先按轨迹统计高频报错，只改前几名。** 用户选择直接全部修改。报错点数量有限，逐条改的成本可控，也免得低频的路径继续给出没有下一步的消息。
- **新建独立的错误层级，替换 `DiagnosticError`。** 现有的类型、包装层和模块错误已经能表达所需信息，另建一套只会多出转换。
- **只靠包装层统一补 `status`。** 实现最省事，但代理还要多跑一次命令。这只作为兜底，常见场景直接给出命令。

`fix/flow-review-friction` 基于 P-008 之前的 develop，也修改 `aligned_review.py` 和 `cli.py`，并修改已删除的提示文件。合入本提案后，这条分支需要重新基于 develop 处理冲突。

## 验证

- 新增 CLI 层回归：按 P-008 验收中实际出现的顺序错误（先评审后验证、先对比后渲染、缺参考图、回执缺字段、定稿前交付核对、记录 ID 错误、缺 LDraw 库）逐条调用命令。断言标准输出可解析为单个 JSON，诊断码符合上表，`next_action` 含该记录 ID 的完整命令。
- 子进程异常的类型和消息出现在报告里，stderr 中没有 traceback。
- `features`、`plan`、`review --set` 校验失败时只输出一个 JSON。
- 运行 CI 的全部检查。
