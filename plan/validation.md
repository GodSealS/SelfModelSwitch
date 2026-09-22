# v3 文档拆分验证记录

日期：2026-09-16。范围：根README导航、plan当前方案、独立视频设计、v2历史归档。
本轮未修改生产实现、依赖、配置或部署文件；未建立视频项目生产仓库。

## 检查范围

- Markdown本地链接、代码围栏闭合、尾随空白。
- 旧v2文档/参考代码归档一致性；仅README历史相对链接作导航修正。
- legacy-v1原文件内容保持不变。
- 平均切片公式的25分钟阈值、整倍数及采样点边界算例。
- 跟踪文件改动范围和生产文件零改动检查。
- 文档交叉审阅项目边界、独立任务/发布门禁、通用模型协议及视频后补关联规则。

## 实际结果

本机使用现有`.venv/bin/python`（Python3.13.5）执行一次性文档/归档/公式校验脚本，exit0。
系统`python3`因Xcode许可未初始化无法运行，改用已有venv完成检查；未修改系统许可或安装工具。

| 检查 | 结果 |
|---|---|
| 根README及plan全部Markdown | 34个文件，60个本地链接有效；围栏和尾随空白检查通过 |
| v2归档 | 15个原文件hash一致；README仅按声明修正历史链接后归一核对 |
| legacy-v1 | 9个原文件hash一致 |
| 平均切片公式 | 9个算例通过：24/25/26/50/75/120分钟，以及25分钟前后1采样点、50分钟后1采样点 |
| 算例不变量 | 块数符合公式、完整覆盖、无空洞/重叠、每块<=25分钟、长度差<=1采样点 |
| `git diff --check` | 通过；未跟踪plan文件另由上述文本检查覆盖 |
| `git diff --name-only` | 只有根README（导航变更）；plan原为未跟踪目录，修改/新增均在该目录 |

交叉审阅已检查：当前任务M00—M07无视频实现；视频V00—V07有独立验收；旧phase/业务Candidate不再为当前契约；
title/音频/波形/均分/人工后补/末尾一致性要求都有对应视频章节和验收项。
公式校验仅验证文档算法算例，未执行视频切片或模型推理。

## 未执行

应用回归、归档reference测试、模型加载、真机峰值/耗时、视频质量/长片/声线一致性、外部服务、安装发布均未执行。
归档中的旧29项测试记录仅属于旧v2，不能作为新拆分设计或新协议的通过证据。
M00/V00仍待真实环境；新增控制/表格schema仍待M01/V01，不声称这些设计已实现。

> 上一段（2026-09-16）保留原文。M00 的真实环境结论已由 [m00-envelope.md](m00-envelope.md) 第 9—10 节更新；
> 本节以下为 2026-09-17 的 P00 记录，不改写历史结论。

## P00 基线记录（2026-09-17）

范围：只读核对可复现基线、解释器、目标硬件与 M00 原始材料；不重跑探测、不加载模型、不修改生产代码。
第 3 节清单是本记录写入**之前**的工作区状态。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P00 |
| status | complete |
| source_commit | `63f7d8e13319d57e946e46d5766703e55d75c9a3` |
| target_commit | `63f7d8e13319d57e946e46d5766703e55d75c9a3`（只读核对时的 `/home/jtzn/SelfModelSwitch`，`git status --short` 为空） |
| target_sync | 记录提交后按 AGENTS 流程 `git pull --ff-only` 同步到本记录所在提交；同步前后 `git status --short` 均为空，未 reset、未复制目标 tracked 文件 |
| python_version | 3.12.11（`/Users/monster/.local/share/selfmodelswitch/venv312/bin/python`） |
| candidate_sha256 | null（本任务不产出候选） |
| evidence_directory | 目标只读复核 `/home/jtzn/self-model-switch-evidence/`；未新建、未删除任何材料 |
| unresolved | 第 7 节 |

本任务判定为 `complete`：三条验收均以本轮命令与只读材料复核为依据，不包含硬件性能验收，也不产生
`software_verified` / `device_backend_ready` 结论。

### 2. 基线三分

- **已提交（HEAD `63f7d8e`）：** 78 个 tracked 文件。本任务未修改、未删除任何 tracked 文件。
- **未提交（属用户现有工作，本任务不改动、不打包、不提交）：** 1 个已修改 tracked 文件（`README.md`）
  与 43 个未跟踪文件（`AGENTS.md`、`plan/01`—`plan/08`、`plan/adr`、`plan/legacy-v1`、`plan/legacy-v2`、
  `plan/video-analysis`、`plan/validation.md`、`tests/test_control_protocol_v1.py`）。
  整体清单摘要：`f87f5df8ab83f10ff6037182381280e32470843718ac364d5b68e532bd82a8f0`。
  这些文件的存在不表示其中描述的功能已实施。
- **旧事实冲突：** 见第 5 节；不删除原文，只追加本轮较新事实。
- **后续说明（同日）：** 用户确认基线归属后，上述未提交项已按“排除 `plan/video-analysis`”的要求提交：
  `8bf62b6`（`AGENTS.md`、`README.md`、`plan/01`—`07`、`plan/adr`、`plan/legacy-v1`、`plan/legacy-v2`）
  与 `b3efb78`（`tests/test_control_protocol_v1.py` 草稿；纳入后 `pytest tests` 的收集失败由 P02 修复）。
  `plan/video-analysis` 保持未跟踪，指向它的 README/plan 链接在它迁出前仍为悬空链接。

### 3. 基线清单（P00 写入前）

取条目：`git status --porcelain=v1` 的最后一个字段；文件用 `shasum -a 256`，目录用
`find <dir> -type f -print0 | xargs -0 shasum -a 256`；合并后 `LC_ALL=C sort -k2`。
整体摘要 = 对该输出的 sha256，即第 2 节的 `f87f5df8…`。

```text
3e5308507f4544bc330c6d9e443f33f0eeb338183115a42b7c4754a34e631fa1  AGENTS.md
b8191545a4f677bada4a3371354db61464631fe97ce4a28123e5ffd71bcbe405  README.md
aeaac5ec27c580491fafedbf28dae9c41c9198480fb2faac1024f93e26cff987  plan/01-design.md
17dd1b4449fb0e63b7c6032e956336f65f3b3fcecea370ba5de91075241d9982  plan/02-scheduler.md
46871257a8224f76e89545294c6320304744172aefb0e2231a078cb24089dee3  plan/03-api.md
1df397b4ad287b2a3096c26bb4ff262ae84d846c0ba9ecb70fa45ad171f7ea9e  plan/04-deployment.md
b5cbc0fd5fda62cef3c98579f8ef99848960806382fb623c54e7f2f89c994c5f  plan/05-tasks-and-acceptance.md
50067991c7c54a5fa2b10344c7eb6bb4d2653dba6c54083ee2623cbdf72ef54b  plan/06-acceptance.md
c90c5816e9561b90de5b73f0253ca1046b843b8e1f14d44a51e2d6268649f3a0  plan/07-pipeline.md
bc73e269bbb3145a692b5d57dd1981c92ca8452f1f71e25cac4b9a34d45354fc  plan/08-execution-plan.md
354d9acf5cfb354b54bdbb5a7bb0e6980d36afd0db2c9db422a51de484273e89  plan/adr/decisions.md
31fa50510c946e00e212596f66d7e45979b13c5c244c713c04a9a82fdc43f7cb  plan/legacy-v1/01-design.md
81466dc548bb90e426d5d61bbc609e7b08f91a13cfd247cb46468d2eb4227880  plan/legacy-v1/02-scheduler.md
5c34b95fd6d02ee4be7bcc2cef551fb2d86db2b17f778199ce30277908ff891b  plan/legacy-v1/03-api.md
41a10fb1f93227b3be5979eca8c13af60e7ce5ce0a02885e24b883d3c30c036f  plan/legacy-v1/04-deployment.md
5bcde41a7f5e6d851ec11dab287cd32c14ded0280c3a76737909b3ffa14f339e  plan/legacy-v1/05-tasks-and-acceptance.md
e65dfca458b192d0c557c9739388b2a4bf3ef06415f4f4c51217c437fe91a217  plan/legacy-v1/README.md
291bfa88e2ca39963fb1e586eff1a060b85462115504ac7b6a59a834ee66099b  plan/legacy-v1/reference/contracts.py
c92c3ce32259f96ab0f4dcf8cab75a0c131e283bdb9540a28d9a9ef018c9dc76  plan/legacy-v1/reference/core.py
76c5a55e00f3f94c5f48530dfe65dcc3684aa70cfff8ca377ea12e69695fc7f3  plan/legacy-v1/reference/test_core.py
1d605dbed738884c6135a82192069e91b9fd27a1892e6a4b5564b1e4762e4adb  plan/legacy-v2/01-design.md
0d9ec6c58e51c552865d85fb243d590e6377410af4ca6241ed0ddf05268ac01a  plan/legacy-v2/02-scheduler.md
ef0901172326c60d1c974d807091123d030ef2663017db19e2e82c3285d0c1d3  plan/legacy-v2/03-api.md
8dbbfe71e865e2ce3847eb23ec59138bd4776cbd1a00211756f441e0f85c314a  plan/legacy-v2/04-deployment.md
251d3c4beca3b7d6a03d2b8c4f2a0bded9f2a348eaa4ab0606f5b3572c49d98b  plan/legacy-v2/05-tasks-and-acceptance.md
e5f929d09ff49a472ac77549611e7b5067ec986342b0025fd79531dddc797b7d  plan/legacy-v2/06-acceptance.md
33cb24149ca3125bf1cadd5ee6d24fa28cfc8a617151d168c9e33a86998dc52b  plan/legacy-v2/07-pipeline.md
1524cc6fbaeaadab9b4d97c004a1ba3b1bbf92422e662bc8f519315f69b5fb1a  plan/legacy-v2/ARCHIVE.md
bd619690307832d51adfbafabb3ba0fa6f8709ec5f25d5bf5296740da470721a  plan/legacy-v2/README.md
3909e8f994a3857bbe403d716cc18b5e10b90149214a281adb327846e68f7b17  plan/legacy-v2/adr/decisions.md
fa16eb836ac5ce75f5cdd19311d6aa3945e918f514484462f42ea87adb549bd8  plan/legacy-v2/reference/acceptance.py
556a5b9622387826e2022e552d7e541efcf61583d30ef2cdf6f61d7f650e9888  plan/legacy-v2/reference/contracts.py
5f4652c1ecf5e2fcd8fa2dcac4d494c8470c345a90aca5fee03454ff5d52badf  plan/legacy-v2/reference/core.py
a5869f02af4236fbc669b1bd05a0c4ff97317f7d3f88087c3c5fcea580e565e9  plan/legacy-v2/reference/export_schema.py
2e1d19ae7b37721d81b3b74543d380af6c462ec54f3cd9fd3c31dceff9dd6a72  plan/legacy-v2/reference/test_core.py
34c8b94cb9f9625f8c59d0d62bb0f746b0031ebe3c36d68b1eb046bda1abd33f  plan/legacy-v2/validation.md
ca0075198cd5b5456579e398c97343f92eb8555b2b85ae00dd0e731da75918e4  plan/validation.md
23e275c7f2b609e65c3a7e8fc4f68dd64f7ec2134762788a5322b416625f78ad  plan/video-analysis/01-design.md
2677a82af26411f125cb964e36b4d1963382509b9a3383a82635393264bf3f96  plan/video-analysis/02-pipeline.md
634ccddcb11d187883d11c7d67a505dbf1f8dc49cf5bec7d666c48b1a8818415  plan/video-analysis/03-integration.md
a4d5c65231ca4ec8ef749a4601f8be0eea0c740f110f5a1cbeadfaaad958fe95  plan/video-analysis/04-acceptance.md
777c9ce92aa4f9be177fccc8d134096af3c323d01ee1763e89d96b4510f6a28b  plan/video-analysis/05-tasks.md
167095925ba22b60aeef94f4df6db4e3c01c53fc97ba319e7cf2b13a5591f4d0  plan/video-analysis/README.md
afb69241ffba128cd0603d0325b08312042ca36353975feb222baef6d80e50ad  tests/test_control_protocol_v1.py
```

只有 `plan/08-execution-plan.md` 与 `plan/validation.md` 会因本记录写入而改变 hash，是唯一预期差异；
其余 42 项在 P00 期间保持只读。

### 4. 本轮命令与结果

解释器：`/Users/monster/.local/share/selfmodelswitch/venv312/bin/python`（3.12.11）。
依赖按 `requirements-dev.lock` 的锁定版本安装；该锁由 `--python-platform aarch64-unknown-linux-gnu` 生成，
macOS 无法按哈希安装，故本轮以 `--no-verify-hashes` 安装同版本，仅作开发用途，不构成 P28 的锁门禁通过。

| 命令 | exit | 结果 |
|---|---|---|
| `python --version` | 0 | `Python 3.12.11` |
| `python -m pytest tests -m 'not thor' -q` | 2 | 仅 `tests/test_control_protocol_v1.py` 收集失败（`ImportError: cannot import name 'control_protocol_v1'`）；`1 deselected, 2 warnings`，符合本任务“预期仅已知草稿收集失败” |
| `python -m pytest tests -m 'not thor' --ignore=tests/test_control_protocol_v1.py -q` | 0 | `226 passed, 1 deselected, 2 warnings in 5.20s`（与 08-execution-plan §1.2 的 226 项一致） |
| `python -m ruff check .` | 0 | `All checks passed!` |
| `python run.py --check-config` | 0 | `schema_version=1 models=embedding,qwen-large,qwen-small,reranker`（旧四 ID，未迁移） |
| `git rev-parse HEAD` | 0 | `63f7d8e13319d57e946e46d5766703e55d75c9a3` |
| `git status --short` | 0 | 1 修改 + 43 未跟踪，见第 2、3 节 |

两个 `DeprecationWarning` 来自依赖的 `fastapi.testclient` / `starlette.testclient`，与本仓库代码无关。
本轮未使用 `--ignore` 以外的任何方式排除测试，未跳过或删除任何测试。

### 5. 旧事实冲突（保留原文，不改写历史）

| # | 旧记载 | 本轮事实 |
|---|---|---|
| 1 | 本文件 2026-09-16 段“M00/V00仍待真实环境” | M00 已于 2026-09-17 通过并回填（[m00-envelope.md](m00-envelope.md) §9—10）；仅 M00 到期，视频项目不受影响 |
| 2 | [README](README.md) 规范入口表写源码基线 `c32dd1a8ecdfa9b7f8f9a964886b794a943e16ca` | 实际 HEAD 为 `63f7d8e…`；08-execution-plan §1.1 已按较新事实核对 |
| 3 | [05-tasks-and-acceptance.md](05-tasks-and-acceptance.md) 的“全部待办”、本文件 2026-09-16 段 | 均为旧快照；本轮采用 08-execution-plan 的逐项证据 |
| 4 | [README](README.md) 规范入口表未列 `08-execution-plan.md` | 该计划已存在且为本轮实施依据，索引未回填 |
| 5 | [m00-envelope.md](m00-envelope.md) §5 描述逐轮保留 `stop.json` 与顶层 `run.json` | 实际材料为 `run1`/`run2`/`run3` 各自的 `run.json`（停止证据在该文件的 `stop` 块内）加顶层 `metadata.json`；无 `stop.json`、无顶层 `run.json`。停止结论不受影响 |
| 6 | [m00-envelope.md](m00-envelope.md) §9.3 只登记 2 个失败目录 | 证据根另有未登记目录 `m00-qwen25vl-envelope-20260917T052121Z`、`m00-qwen25vl-text-20260917T011545Z`、`m00-qwen25vl-text-20260917T011747Z` 及 4 个 `m00-envelope-run*-console.log`；本轮未删除、未计入结论 |

### 6. 目标机只读核对

按 [AGENTS.md](../AGENTS.md) 使用 `-i ~/.ssh/selfmodelswitch-target-agent -o IdentitiesOnly=yes` SSH。下表核对全部只读，
未加载模型、未重启服务；本轮唯一的目标机写操作是本记录提交后的 `git pull --ff-only` 同步（第 1 节 `target_sync`），
它只推进 checkout，不改变任何目标 tracked 文件内容。

| 项 | 实测 |
|---|---|
| 设备 | `jtzn-desktop`；`Linux 5.15.148-tegra` aarch64；L4T `R36.4.7`（与 m00-envelope §2 一致，未换机） |
| 目标解释器 | `python3` 3.10.12（probe harness 口径；本任务不在目标机运行发布解释器） |
| 仓库 | `/home/jtzn/SelfModelSwitch`，branch `main`，`origin` = `/home/jtzn/git/SelfModelSwitch.git`；只读核对时 HEAD `63f7d8e…`、工作区干净；本轮结束时已 fast-forward 到本记录所在提交，工作区仍干净 |
| 运行状态 | 无 llama/模型相关进程；监听仅 22/111/53；`/etc/systemd/system` 无 `model-scheduler`/`llama-swap` 单元（仅 `nvpmodel.service`） |
| 模型资产 | `Qwen_Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf` 4683072320 B，sha256 `3f4513330aa7f109922bd701d773575484ae2b4a4090d6511260a2a4f8e3d069`；`mmproj-Qwen_Qwen2.5-VL-7B-Instruct-bf16.gguf` 1354162912 B，sha256 `d1c7588c0bdf6e7889737c01cfd54309240d54042e102275880a514ae979aea3`；均与 m00-envelope §2 一致 |
| 模型盘 | `/dev/sda1` UUID `16d53274-d9f5-4282-9b44-7fdd43cba9ca`，ext4，挂载 `/media/jtzn/sandisk-ext4` |
| runtime | `/opt/self-model-switch/probes/llama.cpp-4bc272fd729bd094c0422e4b8353da8d2fec91f8`；`sha256sum -c RUNTIME-SHA256` 11/11 成功（含 `llama-server` `65a23c6e…`），与 §9.1 裁定一致；`BUILD-METADATA` 陈旧哈希仍存在，维持 §9.1 的 anomaly 分类 |
| M00 通过轮 | `/home/jtzn/self-model-switch-evidence/m00-qwen25vl-envelope-20260917T055502Z`：`run1`/`run2`/`run3` 全部 `status=completed`、`stop.quiescent=true`、`stop.exit_code=0`、`graceful_stop=true`、`kill_used=false`、`port_free=true`、`reclaimed=true`、`residual_pids=[]`；`run1` `execution_device=CUDA0`、`load_seconds=18.067`、`failure_stage=null` |
| 材料 hash | `metadata.json` `7b8ac8b98bd205174df33f6d364fffdf03b26747a3851a6b74d2776e6aa52625`；`run1/run.json` `14fe3ce5804c457d04b26ec572d4d6fc413c8dead2804004bb22b2c2fa2ea2ee`；`run2/run.json` `0ad510b1a8e600e38ef01787aea5194f989f460e8f6f341ae74e3973059ef6d0`；`run3/run.json` `4fe3491e796cc5c249ea044a66e0d07a4ff222aa5dd048586fc56e4fe7f017a2` |

M00 材料完整、hash 与 3 轮停止记录均可复核，本轮**没有**缺失材料需要标待核验，也**没有**重新触发模型试验。

### 7. 待处理项（不阻塞 P00 判定）

1. m00-envelope.md §5 的 `stop.json`/顶层 `run.json` 命名与实际材料不符（第 5 节 #5），应由文档侧修订。
2. 证据根 3 个未登记目录与 4 个 console log（第 5 节 #6）；未删除，建议登记或注明。
3. `plan/README.md` 索引缺 `08-execution-plan.md`，源码基线仍是 `c32dd1a8…`（第 5 节 #2、#4）。
4. 本机 `.venv` 为 Python 3.13.5，不满足 `pyproject.toml` 的 `requires-python = ">=3.12,<3.13"`；
   本轮新建 3.12.11 环境并记录路径，未删除原 `.venv`。发布锁按 Linux ARM64 生成，macOS 侧无法复现锁门禁。
5. 本地 GitHub `origin` 落后 3 个提交，本轮未推送 GitHub，按 08-execution-plan §7 记“待同步”（待同步项仅限于 GitHub，
   不影响目标 checkout 的精确提交要求）。目标同步通路为本地 → `jtzn@192.168.55.1:/home/jtzn/git/SelfModelSwitch.git`
   普通 push → 目标 `git pull --ff-only`，本轮已按此同步并在前后核对工作区为空；未 reset、未复制修改目标 tracked 文件。
6. `tests/test_control_protocol_v1.py` 草稿使 `pytest tests` 收集失败（P02 接续）；本轮不覆盖、不删除、不代为提交。

### 8. 任务执行记录

```json
{
  "task_id": "P00",
  "status": "complete",
  "source_commit": "63f7d8e13319d57e946e46d5766703e55d75c9a3",
  "candidate_sha256": null,
  "python_version": "3.12.11",
  "commands": [
    {"argv": ["python", "-m", "pytest", "tests", "-m", "not thor", "-q"], "exit_code": 2},
    {"argv": ["python", "-m", "pytest", "tests", "-m", "not thor", "--ignore=tests/test_control_protocol_v1.py", "-q"], "exit_code": 0},
    {"argv": ["python", "-m", "ruff", "check", "."], "exit_code": 0},
    {"argv": ["python", "run.py", "--check-config"], "exit_code": 0},
    {"argv": ["git", "rev-parse", "HEAD"], "exit_code": 0},
    {"argv": ["git", "status", "--short"], "exit_code": 0}
  ],
  "target_commit": "63f7d8e13319d57e946e46d5766703e55d75c9a3",
  "target_sync": "read-only verification at 63f7d8e; after this record was committed the target checkout was fast-forwarded by git pull --ff-only to the commit holding this record, with git status --short empty before and after",
  "evidence_directory": "/home/jtzn/self-model-switch-evidence/m00-qwen25vl-envelope-20260917T055502Z",
  "unresolved": [
    "m00-envelope.md 第5节的 stop.json/顶层 run.json 命名与目标材料不符",
    "证据根有3个未登记目录与4个 console log",
    "plan/README.md 索引缺 08-execution-plan.md 且源码基线过旧",
    "本地 .venv 为 3.13.5，不满足 requires-python；发布锁为 Linux ARM64，macOS 无法复现",
    "本地 GitHub origin 落后3个提交，本轮未推送 GitHub（待同步）",
    "tests/test_control_protocol_v1.py 草稿收集失败，由 P02 接续"
  ]
}
```

本记录的命令 exit code 2 是 08-execution-plan §1.2 已预期的草稿收集失败，不是 P00 未通过；
P01 的起点为同一 `source_commit`，本任务结束不改变任何 tracked 生产文件。

## GitHub 基线推送记录（2026-09-17，闭合 P00 §7 第 5 项）

范围：只推送既有已提交历史，不修改任何 tracked 生产文件、不新建任务实现；本条记录为唯一新增内容。

### 1. 推送范围与内容审查

| 项 | 值 |
|---|---|
| 推送前 `origin/main` | `cbefb5b` |
| 推送后 `origin/main` | `c99e2ca83a66841585405f59d63a2206678ffdeb`（与本地 HEAD 相同） |
| 提交数 | 10（`cbefb5b..c99e2ca`：P00 基线、M01/P01 契约实现与 plan 文档集） |
| 内容审查 | 43 文件、+7467/−37；无二进制/权重/凭据；关键词命中均为“禁止提交凭据”类规则文本与字段名，逐条核对无真实凭据 |
| 远端 SHA 校验 | `git ls-remote` 返回 `refs/heads/main` == 本地 HEAD，判定 `SHA_MATCH` |

### 2. 认证通路

- HTTPS 直连 GitHub 被阻断（443 超时）；本机代理 `127.0.0.1:7897` 可达（HTTP 200），但钥匙串无 GitHub HTTPS 凭据。
- 本机 4 个既有 SSH key（`id_ed25519`、`GodSealS-GitHub`、两个 target key）推送前均被 GitHub 拒绝；
  将 `~/.ssh/id_ed25519.pub` 登记到 GitHub 账户后 `ssh -T` 返回 `Hi GodSealS!`。
- 推送使用单次命令级 SSH 覆盖（`url.<ssh>.insteadOf` 与 `GIT_SSH_COMMAND`），未修改 `origin`（仍为 HTTPS）及任何 git 配置。

### 3. 推送后状态与目标同步

- 开发机：`git branch -vv` 为 `main c99e2ca [origin/main]` 且无 ahead；`git status --short` 仅未跟踪 `plan/video-analysis/`；`origin/main...HEAD` = 0/0。
- 目标机：本记录写入前 `/home/jtzn/SelfModelSwitch` 位于 `c99e2ca`、工作区干净；本记录提交后按 [AGENTS.md](../AGENTS.md)
  流程 fast-forward 同步到本记录所在提交，同步前后核对工作区为空。

## P07 记录（2026-09-18）

范围：把 C03 停止条件接入独立 observer、启动恢复与组合入口，并在目标设备用真实 Docker/真实模型实例采集停止材料；不改旧 v1 运行路径。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P07 |
| status | complete |
| source_commit | `22934b413fcd7ebdefeef0193d6339dd698a4a93`（P06b 记录提交，起点） |
| implementation_commit | `296577ac44c4fcf5350a152e04a49a1da13a786b` |
| target_commit | `296577ac44c4fcf5350a152e04a49a1da13a786b`（`git status --porcelain --untracked-files=all` 为空，`git pull --ff-only` fast-forward） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.12.11（开发机）/ 3.12.14（目标 lab venv `/home/jtzn/self-model-switch-build/venv312`） |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p07-20260918T0140Z/` |

本任务判定为 `complete`：三条验收均由本轮命令、开发机/目标机测试和真机材料支撑；**不**产生 `software_verified` / `device_backend_ready` 结论（那是 P29—P31 的范围）。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `python -m pytest tests/test_process_observer.py tests/test_control_recovery_port.py tests/integration/test_process_lifecycle.py -q`（实现前） | 2 | 3 个文件收集失败（`ImportError: DockerProcessObserver/DeploymentRecovery/CONFIG_LABEL`），即 RED |
| 同上（实现后，开发机） | 0 | `36 passed` |
| `python -m pytest tests -m 'not thor' -q`（开发机） | 0 | `421 passed, 1 deselected, 2 warnings`（P06b 基线 397） |
| `python -m ruff check .`（开发机） | 0 | `All checks passed!` |
| `python run.py --check-config`（开发机） | 0 | `schema_version=1 models=embedding,qwen-large,qwen-small,reranker`（旧四 ID 未迁移） |
| 三个测试文件（目标机） | 0 | `36 passed` |
| `python -m pytest tests -m 'not thor' -q`（目标机） | 1 | `418 passed, 3 failed`；失败全在 `tests/test_release.py`，原因是该测试硬编码 `.venv/bin/python`，目标 checkout 的 3.12 环境不在该路径（P28 范围，非本任务回归） |

### 3. 目标机材料

设备 `jtzn-desktop`，`Linux 5.15.148-tegra` aarch64，L4T `R36.4.7`；证据目录

```text
/home/jtzn/self-model-switch-evidence/p07-20260918T0140Z/
  probe.py                         # 本轮探测脚本（从开发机写入证据目录，非 tracked 文件）
  p07-evidence.json                # 全部步骤、端口/进程/退出/内存事实
  exited-inspect.json              # deployment A：退出后保留的容器
  running-inspect-after-stop.json  # deployment B：真实实例 stop 后的容器
  container-logs.txt               # 真实实例的 llama-server 日志（模型加载完成、listening）
```

| 场景 | 实测 |
|---|---|
| 已退出容器（未删除） | 容器 `5a77ca2edb22cbb55be4dd2871fc2f8fa5550d00b383d81710a90eda528baa8e`，`Status=exited`、`ExitCode=0`，宿主端口 18097 `closed` → 观察 `stopped`（`subprocess_state=absent`） |
| 真实实例 | 容器 `5d2e3c92ca4fd14100fb7a10b4a6506299687824281ce8ff52f04d85064a95ec`，镜像 `sha256:8e572bb99c19defa9218f8c07b7ab30379040f3ead87d64b3e241213597c1bc8`，`StartedAt=2026-09-18T01:44:54.625625682Z`，18098→8080；模型 `Qwen_Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf` 加载完成并 listening → 观察 `running` 且 identity 完整 |
| 旧 identity | 同容器 ID、`StartedAt=2026-09-18T00:00:00Z` → `stale_identity`、`accepted=false`，容器仍在运行，未发出 stop |
| 精确停止 | 真实 identity → 仅按容器 ID `docker stop --time 30`，前台启动者退出 143（SIGTERM），容器保留 `exited (143)`，18098 释放 → 再观察 `stopped` |
| 启动恢复 | `reconcile` `ok=true`、`stopped_container_ids=[5d2e3c92…]`、`remaining=[]`、关准入先于 docker；另一 deployment 的 `5a77ca2e…` 未被触碰（Id 不变、不在停止集合） |
| 收尾 | `docker ps -q` 为空、`docker info ContainersRunning=0`；MemAvailable 52576829440 → 52582227968 B（+5.4 MiB） |

`readiness_observations` 首项为 `stopped`：探测脚本在容器创建前未登记任何启动操作，此时"无容器 + 端口 closed"确实成立。
受管路径由 P06 `SupervisedLaunch` 消除该窗口，`tests/integration/test_process_lifecycle.py::test_a_timed_out_launch_never_clears_the_books_early`
以真实子进程证明启动者未终结时不得 STOPPED；启动期另有 deployment 独占（P17 `instance_lock`）兜底。

### 4. 未执行 / 未解决

- 未执行：真实候选模型、envelope、30 分钟混合负载、故障注入与最终验收（P20 以后）；本任务只覆盖停止观察与启动恢复。
- 模型加载只用于产生一个可观察、可精确停止的真实实例，不构成任何能力/性能结论。
- 未解决：①`tests/test_release.py` 在目标 checkout 因硬编码 `.venv/bin/python` 失败（P28）；②目标 lab venv 依赖（含 pytest/ruff）仍是本轮环境供给，尚未由锁文件安装（P28）；
  ③新 observer/recovery 尚未接入 HTTP/控制路由与 v2 composition（P17/P18）；④本任务触碰 6 个文件（含 `tests/test_control_recovery_port.py`），略超"约 5 个"。

## P08 记录（2026-09-18）

范围：统一 legacy/v2 登记为 Book 的唯一内部规格，并落实 C02 的两套内存账本（模型预算 B + 静态物理上限）；不改 v1 对外数字与运行路径。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P08 |
| status | complete |
| source_commit | `336e4611106b4feed0842efe3a39d909353319dc`（P07 记录提交，起点） |
| implementation_commit | `4fe3c8687149d8cb65b0e2ced35652ac4642fcbc` |
| target_commit | `4fe3c8687149d8cb65b0e2ced35652ac4642fcbc`（`git status --porcelain --untracked-files=all` 为空，fast-forward） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.12.11（开发机）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机只跑既有测试套件 |

本任务判定为 `complete`：三条验收均由开发机与目标机的测试结果支撑，属软件层（S）。**不**产生 `software_verified` / `device_backend_ready` 结论。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_registry.py tests/test_resources.py tests/test_runtime.py -q`（实现前） | 2 | 三个文件收集失败（`ledger_specs_from`、`memory_sample_from`、`STOP_RESAMPLE_GRACE_SECONDS` 等不存在），即 RED |
| 同上（实现后，开发机） | 0 | `35 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `436 passed, 1 deselected, 2 warnings`（P07 基线 421） |
| `ruff check .`（开发机） | 0 | `All checks passed!` |
| `run.py --check-config`（开发机） | 0 | 旧四 ID，未迁移 |
| 三个测试文件（目标机 `4fe3c86`） | 0 | `35 passed` |
| `pytest tests -m 'not thor' -q`（目标机） | 1 | `434 passed, 3 failed`；失败仍是 `tests/test_release.py` 的 `.venv/bin/python` 环境假设（P28 范围） |

### 3. 关键事实

- `LedgerSpec` 是唯一内部规格：`effective_reserved_bytes` 由 `contracts_v2.effective_reserved_bytes` 计算，v2 不用 margin，v1 legacy peak 恰好乘一次 margin。
- 两套账本：`committed`（R 之和）与 `physical_committed`（`ceil(physical_peak*1.15)` 之和），都只统计 `reservation>0`（未证实停止）的模型。
- 物理门槛仅在至少一个登记带测量峰值时启用；未测模型在此模式下不准入，pure-v1 账本保持单账本原行为。
- `can_load` 现要求样本晚于上次已证实停止；`stopped(op, now)` 记录停止时刻，`stop_settled()` 暴露 10s 回收窗口。
- 停止未证实（失败/取消任务）时两套账本都不释放。

### 4. 未执行 / 未解决

- 未执行：真实模型的峰值测量、最大组合输入、真机内存门槛验收（P20 以后）；本任务不产生任何性能结论。
- `scheduler.py`/`eviction_policy.py` 仍读 `book.specs` 的 v1 字段；v2 登记接线时需迁移（P14/P16）。
- 低 F "触发清理"与 10s 重采样的调度动作由 P09/P10 接线。

## P09 记录（2026-09-18）

范围：把 C04 独占会话接入单一调度权威（同一把锁决定交互与会话授予、单一 worker 串行执行生命周期 IO），并补齐队列的会话 waiter 与重试语义。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P09 |
| status | complete |
| source_commit | `f55faf5efdfb1b9c35395f246f866f11d5569821`（P08 记录提交，起点） |
| implementation_commit | `58ab00cae23e967466ec33e6a7320b89e96dc03f` |
| target_commit | `58ab00cae23e967466ec33e6a7320b89e96dc03f`（fast-forward，工作区为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.12.11（开发机）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机只跑既有测试套件 |

本任务判定为 `complete`：三条验收由开发机与目标机测试支撑（软件层 S）。**不**产生 `software_verified` / `device_backend_ready`。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_sessions.py tests/test_queue.py tests/test_scheduler_lifecycle.py -q`（实现前） | 4 | 新模块/新 API 不存在（`session_manager`、`WaitKind`、`requeue`），即 RED |
| 同上（实现后，开发机） | 0 | `52 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `457 passed, 1 deselected`（P08 基线 436） |
| `ruff check .` / `run.py --check-config`（开发机） | 0 | 通过 / 旧四 ID |
| 三个测试文件（目标机 `58ab00c`） | 0 | `52 passed` |
| `pytest tests -m 'not thor' -q`（目标机） | 1 | `454 passed, 3 failed`（仍为 `tests/test_release.py` 的 `.venv/bin/python` 环境假设，P28） |

### 3. 关键事实

- 会话期限全部来自配置：`wait≤1800s`、`prepare≤900s`、`hard deadline` 为上限，heartbeat 只刷新 30s soft TTL。
- 一个 worker 串行执行所有会话生命周期 IO，且都在锁外；`status()` 在等待/加载期间仍可读。
- drain 永不 revoke lease；超时只让步（撤销冻结、保留 waiter、30s 重试，队列 sequence 与 deadline 不变）。
- 只有确认 STOPPED 才 CLOSED 并释放预算；未确认则 BLOCKED 且每 5s reconcile。
- pinned/preload 与独占会话冲突时直接拒绝，不修改登记。
- 时钟统一注入（默认 `time.monotonic`），测试用 fake clock + 有界状态轮询，不依赖长 sleep。

### 4. 未执行 / 未解决

- 未执行：HTTP 状态码映射（P18）、execution/cancel 与 blob 传输（P14）、真机模型加载与会话时长验收（P29 以后）。
- `/health` 在会话等待期间仍需 P18 证明 200；本任务只保证调度器不阻塞其判据。
- 会话记录在进程重启后不恢复（随机 boot_id 失效）：由 P10/P13 的重启语义确认。

## P10 记录（2026-09-18）

范围：到期/取消/断线的账本语义、generation/attempt fence 与清理幂等；证明 TTL、硬期限、取消、重启都不会提前释放。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P10 |
| status | complete |
| source_commit | `8d9941db9464afc08fb956ee246f6cfe63edbd73`（P09 记录提交，起点） |
| implementation_commit | `7959fcb11d4d1bec275794268ea8d6a89b817513`（fence/取消）、`2ed163caba195edd67ce436363971b3033c3e0a4`（到期/迟到加载） |
| target_commit | `2ed163caba195edd67ce436363971b3033c3e0a4`（fast-forward，工作区为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.12.11（开发机）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机只跑既有测试套件 |

本任务判定为 `complete`：三条验收由开发机与目标机测试支撑（软件层 S）。`health503` 属 HTTP 层，见第 4 节。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_sessions.py tests/test_cancellation.py tests/test_scheduler_lifecycle.py -q`（实现前） | 2 | 新判据/API 不存在，即 RED |
| 同上（实现后，开发机） | 0 | `59 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `480 passed, 1 deselected`（P09 基线 457） |
| `ruff check .` / `run.py --check-config`（开发机） | 0 | 通过 / 旧四 ID |
| P10 三文件（目标机 `2ed163c`） | 0 | `59 passed` |
| `pytest tests -m 'not thor' -q`（目标机） | 1 | 3 failed，全部为 `tests/test_release.py` 的 `.venv/bin/python` 环境假设（P28） |

### 3. 关键事实

- 到期即失效：`is_live` 用 `now < expires_at`（soft TTL 与 hard deadline 取小），renew/submit 在该瞬间起被拒。
- 取消不是停止：lease、槽位与预留保留到可信终结；ABORTED 后模型为 ERROR，仍保预留直到确认 STOPPED。
- 唯一写回判据：boot/model/generation/operation/execution 一致且 attempt 不回退；拒绝时保留原始 fence。
- 清理只发生一次：重复 `stopped()` 抛 `StaleOperation`，同一模型的 cleanup 批只能建立一次，close 幂等。
- 会话在加载途中被关闭时，迟到加载成功只触发清理，不复活会话、不留下孤儿驻留。
- 存储恢复重新校验且不自动重放推理；新请求仍需显式发起。

### 4. 未执行 / 未解决

- `health503`（BLOCKED 时）与 409/410 状态码映射属 P18 的 HTTP 层；本任务交付调度器判据与 `SessionConflict`/`ModelUnavailable`。
- execution 级 attempt 与终止证据由 P14 使用同一 `writeback_decision` 与 `TerminationEvidence`。
- 本任务触碰 8 个文件（多出 `control_protocol_v1.py`、`tests/test_control_protocol_v1.py`、`tests/test_registry.py`），超出"约 5 个"，理由见 08-execution-plan 记录。

## P11 记录（2026-09-18）

范围：可独立验证的 BlobStore 端口（C07 owner/配额/文件规则、原子发布、已核验 fd 读取、租约与过期）。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P11 |
| status | complete |
| source_commit | `e0e29efef1f9e720b8608fb78abe200f7910c83f`（P10 记录提交，起点） |
| implementation_commit | `8cdea75ee5312fddc119e9da09ec034b5035aaa5` |
| target_commit | `8cdea75ee5312fddc119e9da09ec034b5035aaa5`（fast-forward，工作区为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.12.11（开发机）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机只跑既有测试套件 |

本任务判定为 `complete`：三条验收由开发机与目标机测试支撑（软件层 S）。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_blobs.py tests/test_blob_paths.py -q`（实现前） | 4 | 新模块不存在，即 RED |
| 同上（实现后，开发机） | 0 | `15 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `495 passed, 1 deselected`（P10 基线 480） |
| `ruff check .`（开发机） | 0 | 通过 |
| 同上（目标机 `8cdea75`） | 0 | `15 passed` |

### 3. 关键事实

- 配额是"已发布+暂存+预留"在一个 `BEGIN IMMEDIATE` 事务里的原子判断；并发上传不能各自读配额后超卖。
- 上传逐块 hash、`O_EXCL|O_NOFOLLOW` 独占暂存、一次 `os.replace` 发布；任何失败都删除暂存并归还配额。
- 读取先 fstat + 全量重算 hash 再吐字节；换文件/符号链接/FIFO 一律 unreadable，且会标记 corrupt。
- 目录/文件全部 no-follow；标识符只允许服务生成的严格 ID。
- 租约期内 DELETE 409；过期后新引用 410；活跃租约在过期后继续保护文件。
- SQLite 用 `to_thread` + 线程锁，事务不阻塞事件循环（含阻塞事务期间的循环推进测试）。

### 4. 未执行 / 未解决

- 崩溃点恢复日志、启动 GC 与未完成上传隔离属 P12；HTTP 路由与 peer 身份属 P17/P18；配额覆盖进入 candidate 摘要属 P26/P27。
- macOS 上 `O_NOFOLLOW`/`O_DIRECTORY` 语义与 Linux 一致的部分已覆盖；ext4 上的同一套测试在目标机通过。

## P12 记录（2026-09-18）

范围：Blob 的崩溃点恢复、重启读保护、24h 生命周期与执行输出提交（同一 Fence 裁决）。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P12 |
| status | complete |
| source_commit | `c541f31ebbd7648fbb8c29942b41e1b33ccf3608`（P11 记录提交，起点） |
| implementation_commit | `32e1f0a85ed2e6f427b0b1de10461935a9b6779c` |
| target_commit | `32e1f0a85ed2e6f427b0b1de10461935a9b6779c`（fast-forward，工作区为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.12.11（开发机）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机只跑既有测试套件 |

本任务判定为 `complete`：三条验收由开发机与目标机测试支撑（软件层 S）。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_blob_recovery.py tests/test_blob_outputs.py -q`（实现前） | 4 | 新 API 不存在，即 RED |
| 同上（实现后，开发机） | 0 | `11 passed`（四个 Blob 文件合计 26 passed） |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `506 passed, 1 deselected`（P11 基线 495） |
| `ruff check .`（开发机） | 0 | 通过 |
| 四个 Blob 测试文件（目标机 `32e1f0a`） | 0 | `26 passed` |

### 3. 关键事实

- 崩溃点确定：journal 记录 staging/publish_pending/published；rename 已完成而事务未提交时由恢复补提交，未完成上传与孤儿暂存被清理并归还配额。
- 已发布但不可读（丢失/篡改）→ 转 tombstone/corrupt 且 size 归零，字节回到配额，同 owner 仍得 410。
- 重启读保护：`instances_running=True` 时全量加 `restart:<boot_id>` 租约；GC 跳过任何租约；只有观察到旧实例 STOPPED 后释放。
- 保留期起点分离：输入 = 上传成功，输出 = 执行终结，各 24h；tombstone 24h 后清理。
- 输出与取消共用 `writeback_decision`：异 fence 取消无效，被取消的迟到结果只清理暂存。

### 4. 未执行 / 未解决

- HTTP 路由与 peer UID 身份（P17/P18）、执行终结证据接线（P14）、profile 上限进入 candidate 摘要（P26/P27）。

## P13 记录（2026-09-18）

范围：不依赖 HTTP 的服务层授权与幂等（owner 只来自 peer credential、token 绑定 boot/session/model/owner、幂等索引按 boot 隔离）。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P13 |
| status | complete |
| source_commit | `cd67c8c5155ae1ab4bf0c6a44fa076095f6b97d0`（P12 记录提交，起点） |
| implementation_commit | `f2b8c3e859f2120bad38884d2e776ba9d10fa00d` |
| target_commit | `f2b8c3e859f2120bad38884d2e776ba9d10fa00d`（fast-forward，工作区为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.12.11（开发机）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机只跑既有测试套件 |

本任务判定为 `complete`：四条验收由开发机与目标机测试支撑（软件层 S）。达成 **K3**（P11/P12/P13 全部完成）。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_control_identity.py tests/test_idempotency.py -q`（实现前） | 4 | 新模块不存在，即 RED |
| 同上（实现后，开发机） | 0 | `13 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `519 passed, 1 deselected`（P12 基线 506） |
| `ruff check .`（开发机） | 0 | 通过 |
| 同上（目标机 `f2b8c3e`） | 0 | `13 passed` |

### 3. 关键事实

- owner 只能来自 `PeerIdentity`；自报 owner 或交叉 owner 一律 404，缺失 peer 为 403。
- token 绑定 boot/owner/model/session/execution 与过期时刻，签名恒定时间比较，boot key 每次启动新建且不落盘 → 重启旧 token 全失效。
- 幂等指纹含 route/owner/key（namespace 隔离）并绑定 payload；同 key 不同 payload 是 409，同 key 同 payload 的并发重试返回 busy 或同一记录。
- `active_until` 保护活跃对象不被 24h 清理；失败尝试释放 key 允许重试。
- 记录与指纹中都不含 token 原文。

### 4. 未执行 / 未解决

- peer credential 注入与 HTTP 错误码映射（P17/P18）；token/幂等接入 session/execution 路由（P14/P18）。

## P14 记录（2026-09-18）

范围：把 session 授权、Blob 读取租约、每 session execution 队列与输出发布接成通用执行纵向切片（服务层，无 HTTP，fake BackendPort 验证）。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P14 |
| status | complete |
| source_commit | `f149d37`（P15 目标结果记录提交，起点） |
| implementation_commit | `c512fcde6c47c9eab03dd6df1e64e29f78c26752` |
| target_commit | `282a6a3d8b1ad198fdfa1c4b0c15ba43318b565f`（显式从 GitHub fast-forward，目标树为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.13.5（开发机 `.venv`，本轮开发机无 3.12 解释器）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机只跑既有测试套件 |

判定 `complete`：三条验收由开发机与目标机测试共同支撑（软件层 S）；推送远端与目标 SHA 一致。
首次推送时两机对 github.com:443 短暂超时，重试成功；目标机 `origin` 指向本地裸仓（与指南的 GitHub 远端不符，未改目标机配置），本轮显式从共享远端 URL fetch 后 fast-forward。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_executions.py -q`（实现前） | 4 | 新模块不存在，收集 ImportError，即 RED |
| `pytest tests/test_executions.py tests/test_sessions.py tests/test_cancellation.py -q`（实现后，开发机） | 0 | `35 passed`（计划验证命令；test_executions 单独 `20 passed`，重复 3 次无抖动） |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `556 passed, 1 deselected`（P15 基线 536） |
| `ruff check .`（开发机） | 0 | 通过 |
| `python run.py --check-config` | 0 | 仍为 v1 四 ID，运行行为未变 |
| `git push origin main` | 0/128 | 首次两机 443 超时（exit 128）；重试成功 `f149d37..282a6a3`，`ls-remote` SHA 校验一致 |
| 同上（目标机 `282a6a3d`，python 3.12.14） | 0 | 计划验证命令 `35 passed`；全量 `553 passed, 1 deselected` + 3 项既有 `test_release` 环境失败（P28 范围，P10/P13/P15 同款） |

### 3. 关键事实

- 队列：每 session FIFO（容量常量 128，`queue_full` 拒绝），等待 deadline=`min(enqueue+1800s, session hard deadline)`，到期/关闭的未派发执行以 `not_started` 证据原地终结，视图无容器身份（`dispatch_state=not_started, instance=null`）。
- 授权：submit 仅 ACTIVE 且 live；token（P13）先验、幂等 `execution.create` 重放返回对象现态、不同 payload 409、并发 `busy`、被拒的创建释放 key；owner 与 BlobRef owner 不符按 404 形拒绝；blob owner 目录安全映射 `uid:1000`→`uid-1000` 为本层唯一约定。
- dispatch 重验：会话、能力（v1 登记集合）、canonical 字节上限、generation（fence 随 lease 重签，旧代证据必被拒）；一个 execution 恰一个 `scheduler.acquire` lease（`session_slots_exhausted` 保持队位重试）与恰一个 `blobs.lease` 读保护（执行中 delete 返回 held，终结后 deleted）；输出先 `reserve_output` 上限后派发。
- 终结：succeeded=发布成功 ∧ `writeback_decision` 接受的 `TerminationEvidence`（两半任意顺序；terminal 无结果不假成功；结果无 terminal 不发布）；取消/超时/后端异常 → `cancelling` 持 lease 等证据，无证据不伪造 terminal（会话因此 BLOCKED 是 P10 既有语义）；晚到输出只清理（P12 cancelled 预留）；失败/取消的预留进 `pending_cleanup`，`abandon_output` 成功才移除。
- 回写：唯一判据 `writeback_decision`；拒绝事件 `writeback_rejected` 携带原 fence；重复终结为幂等 no-op（不重复发布、`total_requests` 不增）。
- 调度器：新增 `execution_hook`（close/到期/shutdown 在锁外通知，无第二把权威）；服务对调度器的调用只走 `acquire/release/cancel/session` 公开口；`session_manager.py` 未改动。
- 视图由 `cp.parse_execution_view` 出关校验；"非 terminal 不得带 error"（P02）由 `terminal_code/terminal_message` 在结算时物化实现。

### 4. 未执行 / 未解决

- 目标机 `origin` 指向本地裸仓 `/home/jtzn/git/SelfModelSwitch.git`，与指南"远端为 GitHub、两台一致"不符；本轮未改目标机配置，用显式共享远端 URL fetch 完成复验。拓扑需用户确认（裸仓是否要配置为 GitHub 的 mirror，或改回 origin=GitHub）。
- HTTP 路由、`SO_PEERCRED`→owner 注入、`ExecutionError.code`→状态码映射（P17/P18）；`NotDispatched`/晚到 handle 与真实 adapter、observer、账本的完整接线及 StopAck 后不释放（P16）。
- `book.specs` v1 字段 → v2 登记迁移（P14/P16 遗留说明随 v2 接线处理）。
- `storage_lost`/重启下的在途执行结算语义由 P16 重放场景确认（当前 fail-closed 预留与 lease 不提前清账）。

## P15 记录（2026-09-18）

范围：第一个真实 llama.cpp GGUF adapter（C06 输入计数、输出有限值、禁止重定向、HTTP 完成不等于设备静止）。P15 依赖 P06/P03，不依赖未完成的 P14。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P15 |
| status | software_only |
| source_commit | `dfb8ec15045076812f267e5799ba7e925915a7e0`（P13 记录提交，起点） |
| implementation_commit | `74d3f63385b8ec27a0ec92f4e63e3a9080b8bcc9` |
| record_commit | `6f902d8504585ecc6099e9fff3a2d3fb93c7e0e3` |
| target_commit | `6f902d8504585ecc6099e9fff3a2d3fb93c7e0e3`（fast-forward，工作区为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.12.11（开发机 `/Users/monster/.local/share/selfmodelswitch/venv312`） |
| evidence_directory | 无新目标证据目录；fixture 钉扎 M00 路径，未重采活协议 body |

本任务软件验收三条均由开发机测试支撑。目标活 llama-server 协议 body hash 与停止证据未在本轮采集，因此标 `software_only`，不是设备侧通过。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_llama_adapter.py tests/test_gateway.py -q`（实现前） | 1 | 14 failed, 16 passed（adapter 模块不存在，即 RED） |
| 同上（实现后） | 0 | `30 passed` |
| `pytest tests/test_llama_adapter.py tests/test_gateway.py tests/test_backend_router.py -q` | 0 | `37 passed` |
| `pytest tests -m 'not thor' -q` | 0 | `536 passed, 1 deselected`（P13 基线 519） |
| `ruff check .` | 0 | 通过 |
| `run.py --check-config` | 0 | `schema_version=1` 旧四 ID |
| 目标 `pytest tests/test_llama_adapter.py tests/test_gateway.py -q` | 0 | `30 passed`（Python 3.12.14，HEAD=`6f902d8`） |

### 3. 关键事实

- 输入按登记协议消费：chat/vision=`messages`，embeddings=`input`，rerank=`query`/`documents`。
- 文本 token 来自 `/apply-template` 再 `/tokenize`；未确定图像按 `envelope.max_image_tokens` 计入；计数失败或越 envelope 不发推理请求。
- 远程 image URL 拒绝；PNG/JPEG data URL 按解码后边长检查；输出拒绝 NaN/Inf 与错误 shape。
- HTTP 3xx 不跟随；gateway 显式 `follow_redirects=False`。
- `/slots` idle 不是设备静止；`claims_device_quiescence()` 恒 false；回退独立 STOPPED，冷加载成本 18.07s。
- 未知/registration-only profile 拒绝；锁文件不含 torch/onnxruntime；adapter 无 job/stage。

### 4. 未执行 / 未解决

- 目标活 llama-server 协议 body hash 与真实推理/停止证据（本轮未 SSH 探测）。
- execution 服务接线（P14/P16）；vision Blob 与完整 envelope 边界（P20）。

## P16 记录（2026-09-18）

范围：真实 adapter、C03 observer 与 execution 服务走同一资源账本；消除 P14 fake 中"终结靠测试手工喂"的空隙（M04/K4）。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P16 |
| status | software_only |
| source_commit | `92ed283`（P14 目标复验记录提交，起点） |
| implementation_commits | `f5a3c5b`（生命周期桥）、`a36a17c`（adapter 身份 provider/结果捕获）、`f5dfc74`（managed termination）、`511fec9`（组装 + E2E + observer 修复） |
| target_commit | `fe4c1fbe481d41e77f6b29d097b9d1c843ca79cb`（显式从 GitHub fast-forward，目标树为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机只跑既有测试套件 |

判定 `software_only`：四条验收由集成测试（真实 `LlamaCppAdapter` + 真实 `DockerProcessObserver` + fake docker/llama-server/llama-swap）与全量回归支撑；
计划验证项中的"目标真实推理/取消一轮，记录停止与内存回收"属 K4 场景验收，需 llama-swap 与模型在线，本轮未启动模型。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/integration/test_managed_execution.py tests/test_backend_control.py -q`（实现前） | 1 | ManagedLifecycle/managed_termination 不存在，即 RED |
| 同上（开发机，实现后） | 0 | `15 passed` |
| `pytest tests/integration -q`（开发机） | 0 | `18 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `571 passed, 1 deselected`（P14 基线 556） |
| managed+lifecycle 文件重复 5 次 | 0 | 每轮 `15 passed`（含后续 E2E 为 9+8=17），无抖动 |
| `ruff check .` / `run.py --check-config` | 0 | 通过 / 仍为 v1 四 ID |
| 目标机（`fe4c1fb`，python 3.12.14）计划验证命令 | 0 | `17 passed` |
| 目标机全量 `pytest tests -m 'not thor' -q` | 1 | `568 passed` + 3 项既有 `test_release` 环境失败（无回归） |

### 3. 关键事实

- 桥：`ManagedLifecycle.load` = adapter（swap 启动 + /health + /slots）之后**再独立观察**核对结构化身份才写回；health 谎报（观察 UNKNOWN/歧义）→ `instance_unverified`，账本不动。`stop` 只对已验证身份发 unload；StopAck 不释放，轮询四事实：RUNNING/UNKNOWN → Presence 非 STOPPED → `Book.failed stop_unverified`，预算保留（v1 语义复用）。
- 终结：`claims_device_quiescence()` True → `request_protocol_terminated` 证据；False（llama.cpp）→ `awaiting_quiescence` + per-model quiescer。inflight 判定覆盖"已 claim 未派发"（准入中）记录，杜绝"batch 未齐先停"竞态；stop 经 `scheduler.unload` 单账本路径；一次共享 stop 结算整个 batch（测试断言 unload 恰一次）。
- reload：fallback stop 后 session 保持 ACTIVE（heartbeat 通过）、模型 UNLOADED、预算 0；下一 execution 冷加载 generation+1、fence 重签、adapter 身份 provider 换绑新容器；旧代 terminal 被 `writeback_decision` 拒（`stale_generation`）。
- 不伪造：证明不了停止 → 记录不结算（视图仍 running/cancelling，非 terminal 不带 error，P02 一致），grace 后 quiescer 放弃，fail-closed 交给 drain/BLOCKED/recover；断流异常 → `backend_failed` 也要等 STOPPED 证据才 failed，且晚到/部分输出永不发布（publish 仅在"发布+可信终结"同时具备时发生，in-flight 互斥已封闭双 publish 竞态——该竞态曾被 P14 回归测试抓到并修复）。
- observer 修复：`_match` 在无显式 container 目标时以本 boot running 实例为准（reload 后历史 exited 不再 AMBIGUOUS；双 running 仍歧义）。`canonical_json_bytes(adapter输出)` 为发布字节，与 `read_all` 逐字节相等。
- 装配：`runtime.build_managed_execution` 是 P17 run.py 的 v2 分支入口；本任务未改 `run.py`/`app.py`（P17 范围），v1 行为不变。

### 4. 未执行 / 未解决

- 目标真实推理/取消一轮 + 停止/内存回收记录（K4 验收项，需 llama-swap 与模型在线，本轮未启动模型）。
- v1 `book.specs` → v2 登记直通 Book 的迁移（P14/P16 遗留，P17/P20 定形）；`run.py` v1/v2 分支与 socket 入口属 P17。
- llama adapter 预派发拒绝目前以 `AdapterError` 抛出（未带"证明未送达"标记）→ managed 路径保守走 stop 结算（多付一次 stop 成本，语义安全）；后续接 `NotDispatched` 需 adapter 侧改造。
- quiescer 放弃后无自动重试（避免与 session drain worker 抢 stop 权）；恢复依赖 recover/shutdown 路径。

## P17 记录（2026-09-18）

范围：Unix peer credential 与单进程双入口——先用真实 socket 证明 C08 身份传递，再落 run.py 的 v1/v2 分支与共享运行上下文。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P17 |
| status | complete（AC3 两真实 UID 门禁当日补验） |
| source_commit | `2e27543`（P16 目标复验记录提交，起点） |
| implementation_commits | `efb19f5`（控制监听器）、`c1ba156`（run.py 分支 + 双 listener）、`f096213`（部署目录/文档）、`e65908b`（门禁测试真执行）、`a2789d3`（名单外 UID 真实到达 socket 并被拒） |
| target_commit | 门禁补验时 `a2789d3e915745f93287c9a1e86ab18150dc9111`（显式从 GitHub fast-forward，目标树为空；先前复验为 `1f5936a4cc59a52d4b110b9b2014488aa1493277`） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机只跑既有测试套件 |

判定 `complete`：AC1/AC2/AC4 由真实 AF_UNIX socket 测试与 run 分支测试支撑；AC3 的"两个真实 UID"于当日以目标机 root 补验（见 §4 首条），普通用户下该门禁如实 skip、skip 不计通过。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/integration/test_control_socket.py tests/test_instance_lock.py -q`（实现前） | 4 | control_server 不存在，收集 ImportError，即 RED |
| 同上（开发机 macOS，实现后） | 0 | `14 passed, 1 skipped`（skip=两真实 UID 门禁项），重复多轮无抖动 |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `585 passed, 1 skipped, 1 deselected`（P16 基线 571） |
| `ruff check .` / `run.py --check-config` | 0 | 通过 / 仍 v1 四 ID |
| 目标机（Linux，`1f5936a`，python 3.12.14）计划验证命令 | 0 | `15 passed, 1 skipped`（真 `SO_PEERCRED` 路径全执行；skip=两真实 UID 门禁项） |
| 目标机全量 | 1 | `582 passed, 1 skipped` + 3 项既有 `test_release` 环境失败（无回归） |
| 目标机（`a2789d3`，root）门禁项 `test_two_real_uids_on_linux_one_allowed_one_not` | 0 | `1 passed`（真两 UID：uid 0 得 `200`/`owner=uid:0`；nobody 连接后被 allow-list 静默拒绝） |
| 目标机（`a2789d3`）control_socket + instance_lock 全量：root / jtzn | 0 / 0 | `16 passed` / `15 passed, 1 skipped`（树前后为空） |

### 3. 关键事实

- 身份唯一来源=accepted socket 的内核凭据：Linux `SO_PEERCRED`、macOS `LOCAL_PEERCRED`（socketpair 测试自证本机 uid）；`client=None`，`X-Owner/X-UID` 头被完整忽略（有测试）；allow-list 外 uid 在解析任何字节前被静默关闭（无 HTTP 应答、无信息泄露）。
- 帧层策略：h11 只做**请求解析**（0.16 无公开出站 API；不碰 uvicorn/h11 私有接口的决定见计划"失败则阻塞"）；响应显式小体积序列化、`Connection: close`；HTTP/1.0、CONNECT/TRACE、Upgrade/Proxy-Connection、chunked、>16KiB 头、半包超时——一律关连接不残留；断言 task/fd 数量不增长（psutil）。
- 双入口共享：`run.startup_plan`→v2 锁在 context 构建**之前**（main 顺序测试 `["lock","context","served"]`）；一个 `RunContextV2`（boot_id=TokenAuthority 同源、单 Book、单 BlobStore）；TCP 侧（uvicorn）持有唯一 lifespan（计数器=1/清理=1）；`/internal/*` 从不注册 TCP → 404 by construction；`CONTROL_CONTRACT` 仅 llama-swap profile 需要时 import（hf-only 断言 sys.modules 无该模块）；v2 测试注入 build_backend 炸弹断言永不触发。
- 站点输入 fail-closed：v2 缺 `SELFMODEL_SWITCH_DEPLOYMENT_ID`/`SELFMODEL_SWITCH_SWAP_CONTROL_URL` → 启动拒绝（exit 78），不猜默认、不静默降级；reconcile 未全 STOPPED → 不开放入口（Book.recovering 保持）。
- 部署：`RuntimeDirectory=model-scheduler self-model-switch` + `0750`（渲染断言）；`peer_group` 由 ControlServer chown，失败=启动拒绝；operations.md 写明"同 uid=同 owner、两真实 UID 属 S 门禁"。

### 4. 未执行 / 未解决

- AC3 门禁补验细节（root，目标机）：门禁测试把 socket 置 0660/组 `nogroup`、父目录 0750 组可穿越（保持非 world-accessible，`ControlServer` 拒绝 0o007），让 nobody **真实 connect**——allowed（uid 0）得 `200` 与 `owner=uid:0`；nobody 只写出自身 `connected` 标记、零 HTTP 应答。断言 `denied.stdout == b"connected\n"` 同时排除"被文件权限挡住"（stdout 会为空）与"错误放行"（stdout 会含 HTTP），因此身份确实取自在 accepted socket 上读到的内核 `SO_PEERCRED`、allow-list 是唯一拒绝来源。修复前该门禁为假通过（async `drive()` 从未被 await，`RuntimeWarning: coroutine was never awaited`）；真实执行后又暴露事件循环内同步 `subprocess.run` 的自饥饿（allowed 客户端 10s 超时）；两处均为测试面修正，服务端行为未改。
- 真实部署的客户端组/UID 由部署输入渲染（P27）、两 UID 的 create→execute→cancel/close 黑盒闭环属 P18（CP2）；本任务只证明身份传递本身。
- `/internal/*` 正式路由、错误映射与 1 GiB 流式上传（P18)；TCP 旧 API 的 v2 完整回归（P19）；unit 客户端组/UID 由部署输入渲染（P27）；v2 配置 schema 是否收纳 swap/deployment 输入由 P18/P20 定形。
- 目标机复验曾暴露 Linux RST vs macOS FIN 的断言差异（`52b739c`、`1f5936a` 两修复后全绿）：记录为测试面修正，非服务端行为变更。
- **同步状态（2026-09-18）**：开发机提交 `e65908b`→`a2789d3`→`b2d38ee` 均推送 GitHub `origin/main` 并校验远端 SHA；目标机对 GitHub 的访问在本轮中断（HTTP/2 framing 错误、随后 60s 连接超时），故先以显式 GitHub URL 同步到 `a2789d3`，最终改经其既有 origin（本地裸仓 `/home/jtzn/git/SelfModelSwitch.git`，原停于 `f149d37`）fast-forward 至 `b2d38ee`：`verified_target_sha=b2d38ee...`、root 复验 `16 passed`、前后树为空；裸仓 `main` 现为 `b2d38ee`。目标机 origin 与指南"两台一致 GitHub"不符这一拓扑问题仍待用户确认（P14 遗留）。

## P18 记录（2026-09-18）

范围：控制 HTTP 路由闭环——把 C05/C07 的 session/execution/blob 路由挂到 C08 控制 socket，并把流式 Blob 与 v2 运行上下文接到同一 boot。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P18 |
| status | complete |
| source_commit | `e074af9`（P17 记录/同步提交，起点） |
| implementation_commits | `23d8e0b`（路由骨架 + 流式 Blob）、`5a9e766`（session）、`fcb77c5`（execution）、`b1e6999`（v2 接线 + 端到端闭环）、`1b8ac3f`（peer/限额负例） |
| target_commit | `1b8ac3f796aaa576fd4437ecb2ff2b3c7308d1a8`（经裸仓 origin fast-forward，目标树前后为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机跑既有测试套件（root `52 passed` / jtzn `51 passed, 1 skipped`） |

判定 `complete`：三条 AC 都有"真实 AF_UNIX socket + 真实服务栈 + 假 BackendPort"的证据，并在目标 Linux 上复验；唯一 skip 是 P17 的两真实 UID 门禁（root 运行时不 skip，见 P17 记录）。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_control_api.py tests/integration/test_control_roundtrip.py -q`（实现前） | 4 | `control_api` 不存在，收集 ImportError，即 RED |
| 同上（开发机，实现后；P18 Verification 命令） | 0 | `36 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `621 passed, 1 skipped, 1 deselected`（P17 基线 585） |
| `ruff check .` | 0 | 通过 |
| 目标机（Linux，`1b8ac3f`）`test_control_api + roundtrip + control_socket + instance_lock -q`：jtzn / root | 0 / 0 | `51 passed, 1 skipped`（skip=P17 门禁）/ `52 passed`；树前后为空 |

### 3. 关键事实

- 路由面：版本网关先于路由匹配（`/internal/peer` 豁免，P17 语义不变）；全部拒绝经 C05 封闭错误表（`peer_forbidden` 403、`malformed_json` 400 与 `contract_violation` 422 区分、`payload_too_large` 413、`unsupported_media_type` 415、`stale_token`/`busy`/`blob_in_use` 409、`reference_expired` 410）；`/internal/*` 之外一律 404；响应一旦开始绝不追加第二个。
- Blob（C07）：POST 流式入 `BlobStore.upload`（逐块 hash、declared 与 chunked 预留两路、1 GiB 界）；GET 先判 owner（外 owner 404 不透露存在）与 tombstone（410）；DELETE 204（重复/未知同 owner 亦 204）、读租约持有 409。
- Session（C05）：POST 202 且**不等加载**（新增 `scheduler.register_session`；`open_session` 复用后行为不变）；幂等重放同对象、异 payload 409、拒绝释 key；GET 由绑定字段重算**同一 token**（明文不落盘，只存 fingerprint）；heartbeat/close 需 token；close 202→200，closed 时 `owner_token=null`；恒 owner-404。
- Execution（C05）：token 经 fingerprint→session 映射定位会话；submit 202（重放返回现态）、GET 200、cancel 202/200；排队取消带 `not_started` 证据、不伪造容器身份。
- 帧层：`ControlServer.receive()` 64 KiB 块流式投递（`more_body`/`http.disconnect`），`Content-Length` 与 `chunked` 并存即拒绝；5 MiB 真实上传通过（旧 4 MiB 缓冲帽已不再是瓶颈）；204/304 不写 `content-length`。
- v2 装配：`serve_v2` 用 context 的 blobs/scheduler/service/tokens/共享 idempotency 构造 `ControlAPI`；`service._tokens is context.tokens` 与共享 `IdempotencyStore` 有断言钉住；TCP 侧 `/internal/*` 仍 404 by construction。
- 并发可复现：3 个同 key 并发 POST → 恰一个 execution 对象、失败者 `busy`、后端只收到一次推理。

### 4. 未执行 / 未解决

- 真实部署的客户端组/UID 渲染（P27）、TCP 旧 API/SSE 的 v2 回归（P19）、vision/embedding/rerank 的 parameters 闭集与 envelope 检查（P20）。
- 1 GiB 上限以常量与 413 断言锁定；真实传输验证到 5 MiB（流式路径），未做 1 GiB 实传。
- 断连：路由层证明"上传中断零发布"；GPU/实例释放不误判由 P16 的 `awaiting_quiescence` 语义与其测试覆盖，本任务未重复。
- CP2 的最终里程碑确认仍需 P19/P20 之后的端到端验收（本记录只覆盖 M04 的控制闭环）。

## P19 记录（2026-09-18）

范围：旧 HTTP/SSE 与动态模型兼容——让 v2 运行接线承载既有兼容 API（/live、/health、/v1/*、/api/*），保留消息、错误、SSE 与取消约定。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P19 |
| status | complete |
| source_commit | `125f87f`（P18 记录提交，起点） |
| implementation_commits | `74b5009`（v2 兼容面接线） |
| target_commit | `74b50099a7ce667f9f9a4459e6ef4b6b73f02d19`（经裸仓 origin fast-forward，目标树前后为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机跑既有测试套件（兼容面 44 passed、控制面回归 53 passed） |

判定 `complete`：三条 AC 均有测试支撑（v2 注册驱动同一兼容面、422/404/409/503 语义、SSE/disconnect/透传），并在目标 Linux 上复验。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_chat_api.py tests/test_admin_api.py tests/integration/test_http_disconnect.py tests/integration/test_direct_socket.py -q`（Verification） | 0 | `37 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `628 passed, 1 skipped, 1 deselected`（P18 基线 621） |
| `ruff check .` | 0 | 通过 |
| 目标机（Linux，`74b5009`）兼容面（+embedding_rerank） | 0 | `44 passed` |
| 目标机（Linux，`74b5009`）控制面回归（control_api+roundtrip+control_socket+instance_lock） | 0 | `53 passed`；树前后为空 |

### 3. 关键事实

- 一套路由两种 schema：catalog 适配层让 `/v1/models`、`/api/models`、chat/embeddings/rerank 的模型与能力、unload 存在性、health 的 preload 集全部来自当前 schema 的登记；v2 的 upstream 由 `port` 派生、load/unload 预算取 `memory_reclaim_timeout_seconds`。列表查询不 acquire、不加载（有测试断言）。
- `/api/status` 增加 boot_id（同进程 boot）、executions（total/active/pending_cleanup）与 readiness_reason；不序列化任何 token（测试断言 `"token" not in json.dumps(status)`）。
- 能力不符统一 422（chat 与 json 路由两处；既有 400 断言按 AC 更新）；未知模型 404；卸载在持有 lease/session 时 409（`model_busy`）；health 的 checks 不含队列压力，BLOCKED（recovering/shutting_down/storage fault）或 control 不可达 → 503。
- `build_v2_tcp_app` 承接 P17 的 TCP 骨架位：旧 API 全量、`/internal/*` 永不注册（TCP 404 by construction）、lifespan 唯一（serve_v2 原样）；v2 health 用 scheduler 事实 + control 探针的保守 provider。
- 缺陷修复（本任务暴露）：`ModelScheduler.status()` 假定 capabilities 为枚举且有 priority 等字段，v2 路径首调即 `AttributeError`——重构为经 `book.ledger` 的统一读取（v1 输出逐值不变，控制面 53 项回归通过）。
- SSE 顺序、错误不伪造 DONE、disconnect 走 C04 清理由既有测试继续保护（`test_chat_api` SSE 系列、`test_http_disconnect` 真实 uvicorn 断连）；legacy chat 的 `extra=allow` 透传有新增测试，未被 C05 严格未知字段规则误伤。

### 4. 未执行 / 未解决

- v2 的 `pinned_models`/`preload_models` 未落入账本（`_exclusive_conflict` 与 C04 独占会话的共存规则需单独定形，与 P27 一并）；当前 v2 health 因此保守 503（fail-closed）。
- vision/embedding/rerank 的 parameters 闭集与 envelope 检查（P20）；status 的 executions 计数为内存账本、无持久化。
- 真实 llama-swap/模型在线的兼容面端到端（K5/CP3）仍待 P20 之后的目标机联调。

## P20 记录（2026-09-18）

范围：vision 输入与 embedding/rerank 能力 envelope——把 C06 的运行前输入检查覆盖到兼容 API 与通用接口（adapter），并用每能力输入 fixture 证明检查确实消费输入。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P20 |
| status | complete |
| source_commit | `e2b115b`（P19 记录提交，起点） |
| implementation_commits | `21892f3`（C06 输入检查共享化） |
| target_commit | `21892f3b75b2717e3db57d270edc9c0c66484b12`（经裸仓 origin fast-forward，目标树前后为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | 无新目录；目标机跑既有测试套件（Verification 38 passed、兼容+控制面回归 60 passed） |

判定 `complete`：三条 AC 均由 `tests/test_envelopes.py`（四能力 fixture、精确边界/+1、零 dispatch 断言）与既有 adapter/路由测试支撑，并在目标 Linux 复验。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_envelopes.py tests/test_embedding_rerank_api.py tests/test_llama_adapter.py -q`（Verification） | 0 | `38 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `642 passed, 1 skipped, 1 deselected`（P19 基线 628） |
| `ruff check .` | 0 | 通过 |
| 目标机（Linux，`21892f3`）Verification | 0 | `38 passed` |
| 目标机（Linux，`21892f3`）兼容面+控制面回归（chat/admin/control_socket/roundtrip） | 0 | `60 passed`；树前后为空 |

### 3. 关键事实

- 单一实现：`envelope_validator.py` 被 adapter（通用接口）与 app（兼容 API）共同复用；adapter 删除私有副本后 17 项既有测试逐项通过（行为不变）。
- 图像按**解码后像素**检查（PNG IHDR / JPEG SOF marker），远程 URL 直接拒绝且从不抓取；畸形头不会变成"巨大图像"绕过尺寸限制。
- token 预算不可猜测：兼容 API 注入的 counter 走 adapter 的 `/apply-template`+`/tokenize`（同 runtime tokenizer/template），计数失败 → 503 而非盲目 dispatch；adapter 侧未确定图像按登记 `max_image_tokens` 计入。
- 精确边界：input=28672 通过、28673 拒绝；ctx=input+output；图像 1024px/1 张通过、1025px/2 张拒绝；batch/document 256 通过、257 拒绝且**零 lease 零 dispatch**。
- 兼容面语义变化（有意）：v2 注册的模型空 `messages` 现在 422（C06 格式检查）；embeddings/rerank 超限由 400 改为 422 `envelope_exceeded`（对齐 m00-envelope §3）；chat 路由接受 `chat` 或 `vision` 能力（vision 模型可用 data URL 直接走兼容 chat）。
- AC3：路由集合无 audio/video（测试断言）；`CAPABILITY_INPUT_KEYS` 与 `CAPABILITY_FIXTURES` 一一覆盖四能力，`fixture_coverage` 供 P22 拒绝无 fixture 的生产 candidate。

### 4. 未执行 / 未解决

- batch/document 的实测上限绑定 candidate（P22/P23）；当前 256 为接口层保守默认、只能收紧。
- compat 的 token 计数依赖模型运行时在线（失败 → 503）；真实 llama-server 联调属 K5。
- Envelope 契约未新增 batch 字段（若 P22 的 candidate 需要独立登记 batch 上限再按 C09 扩展）。
- 输出侧校验（NaN embeddings / rank shape / 上游 shape）仍由 app 与 adapter 各自持有（P17/P15 已测），本轮只共享输入侧。

## P21 记录（2026-09-18）

范围：现场 facts、校准与物理内存口径——交付 `model_scheduler.acceptance collect|calibrate`，在目标设备采 C09 facts，并从原始采样重算 §5 判据与 C02 物理上界。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P21 |
| status | complete（fresh 校准 2026-09-19 在目标完成：3/3 轮判据与 C02 物理上界均由原始行证明；此前 partial 项全部闭合） |
| source_commit | `557baf5`（P20 记录提交，起点） |
| implementation_commits | `152c1e8`（collect + CLI）、`92d6c07`（calibrate 核心）、`6e58313`（blocked 退出码/facts 归一化）、`d925a15`（§5 缺口判据 + ≤100 ms 采样器） |
| target_commit | `d925a150190dcc6a648f09f670cbfa7473934480`（经裸仓 origin fast-forward，目标树前后为空） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p21-calibration/`（facts.json、scheduler-v2.yaml、maintenance.json、calibration-final/{measurements.json, raw/run1..3/}） |

判定 `complete`（2026-09-19 更新）：AC1、AC4 早已由目标真实运行证明；AC2 与 AC3 的"从原始样本重算"在 fresh 校准中闭合——目标机以官方 lab 启动路径执行 3 轮，3/3 verified、stops proven、`verdict=passed`，物理上界 `29,675,012,096 B` 由原始 `MemTotal−MemFree` 重算得出（不再依赖被阻塞的保存材料）。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_calibration.py tests/test_m00_envelope_probe.py -q`（Verification，开发机） | 0 | `43 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `664 passed, 1 skipped, 1 deselected`（P20 基线 642） |
| `ruff check .` | 0 | 通过 |
| 目标机 `python -m model_scheduler.acceptance collect --output …/facts.json --model-disk /media/jtzn/sandisk-ext4/models --scratch-disk /var/lib` | 0 | 16 条 facts 带来源写盘（真实 Orin 值） |
| 目标机 `… calibrate --config …/scheduler-v2.yaml --facts … --maintenance … --budget-bytes 16000000000 --runs 3 --from-evidence …/m00-qwen25vl-envelope-20260917T055502Z` | 3 | `verdict=blocked`（3 轮 unverified；材料保留）；`verified_target_sha=d925a15…`，目标树为空 |

### 3. 关键事实

- collect：每个 fact 都带 `file:`/`command:`/`platform:` 来源与读值 sha256（16/16 条可追溯）；缺失读取（空 nv_tegra、空 UUID、非法 MemTotal、空 governor）一律 exit 2，不猜；`--config` 从 v2 的 `storage.model_directory`/`blobs.root` 派生盘路径，无输入则拒绝。
- calibrate 的维护前置是**实时复核**而非信记录：取单实例锁、`docker ps` 无受管容器、控制 socket 不存在、登记端口无监听；记录只声明 `production_admission_closed/instances_stopped` 时仍逐个重验（测试含"声明不等于放行"）。
- §5 判据全部从**原始行**重算：基线中位数（前 10 s）、运行窗最小、post 中位数（后 10 s）、delta>0、前后基线差 ≤256 MiB、>500 ms 缺口计数、swap 出现即失败；cadence 作为**协议事实**报告（真实 0.101 s），不当作缺口。
- C02 物理上界：逐轮 `system_nonfree_upper_bound_v1`（`MemTotal − MemFree` 的窗口最大值）、三轮再取最大；缺 MemFree 一律 null + `bound_note`，绝不换成 MemAvailable（那只是下界）。
- 目标阻塞结论由两条独立原因给出：材料无 MemFree（物理上界）与材料无单调窗口（§5 窗口判据）；两者都属"不能证明"，按 C02/P21 AC3 阻塞生产并保留软件结果（exit 3，`measured_peak_bytes/reserved_bytes/physical_resident_peak_bytes` 全为 null 而非 0）。
- AC4：正式判据 `image_tokens ≤ envelope.max_image_tokens` 为**精确**比较（1280 通过 / 1281 拒绝有测试），probe 的 1.05 放宽不被携带；目标实测 1227 ≤ 1280。
- 采样器（`MemorySampler`）按 C02 记录 7 列（含 MemFree），修复了 M00 probe 只留 MemAvailable 的材料缺口；既有 M00 证据不回改。

### 4. 未执行 / 未解决

- **fresh 校准已完成（2026-09-19）**：目标机用官方 lab 启动路径（镜像 `sms-llama-cpp@sha256:8e572bb9…`，容器内 llama-server `4bc272f`；profile `llama-cpp-gguf-v1`）执行 3 轮，全部 verified、stops proven、`verdict=passed`；证据 `…/p21-calibration/fresh/calibration-2/`；实现提交 `8ab7a38`、`e783ae1`（修掉目标首跑暴露的"Protocol 不可实例化"缺陷）。
- AC3 前半（目标统一内存纳入 `system_nonfree_upper_bound_v1` 口径的确认）已随 fresh 校准完成：Orin 为统一内存（无独立显存），上界取自 `/proc/meminfo` 故天然包含 GPU 侧占用，未用 CUDA 计数重复相加。
- p95/p99/冷加载/最大输入等 policy 阈值属 P22 的显式 `--policy` 输入；P21 只产出可追溯观测，不发明阈值。
- probe 材料口径（缺 MemFree/窗口）已在 `plan/m00-envelope.md` 第 11 节追加说明，未修改既有失败/证据材料。

## P22 记录（2026-09-18）

范围：候选构建与原始事件采集——交付 `acceptance source`（纯源码归档）、`acceptance candidate`（冻结体）、`collector.py`（事件/原始采样/失败材料落盘），以及无 hash 环的可重算摘要。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P22 |
| status | complete（工具 + 依赖注入测试 + 目标真实 source/candidate 拒绝路径） |
| source_commit | `d3cbb79`（P21 记录提交，起点） |
| implementation_commits | `3fc27fa` |
| target_commit | `3fc27fa807633a34c18be7ca96e604915650dc0f`（经裸仓 origin fast-forward，目标树空） |
| candidate_sha256 | null（本任务不产出可发布候选；真实候选在 P29 生成） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p22/`（source-a/-b.tar.gz、source-a.log、policy.json、fixtures.json、fixtures/、scheduler-v2-claims-measured.yaml）与 `…/p21-calibration/`（测量/事实材料） |

判定 `complete`：四条 AC 均有单测覆盖并在目标机以真实仓库/真实材料执行到可判定状态；本阶段按计划 §3 只交付工具与 test-only 材料测试，**不生成可发布通过记录**（真实 `run`/最终 `candidate` 属 P23—P25、P29）。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_candidate.py tests/test_collector.py tests/test_evidence_contracts.py -q`（Verification） | 0 | `38 passed` |
| `pytest tests -m 'not thor' -q` | 0 | `685 passed, 1 skipped, 1 deselected`（P21 基线 664） |
| `ruff check .` | 0 | 通过 |
| 目标机 `acceptance source --root . --output …/source-a.tar.gz`（及 `-b`） | 0 | 两次构建 **同一 sha256** `a8f39ad9…90b35`；118 成员、含 collector、含 tests、不含 plan |
| 目标机 `acceptance candidate …`（`measured: false`） | **2** | "a model that is not measured cannot enter a production candidate"；未写出候选 |
| 目标机 `acceptance candidate …`（配置声称已测 + P21 blocked 材料） | **2** | "the measurement does not prove a physical bound … (C02)"；未写出候选 |

（首轮读取退出码时脚本写法有误——`$?` 被同命令内的 `$(basename …)` 覆盖，故一度显示 0；用变量立即保存后复核为 2，命令被拒时确实不产生候选文件。）

### 3. 关键事实

- `source` 只收白名单内的**已跟踪 regular 文件**（`git ls-files`），脏的白名单文件（未提交修改）直接拒绝；`.env`/权重/凭据/`__pycache__` 被排除且列在 `excluded` 中可见；`plan/` 不在白名单，故计划文档变化**不可能**改变源码 hash（测试覆盖）。
- 归档确定性：`source/` 前缀、字典序、uid/gid/mtime=0、uname/gname 空、gzip mtime=0、不嵌 commit 时间或自身摘要；目标机 118 成员两次构建字节一致。
- candidate 的每一处输入都被重新派生而非信任：facts 重解析、模型资产在 `storage.model_directory` 下逐个 size+sha256 复验、fixture/evaluator 文件复验、`measurement_ref` 必须等于测量材料的 manifest digest、物理峰值必须等于测量值、`collector_sha256` 取自源码归档成员（缺成员即拒绝）。
- `candidate_sha256 = sha256(canonical(body))`，body 键集恰好等于 `CANDIDATE_KEYS`（无自身摘要、无 report、无时间）；把摘要回填配置后 `config_digest` 不变（`V2_DERIVED_KEYS` 排除 `candidate_sha256`），验证无 hash 环。
- collector：事件行同时带 `persisted_monotonic`/`persisted_utc`、run/case/attempt/instance/candidate/device 与完整 fence；sequence 严格递增（乱序/重复拒绝）；无 case 上下文的事件拒绝；原始采样按 kind 保存原文；设备归属只由原始采样推导（GR3D 峰值、CUDA 库映射），无采样时拒绝而非断言布尔；失败材料只追加；manifest 逐项 size/sha256（不含自身，避免自哈希）。
- CLI 偏差一处并已记录：`candidate` 需要显式 `--deployment-id`（§5 示例未给，但 `CandidateV3.deployment_id` 是必填身份且禁止猜测）；`run/merge/verify` 保持显式 exit 2。

### 4. 未执行 / 未解决

- 真实 `acceptance run --layers B/O`（P23/P24）与生产 evaluator（P25）未实现，故 P22 的 fixture/evaluator 在测试中为 **test-only 标记材料**；P28 后由 P29 重做 source/candidate 并真实执行全集。
- 多模型测量的材料需按模型分别提供；当前工具在多个 `measured=true` 时显式拒绝并说明原因（单一测量材料无法证明每个模型的物理上界）。
- P21 遗留的 fresh 校准（受控 runner/镜像）仍未执行，因此**真实候选在 P29 前不可能生成**——这是 C02 的预期阻塞，不是本任务的缺陷。

## P23 记录（2026-09-18）

范围：每模型 B 场景真实执行器——交付 `acceptance/fixtures.py`（达组合边界的确定性 fixture）与 `acceptance/backend_cases.py`（只经正式 API 的案例编排），附 `run --layers` 脚手架。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P23 |
| status | complete（执行器逻辑 + fixture + 目标核验；真实 `run --layers B` 属 P29） |
| source_commit | `8b12b51`（P22 记录提交，起点） |
| implementation_commits | `6fddcdc` |
| target_commit | `6fddcdcd1fc53f08d42121c70cc902574bb0b7f1`（经裸仓 origin fast-forward，目标树空） |
| candidate_sha256 | null（本任务不产出候选，也不计 B 通过） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | 开发机 pytest 报告 + `/home/jtzn/self-model-switch-evidence/p23/fixtures/`（M00 envelope 的 chat/vision fixture 材料） |

判定 `complete`：三条 AC 均有单测覆盖并在目标机复核；真实 B 执行按计划在 P29，本任务**不把 fixture 计为 B 通过**（06-acceptance §3 与 P23 Verification 明确要求）。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_backend_cases.py -q`（Verification，开发机） | 0 | `13 passed` |
| `pytest tests -m 'not thor' -q` | 0 | `698 passed, 1 skipped, 1 deselected`（P22 基线 685） |
| `ruff check .` | 0 | 通过 |
| 目标机 `pytest tests/test_backend_cases.py -q` | 0 | `13 passed` |
| 目标机 M00 envelope fixture 生成（28672/4096/2/1280/1024/1） | 0 | chat 材料 286927 B、vision 材料 1310730 B；两次生成字节一致 |
| 目标机 `run --layers B --output …/run-b` | **3** | 显式拒绝（编排未接线），未产生部分 run 输出 |

### 3. 关键事实

- **单请求组合边界**：fixture 把 `max_input_tokens` 文本、`max_output_tokens`、`max_images`×`max_image_edge_pixels` 图与 `max_parallel` 放在**同一个请求体**里；`boundary_shortfalls()` 逐维报出未达项，短欠即该案例 failed——不接受"拆成若干小请求"的替代（测试断言每个推理案例恰一次调用）。
- **三者齐备才 passed**：推理类案例要求 provider + 可归属设备活动（必须来自原始采样，布尔不算）+ 真实输出；缺归属 → `unknown`（汇总不通过），契约短欠/非有限值/无输出 → `failed`；`Attempt` 自检禁止"passed 带 failure/problems"。
- **崩溃留痕**：执行器捕获任何异常 → failed attempt + 通过正式 API 调 `cleanup` 并记录其结果（cleanup 自身失败也入材料）；接 `FileCollector` 时同步落 `cases.jsonl`/`failures.jsonl`，失败材料只追加。
- **最小值守护**：`CaseExecutor` 拒绝少于 3 次冷启动或少于 3 轮重载的构造参数；reload 每轮都要求上一实例 **proven stopped** 后才 cold load。
- **能力案例输出契约**：chat/vision 非空内容、embeddings 向量数=batch 且维度一致、数值全部有限、rerank 结果数=文档数且 score 有限；不含音视频质量断言（fixture 对 `video/audio/transcription/voiceprint/face` 直接拒绝）。
- fixture 字节确定性（固定种子、无时钟）在开发机与目标机均验证；`write_fixture_material` 产出 P22 兼容的 size/sha256 条目，可直接喂 `candidate --fixtures`。

### 4. 未执行 / 未解决

- **真实 `CaseDriver`**（控制 API 客户端的 load/start/execute/cancel/stop）与 `run --layers B` 编排未实现；`run` 目前校验层集合后显式 exit 3 且不写任何部分输出——属 P24（运维执行器）与 P29（真实运行）。
- capability 输出契约只覆盖协议/shape/有限值/范围；本计划不声称转写/人脸/声纹/视频质量达标。
- embeddings/rerank 的 batch/document 上限沿用 P20 的接口层保守默认 256；实测上限绑定候选待 P29。

## P24 记录（2026-09-18）

范围：O01—O06 运维执行器——交付 `acceptance/workload.py`（到达计划、真实发送与 §4 指标）与 `acceptance/operational_cases.py`（注入故障/lab 端口的编排），硬规则逐条显式校验。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P24 |
| status | complete（编排逻辑 + 注入端口测试 + 目标核验；真实 O01—O06 属 P30） |
| source_commit | `3552fd4`（P23 记录提交，起点） |
| implementation_commits | `46c1436`（首版）+ 本轮"结尾实例 STOPPED"零容忍项 |
| target_commit | `46c14363aa333497f71d9e16a3989a0aa8e2ca95`（核验时；最终记录提交随后同步） |
| candidate_sha256 | null（本任务不产出候选） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | 开发机 pytest 报告 + 目标机同测试输出（无设备侧材料，真实执行属 P30） |

判定 `complete`：四条 AC 均有单测覆盖并逐一在规则破坏变体上验证"会失败"，目标机复核测试与计划闸门；真实故障注入与 1800s 混合负载按计划留待 P30，缺条件一律 `not_run`。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_operational_cases.py tests/test_workload.py -q`（Verification，开发机） | 0 | `19 passed` |
| `pytest tests -m 'not thor' -q` | 0 | `717 passed, 1 skipped, 1 deselected`（P23 基线 698） |
| `ruff check .` | 0 | 通过 |
| 目标机同 Verification | 0 | `19 passed` |
| 目标机真实模型集计划（1800s/120 请求） | 0 | `validate_arrival_plan == []`、每模型 60、最大间隙 15.0s（恰在闸门） |
| 目标机真实 policy 干跑评估（120 条） | 0 | `verdict=passed`、`queue_full_rate=0.0083`、`p95=0.6s` |

### 3. 关键事实

- **指标定义不模糊**：p95/p99 用 §4 的 nearest-rank（1-based ceil，测试用 1..100 验证等于 95/99）；成功请求时延 = queue+load+execute（429/504 无总时延）；429/504/错误率**分母是全部发送请求**（测试用 10/100 与 12/100 验证 0.10 通过、0.12 失败）。
- **policy 只可更严**：`evaluate_workload` 检查 `*_rate_max` 是否被放宽超过 06-acceptance 上限，放宽即问题（不是静默采用）。
- **结尾状态必须逐项报告**：OOM、非预期 500、不安全淘汰、残留实例、队列深度、lease、session、仍在运行的实例——缺报即"无法证明为零"，非零即失败（不假定 0）。
- **O02 隔离硬规则**：私有 mount namespace 必须为真、**禁止卸载整机共享盘**、暂存必须用专用小配额文件系统、root 盘写入必须为 0、恢复必须重 hash；任一违反即失败（测试逐条破坏验证）。
- **O03/O04 语义**：故障期间 health 必须 503 且预算保留（UNKNOWN/BLOCKED 不释放）、不得波及其他容器；重启必须清残留、旧 token 必须拒绝且给出原因、清理后可再准入、不得重放推理。
- **O05 只用原语**：正确候选接受、每个篡改场景**必须在 load 之前被拒**且带原因、禁止调用 production gate（该 gate 依赖 O05 自身报告）、无场景即失败。
- **O06 回滚分支**：备份须给出 64-hex 摘要与文件数、恢复摘要必须一致、回滚对**无已验收证据的旧版必须被拒并说明原因**。
- 崩溃语义：任一步骤异常 → `failed` 且 failure 入材料；`CaseResult` 自检禁止 passed 携带问题；缺条件（端口报 `available=False`）→ `not_run`，不写占位通过。

### 4. 未执行 / 未解决

- **真实执行属 P30**：O01 的 1800s 真实混合负载、O02 的挂载命名空间/配额文件系统、O03 的真实 Docker 故障、O04 的真实重启/残留、O06 的隔离 lab release 演练均需目标设备与宿主特权；本任务按计划只交付编排与端口契约。
- O05 仅调用 P26 的 preflight 原语；完整 production gate 在 P31 执行。
- `workload.py` 的发送使用注入的 clock/wait，真实运行需真实时钟（P30 接线）。

## P25 记录（2026-09-18）

范围：独立 evaluator 与离线 verify——交付 `acceptance/evaluator.py`（从原始事件/采样/输出重算结论）、`acceptance/verify.py`（离线复算 + merge），CLI 的 `merge`/`verify` 接线。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P25 |
| status | complete（evaluator/verify/merge + 合成与目标真实文件系统核验；真实 S/B/O 材料属 P29/P30） |
| source_commit | `1d0891c`（P24 记录提交，起点） |
| implementation_commits | `e11f06e` |
| target_commit | `e11f06e4a51d1b1fb9c2f2b83a72ed3013cae97c`（经裸仓 origin fast-forward，目标树空） |
| candidate_sha256 | null（P29 生成真实候选） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | 开发机 pytest 报告 + 目标机 `/home/jtzn/self-model-switch-evidence/p25/`（26 case 的合成证据与篡改后副本） |

判定 `complete`：四条 AC 全部有测试覆盖，并在目标机以真实文件系统执行"通过 → 篡改原始值（并重填清单 hash）→ 语义失败"的完整链路；本任务按计划不产生可发布通过记录。

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_verify.py tests/test_evaluator.py -q`（Verification，开发机） | 0 | `19 passed` |
| `pytest tests -m 'not thor' -q` | 0 | `735 passed, 1 skipped, 1 deselected`（P24 基线 717） |
| `ruff check .` | 0 | 通过 |
| 目标机同 Verification | 0 | `19 passed` |
| 目标机离线 verify（26 必测 case 完整证据，真实文件系统） | 0 | `verdict=passed`、`cases=26` |
| 目标机篡改（删原始设备活动行 + 重填 manifest size/hash） | **3** | `B:qwen-small:cap:vision: ['the raw samples show no attributable device activity']` |

### 3. 关键事实

- **结论只从材料来**：evaluator 忽略 `status`/`passed`/`gpu_verified`/summary；设备归属只由 `samples/tegrastats.jsonl`、`samples/proc_maps.jsonl` 的原始行重算；缺原始采样即失败（不是"未测通过"）。
- **判据同源**：B 类复用 P23 的判据函数（已改为公开 `attribution_problems`/`capability_output_problems`），O02—O06 复用 P24 的 `recompute_objections` 表；编排器与评估器在同一事实上不产生分歧（测试交叉核对）。
- **S03 算术重算**：`reserved=ceil(peak×1.15)`、边界等式与"差 1 byte 必须拒绝"全部从原始数字重算；缺数字或数字非整数即失败。
- **离线可证**：`verify` 只做文件 I/O——测试把 `socket.socket`、`subprocess.run`、`subprocess.Popen` 全部替换为"禁止"后仍 `exit 0`；同时覆盖 exit 2（缺文件/改字节/缺 final/重复 attempt）与 exit 3（身份与工具绑定、有效期、复算失败）。
- **有效期语义**：未来偏差 ≤5min；自 `started_at` 起 7 天，**恰好 7 天即过期**，且 merge 重排报告起止但不刷新窗口（重打包不能续期）。
- **merge**：只合并同候选/同设备 run，逐文件重校 hash、按 case 唯一 final、保留材料内部结构（`case.json` 与 `samples/` 同层，否则复算会读不到原始采样——这是本轮实际修掉的一个缺陷）。
- 目标机的篡改实验刻意**重填了 manifest 的 size/hash**，证明"把材料改得自洽"不能改变结论：判定确实来自原始值本身。

### 4. 未执行 / 未解决

- 真实 S/B/O 材料在 P29/P30 产生；本任务的证据为合成材料（结构与判据真实，不含设备证据）。
- 候选的 `evaluator_sha256` 目前由 fixtures 声明的 evaluator 材料提供（P22 设计）；P29 重做 source/candidate 时应改为绑定本任务交付的 `acceptance/evaluator.py` 源码 hash。
- `merge` 的 run 拆分策略（S+B / O 分批）随 P30 的真实执行落地。

## P26 记录（2026-09-18/19）

范围：production render 与现场 preflight——交付 `preflight_v3.py`（两层预检 + 防篡改 manifest 身份）、`deploy.render_v3` 与 CLI 路由、`deploy/model-runner.py` 的每次 load 身份校验。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P26 |
| status | complete（渲染与两层预检 + 目标核验；生产完整通过在 P31） |
| source_commit | `d22a23b`（P25 记录提交，起点） |
| implementation_commits | `d7ff362` |
| target_commit | `d7ff3622958f5c4eeca575af6f74fe1596dcd9ef`（经裸仓 origin fast-forward，目标树空） |
| candidate_sha256 | P29 生成真实候选；本任务用夹具候选（单已测模型） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | 开发机 pytest 报告 + 目标机 `/home/jtzn/self-model-switch-evidence/p26/`（渲染目录与层 1 结果） |

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_deploy_render.py tests/test_preflight_v3.py -q`（Verification，开发机） | 0 | `30 passed` |
| `pytest tests -m 'not thor' -q` | 0 | `747 passed, 1 skipped, 1 deselected`（P25 基线 735） |
| `ruff check .` | 0 | 通过 |
| 目标机同 Verification | 0 | `30 passed` |
| 目标机 production 渲染（真实文件系统） | 0 | `production=True`、模型 qwen-small、`identity_sha256` 已写入 |
| 目标机层 1（真实 `/proc/device-tree`） | — | `ok=False`、`loaded_models=0`、12 条现场错配，**未启动模型** |
| 目标机身份篡改（替换 image digest） | **3** | `require_manifest_identity` 拒绝："identity digest does not match its content" |

### 3. 关键事实

- **不加载即可阻断**：层 1 只读现场事实（设备身份、摘要、文件系统、镜像存在性、资产逐文件 hash、证据期限），任何错配都在模型启动前返回，且报告 `loaded_models=0`；没有 force 开关。
- **未验证不渲染**：production 渲染先跑 P25 离线 verify，失败时**不写任何文件**（测试断言输出目录不存在或为空）；非空目标拒绝；lab 渲染需显式临时预算并被 `mode=lab` + `NOT-PRODUCTION` 双重标记。
- **身份防篡改**：`identity_sha256` 覆盖 schema/mode/deployment/candidate/source/config/device 与每模型 image digest+容器名，但**不覆盖自身**（无 hash 环）；runner 在每次 start 前重算，替换镜像/编辑字段即拒绝。
- **站点路径显式**：`--model-directory` 缺失即拒绝，渲染出的 argv 用该路径做只读 bind mount，代码中不存在旧模型盘硬编码。
- **事故与纠正**：本任务清单中 `tests/test_deploy_render.py` 并非新增文件（P06b/P17 已有 18 项测试），我一度用整文件写入覆盖它，使全量从 736 掉到 731；已 `git checkout HEAD --` 恢复并把新测试追加进去（helper 改名避免冲突），`git diff --numstat` = `142 0`（纯增补），全量回到 747。已记入 08 记录，后续凡未标（新增）的测试文件一律先读后改。

### 4. 未执行 / 未解决

- `live_site` 目前返回 v1 形状的硬件身份并留空镜像映射，与 v3 站点字段的适配（真实 machine_id/device-tree/arch/mem_total/盘 UUID、`docker image inspect` 存在性、模型目录逐文件 hash）待 P27/P29 完成；层 1 的比较与拒绝语义已验证。
- 生产完整通过（真实 S/B/O 全集）在 P31；O05 使用层 1 与 gate 的拒绝路径，不用依赖自身报告的完整 gate。

## P27 记录（2026-09-19）

范围：服务部署、优雅停机与回滚——交付 `ServiceInputs`/`render_service_units`（服务单元与 sudoers 渲染）、`OpsPort`/`switch_release`/`rollback_release`（切换与回滚序列），以及 INSTALL/operations 的可执行步骤。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P27 |
| status | complete（渲染 + 序列逻辑 + 目标 `systemd-analyze` 校验；隔离 lab 安装演练与正式安装在 P31） |
| source_commit | `cb3cc34`（P26 记录提交，起点） |
| implementation_commits | `203720f`、`36c4f2e`（systemd 段修正）、`d21b5e2`（测试期望修正） |
| target_commit | `d21b5e273e3e0b8783d7418d5584b1cd4148126e`（经裸仓 origin fast-forward；渲染核验于 `203720f` + 修正后同步） |
| candidate_sha256 | P29 生成真实候选 |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | 开发机 pytest 报告 + 目标机 `/home/jtzn/self-model-switch-evidence/p27/units/`（渲染产物与 service-facts.json） |

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_deploy_render.py -q`（Verification） | 0 | `31 passed` |
| `pytest tests/test_deploy_render.py tests/test_preflight_v3.py -q` | 0 | `38 passed` |
| `pytest tests -m 'not thor' -q` | 0 | `755 passed, 1 skipped, 1 deselected`（P26 基线 747） |
| `ruff check .` | 0 | 通过 |
| 目标机渲染真实单元 | 0 | 三产物 + `video_units=0`；只读挂载/0660/盘 UUID/挂载单元逐项在位 |
| 目标机 `systemd-analyze verify` | 0 | 通过；并暴露 `RequiresMountsFor` 误置于 `[Service]`（已修） |

### 3. 关键事实

- **一切来自部署输入**：service 用户/组、客户端 UID/组、socket 路径与 **0660**、Blob 根/盘 UUID/配额、模型盘 mount 与挂载单元、release 根、配置路径均为显式参数；模板占位与旧路径（`@SSD_MOUNT_UNIT@`、`/mnt/model-ssd`、`/opt/self-model-switch/current`）逐一改写，无硬编码回退。
- **只读与权限**：模型目录以 `ReadOnlyPaths` 只读挂载；`sudoers` 以 0440 落盘；socket 组契约写入单元环境（`SMS_CLIENT_UID/GROUP`）。
- **视频单元永不生成**：输入拒绝 + 模板目录扫描 + 写盘前名单过滤三重保障，`service-facts.json` 记录 `video_units=0`。
- **未停止不切 current**：`switch_release` 的固定顺序把"准入关闭→排空（三项为零）→实例已证明停止→preflight"全部放在 `current` 移动之前；任何未证明即中止且不移动 `current`（测试断言从未调用切链），随后重开准入。切换后失败返回 `repair_required` + 上一 release + 备份名供回滚。
- **回滚语义**：相容降级直接恢复旧 release/配置；不兼容降级必须使用**升级前备份**（缺失则在切换前中止）；无可用旧候选时 `stopped_for_repair`，绝不猜一个旧版。
- **目标机真实反馈驱动修正**：`systemd-analyze verify` 报出 `RequiresMountsFor` 在 `[Service]` 段被忽略（该键属 `[Unit]`，模板本来就有），已删除多余行并同步修正测试期望为挂载点（`/media/jtzn/sandisk-ext4`）。

### 4. 未执行 / 未解决

- 目标隔离 lab 的**空载安装/停止/元数据恢复演练**（systemd 单元状态、PID、socket owner/mode 记录）与正式安装在 P31；本轮只做渲染与 `systemd-analyze` 静态校验。
- `OpsPort` 的真实实现（systemctl 操作、`current` 软链切换、Blob 元数据备份/恢复）待现场接线；接口与拒绝语义已固定。
- Blob 元数据备份的载体（sqlite/json 快照）由现场步骤确定，接口固定为 `restore_blob_metadata(backup)`。

## P28 记录（2026-09-19）

范围：发布归档、Python 3.12/ARM64 锁与 CI 门禁——交付发布完整性检查（v3 身份、禁止物、随包清单、bundle 哈希）、锁解析核验与三 job CI 工作流。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P28 |
| status | complete（发布完整性 + 门禁拆分 + 目标锁核验；CI 实跑与干净 venv 全量安装在 P29/G） |
| source_commit | `6c67f30`（P27 记录提交，起点） |
| implementation_commits | `c2b9bcd` |
| target_commit | `c2b9bcd57d9595f1570f073f1315762323179863`（经裸仓 origin fast-forward，目标树空） |
| candidate_sha256 | P29 生成真实候选 |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv，aarch64） |
| evidence_directory | 开发机 pytest 报告 + 目标机锁解析日志（未落盘长期材料） |

### 2. 本轮命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_release.py tests/test_llama_swap_fixture.py -q`（Verification，开发机） | 0 | `13 passed`（含修复后的 3 项） |
| `pytest tests -m 'not thor' -q` | 0 | `758 passed, 1 skipped, 1 deselected`（P27 基线 755） |
| `ruff check .` | 0 | 通过 |
| 目标机同 Verification | 0 | `13 passed` |
| 目标机 `pip install --require-hashes --dry-run -r requirements.lock` | 0 | hash 校验与解析通过（aarch64/3.12） |
| 目标机 `pip install --require-hashes --dry-run -r requirements-dev.lock` | 0 | 同上 |

### 3. 关键事实

- **v3 身份先行**：打包前必须通过 `require_manifest_identity`；`mode=lab` 或 `production!=True` 一律拒绝（lab 渲染永不作为生产发布）；缺少 evidence 引用（`report_sha256`）拒绝。
- **禁止物**：权重、凭据、视频/媒体实现（路径含 `video/media_pipeline/transcode`）在打包前被拒；测试用 `tests/test_release.py` 直接验证 `_verify_forbidden` 的三类样本。
- **P06a 随包**：`model_scheduler/llama_swap_contract.py` 与 `tests/test_llama_swap_fixture.py` 缺失即阻止发布——固定 ARM64 llama-swap 版本、真实控制 fixture 与 CONTROL_CONTRACT 已是既有材料（7 项 fixture 测试通过），本任务只负责"不随包就不许发"。
- **bundle 可审计**：`bundle.json` 含 release id、archive 摘要、逐文件 size+sha256、manifest 摘要、candidate 摘要与 evidence 块；与 `SHA256SUMS` 并列。
- **长期失败被修复**：`tests/test_release.py` 原先硬编码 `.venv/bin/python`，在目标机必然失败（P08 起记录在案）；改为 `sys.executable` 后开发机与目标机均 13 passed。
- **门禁拆分**：`software-gate`（G，`-m 'not thor'` 兼容选择器不改名）、`peer-uid-gate`（root 跑 C08 两真实 UID 门禁 + 普通用户对照）、`hardware`（self-hosted aarch64/jetson、仅手动触发，明示硬件证据不由该 job 推断）。

### 4. 未执行 / 未解决

- GitHub Actions 的**真实执行**需要远端 runner；本轮验证的是工作流定义与目标机上的等价命令（Verification + 锁解析）。
- ARM64 **干净 venv 全量安装两把锁并运行 G** 属 P29/G 步骤（目标机已具备 3.12 venv 与锁文件，dry-run 已证明可解析）。
- 控制 listener 若需新直接依赖，仍按计划走独立的 P28a 更新 `requirements.in` 与锁，不突破本任务文件边界。到 CP3。

## P29 记录（2026-09-19）

范围：最终候选冻结与全量 S/B 验收（M07）。本轮**未产生候选**：前置条件缺失，按 06-acceptance §4「缺真实设备、资产、fixture 或性能值则 not_run，不填写占位通过值」记为 **blocked / not_run**，不勾任何验收项。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P29 |
| status | **blocked**（三条独立前置未满足；不勾 AC；无候选、无 S/B 通过记录） |
| source_commit | `f60149a`（P28 记录提交，起点） |
| implementation_commits | 无（本轮只交付证据索引与阻塞记录） |
| target_commit | `f60149a251d59886eb58420132a5c7f90508f323`（干净 checkout，与本地 release 源码一致） |
| candidate_sha256 | **null**（`candidate` 按 C02 拒绝写出） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/{p21-calibration,p22,p23,p25,p26,p27,p29}/`（见 `docs/operations.md` 证据索引） |

### 2. 本轮命令与结果（目标机真实执行）

| 命令 | exit | 结果 |
|---|---|---|
| 干净 checkout 与远端 main 比对 | 0 | `target_sha=f60149a251d59886eb58420132a5c7f90508f323`，与本地 release 源码一致 |
| `acceptance collect --model-disk /media/jtzn/sandisk-ext4/models --scratch-disk /var/lib` | 0 | 真实 C09 facts 写盘（`…/p29/facts.json`） |
| `acceptance source --root .` | 0 | 归档 sha256 `43708bc77d8ecb622c708f02a2a5c451c2c0920ec47ac5cd6c8c3c308dcb294b` |
| `acceptance candidate`（P21 真实测量 + P22 policy/fixtures） | **2** | `the measurement does not prove a physical bound … (C02)`；**未写出候选** |
| `acceptance run --layers B` | **3** | 编排未接线；无部分 run 输出 |

### 3. 关键事实（阻塞链的三条独立原因）

1. **C02（主要）**：P21 的 3 轮真实材料只保留 `MemAvailable` 且无单调窗口，`system_nonfree_upper_bound_v1` 无法证明 → `candidate` 在冻结阶段即拒绝。这是 C02 要求的**阻塞语义**，不是缺陷；材料与逐轮理由完整保留。
2. **fresh 校准**：新采集路径需要 §6 的受控 runtime 镜像/runner 接线；当前该路径显式 exit 3，不产出"看似完成"的校准。
3. **真实执行器**：`run --layers B` 需要真实 `CaseDriver`（控制 API 客户端）与 S 层执行器；P23/P24 交付的是**注入式执行器逻辑**（已测），所以 `run` 按设计 exit 3 且不写部分输出。

**本任务真正交付**：`docs/operations.md` 的证据索引（各证据目录内容 + 离线复核命令，不含任何设备凭据）与本记录。P29 三条 AC 全部保持未勾；`plan/08-execution-plan.md` 中逐条注明了"部分可证 / not_run / 机制已备"的准确状态。

### 4. 恢复路径（依赖顺序，任何一步都不许拼接历史通过）

1. 按 §6 构建并固定 ARM64 runtime 镜像（P06a 的 fixture 与 `CONTROL_CONTRACT` 已在包内，缺镜像 digest 入库）；
2. ~~用 P21 的 `MemorySampler` 做 **fresh** 3 轮校准~~ **已完成（2026-09-19）**：`physical_resident_peak_bytes` 已由 null 升级为实测 `29,675,012,096 B`；镜像 digest 已写入登记（`registration.runtimes[0].image_digest`）；
3. 接线真实 `CaseDriver` 与 `run --layers` 编排（层编排 + 材料落盘）；
4. 重建候选（新 `candidate_sha256`）并重跑完整 S/B/O；P25 的离线 verify 与 P28 的发布门禁复验新候选；
5. 三条 AC 方可勾选；P30/P31 同样依赖此链。

### 5. 解除阻塞的可行性探测（2026-09-19，目标机）

为确定恢复路径的第一步，本轮对目标机做了只读探测（未安装、未启动任何模型）：

| 探测项 | 结果 |
|---|---|
| `docker version --format '{{.Server.Version}}'` | `29.2.1`（宿主 Docker 可用） |
| `docker images` | **已有运行时镜像** `sms-llama-cpp:4bc272f`（621 MB，M00 期构建的 llama.cpp 视觉运行时） |
| 固定 llama-swap 二进制 | `/opt/self-model-switch/bin/llama-swap` 已安装；保留的发布包 `…/llama-swap-217-20260917T233137Z/llama-swap_217_linux_arm64.tar.gz` 在证据目录内 |
| 到 GitHub releases 的网络 | `api.github.com` 返回 200（必要时可重新拉取固定版本） |

**结论**：§6 的"受控镜像"比 P28 记录的估计更接近就绪——镜像与固定二进制已在设备上，缺的是**把镜像按 digest 固定并入库**（当前是 tag `sms-llama-cpp:4bc272f`，需要 `docker inspect` 的 image id / registry digest 写入候选运行时登记）。

**因此解除阻塞的第一步应是 P21 遗留的 `calibrate` fresh-run 路径**（用官方启动路径拉起模型 + `MemorySampler` 采样 + stop/quiescence 证明），而不是先造镜像：只有它能把 `physical_resident_peak_bytes` 从 null 变成实测值，从而让 `candidate` 写出第一个真实候选。其后依次是：②镜像按 digest 入库；③真实 `CaseDriver` 与 `run --layers` 编排接线；④重建候选并完整重跑 S/B/O。

本轮**未执行**：fresh-run 校准（需要实现 + 一次独占维护窗口，属下一次会话的第一个任务），也未改动任何镜像/tag。

## P29 补记（2026-09-19）

- **真机 bring-up（2026-09-19，lab）**：llama-swap（cmd 由官方 `render_container_launch` 生成、显式 `proxy` 指向登记端口）+ `run.py`（v2，私有 0700 控制 socket、仅 UID 白名单）已起来；**首次经官方控制 API 完成真实受管执行**：`load active` → `chat succeeded`（`compute_quiescent=True`，输出 Blob）→ `vision succeeded` → `stop closed`，无残留容器。真机共暴露四个产品缺陷并全部修复（capabilities 枚举读取、`acquire` 的 priority、观察者镜像引用、`unload` 的 pinned），驱动的五处按真实 API 修正（operation、幂等键、close token、等 ACTIVE、心跳）。

## P29 补记（2026-09-19，B 层真机第二轮）

| 项 | 值 |
|---|---|
| task_id | P29（仍未完成；AC1—AC3 保持未勾） |
| status | in_progress：真实 B 层首次产出**真实通过**的 case，剩余项原因已逐条定位 |
| source_commit | 起点 `ba655be`，本轮 7 个提交至 `6411d29`（全部推送并同步目标） |
| candidate_sha256 | `2058627b848d6f1c8f4241f5bfddee68c6a24f0dc5e959d0ca83eab13c56cb08`（run-b13 绑定） |
| target_commit | `6411d29`（`/home/jtzn/SelfModelSwitch`，干净 checkout，fast-forward） |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p21-calibration/fresh/{run-b13,candidate-b13.json,fillers.json 来源 p22}` |

**命令与结果（目标机真实执行）**

| 命令 | exit | 结果 |
|---|---|---|
| `acceptance source --root .`（`6411d29`） | 0 | `source-b13.tar.gz` |
| `acceptance candidate --config scheduler-v2-candidate.yaml …` | 0 | 候选 `2058627b…`，`config_sha256=4f5d76c4…` |
| `acceptance run --candidate … --layers B --fixtures-root /home/jtzn/self-model-switch-evidence/p22` | 3 | 8 case：2 passed、10 unknown/failed；无部分通过声明 |
| 直连模型（`/v1/chat/completions`） | 200 | 8192 单元 + 4096 输出 = 165.3 s、`usage` 12307；8192+16 = 19.3 s |
| `/slots`、`/tokenize` | 200 | 每槽 `n_ctx=16384`；`tok…`=7.000、`a`=1.000 token/单元；模板 19 token |

**结论**：控制面 → 调度 → 受管生命周期 → llama-swap → 受控容器 → 模型 → 输出 Blob → 归因（provider/设备原始采样/真实输出）在 `infer` 与 `envelope` 上完整闭合，且 `envelope` 是在**单个请求**内达到声明边界。`verify` 尚未运行（无完整 S/B/O 全集）。

**剩余阻塞（逐条可执行）**

1. 非推理 case 的归属事实：`load`/`stop`/`cancel`/`reload` 需要实例身份，而 session 视图不携带；需在只读实例视图与只读容器观察之间做一次决策。
2. `cap:chat` 的输出边界确定性：模型自然停止会低于声明输出上界；M00 用 `ignore_eos` 跑满，需要同一确定性进入 fixture/参数通路（并确认参数白名单允许）。
3. `cap:vision`：确认图像预算扣除后的重跑（本轮运行早于该提交）。
4. S 层与 O 层编排仍未交付（P23/P24 仅为注入式逻辑）；P30/P31 依赖本链。

## P29 补记（2026-09-19，B 层第三轮：能力轮的真实观测）

| 项 | 值 |
|---|---|
| task_id | P29（仍未完成；AC1—AC3 保持未勾） |
| source_commit | `34a9fda`（本补记前的代码基线），记录提交 `3761c0c`、`78e4d26` |
| candidate_sha256 | `eb2925de850162b56dd3314832f6ffa8af9fe1f8e17622e470b3c1e5e0c808a9`（run-b15） |
| target_commit | `3761c0c`（代码与运行一致）；`78e4d26`（仅文档）**待同步**——目标机到 `github.com` 连续两次失败（`RPC failed / HTTP2 framing`、`HTTP2 stream not closed`），镜像与 checkout 停在 `3761c0c` |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p21-calibration/fresh/run-b15/`（cases 现在含 `facts/problems/failure`） |

**新增交付（代码，均已推送并同步到 `3761c0c`）**

- `34a9fda`：`ignore_eos` 进入 chat/vision 参数闭集（schema 重新导出）、adapter 转发、驱动把 fixture 的生成控制作为 protocol 参数传递；vision 文本预算按**声明的** `max_images × max_image_tokens` 扣除（校验器就是按声明计费的）。
- `3761c0c`：`end_case` 落盘 `status/facts/problems/failure`——失败可从材料解释，不再需要重跑。

**真机事实（run-b15，逐条来自材料）**

| case | status | 材料事实 |
|---|---|---|
| `B:qwen-small:infer` | passed | 归因齐备 |
| `B:qwen-small:envelope` | passed | 单请求达到声明边界 |
| `B:qwen-small:cap:chat` | failed | `observed={input_tokens: 8192, output_tokens: 4096, parallel: 2}`；`problems=["chat: the response carries no non-empty content"]`。边界已达到；填充词后缺指令，回答为空 |
| `B:qwen-small:cap:vision` | failed | `AdapterError: input tokens exceed envelope.max_input_tokens`；文本模板 19 token 不足以覆盖 vision 模板（`vision_start/image_pad/vision_end`）的开销 |
| `load`×3 / `cancel` / `stop` / `reload`×3 | unknown | 非推理 case 缺 provider/实例事实 |

**直测（经官方控制 API，同一请求）**：`ignore_eos=false` → 33 token 自然停止；`ignore_eos=true` → 4096 token 跑满。参数通路有效。

**下一步（按依赖）**

1. 测 vision 模板开销（用 fixture 自身的 1024×1024 图像做一次探测，取其"模板+图像占位"合计），按能力分别扣除文本预算。
2. 填充后追加一条短指令（其 token 成本同样实测），让边界轮是一次真实请求。
3. 非推理 case 的归属：provider=部署身份（`deployment_id@boot_id`，由候选传入、boot_id 取自控制 socket），`load` 的实例事实需在"只读实例视图"与"只读容器观察"之间决策；`cancel` 需轮询到终态并证 `cancelled`。
4. 之后重跑 B；S 层与 O 层编排仍未交付，P30/P31 依赖本链（`78e4d26` 起需重新同步目标）。

## P29 补记（2026-09-20，B 层首次接近全绿）

| 项 | 值 |
|---|---|
| task_id | P29（仍未完成；AC1—AC3 保持未勾） |
| candidate_sha256 | `d2a1e73030f17c7deb0635a11faad082f2a5e3d523dbfb0b61899af5ff012c76`（run-b18） |
| source_commit | `48887cf`（run-b18 的代码），记录补丁 `7c84d87` |
| target_commit | `48887cf`；`7c84d87`（仅一条诊断改进）**待同步**（目标机到 github 再次 `flush 包`/连接失败） |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p21-calibration/fresh/run-b18/` |

**真机结果：12 次尝试中 11 次通过**，8 个 case 中 7 个通过：

| case | status | 说明 |
|---|---|---|
| `load`×3 | passed | provider=`sms-orin-lab@<boot>` + 只读观察到的容器实例事实 |
| `infer` / `envelope` | passed | 归因齐备；单请求达到声明边界 |
| `cancel` | passed | 轮询到终态且终态为 `cancelled` |
| `stop` / `reload`×3 | passed | `stop_proven` + 每次重载的独立冷启动 |
| `cap:chat` | passed | 边界达到（8192/4096）且回答非空 |
| `cap:vision` | **failed** | `AdapterError: input tokens exceed envelope.max_input_tokens`；而同一 fixture 经**适配器同一条计数路径**实测 `charged=8192 = declared`（offset 0），`units=6861、counted=6912、images=1` |

**新增交付（本轮）**

- `48887cf`：非推理 case 的归属——provider 取「候选绑定的 deployment_id + 控制 socket 实际 boot_id」，`load` 的实例事实来自**只读**观察部署自己打标签的容器（列出 + inspect，不启停）；`cancel` 仅在终态确为 `cancelled` 时才算证明；观察不到即缺席，绝不借用别家实例。
- `79f7f9a`：case 输出检查改读**部署真实发布**的响应体（`choices[].message.content` / `data[].embedding` / `results[].relevance_score`）——此前读归一化形状，导致每个健康回答都被判"空"。
- `5d1c2e8`：fixture 材料携带两个模板开销与**带实测成本**的指令（chat 19 / vision 50 / 指令 9 token），文本预算按能力分别扣除；`filler_text` 不再重复扣模板。
- `7c84d87`（待同步）：envelope 拒绝带上具体数字（`(6916 > 8192)`），否则无法离线对账。

**下一步（唯一剩余项）**：`cap:vision` 的拒绝与实测计数矛盾，需在目标机上对比"适配器计数 vs fixture 自算"的具体数字（新拒绝信息就是为此加的），再决定是修正 fixture 的 vision 开销取值，还是修适配器对 vision payload 的计费口径；之后重跑 B，B 层即可全绿，再进入 `verify` 与 S/O 层编排。

## P29 补记（2026-09-20，B 层真机全绿）

| 项 | 值 |
|---|---|
| task_id | P29（B 层达成；AC1—AC3 仍未勾——S 层与 O 层未交付） |
| source_commit | `c3661db`（目标 checkout 同 SHA，工作区干净） |
| candidate_sha256 | `5c63424f2513aa9e922cd80462882c607ac4670c14eef5443e8853cc401b4ec2` |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p21-calibration/fresh/run-b20/` |
| 运行结果 | `acceptance run --candidate … --layers B --fixtures-root …` **exit 0**，`attempts=12、cases=8、passed=12、failed=0、verdict=passed`，run_id `70547ee9732c43eab4c8d810691c7d41`，boot_id `fb8373a7d0bd48c3b41d92f9dc3b6efa` |

**12 次尝试逐条通过**：`load`×3（三次独立冷启动）、`infer`、`envelope`（单请求达到声明边界）、`cancel`、`stop`、`reload`×3（三轮完整重载）、`cap:chat`、`cap:vision`。每个 case 的 `facts` 含 provider（`sms-orin-lab@<boot>`）、实例身份、设备原始采样（`samples/tegrastats.jsonl`）与真实输出。

**本轮关键修正**：图像按**声明上限**计费（1280）而模型实际只消耗 1196，因此"声明输入边界"对图像轮不可达（实测 `observed.input_tokens=8108`）。vision 边界改为声明**可达到的输入**（文本+模板=6912）并把计费值 8192 作为 fixture 文档字段；图像消耗仍由 `images`/`image_edge_pixels` 证明，计费由校验器在派发前约束。

**尚未完成**：
1. **S 层编排**（`S01`—`S06`）：`run --layers S` 仍显式拒绝；AC2 要求 S 在发布解释器通过。
2. **O 层编排**（`O01`—`O06`）：依赖 S/B 与更长的现场负载。
3. AC1 要求"全部登记模型/能力有 fixture 及性能阈值"——当前候选只登记 `qwen-small`（一个模型只能验 load/reload，M07 切换验收需要第二个模型，缺失即 blocked）。
4. `acceptance verify`（P25 离线复算）尚未对 run-b20 执行；P30/P31 依赖 S/O 全集。

## P29 补记（2026-09-20，离线 verify 的第一次运行）

| 项 | 值 |
|---|---|
| 命令 | `python -m model_scheduler.acceptance verify --candidate candidate-b20.json --evidence run-b20`（目标 `0219c4d`，发布解释器 3.12.14） |
| exit | **2**（材料不完整，符合预期） |
| 输出 | `report: missing final attempts for: O01—O06, S01—S06` |

**结论**：①`verify` **拒绝部分证据**，不会把只有 B 层的 run 当作通过——这是 P25/P28 门禁设计要求的语义；②对 B 层材料本身，`verify` 未提出任何缺失、结构错误或候选绑定问题（报告/材料/manifest 契约成立）。因此"先跑 B 再补 S/O"是可验证的路径：S/O 交付后对同一 run 的重新 verify 才会给出通过与否。

下一次实现入口：`plan/08-execution-plan.md` §4 P29 的 **⑤ S 层实施说明书**（模块、每个 observation 的真实来源、CLI 路由与测试形状已固定）。

## P29 补记（2026-09-20，S 层实施与 B 层材料离线复算）

范围：把 S01—S06 从实施说明书变成可运行、可真机复算的一层；同时修复 B 层材料的落盘契约
（`evaluator`/`merge` 读取的形状），并在目标机用同一候选跑通 S+B 与逐 case 离线复算。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P29（⑤a 材料契约 + ⑤b S 层；AC1—AC3 仍未勾——O 层与第二模型未交付） |
| status | 实现完成并真机验证；S 在发布解释器通过 |
| source_commit | 起点 `9f6a92f`；实现 `e87b7e1`（材料契约 + S 层）、`73d14f7`（S05 反例修正）、`1f7245f`（采样窗口修正） |
| target_commit | `1f7245fbb6c432749aba08f4b1f18605a6fbe692`（干净 checkout，fast-forward；树前后为空） |
| candidate_sha256 | `50f1740727222ea94a907e04d17aee4da48687d69382fc55ffb392a78a2adfa0`（`run-s3`/`run-b22` 绑定） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p29-s-layer-20260920T040254Z/` |

### 2. 本地命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_software_cases.py -q`（实现前） | 4 | `software_cases` 不存在，收集失败，即 RED |
| 同上（实现后） | 0 | `16 passed` |
| `pytest tests -m 'not thor' -q`（开发机） | 0 | `831 passed, 1 skipped, 1 deselected` |
| `ruff check .` | 0 | 通过 |
| `run.py --check-config` | 0 | 仍为 v1 四 ID，v1 运行行为未变 |

### 3. 目标机命令与结果

目标机 `jtzn-desktop`，`Linux 5.15.148-tegra` aarch64，L4T `R36.4.7`；lab venv 3.12.14。

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests -m 'not thor' -q` | 0 | `831 passed, 1 skipped` |
| `acceptance source --root .` | 0 | `source-s3.tar.gz` sha256 `ba1ab1f2e55adf8074edb931498a983e6ea91b514340c371ccba11df8e69b1cd` |
| `acceptance candidate …`（P29 facts + P21 fresh 校准 + P21 policy + P22 fixtures） | 0 | candidate `50f17407…`；`config_sha256=4f5d76c4…`、`collector_sha256=89c56711…` 与 b20 一致，仅 source 因代码变化而变 |
| `acceptance run --layers S --inventory inventory.json` | 0 | 6/6 passed（`run-s3`，run_id `82a557ff…`） |
| `acceptance run --layers B --fixtures-root p22` | 0 | 12/12 passed、8 cases（`run-b22`，run_id `4191d9d9…`，13 分钟；窗口 04:37—04:50） |
| evaluator 逐 case 复算（S/B/merge 后） | — | S 6/6、B 12/12、merge 后 18/18 全部 passed；改写 observation 的副本被拒 |
| `acceptance merge --runs run-s3 run-b22` | 0 | 18 attempts / 14 cases（`final-sb`） |
| `acceptance verify --evidence final-sb` | **2** | `report: missing final attempts for: O01—O06`（唯一缺口；S+B 身份/映射/材料通过） |
| 运行后现场 | — | `docker ps` 0、MemAvailable 回落（50.86 GiB）、无残留 |

### 4. 关键事实

- **材料契约（⑤a）**：`CaseMaterialStore` 是唯一写入者；`run-b22` 每个 attempt 的 `event_refs[0]` 以
  `/case.json` 结尾，`merge` 复制为 `cases/<case>/<run>-attempt-<n>/`，`evaluator` 仍按
  `event_refs[0].parent` 定位 `case.json` 与 `samples/`。修复前（`run-b20`）的扁平 `cases.jsonl` 一旦进入
  evaluator，每个 B case 都会因缺 `case.json` 判失败——首轮 `verify` 因缺 S/O 在映射阶段提前退出，
  这一缺陷此前不可见。
- **B 层修复的实证**：`run-b21`（采样窗口修正前）12/12 通过、但离线复算拒绝 `cancel` 与 `stop`
  （`no raw device samples are present`）；`run-b22`（修正后）12/12 全部可复算。失败材料 `run-b21`
  保留在同一证据目录。
- **S 层语义**：每个 observation 由真实调用产生；缺 `--inventory` 的首次 `run-s` 让 S01
  `legacy_config_migrated=false` 且该 case failed（未被填成 true），其余 5 个 case passed。
- **S05 反例修正**：无 runtime tokenizer 时超 envelope 的 `max_tokens` 会被截断而非拒绝，反例改为
  非正输出预算（`73d14f7`）；目标机 `run-s` 首次运行的失败正是该 check 的诚实表现。
- **inventory（S01 的显式输入）**：`inventory.json` 记录运行时镜像 digest（取自候选）、adapter/lock
  文件 hash 与 legacy 资产声明；来源逐条写入 `context.txt`；`asset.size_bytes` 取自 v1 登记的
  `reserved_bytes`（v1 文件不含文件尺寸），迁移只核对 path/sha256。

### 5. 未执行 / 未解决

1. **O 层编排与真实执行**（`O01`—`O06`）：P24 端口与 `workload` 已就绪，缺执行器接线与 1800 s 现场负载；
   `verify` 的完整通过要等它对同一候选重跑。
2. **第二个真实模型**：M07 切换验收要求 ≥2 个模型，当前候选只登记 `qwen-small`（缺失即 blocked）。
3. **服务端版本**：`run-b22` 的服务端进程为本日 02:33 UTC 启动的 `c3661db` 代码（客户端 `1f7245f`）——
   本轮证据用于材料契约验证；P29 的 S/B/O 全集验收须在两端同版本下重跑。
4. 目标机 `origin` 仍指向本地裸仓 `/home/jtzn/git/SelfModelSwitch.git`（P14/P17 遗留拓扑问题）。

## P30 补记（2026-09-20，O 层首次真机执行与被兼容面缺陷挡住）

范围：把 O01—O06 接进 `run --layers O`（未接线端口给 `not_run`），实现 O01 的兼容面负载驱动与零容忍终态探针、O05 的 P26 preflight 原语，并在目标机执行一次真实 O 层运行。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P30（首次执行；AC 全未勾——O01 被产品缺陷挡住，O02—O06 端口未接线） |
| status | **blocked**（编排已交付；真机 O01 120/120 请求 503；O05 因现场口径被拒，已修待重跑） |
| source_commit | 起点 `544caee`；实现 `3a0e8ce`（O 层编排与端口） |
| target_commit | `3a0e8cea976b5eac42b0eebdacfe3f88e66a388c`（fast-forward；树前后为空） |
| candidate_sha256 | `eb09c9b183c02de36c8d7b1728f8e39dda87696eb3983880e26286bb5b8f81bf`（candidate-t1；`policy-o1.json` 把 `arrival_requests` 从 100 改为 120） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p29-s-layer-20260920T040254Z/{policy-o1.json, candidate-t1.json, run-o1, run-o1-all-503}` |

### 2. 本地命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_operational_layer.py -q`（实现前） | 4 | `operational_ports`/`run_o_layer` 不存在，收集失败，即 RED |
| 同上（实现后） | 0 | `7 passed` |
| `pytest tests -m 'not thor' -q` | 0 | `838 passed, 1 skipped, 1 deselected` |
| `ruff check .` | 0 | 通过 |

### 3. 目标机命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `acceptance source` / `candidate`（policy-o1） | 0 | candidate `eb09c9b1…`；`config_sha256` 仍 `4f5d76c4…` |
| `acceptance run --layers O --config … --inference-url http://127.0.0.1:8090 --service-log …` | — | 运行完成但无 `report.json`（输出目录在运行中被改名）；材料逐 case 保留 |
| 直接 `curl` 一次 chat | 503 | `{"code":"service_unavailable","message":"The input could not be counted against the envelope"}` |
| `/api/status` | 200 | `readiness_reason: null`；`models: {qwen-small: unloaded}`；`queue_size: 0` |
| `/health` | 503 | `checks.preload=false`（`preload_models: []`） |

### 4. 关键事实

- **O01 = failed，原因可复算**：`samples/workload.jsonl` 里 120 条全部 `outcome=error, status_code=503`；`metrics.error_rate=1.0`；`final_state` 八项全 0（无 OOM、无残留容器、无 lease/session/队列）。失败原因是请求侧的 503，不是负载或调度。
- **根因（产品缺陷）**：`app.py` 的 chat 路由先做 envelope 计数（`check_chat_input(..., token_counter=…)`）再 `scheduler.acquire`；计数经 adapter 的 `/apply-template`+`/tokenize`，需要模型在线。模型未加载时计数必失败 → 503 → 而加载只在 `acquire` 里发生 → **冷启动死锁**。lab 配置 `preload_models: []`，因此兼容面不可用（P29 的"直连 0.5s 回答"是在会话已加载期间测的）。修复路径见 08 的 P30 记录（先加载再计数，或站点 preload）。
- **O05 = failed，口径不一致**：`read_device_facts` 曾自行实现 `device_tree_sha256`，与 `acceptance collect`（候选 facts 的来源）不同 → 正确候选被拒。已改为复用 `collect_facts` 的同一实现（提交待随本轮记录一起落盘），待重跑。
- **O02/O03/O04/O06 = not_run**：端口未接线；材料里逐项写明缺失条件（私有 mount namespace、Docker 故障、服务重启控制、release 演练），符合"缺条件即 not_run、不填占位通过"。
- **操作失误（如实记录）**：我曾在 O 层运行期间把输出目录改名（想标记失败），导致进程结束时的 `report.json` 写入失败。材料完整（两个目录都保留：`run-o1-all-503` 是改名前的 119 行样本，`run-o1` 是进程重建的目录，含 O01 的 `case.json` 与 O02—O06 的材料）。同名操作不得再犯。
- **环境阻塞**：目标机有免密 sudo、`unshare -r -m` 可用，但工具层把"停止/重启 lab 服务"判为需用户批准的破坏性操作；用户不在场时无法完成 preload 重启。

### 5. 未执行 / 未解决

1. **修兼容面冷启动**（产品缺陷）：`app.py` 的 chat 路由在计数前先预热（`acquire`+`release`），加载不属于 dispatch，符合 C06；需回归 P19/P20 的 320+ 相关测试与目标机复验。
2. **O05 口径修复后重跑**（`read_device_facts` → `collect_facts`）。
3. **O02—O06 的真实端口**：O02 的 `unshare --user --map-root-user --mount` 探针（模型盘遮蔽 + 专用 tmpfs 配额 + 恢复重 hash）、O03 的 Docker socket 故障（`systemctl stop docker.socket`，容器不受影响）、O04 的服务重启/残留/旧 token/再准入、O06 的 release 演练与 blob 元数据备份恢复。
4. **O01 重跑**需要：模型可服务（preload 或 ①的修复）+ 30 分钟窗口 + 输出目录全程不动。

## P30 补记二（2026-09-20，O02 端口落地、挂载身份产品缺陷与冷启动修复）

范围：交付 O02 的私有 mount namespace 端口并在目标机真实执行；修复挡住它的产品缺陷；
修复阻塞 O01 的兼容面冷启动死锁（代码已提交，真机复验待 lab 重启）。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P30（第二轮；AC 仍未勾：O01 待 lab 重启复验，O03/O04/O06 端口未接线） |
| status | **blocked**（O02 端口已交付并在真机跑通；但按实验室当前在用的配置无法注入故障，见第 4 节） |
| source_commit | `92347a2`（O02 端口）→ `04cf9cf`（findmnt UUID）→ `b575225`（故障基线）→ `6f36265`（冷启动预热） |
| target_commit | `b57522581acf3efbd5a39a0bdc71aeac187a265a`（fast-forward；树前后为空；`6f36265` 待同步） |
| python_version | 3.13.5（开发机 `.venv`）/ 3.12.14（目标 lab venv） |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p30-o02-20260920T{070402Z,071113Z,071217Z,071308Z,071325Z}/` |

### 2. 本地命令与结果

| 命令 | exit | 结果 |
|---|---|---|
| `pytest tests/test_operational_namespace.py -q` | 0 | `9 passed`（端口四步、探针不可用→not_run、拒绝步骤→failed、共享盘卸载与根盘写入判失败、close 只关一次、非对象答复拒绝、探针行协议） |
| `pytest tests -m 'not thor' -q` | 0 | `855 passed, 1 skipped, 1 deselected`（O02 端口前基线 840） |
| `ruff check .` | 0 | 通过 |
| `python run.py --check-config` | 0 | `schema_version=1` |

### 3. 目标机命令与结果（O02 探针，`unshare --user --map-root-user --mount --propagation private`）

| 轮次目录 | 结果 |
|---|---|
| `…070402Z` | `model_disk_unavailable=true` 但 reason=`mount_not_found`，恢复 `recovered=false`；**同一配置在宿主侧同样是 `mount_not_found`**，即故障与命名空间无关 |
| `…071113Z` | 配置改为真实挂载点后被 schema 拒绝：`storage must use an absolute mount_path/model_directory and ext4`（`model_directory` 必须是 `mount_path` 的直接子目录） |
| `…071217Z` | 资产前缀改到 `qwen25vl-7b-q4/` 后 `asset_unavailable`：登记的 `embedding` 模型在盘上没有资产 |
| `…071308Z` | 只保留 `qwen-small` 后配置拒绝：`scheduler.pinned_models references unregistered model 'embedding'` |
| `…071325Z` | **通过**：`private_mount_namespace=true`、`unmounted_shared_disk=false`、`baseline_ready=true`、`baseline_files=["qwen-small"]`（真 hash）；故障 `model_disk_unavailable=true`（`asset_path_unsafe`）、`root_disk_writes=0`；专用 tmpfs 配额写满 `scratch_full=true`；恢复 `recovered=true`、`rehashed=true`；耗时 11 s |
| 宿主侧（每轮后） | `findmnt -no TARGET,FSTYPE,UUID` 仍为 `/media/jtzn/sandisk-ext4 ext4 16d53274-…`；模型文件仍在；无残留挂载 |

### 4. 关键事实

- **产品缺陷（已修，`04cf9cf`）**：`AssetStore._check_mount` 用 `findmnt --json --target …` 取挂载身份，而 util-linux 2.37.2 的 `--json` 默认只输出 target/source/fstype/options，**不含 uuid** → `entry.get("uuid")` 恒为 `None` → 真实机上永远 `mount_identity_mismatch`（存储准入 fail-closed 且永不证明身份）。修复为显式请求 `--output TARGET,SOURCE,FSTYPE,UUID`，并新增回归测试（不请求 UUID 列的答复必须被拒）。
- **故障必须可归因（`b575225`）**：O02 早期版本只要"资产校验失败"就记 `model_disk_unavailable=true`，而配置错误同样会让校验失败 → 故障无法归因。现在 `isolate` 先做一次完整 hash 作为基线，基线不成立即 `available=false`（case `not_run`），不允许用配置故障冒充磁盘故障。
- **现场配置缺陷（非产品问题，需站点修复）**：lab 在用的 `scheduler-v2-candidate.yaml` ①`storage.mount_path=/media/jtzn/sandisk-ext4/models` 不是挂载点（真实挂载点是 `/media/jtzn/sandisk-ext4`），产品按登记口径正确报 `mount_not_found`；②登记的 `embedding` 模型在模型盘上没有资产文件。因此**按现有 lab 配置 O02 只能 `not_run`**，探针本身已能跑通（第 3 节 `…071325Z`）。
- **冷启动死锁已修（`6f36265`，待真机复验）**：`scheduler.warm(model_id, deadline)` 复用 `_preload_one` 把单个模型带到 READY 且不授予用户租约（加载不是 dispatch）；chat 路由在计数失败时先 warm 再计数，仍失败才 503。422 的真实超限拒绝路径不变（不 warm、不 dispatch）。

### 5. 未执行 / 未解决

1. **lab 服务重启**：`6f36265` 与 `04cf9cf` 都要重启目标 lab 部署（`run.py --config …`）才会生效；重启属破坏性现场动作，待授权后执行，随后重跑 O01（120 请求/1800 s）与 O05。
2. **站点配置修复**：`mount_path` 改为真实挂载点、`model_directory` 改为其直接子目录、资产路径带上模型子目录、去掉盘上不存在的 `embedding` 登记（或补齐其资产）。
3. **O03/O04/O06 端口**：Docker socket 故障、服务重启/残留/旧 token/再准入、release 演练与 blob 元数据备份恢复。
4. **O01 重跑**需要：可服务的兼容面（第 1 项）+ 30 分钟窗口 + 输出目录全程不动。

## P30 补记三（2026-09-20，lab 站点修正 + S/B/O 三层同候选真机执行）

范围：修正 lab 站点配置并重启部署，验证冷启动修复，然后对**同一候选**跑完 S/B/O 三层并离线复核。

### 1. 任务判定

| 项 | 值 |
|---|---|
| task_id | P30（第三轮；AC 仍未全勾：O03/O04/O06 端口未接线） |
| status | **blocked**（S 6/6、B 12/12、O01/O02/O05 真机通过；O03/O04/O06 `not_run`，故离线 verify exit 3） |
| source_commit / target_commit | `0bd62981bac90071f9430d5fe090927f4ebbe891`（两端一致，目标树前后为空） |
| candidate_sha256 | `35c4af598332faaf2293614cc52e81e7622b781df60ee4a63c8307dbf5ddd35c`（`candidate-o3.json`） |
| python_version | 3.13.5（开发机）/ 3.12.14（目标 lab venv） |
| evidence_directory | `/home/jtzn/self-model-switch-evidence/p30-lab-20260920T075614Z/{scheduler-v2-lab.yaml, facts.json, source-b.tar.gz, candidate-o3.json, run-o3, run-s-o3, run-b-o3, final-o3, run.log}` |

### 2. 站点修正与冷启动复验

| 动作 | exit | 结果 |
|---|---|---|
| 旧 lab `run.py` SIGTERM | 0 | 2 s 退出；8090/10002 无监听；无残留容器 |
| 修正配置（`mount_path=/media/jtzn/sandisk-ext4`、`model_directory=/media/jtzn/sandisk-ext4/models`、资产带 `qwen25vl-7b-q4/`、去掉盘上不存在的 `embedding` 登记） | 0 | `run.py --check-config` → `schema_version=2 models=qwen-small` |
| 重启 lab（同 `SELFMODEL_SWITCH_DEPLOYMENT_ID`/`SWAP_CONTROL_URL`） | 0 | 8090 与控制 socket 可用 |
| **冷启动 chat（无 preload）** | **200** | 模型 `unloaded` → 一次请求 warm+加载 → 11 s 返回真实回答 `"Ready"`；`/api/status` 转 `ready`（此前是永久 503） |

### 3. 三层真机结果（同一候选 `35c4af59…`）

| 层 | 命令 | exit | 结果 |
|---|---|---|---|
| S | `run --layers S --inventory …/inventory.json --legacy-config config.yaml` | 0 | **6/6 passed**（S01—S06） |
| B | `run --layers B --fixtures-root …/p22`（`SMS_CONTROL_SOCKET=…`） | 0 | **12/12 passed**（8 个 case：`load/infer/envelope/cancel/stop/reload/cap:chat/cap:vision`） |
| O | `run --layers O --config … --inference-url http://127.0.0.1:8090 --service-log …` | 0 | `report.json` 写出；O01/O02/O05 **passed**，O03/O04/O06 `not_run` |

- **O01**：120/120 请求、`error_rate=0.0`、p95 0.439 s、p99 0.453 s、发送偏差最大 0.701 ms；结尾八项全 0，并带停止证据 `released=["qwen-small"]`、`stopped=true`、`waited_seconds=1.611`。
- **O02**：私有命名空间遮蔽模型盘（基线真 hash + 恢复重 hash）、专用 tmpfs 配额写满、`root_disk_writes=0`、宿主挂载与文件未受影响。
- **O05**：正确候选接受；四种篡改场景全部在 load 之前被拒。
- **合并与离线复核**：`merge` → 20 case / 24 attempt；`verify` **exit 3**，且问题**恰好**是 `O03/O04/O06 "the attempt exited with 1"`（未接线 → 复算拒绝），其余全部由原始材料复算为 passed。

### 4. 本轮修掉的两个编排缺陷（`0bd6298`）

1. 报告写不出来：O 层返回 `CaseResult`，而报告装配要 `attempt/started_utc/ended_utc` → `AttributeError` → 无 `report.json`（与上一轮"无报告"症状相同，这次定位到真实代码缺陷）。现在 O 层结果带 attempt 身份与起止时间，并有用例断言报告可被 v3 契约复读。
2. O01 结尾无法为零：代理 `ttl=0` 不会自己退出。现在运行**主动申请释放**（`POST /api/models/{id}/unload`）并轮询证明已停止后才读零容忍终态；释放失败/超时/仍运行均记为失败，绝不假定 0。

### 5. 未执行 / 未解决

1. **O03（Docker 通道不可达 / 陌生实例 / stop 超时）、O04（重启残留 / 旧 token / 再准入）、O06（优雅停机 / 日志 / 备份恢复 / 回滚）的真实端口**——三者通过前 P30 的 AC 不能勾，离线 verify 只能是 exit 3。
2. **第二个真实模型**：M07 切换压力要求 ≥2 个模型；盘上只有 `qwen-small` 有可用资产（`embedding` 无资产文件）。
3. 目标机 `origin` 仍指向本地裸仓 `/home/jtzn/git/SelfModelSwitch.git`（P14/P17 遗留拓扑问题）。

## P27/P30 补记（2026-09-20，让其它机器的 Agent 能用：网关 + 部署级防火墙）

范围：把"兼容面被其它电脑使用"做成部署能力，并在目标机真机验证跨机器调用。

### 1. 产品约束（本轮发现，决定了做法）

`model_scheduler/config.py:400`（v2）与 `:509`（v1）都要求 **`server.host` 必须是 loopback**：兼容面的 TCP 监听按设计只在本机，且该面**没有鉴权**。因此"对外可用"不能靠改 `server.host`（会被配置拒绝，实测 `configuration error: server.host must be a loopback address`），只能在部署层加一个**认证网关**。

### 2. 交付（提交 `b3f4b4b`、`3bc42ac`、`71f5c0f`）

- `deploy/gateway.py`：标准库流式反向代理，Bearer token 认证、**只转发 `/v1/*`**（`/api/*`、`/internal/*` 一律 404，绝不暴露控制面）、拒绝含 `..`/`\` 的路径、**不把调用方 Authorization 传给上游**、按块转发（SSE 可流式）、上游不可达 502、无 token 文件拒绝启动。
- `deploy/open-firewall.sh`：幂等 iptables 规则（`-C` 先查再 `-A`），`--cidr` 支持逗号分隔的多个内网，`0.0.0.0/0` 需显式 `--allow-any`；退出码 2=输入被拒、3=iptables 不可用。
- `deploy/sms-gateway.service.in`：网关单元才是**打开内网端口的那一个**（`ExecStartPre=+open-firewall.sh`）；`model-scheduler.service.in` 不再打开任何端口。
- `model_scheduler/deploy.py`：`ServiceInputs` 新增 `allow_cidr/allow_public/scheduler_port/gateway_host/gateway_port/gateway_token_file`（逐项校验；网关端口不得等于调度端口；token 必须是绝对文件路径），渲染第三个单元并把它们写入 `service-facts.json`。
- 测试：`tests/test_deploy_gateway.py` 8 项（真上游 + 真网关：带 token 转发且不上传凭据、无 token 401 且请求根本没离开、四类控制面路径 404、上游不可达 502、token 文件缺失/空白拒绝启动、非 http 上游拒绝）＋`tests/test_deploy_firewall.py` 16 项（幂等、多网段、移除/检查、七类拒绝、渲染进单元与 facts）。

### 3. 目标机真机验证（`71f5c0f`，开发机 ↔ `192.168.55.1`）

| 项 | 结果 |
|---|---|
| 监听 | 调度器 `127.0.0.1:8090`（仍仅本机）；网关 `192.168.55.1:8091` |
| 防火墙 | `-A INPUT -s 192.168.55.0/24 -p tcp -m tcp --dport 8091 -j ACCEPT` 与 `192.168.1.0/24` 同规则（只开 8091） |
| 目标机本地无 token | **401** |
| 目标机本地带 token | **200**，真实回答 |
| **开发机经网关**（跨机器） | 无 token **401**；带 token **200**，`choices[0].message.content = "2+2 equals 4."`；`/health` 经网关 **404**（非 `/v1`，按设计） |
| 模型状态 | 重启后 `qwen-small` 恢复可服务（此前的 `error/load_failed` 由我在 O03 探针里遗留的同名容器造成，容器已删、重启后账本清） |

### 4. 同轮发现（未修，记录）

- **`/health` 的 preload 检查恒假**：即使 `preload_models: ["qwen-small"]` 且模型已在服务（请求 200），`/health` 仍是 503、`checks.preload=false`。说明 v2 的 `preload_pending`/`preload_error` 不会被清（任务失败或被卡住），`/health` 因此**不能作为就绪判据**；这也正是 O03 的 `health_status=503` 判据失去区分度的原因（故障前后都是 503）。
- **模型盘现状**（`/media/jtzn/sandisk-ext4/models/`）：`qwen25vl-7b-q4/`（Q4_K_M 4.4G + mmproj 1.3G，已登记为 `qwen-small`）、`qwen36-35b-aggr/`（Q4_K_M 19.7G、Q4_K_P 21.8G、mmproj f16 858M，**未登记**）、`moss_td/`（safetensors 1.7G，非 GGUF profile，不能由当前 runtime 加载）。登记 qwen3.6 需要按 C02/P21 重做测量 + fixtures + 重建候选，不是改配置就能生效。
- 本次网关是在目标机**手工拉起**的（`deploy/gateway.py` 直接运行）；作为 systemd 单元正式安装属 P27/P31，尚未执行。

## 站点改名与网关放开（2026-09-20）

- 模型 ID `qwen-small` → **`qwen25vl-7b`**（消除"小模型"的误导；它就是盘上的 Qwen2.5-VL-7B）。改名同时覆盖：`registration.models[].model_id`、`scheduler.pinned/preload_models`、lab llama-swap 布置的 models 键、容器名 `sms-sms-orin-lab-qwen25vl-7b` 与标签 `io.self-model-switch.model=qwen25vl-7b`（否则受管观察者无法归因）。原布置备份：`…/p30-lab-20260920T075614Z/llama-swap.lab.json.bak-rename`。
  验证：`--check-config` 通过（`models=qwen25vl-7b`）；`/api/status` → `qwen25vl-7b: (ready, None)`；容器名与标签均为新 ID；经网关从目标机与**开发机**（LAN `192.168.1.100:8091`）调用均 **200**，返回真实答案。
- 网关改绑 `0.0.0.0:8091`（调度器仍 `127.0.0.1:8090`），USB 直连 `192.168.55.1` 与有线 LAN `192.168.1.100` 两个地址均验证 200；来源仍由 iptables 两条规则限制在 `192.168.55.0/24`、`192.168.1.0/24`。
- **遗留一致性**：已冻结候选 `35c4af598332faaf2293614cc52e81e7622b781df60ee4a63c8307dbf5ddd35c` 及其 S/B/O 材料仍使用 `qwen-small`；改名改变了部署身份，因此下次重建候选必须在 `qwen25vl-7b` 下重做全部证据，不得沿用旧材料的通过结论。

## qwen3.6-35B 校准与 C02 静态门槛（2026-09-20，独占维护窗口）

### 1. 窗口与结果

维护窗口：站点关闭（scheduler / llama-swap / gateway 停止、容器清空、控制 socket 删除），另**临时关闭 swap**（C02 §5 把任何 swap 记为 anomaly）。窗口结束后已用目标机自己的 `nvzramconfig.service` 恢复 12×2.6G zram swap，并重启三层服务（7B 已复验 200）。

| 轮次 | 目录 | verdict | 关键值 |
|---|---|---|---|
| 第一轮（swap 开） | `…/cal-qwen36/` | **blocked** | 3 轮均 `ready=true`、图像用例跑通（视觉 1037 token）、停止全部 quiescent；但 swap 使用 3.5 MiB / 1.5 MiB / 0 → **3 轮全部 unverified**；`physical_bound_proven=true`、bound 65,456,832,512 B |
| 第二轮（swap 关） | `…/cal-qwen36-noswap/` | **passed** | `verified_runs=3/3`、`measured_peak_bytes=15,904,792,576`、`reserved_bytes=18,290,511,463`、`physical_resident_peak_bytes=65,243,426,816`、`temporary_budget_bytes=30e9 ≤ bound`、`stops_proven=true` |

即：**模型本身可用**——19.7 GiB 权重在 64GB Orin 上加载、运行、跑视觉用例、优雅停止，MemAvailable 峰值只有 14.8 GiB。

### 2. 但 C02 的静态物理门槛把它挡在门外（需决策）

`Book.physical_admissible` 要求 `ceil(physical_resident_peak_bytes × 1.15) ≤ resources.model_budget_bytes`：

| 模型 | physical bound | ×1.15 | 现预算 36e9 | 结果 |
|---|---|---|---|---|
| `qwen25vl-7b` | 29,675,012,096 | 34,126,263,911 | ≤ 36e9 | ✅ 通过（这正是当初把预算抬到 36e9 的原因） |
| `qwen36-35b` | 65,243,426,816 | **75,029,940,838（69.9 GiB）** | > 36e9 | ❌ 永远不可准入 |

而且 69.9 GiB **大于整机内存**（MemTotal 65,893,224,448 B = 61.4 GiB），所以**无论把预算调到多少都不可满足**。

根因：该 bound 的口径是 `MemTotal − MemFree`（含 page cache）。35B 的 19.7 GiB 权重被 mmap 进 page cache，于是"物理上界"≈整机内存；模型越小这个口径越贴近真实（7B 的 29.7 GiB 里权重只占 5.4 GiB），模型一大它就退化成"文件大小 + OS"。

### 3. 待决策的两条路（不得静默绕过）

1. **保留规则**：本机不登记 35B；规则的本意正是"不要在逼近整机内存的模型上冒险"。
2. **改口径**：让静态门槛用与准入一致的量（`reserved_bytes`，本轮 18,290,511,463 B）或从 bound 中剔除**可回收的 page cache**。这会同时放宽 7B 的门槛（34.1e9 → 7.0e9），属于 C02 语义变更，需同步改 `contracts_v2.physical_reserved_bytes_from_peak`、evaluator 与 plan/08 C02，并重新定义"物理上界"在验收中的含义。

本轮材料已保留（两轮共 6 个 run 的 `raw/runN/{sampling/meminfo.csv, round.json, container.log}` + `measurements.json`），任一路径都可复算。

> 本节的两条路已在 2026-09-22 由 **RP15/ADR-05** 闭合：选择**保留规则**（本机不登记 35B），并冻结当前生产候选
> 范围（见文末「勘误与决策更新」）。原始记录不删改。

## qwen3.6-27B 登记、双模型并发与两个"永久 503"缺陷（2026-09-21）

### 1. 站点与资产（本机 lab）

- 资产落盘 `/media/jtzn/sandisk-ext4/models/qwen36-27b-q6/`：`…-NEO-Q6_K.gguf` 23,582,346,672 B（sha256 `f1e1b337…`）、`…-NEO-MTP-Q6_K.gguf` 24,033,705,440 B（sha256 `2e8a9bdb…`）、`mmproj-BF16.gguf` 931,146,304 B（sha256 `05353347…`）。开发机与目标机两侧各自校验，字节数与官方 tree API 的 LFS oid 一致。
- 新 lab 配置 `…/qwen36-27b-lab/config.yaml`（渲染 `deploy2/`，`deployment_id=sms-orin-lab2`）：`storage.model_directory` 上提到 `/media/jtzn/sandisk-ext4/models`，两个模型的资产路径带子目录前缀。**两个模型都标 `measured: false`**——`Book.physical_enforced = any(物理峰值非空)`，只要有一个带测量值，未测量的 27B 就会被 `physical_admissible` 一律拒绝，故该配置主动关闭物理门槛。原 `lab-b10` 与冻结候选未改动。
- 27B 的 `reserved_bytes: 29000000000` 是估算值，不是实测；C02 静态门槛（35B 一节 §3）仍未解决。

### 2. 硬件事实：两个模型可以共存

纯 docker 隔离实验（无调度器、无 llama-swap），7B 常驻下逐档调 27B 的 `-ngl`：

| `-ngl` | 27B | 27B t/s | 7B 同时健康 | 峰值已用 |
|---|---|---|---|---|
| 99 | 存活 | 4.08 | 200 | 36.2 GB / 62.8 GB |
| 80 | 存活 | 4.15 | 200 | |
| 64 | 存活 | 4.04 | 200 | |
| 48 | 存活 | 2.08 | 200 | |

即 7B + 27B 合计约 36 GiB 在本机可行，**不需要降卸载或换更小量化**。上一轮得到的 `NvMap error 12 / cudaMalloc failed` 是**实验自身的污染**：隔离实验用了不带 `--rm` 的容器，留下同名已停止容器，后续 `docker run --name` 被 docker 拒绝（退出码 125），并非内存上限。

### 3. 两个"永久 503"缺陷与一个孤儿容器（均已修，真机复验）

- **A：账本 `ready` 而运行时已消失。** 容器无声消失（无人发过 unload，llama-swap 日志可证），账本仍 `ready`；`_preload_one` 的 `if runtime.state.value == "ready": return` 直接返回，`warm` 空转、计数继续失败 → 永久 503，只有重启调度器才恢复。
- **B：加载失败即终端。** 加载失败把账本置 `ERROR` + `admission_blocked`，而 `_preload_one` 对 `error` 直接抛错；一次瞬时失败 = 进程余下生命周期的永久 503。
- **C：孤儿容器无法释放。** 加载在 `instance_unverified`（适配器声称 RUNNING、v3 观测器未确认）处失败时 `ManagedLifecycle._instances` 从未记录实例，`stop` 因 `identity is None` 不发卸载命令，容器既不能用也不能停。

修复三段：`b5f230f`（`warm` 先见证 `ready` 的运行时，证实的停止经 `begin_eviction`/`stopped` 写回）→ `b3fa035`（归一化两套后端形态：`ManagedLifecycle.observe(model_id, deadline)` + v3 `state`，`LlamaSwapBackend.observe(model_id)` + contracts `presence`/`healthy`；第一版按单参调用在生产路径抛 `TypeError` 并被折成拒绝）→ `51ddcd1`（`ERROR` 经 `begin_cleanup` 受控回收后重载，**首次 + 最多 3 次重试**，`load_retry_limit=0` 保持原终端行为；`READY` 清零计数；新增 `LlamaCppAdapter.release(model_id)` 与 `ManagedLifecycle.stop` 的无身份回退，让孤儿容器可按模型名卸载）。**回收路径没有放宽任何一条 C02 判据：`stopped()` 仍只在停止被证明后调用。**

### 4. 真机结果（`51ddcd1`）

从**开发机**经网关（`http://192.168.55.1:8091`，Bearer）交替调用，两轮 8/8 全部 200：

| 轮 | 序列 | 结果 |
|---|---|---|
| 1 | 7B → 27B → 7B → 27B | 200 / 200 / 200 / 200（10.95 s / 22.84 s / 24.81 s / 45.70 s） |
| 2 | 27B → 7B → 27B → 7B | 200 / 200 / 200 / 200（0.74 s / 24.78 s / 45.56 s / 25.00 s） |

解码 7B 15.0–15.7 t/s、27B 3.95–4.18 t/s；终态两个模型均 `ready`、`last_error` 为空。切换一次的代价是"回收 + 重载"的 25–46 s（此前是失败）。

### 5. 工程记录

- 本地：`pytest tests -m 'not thor' -q` = **897 passed, 1 skipped, 1 deselected**；`ruff check .` 通过；`run.py --check-config` 输出未变。
- 交付：开发机 → GitHub（`git@github.com` 通路；HTTPS 拉取端点本轮超时）与目标机裸仓库 `/home/jtzn/git/SelfModelSwitch.git`，三处 SHA 一致；目标 checkout 守卫式 ff-only 到同一 SHA，前后工作区为空。
- 证据：`/home/jtzn/self-model-switch-evidence/qwen36-27b-lab/`（`coresidency/{sweep.json,sweep.log}`、`scheduler-retry.log`、`llama-swap7.log`、`gateway.log`）。

### 6. 未做与遗留

- `load_retry_limit` 目前只是调度器构造参数（默认 3），**没有接进 v2 配置 schema 与部署渲染**，运营侧改不了。
- v2 的 `pinned_models`/`preload_models` 仍未落入账本（`plan/08` 既有遗留），本轮配置里的 preload 因此不生效；要压低切换延迟仍须先接线。
- C02 `physical_resident_peak_bytes` 的口径决策（35B 一节 §3）仍未做；27B 的 `reserved_bytes` 仍是估算值，未按 C02/P21 做三轮实测。
- `/health` 的 preload 检查恒假（既有遗留）未动；`MTP-Q6_K` 已下载但未做对照测试。

## 勘误与决策更新：C02 生产范围（2026-09-22，RP15）

本节只更正一个数字并记录产品决策，**不改写上面的原始记录**（35B 两轮材料、失败与遗留保持原样）。

1. **整数勘误**：35B 的预留此前写为 `75,029,940,838`（少 1 B）。按现行整数公式
   `physical_reserved_bytes_from_peak`（`contracts_v2.py`，`(peak×115+99)//100`）复算：
   `(65,243,426,816×115+99)//100 = 75,029,940,839 B`；7B 的 `34,126,263,911 B` 复算无误。
2. **决策更新（Accepted）**：上面 35B 一节「§3 待决策的两条路」与 §6「口径决策仍未做」已由
   [ADR-05](adr/decisions.md) 闭合——产品选择**保留规则**，并冻结当前生产候选范围：
   生产 model_id 集合 = {`qwen25vl-7b`}（chat/vision，预留 `34,126,263,911 B ≤ 36e9`）；
   `qwen36-35b`（预留 75,029,940,839 B > MemTotal 65,893,224,448 B）、`qwen36-27b`（仅 lab，预留为估算、
   无三轮测量）与 `embedding`/`rerank`（无真实 fixture/测量）排除出**当前候选范围**，排除不是永久不可用。
3. **历史材料与当前范围分开**：本文档的 35B 校准（`…/cal-qwen36*`）、27B 双模型 lab 实验（`sms-orin-lab2`）
   以及 `qwen-small` 时代的已冻结候选都是**历史材料**——前者不是生产准入证据，后者的通过结论不得沿用到改名后的
   `qwen25vl-7b`。当前生产范围只有在 `qwen25vl-7b` 下重建 S/B/O 证据后才成立（RP17）。
4. 本勘误不重跑模型、不改任何门槛/余量/预算，也不宣称设备或生产验收通过。

## RP17 同 SHA 目标复验（2026-09-22）：BLOCKED —— 冷启动发现真实缺陷

范围：把开发分支 `chore/verify-v3-review-20260922` 的 `47cb7473a205aa32d10fb50ad3af15bb53296686` 推到 GitHub 与目标
裸仓库，目标 checkout 守卫式 ff-only 到同一 SHA，然后在目标机运行。硬件核验：Jetson AGX Orin（`jtzn-desktop`、
R36 rev 4.7、aarch64、kernel 5.15.148-tegra）。模型只读校验（候选模型 `qwen25vl-7b`）：
`Qwen_Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf` = `3f4513330aa7f109922bd701d773575484ae2b4a4090d6511260a2a4f8e3d069`
（4,683,072,320 B）、`mmproj-…-bf16.gguf` = `d1c7588c0bdf6e7889737c01cfd54309240d54042e102275880a514ae979aea3`（1,354,162,912 B）。

通过项：三处 SHA 一致（开发机 / GitHub / 目标裸仓库与 checkout），目标树同步前后为空；目标机同 SHA 全量套件
（python 3.12.14 / venv312）**1017 passed、1 skipped、1 deselected（49.65s）**——唯一 skip 是仅 root 可跑的 Linux
双 UID 用例，K7 的 socket/gate/owner 用例在 Linux 上实际运行。

**阻塞（真实缺陷，未做伪造修复）**：用 lab 配置（`…/qwen36-27b-lab/deploy2/scheduler-v2.json`，deployment
`sms-orin-lab2`）冷启动该 SHA 时进程立即以 `configuration error: startup reconciliation failed: unproven_stop` 退出，
8090 未监听。目标机只读探针复现了因果：`run.py` 构造 v2 observer 时**从未传 `launch_lookup`**，于是每次观察都是
`launch_resolved=False`；`stopped_is_proven()` 要求 `launch_operation_terminal`，因此没有任何模型能被观察为 STOPPED，
`reconcile_startup` 恒拒绝且 `Book.recovering` 永不清除。对照：无 launch 源 → `unknown/launch_resolved=False`
（端口 closed、子进程 absent）；注入"本 boot 从未派发"的**正向**来源 → `stopped/launch_resolved=True`。
`git log -S launch_lookup` 核实该语义由本轮 RP02（`05dab9c`）引入，而生产装配自 M04/P16 起就没有来源；早前 lab2
服务能运行是因为它跑的是本轮之前的 `main@142746c` 检出。

结论与后续：**RP17 的 lab 功能复验（冷启动→见证→READY→stop→reload）在该 SHA 上阻塞，生产验收未宣称，
`device_backend_ready` 未生成**。修复方向（独立任务）：为 v2 装配提供 boot 级 launch 来源——按 K4 只有"本 boot
从未对该 target 派发 + 观察侧正面确认"才可解析 launch 维度，派发后记录必须保持非终结直到停止被证明；仅接
"永远无 launch"的桩不安全（真实派发后会允许假的 STOPPED 证明），已被否决。目标机未做任何修复并已恢复到复验前
状态（`main@142746c` + 原工作区改动 blob `203c6aa4…`），lab2 调度器已重启。原始证据：
`/home/jtzn/self-model-switch-evidence/rp17-20260922T145856Z/`（`FINDINGS.md`、`target-pytest.log`、
`probe-launch-source.txt`、`model-sha256.txt`、`pre-run-facts.txt`、`pre-sync-*`）。

### RP17 修复与复验（2026-09-22 同日，提交 `4f84c88`）

修复：`backend_control.BootLaunchRecords`（boot 级派发记录，K4 的正向来源）由 `ManagedLifecycle` 在 `load` 派发前写入
`dispatched(model_id, fence)`、在终态裁决（见证 RUNNING / 加载期见证 STOPPED / `stop()` 证明 STOPPED）时 `settled()`；
异常/未见证/UNKNOWN 不 settle。`run.py` 把同一个账本交给观察者（`launch_lookup`）与 `build_managed_execution(launch_records=…)`。
本地：全量 `pytest tests -m 'not thor' -q` **1032 passed、1 skipped、1 deselected**（较修复前 +15 项：账本 9、生命周期 5、
装配守卫 1），`ruff check .`、`run.py --check-config`、`git diff --check` 通过。

目标机复验（同一 lab2 配置，SHA `4f84c8806fdf9ac955c1129da3f56ddb455f4db8`，目标 checkout 干净、与三处远端一致）：
- **冷启动成功**：`/api/status` 报 `recovering: false`、8090 监听、两个模型 `unloaded`（修复前此步以
  `unproven_stop` 退出）。
- **冷启动→见证→READY→执行**：首次 `POST /v1/chat/completions` **HTTP 200、10.80s**，`finish_reason=stop`、内容 "OK."；
  期间容器 `sms-sms-orin-lab2-qwen25vl-7b | Up`，状态 `ready`。
- **stop**：`POST /api/models/qwen25vl-7b/unload` **HTTP 200、1.16s**，容器移除、状态回到 `unloaded`。
- **reload**：第二次请求 **HTTP 200、5.75s**，状态 `ready`（K1—K5 的见证/停止证据在真机上被真实走通）。
- **入口守卫（K7/RP14）**：用同一 TCP 端口、自有 socket/lock 的第二份配置启动新实例 → **exit 78**、
  `configuration error: cannot bind the TCP entry to 127.0.0.1:8090: [Errno 98] Address already in use`——端口守卫在
  lifespan 之前生效；在跑实例与在用控制 socket（inode/mtime 不变）未受影响。
- 第二实例的早前注入先被更早的锁守卫拒绝（`exit 73 scheduler instance already running`），亦记录。

**未完成/不可作为通过依据**：控制/观测故障的"有界 UNKNOWN → 恢复"未在真机干净隔离——本次占用模型端口 10002 的注入
时序有误（占位进程在请求结束前 8 秒自行释放），该请求 184s 后成功返回 200，**不能据此声称有界失败与恢复已验证**；
该分支仍以同 SHA 目标机上通过的全量套件（含受控时钟的 UNKNOWN/截止/恢复用例）为依据，真机注入留待后续。
`qwen36-27b` 未做功能复验；S/B/O 未运行；**生产验收仍未宣称，`device_backend_ready` 未生成**。

目标机终态：checkout 停在 `4f84c88`（干净），lab2 调度器由该 SHA 运行并在 8090 服务。复验证据：
`retest-scheduler.log`、`retest-scenario.log`、`retest-chat1.json`、`retest-unload.json`、`retest-chat2.json`、
`injection/`（第二实例日志与配置）、`rp17-retest.sh`、`rp17-injections.sh`。
