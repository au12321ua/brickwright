# R-31039 固定起点的喷气机返修

本试次演示独立图片评审如何驱动一次局部修改。题面、机位、准用零件、件数上限及提交要求继续使用 `/app/input/` 内的原件；搭建起点改为 `/app/replay/baseline-build.py`。这是用于复放的搭建源码。保留其余主要设计。原始任务中的从零搭建约束不适用于本试次。

使用 `/app/BRICKWRIGHT.md` 的 Build flow。在新记录按 `--flow --task-json /app/input/task.json --review-rounds 2` 创建后复制固定起点至生成的 `build.py`。用 `/app/replay/features.json` 载入题面三项公开特征，在 `external plan` 写入符合现有模型的规划。用未修改的起点通过整版结构检查，按 `/app/input/cameras.json` 渲染并生成可追溯的首轮冻结评审包。

把 `external review-instructions` 返回的冻结说明交给一个实际派发的独立 Codex 评审会话。只给这一轮图片、题面、清单和回写协议，不提供搭建历史、分数、预期偏差或拟定修法。请它用图片工具打开每张图片，独立指出一个能被另一机位印证的局部差异，并把清单以外可辨识的特征补记在报告。保存有效回执和评审报告。另保存初始五视角成品图至 `/logs/artifacts/replay-before/`，每张命名为 `submission-<view>.png`，并在修改前保留检查过的 MPD 或保存其 SHA-256。

按首轮评审意见做一次实际的局部修改。复跑双栏结构检查、同机位渲染、对齐指标和实际独立复评。保存最终五视角成品图至 `/logs/artifacts/replay-after/`，仍命名为 `submission-<view>.png`。保留前后评审、局部叠图、选择的修改区域以及最终定稿；交付 `/logs/artifacts/model.mpd`。视图目标仍从 `/app/input/` 读取。主代理与每个评审均为 gpt-6.2-sol medium。没有完成的阶段请留下实际原因。
