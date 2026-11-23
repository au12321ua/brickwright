#!/bin/sh
set -eu
ln -sfn /opt/brickwright/bin/brickwright /usr/local/bin/brickwright
cat >> /app/AGENTS.md <<'INSTRUCTIONS'

## 独立代理权限

本题授权使用 Codex 原生 `spawn_agent`、`send_input`、`wait_agent` 等工具派发独立评审或模块搭建工作。主代理与子代理均使用当前 Codex `gpt-6.2-sol`，推理强度 `medium`，请沿用此配置。作为子代理时，按收到的任务操作并将结果返回父会话。确保每个子代理有独立会话和单独的输出路径。
INSTRUCTIONS
