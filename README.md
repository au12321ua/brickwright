# BrickWright

用 Codex 或 Claude Code 把文字描述或参考图搭成可以在 LDraw 查看器中打开的 LEGO® 模型。宿主代理用 BrickLayer Python SDK 选件、摆放和试装，用 BrickWright skill 查搭法、检查模型并导出成品。

## 搭第一个模型

在本 fork 的仓库根目录用 uv 安装锁定版本的 SDK 和 CLI。

```sh
uv sync --locked
```

按使用的宿主执行其中一条，把 skill 安装到当前项目。

```sh
# Codex
uv run brickwright skill install --target .agents/skills/brickwright

# Claude Code
uv run brickwright skill install --target .claude/skills/brickwright
```

在同一目录打开宿主，把下面这句话作为第一个任务。

> 用 BrickWright 把一块蓝色 2×4 砖叠在一块红色 2×4 砖上。检查它们是否连在一起，完成后给我模型文件的路径。

代理会创建搭建记录，在其中编辑 `build.py`，运行 `validate` 和 `finalize`。每条命令输出一个 JSON，代理按其中的 `next` 推进或修复。默认成品在 `records/<record_id>/out/build.ldr`。需要渲染预览时，将 `LDRAW_LIBRARY_PATH` 指向本地 LDraw 零件库目录。

## 运行带参考图的 demo

OMR36 的单题演示要求搭建蓝色动力喷气机。搭建者使用原题创建流程记录，在指定机位将当前模型与目标图对照，可以自行评审。demo 只接收 `finalize` 成功的版本，接收端逐字节核对提交的 MPD、定稿导出和已验证快照。准备 Harbor 任务所需的原题、命令和输出目录见[单题演示](demo/omr36/README.md)。

## 按任务查文档

| 想做什么 | 入口 |
| --- | --- |
| 手动建记录、查搭法、检查和交付模型 | [BrickWright CLI](packages/brickwright/README.md) |
| 用 Python 建模、查零件能力或直接导出 LDraw | [BrickLayer SDK](packages/bricklayer/README.md) |
| 修改这个 fork 的代码、skill 或知识库 | [维护约定](AGENTS.md) · [交接](docs/HANDOFF.md) |

## 上游研究

***Agentic Creation of Physically Feasible Brick Builds***  
[Ava Pun](https://avapun.com/)<sup>1</sup>,
[Kevin Dai](https://github.com/K1ngHungry)<sup>1</sup>,
[Sally Chen](https://sally-chen.github.io/)<sup>1</sup>,
[Maxwell Jones](https://maxwelljon.es/)<sup>1</sup>,
[Ruixuan Liu](https://waynekyrie.github.io/)<sup>1</sup>,
[Kangle Deng](https://dunbar12138.github.io/)<sup>2</sup>,
[Changliu Liu](https://www.ri.cmu.edu/ri-faculty/changliu-liu/)<sup>1</sup>,
[Maneesh Agrawala](https://graphics.stanford.edu/~maneesh/)<sup>3</sup>,
[Deva Ramanan](https://www.cs.cmu.edu/~deva/)<sup>1</sup>,
[Jun-Yan Zhu](https://www.cs.cmu.edu/~junyanz/)<sup>1</sup>  
<sup>1</sup>Carnegie Mellon University, <sup>2</sup>Independent researcher, <sup>3</sup>Stanford University

上游论文在 512 个提示词样本上报告了 100% 的物理有效率，对照代理为 9% 至 69%。下图展示了该研究中的灯塔、《星月夜》和螃蟹。[论文](https://arxiv.org/abs/2610.31651)、[项目主页](https://avalovelace1.github.io/brickwright/)和[上游 PyPI 包](https://pypi.org/project/brickwright/)。

![An agent builds a lighthouse, Van Gogh's The Starry Night, and a crab from real LEGO® parts, with no collisions or floaters, fully connected](assets/video-thumbnail.webp)

### 相关研究

- [BrickBench: Evaluating Agentic Brick Design](https://brickben.ch) (October 2026)—a benchmark and environment for agentic LEGO® design
- [BrickNet: Graph-Backed Generative Brick Assembly](https://kulits.github.io/BrickNet/) (April 2026)
- [LegoACE: Autoregressive Construction Engine for Expressive LEGO® Assemblies](https://dl.acm.org/doi/10.1145/3757377.3763881) (December 2025)
- [LEGO®-Maker: Autoregressive Image-Conditioned LEGO® Model Creation](https://dl.acm.org/doi/10.1145/3763285) (December 2025)
- [Generating Physically Stable and Buildable Brick Structures from Text](https://avalovelace1.github.io/BrickGPT/) (May 2025)
- [Learn to Create Simple LEGO Micro Buildings](https://dl.acm.org/doi/10.1145/3687755) (November 2024)

### 引用

引用上游研究时请使用以下条目。

```bibtex
@misc{pun2026brickwright,
  title = {BrickWright: Agentic Creation of Physically Feasible Brick Builds},
  author = {Pun, Ava and Dai, Kevin and Chen, Sally and Jones, Maxwell and Liu, Ruixuan and Deng, Kangle and Liu, Changliu and Agrawala, Maneesh and Ramanan, Deva and Zhu, Jun-Yan},
  year = {2026},
  eprint = {2610.31651},
  archivePrefix = {arXiv},
}
```

### 致谢

This work is partly supported by the Packard Foundation, Sloan Foundation, and IITP grant funded by the Korean
Government (MSIT) (No. RS-2024-00457882, National AI Research Lab Project). Ava Pun is supported by the Meshy
Fellowship, Tata Consultancy Services Presidential Fellowship, and NSERC CGRS D. Maxwell Jones is supported by the
Rales Fellowship.

---

*LEGO® is a trademark of the LEGO Group, which does not sponsor, authorize, or endorse this project.*
