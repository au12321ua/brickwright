# P-009 — 流程命令的可执行恢复

Status: draft

## 问题与目标

宿主代理靠命令输出决定下一步。验收目标定为：**宿主拿到一次失败或阻断的结果后，只凭输出就能在同一条记录上完成下一步修复。** 报错整齐只是手段。代理照着输出做，仍可能重试错文件、白占评审轮次或误以为可以交付，这些都算不达标。

现状的问题来自 P-008 验收后的探针，以及 Codex 的只读审计（2026-11-23）：

- **动作不能照做。** `validate`、`finalize`、`status` 虽然给出 `next_action`，但命令是固定拼出来的。用 `--records-dir` 和 `--set 'my plan.json'` 提交无效规划后，返回的命令丢了记录目录，文件名也换成了 `plan.json`。命令里也不带当前解释器，PATH 上没有 `brickwright` 时同样无法执行。
- **没有动作。** 记录、产物、评审、渲染和交付几块约有 80 处直接抛 `ValueError`。包装层把它们一律记为 `flow.invalid_input`，`next_action` 为空。先 `compare` 后 `render` 会返回原始文件路径；先请求评审说明、还没验证时，只说缺结构快照。`verify-delivery` 拒收时也没有下一步。
- **输出不能稳定解析。** 无效的 `plan --set` 会输出两段 JSON，共约 8.9 KB：完整 schema 加一段由 `typer.Exit` 被重复包装而来的 `{"error": "1"}`。空评审报告输出约 4 KB，同一段 pydantic 报错重复三遍。给 `validate` 或固定机位 `render` 一个不存在的记录 ID，stdout 为空，stderr 是 rich traceback，原因是这两条命令在进入包装层前就加载了记录。
- **评审的“成功”有歧义。** `review --set` 收到过期回执（`stale`）或评审失败的尝试（`failed`）时都退出 0，计入的轮数都是 0，而且这一轮的 ID 被永久占用。`review ID` 展示的 schema 只是失败尝试的格式，只要求两个字段；提交普通报告实际要求八个字段。
- **子进程原因丢失。** worker 抛异常时只返回 “exited with code 1”，traceback 落到 stderr，诊断还让代理去看 stderr。未安装 LDraw 库时 `render` 就是这样。
- **`status` 不给首选动作。** 新建的记录同时列出特征、规划、结构和评审四项阻断，其中几项还缺前置条件，由代理自己挑顺序。

用户 2026-11-23 确认不做轨迹统计，整体修改；评审先不跑真实宿主实测。

## 方案

### 结果信封

给宿主用的流程命令（`external` 下的记录、产物、验证、渲染、对比、评审、定稿、交付核对和模块命令）统一输出形状和退出码：

- stdout 只输出一个 JSON 对象，包含 `ok`。成功时附命令自身的结果。失败或被阻断时，`error` 给一句原因，`diagnostics` 逐条列出，`next` 给出**一个**首选动作。
- 退出码 0 表示本次操作达到目的，1 表示失败或被阻断。参数错误、记录加载失败、校验失败、交付拒收和子进程失败都走这个信封，不再出现空 stdout 或 traceback。`validate` 和 `render` 改为在包装层内加载记录。
- 每段文字只出现一次：`evidence` 只在异常原文与消息不同时附上原文。pydantic 错误压成一句摘要，证据按 `path` 和 `reason` 逐条列出，链接和输入回显去掉。提交失败时不再附完整 schema 和报告原文，需要时用不带 `--set` 的命令读取。
- `validate`、`finalize` 和 `verify-delivery` 现有报告的字段保留，只补上 `next`。`verify-delivery` 不另设一套顶层错误字段。

### 动作

`Diagnostic` 增加结构化的 `action`，与 `next` 同形：

| `kind` | 含义 | 字段 |
| --- | --- | --- |
| `run` | 执行一条命令 | `argv`（参数列表）及其 shell 转义后的 `command` |
| `edit` | 修改文件后再执行某命令 | `path`、要改的位置（字段路径或编号），以及修改后的 `then` |
| `provide` | 需要宿主补充外部输入 | 期望的路径或文件名，以及补齐后的 `then` |

动作只在 CLI 边界生成，用本次调用的解释器（`sys.executable -m brickwright`）、实际记录目录、完整记录 ID 和实际文件路径拼出参数。业务代码只返回诊断码和证据，例如缺失的视图名、出错的字段路径，不自己拼命令。`next_action` 字符串保留，取 `command` 或一句说明，兼容现有读取方。

### 诊断码

复用 BrickLayer 已有的 `DiagnosticError(ValueError)`，它带 `code`、`evidence` 和 `next_action`，模块的 `ModuleError` 就是它的子类。BrickWright 增加 `FlowError` 子类，现有约 80 处按场景归类：

| 场景 | 诊断码 | 首选动作 |
| --- | --- | --- |
| 缺特征或规划、结构未验证或未通过 | 沿用 `flow.features`、`flow.plan`、`flow.structure` | 编辑并提交产物，或运行 `validate` |
| 未渲染，或模型、机位在渲染后改变 | `review.render_missing`、`review.render_stale` | `render`；只有模型或机位确实变化时才要求重渲 |
| 未对比，或缺参考图 | `review.comparison_missing`、`review.references_missing` | `provide` 期望的 `goal-<机位>.png` 路径，再运行 `compare` |
| 评审回执字段或内容不合格 | `review.report_invalid`，证据给出字段路径或条件编号 | `edit` 原报告，再向同一轮提交 |
| 回执对应的证据已过期，或冻结评审包被改动 | `review.round_stale`、`review.packet_changed` | 按过期的项刷新证据，再请求新一轮评审说明 |
| 会话冲突，或同一轮已提交过另一份 | `review.session`、`review.round_closed` | 请求新一轮评审说明 |
| 源码或输入在操作过程中改变 | 沿用 `flow.changed` | 重新执行当前命令 |
| 尚未成功定稿、定稿已失效、提交文件与快照不同 | `delivery.not_finalized`、`delivery.stale`、`delivery.mismatch` | `finalize`，或重新复制 `out/build.ldr` |
| 找不到记录 | `record.not_found` | `run` `ls --records-dir DIR`，并提示 `BRICKWRIGHT_RECORDS_DIR` |
| 渲染时缺 LDraw 库 | `render.ldraw_missing` | `provide` `LDRAW_LIBRARY_PATH` |
| 机位、题目 JSON、模块等其他输入不合格 | `flow.invalid_input` | 说明期望的格式 |

没有专门动作的流程错误，`next` 取当前记录的 `status` 首选动作；记录不可读时取 `record.not_found` 的动作。

### `status` 的首选动作

`status` 保留全部阻断项，另给 `next`：按特征 → 规划 → 验证 → 渲染 → 对比 → 评审 → 定稿的顺序，取第一个前置条件已满足的阻断项。缺机位文件或参考图时给 `provide`，不让代理猜路径。评审已被采纳且结论为 `needs_changes` 时，按剩余轮数和遗留项选择：轮数未用尽就返工（编辑 `build.py` 后验证），已用尽就定稿。

### 评审结果分三层

`review --set` 的结果分别回答三个问题：

- `receipt`：回执是否已归档，取值为 `accepted`、`stale` 或 `failed`。
- `counted`：本轮是否计入评审轮数，以及已用和剩余的轮数。
- `deliverable`：当前快照能否定稿，即 `status` 里的 `review_outcome`。

退出码按目的给出。`accepted` 退出 0。按要求归档的 `failed` 也退出 0，但 `next` 指向新一轮评审说明。`stale` 退出 1，`next` 给出需要刷新的证据。字段校验失败时不写回执，结果明确说明这一轮仍然开放，可以修正后提交同一份报告。

`review ID` 展示的 schema 由实际提交模型生成：给出普通报告的 schema，失败尝试的格式单独标明。`review-instructions` 在轮次目录写一份报告模板，预填轮次 ID、输入摘要、评审者会话字段和待评的条件、特征编号。图像观察和结论留给评审者填写。

### 子进程

worker 捕获普通异常，把原因码、阶段、异常类型和消息交回父进程，stderr 不再出现 traceback。父进程分三种情况报告：worker 内异常（`execution.child_exception`，消息形如 `LDrawLibraryNotFound: …`）、超时、进程异常退出（只有退出码）。诊断不再提示查看 stderr。LDraw 库只在实际渲染前检查：`render`、`render-angle`、带渲染的定稿。`compare` 只读已渲染的图，不做这项检查。

### skill 配合

安装后的 skill 属于本提案的交付范围：

- `SKILL.md` 只教一条恢复规则：读取结果，完成 `next`，再看当前状态。流程、路径和环境问题照返回的动作处理；碰撞、悬空、连接歧义等建模问题才按诊断码检索搭建知识。诊断码和命令由运行中的 CLI 提供，skill 不维护第二份对照表。
- `references/review.md` 给出两条真实返工路径：回执漏填 C01 时，补原报告并向原轮次重新提交；评审后改了模型，就重新验证，为新快照渲染、对比和评审。说明报告模板的用法。交付仍以 `finalize` 成功、实际提交文件通过逐字节核对为准。
- `references/flow.md` 改为以 `status` 的 `next` 推进。

### 其他

`knowledge search` 接受多个词，按空格合并后查询。

## 备选与取舍

- **先按轨迹统计高频报错，只改前几名。** 用户选择整体修改。报错点数量有限，并且是否达标要按“能否照做”判断，逐条改更直接。
- **新建错误层级，替换 `DiagnosticError`。** 现有类型和模块错误已能携带所需信息，另建一套只会多出转换。
- **由业务代码直接拼命令。** 业务代码不知道本次调用的解释器、记录目录和实际文件路径，这正是现状丢目录、改文件名的原因。因此只返回证据，由 CLI 拼动作。
- **只靠 `status` 兜底。** 实现最省事，但代理要多跑一次命令，而且 `status` 无法知道本次提交用的是哪个文件。只作为兜底。

`fix/flow-review-friction` 基于 P-008 之前的 develop，也修改 `aligned_review.py` 和 `cli.py`，还修改了已删除的提示文件。本提案合入后，那条分支需要重新基于 develop 处理冲突。凡是按原报错文本做匹配的测试或脚本都要相应更新。

## 验证

- **端到端恢复（主验收，开发中脚本化）。** 安装 skill，在带空格的非默认记录目录中创建流程记录，完成特征、规划、验证、渲染和对比。故意提交一份漏填条件的回执，脚本只读取返回的 `next` 和证据，补好原报告，并原样执行返回的 `argv` 重新提交。断言：仍是同一条记录、同一轮被采纳；定稿后实际提交的 MPD 与已验证快照逐字节一致。真实宿主实测仍按 U-13 留到开发后。
- **每类诊断执行返回的动作。** 按上表逐类制造场景，至少覆盖：先评审后验证、先对比后渲染、缺参考图、回执字段错误、回执过期、定稿前交付核对、提交文件被改动、记录 ID 错误、渲染缺 LDraw 库。每条断言 stdout 是单个 JSON、诊断码正确，并实际执行 `next`。`run` 类动作执行后，记录应前进到下一阻断或成功；`edit` 和 `provide` 类动作由测试按证据补齐后执行 `then`。
- **形状。** 无效的 `features`、`plan` 和 `review --set` 只输出一个 JSON，不附完整 schema。对缺记录的 `validate` 和 `render`，stdout 输出信封，stderr 中没有 traceback。worker 异常的类型和消息出现在报告中。`compare` 在零件库不可用时仍能对比已渲染的图。
- **评审分层。** `accepted`、`stale`、`failed` 三种回执的退出码、轮数计入和 `deliverable` 符合上文。`review ID` 的 schema 能通过一份合格的普通报告。
- 运行 CI 的全部检查。
