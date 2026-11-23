
## 本次评测的已启用流程

任务原件为 `/app/input/task.json`。请按 `/app/BRICKWRIGHT.md` 里的 Build flow 指南执行本次任务。上方的常规外部入口仍适用，创建记录和渲染采用下列命令。

```sh
brickwright external instructions --flow > /app/INSTRUCTIONS.md
brickwright external init '<题目摘要>' --flow --task-json /app/input/task.json --image /app/input/goal-angle.png --review-rounds 2 --model gpt-6.2-sol --effort medium
brickwright external render '<record_id>' --cameras /app/input/cameras.json
brickwright external compare '<record_id>' --reference-dir /app/input
```

保留输入原件和两轮有效外观评审预算。按 `external features` 和 `external plan` 提示创建 JSON，将原始条件逐项映射；后续记录写入都通过 `external`。用 `external status` 了解下一步。结构检查通过后，以 `external review-instructions` 打印冻结评审说明，实际派发独立的同模型 Codex 评审，让它直接查看该轮图片，并将收到的回执通过 `external review --set` 保存。修订后重新检查、渲染和复评。最后仍将当前已定稿记录的导出件原样复制为 `/logs/artifacts/model.mpd`。阶段或评审失败时保留记录和诊断。如果没有本题专门追加的分块安排，请由主代理整体搭建。
