# OMR36 BrickWright Maestro 对照

冻结配置见 [protocol.json](protocol.json)。正式对照在 `from-zero-v2-long-budget` 的六题上各跑两次，两版共享独立代理授权和 CLI 入口修复。基线继续用 `594effd` 的原外部流程。新版只替换 BrickWright 附件。巴士整体组采用新版六题对照中的两个 R-10258 试次。分块组另跑两次，同用新版附件且增加任务分块说明。原始一次/题的运行用于预校准和排查，独立派发试验用于核对 Codex 实际行为。

在能访问本机 Maestro 且已安装服务配套 `maestro-sdk` 0.7.2 的 Python 环境中给出 `MAESTRO_URL`、`MAESTRO_TOKEN_FILE`。源码提交和任务附件记录在私有输出目录。不要将令牌放入附件或打印。

```sh
python docs/design/omr36-evaluation/maestro.py build "$BRICKWRIGHT_REPO" "$CANDIDATE_COMMIT" "$EVAL/product"
python docs/design/omr36-evaluation/maestro.py submit "$EVAL" "$EVAL/product" "$P005_CALIBRATION" baseline
python docs/design/omr36-evaluation/maestro.py submit "$EVAL" "$EVAL/product" "$P005_CALIBRATION" candidate bus-modular
python docs/design/omr36-evaluation/maestro.py collect "$EVAL" baseline candidate bus-modular
```

`build` 从 Git 对象构建两份 wheel，使用与冻结基线一致的 `uv.lock`。`submit` 核对 Maestro 解析后的模型版本、推理强度、Codex 二进制版本、每题环境资源、时限和任务选择。重复执行复用同一试次请求。`collect` 保留平台未计分状态、所有可用重试记录和归档缺项；没有平台分或交付证据的试次保留未知值。每组 `runs/<arm>/index.json` 给出原始平台结果、视图分数、产物路径、校验和评审记录，以及每个实际 Codex 会话的用量。Harbor 汇总和逐会话用量分别保留。试次的代理用时覆盖搭建至交付后的代理退出；端到端试次用时另加环境准备、附件安装及验证器。

用 U-10 的独立人工盲包逐条评阅 `task.json.checks`。在锁定评语前，不向评阅者分发此目录的私有 `state.json`、归档、计量索引和版本对应表。巴士须先满足交付、平台分和原始完成条件三个质量条件，再判断分块组完成交付的代理用时均值是否缩短。能力证据另查同时运行的两个子会话、接口自检、合并前后同一无关快照的摘要，以及父记录的重新验证与评审。

独立派发校验的输入可以用 `stage-control FROZEN_JET_TASK HISTORICAL_JET_VERIFIER OUTPUT` 重建。它复用了打包的原题机位、目标图和运行环境，另带历史成品的两张匿名视图。按相同附件和模型执行一个 600 秒真实试次。它只核对实际派发和读图，独立喷气机返修仍按 [replay.md](templates/replay.md) 的任务运行。

## 独立派发实测

[Maestro 控制组](http://localhost:8420/runs/run_01m83dmfcd9vztwkpv59j8mcde) 的验证器得 1.0。父会话调用 `spawn_agent` 和 `wait_agent`。子会话的原始日志直接绑定父 ID，记录 `gpt-6.2-sol`、medium 和 Codex 0.170.0。一个 `view_image` 批次返回了主侧视及端视的目标与成品共四幅图。评阅者自行定位主侧视双尾翼的阶梯前缘，并以端视核对表面差异和两件模型都具备的外倾。对图上方框的裁切复核确认了主侧视差异。[会话、用量与证据摘要](control-evidence.json)

Harbor 的原始试次用量在这次派发控制中只对应最后的子会话。主代理和子代理的实际用量分别按各自 Codex 日志里的最后一个非空累积计数读取。所有视图判断还需要回查原始读图记录和回执，单纯打印评审说明无法证明已派发。

## 正式运行与校准

[配对基线](http://localhost:8420/runs/run_01m83css5zw7djn5rscdvfhrcv)和[新版](http://localhost:8420/runs/run_01m83gg16jhczmh0y1vd54eccd)各锁定 12 个试次。解析后的配置只在产品附件字段有差异。用 `e88513e` 构建的两份 wheel 通过本机 CLI 启动检查，并在原勃兰登堡门题的 Oracle 检查得到 1.0。[版本、配置和产物摘要](run-bindings.json) · [Oracle 复核](http://localhost:8420/runs/run_01m83ge5vvpd991wby9vpffmgf)

候选渲染校准覆盖 30 个同机位视图。两项存量 PNG 指标均精确复算，参考重渲染指标最小值为 1.0，基线重渲染指标最大绝对差为 `0.000009870499052433424`。

归档结构返修计数使用顺序校验报告，统计校验失败后已检查的新源码或流程快照。同一快照的重复检查只保留为检查次数。外观返修计数使用相邻有效评审的结构快照变化。完整归档还保留报告状态、失败尝试和无修复的重新评审。

`audit.py ARCHIVE FROZEN_HARBOR_TASKS OUTPUT` 在候选或分块归档上复核交付证据。它核对每条原始条件及特征清单映射、定稿快照和 MPD 摘要、评审轮数、父会话的原生派发回执、子会话的实际模型及推理强度。每张必读图的 SHA-256 必须出现在子会话实际图片工具返回中。结果另列偏离和原始资料缺项，保留原平台分。
