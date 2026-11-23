
## R-10258 任务分块组

题面、机位、允许件、颜色、件数和评审标准按原任务执行。至少划分下层（可含底盘）和上层（可含车顶）。由主代理按题图约定整体尺度、车头车尾、窗柱节奏、世界坐标接缝和共享配色。楼梯与后入口仍应衔接。将各模块的世界占用、原点、朝向、预算和接口写入原计划。CLI 字段与示例见 `/app/BRICKWRIGHT.md`。接缝可用少量专门预留的薄板凸点和上层反凸点。1×1 薄板 3024 的凸点接缝可沿 LDraw [0,-1,0] 进入上层，横截面半宽可用 [6,6] LDU，深度可用 4 LDU。其余下层凸点须留在下层占用内。接缝的具体位置与部件由你按题目决定。

先用 `brickwright external module init <父记录> <模块编号>` 创建子记录。依次立即调用两次 `spawn_agent` 再等待结果，让两个独立 Codex 搭建会话的工作实际重叠。初次任务名用 `build_lower`、`build_upper`，均用 gpt-6.2-sol medium，`fork_turns="none"`。明确告诉每个子代理只编辑自己的记录，读取目标图、子模块契约和允许件；验证、定稿并交回回执。每个子代理在会话开始时以自己的 `CODEX_THREAD_ID` 运行 `brickwright external module instructions <子记录> --session-id "$CODEX_THREAD_ID"`，完成普通 `external validate <子记录> --timeout 180` 与 `external finalize <子记录> --timeout 240` 后，用同一会话 ID 执行 `external module finish`。如果该变量不可用，以其任务名记回执，归档时按子会话路径比对日志。父记录的 `create_build()` 使用 `from brickwright.modules import place_module` 和 `place_module(build, "<模块编号>")` 摆放模块；通过父记录结构验证后再进入整机外观评审。

首轮冻结稿结构通过并取得独立评审后，按该轮证据选一个模块局部返工，保留另一个已通过模块的原快照。即使首评通过，也完成一处能保持题面要求的合理局部完善，例如统一一处窗柱节奏或改善后入口。使用独立的 `rework_lower` 或 `rework_upper` 会话，保存更新前后的合并、标准接口证据和有效评审；只用同样的两轮评审预算，并对新合并进行第二次评审。最终交付父记录导出的正式 MPD。失败仍保留独立会话、子记录和父记录。记录目录由本题环境的 `BRICKWRIGHT_RECORDS_DIR` 指定，保留在归档路径内。
