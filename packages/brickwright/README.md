# BrickWright CLI

从仓库根目录运行以下命令。首次使用先按[安装说明](../../README.md#搭第一个模型)安装工作区和宿主 skill。安装命令打印 `SKILL.md` 的路径，里面有 SDK 解释器和可运行的两砖示例。更新已有安装时，若工具提示文件内容有变，用同一目标和 `--replace` 重新安装。

## 建立普通记录

```sh
uv run brickwright external init '把一块蓝砖叠在红砖上'
```

记下返回的 `record_id` 和 `build_path`。编辑后者，按安装好的 skill 示例定义 `create_build()` 和 `run_tests()`。以下命令中的 `ID` 均替换为该记录的 `record_id`。

```sh
uv run brickwright external validate ID
uv run brickwright external finalize ID
```

检查失败时从报告的 `diagnostics` 查看具体位置和原因，修改后再验证。成功定稿后，记录目录下的 `out/build.ldr` 可在 LDraw 查看器中打开。记录默认位于当前目录的 `records/`，可设置 `BRICKWRIGHT_RECORDS_DIR` 或为各记录命令指定同一个 `--records-dir DIR`。

## 找搭法和 SDK 参数

```sh
uv run brickwright knowledge search '舱门翻开'
uv run brickwright knowledge show hinged-panels
```

搜索也接受零件号和诊断码。返回的 `path` 相对于安装好的 skill 目录。`show` 可直接读取条目、示例和来源。查方法签名时，从该目录的 API 索引选一个主题，或按符号名搜索。[BrickLayer SDK](../bricklayer/README.md#api-reference)

## 交付带参考图的任务

用 `external init "题目摘要" --flow --task-json task.json` 开始流程记录。先提交特征和规划，验证整件，再按题目机位渲染并评审当前版本。主代理可以自评。每条 `external` 命令只输出一个 JSON，其中 `next` 给出下一步：直接执行的命令、先改文件再执行的命令，或需要补充题目输入的命令。`external status ID` 会列出当前快照、全部阻断项和同一个 `next`。命令顺序和必需输入见[流程说明](src/brickwright/skill/references/flow.md)。

成功执行 `external finalize ID` 后，把该记录的 `out/build.ldr` 原样复制到题目指定的位置。使用同一个 `--records-dir` 或保留原工作目录和环境设置运行接收命令，将 `model.mpd` 换成实际提交文件的路径。

```sh
uv run brickwright external verify-delivery ID model.mpd
```

仅当提交的 MPD、当前成功定稿的导出和已验证快照的字节一致时通过，结果包含快照 ID、SHA-256 和评审方式。

*LEGO® is a trademark of the LEGO Group, which does not sponsor, authorize, or endorse this project.*
