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
| target_commit | 见 08 记录提交（推送后同步复验） |
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
| managed+lifecycle 文件重复 5 次 | 0 | 每轮 `15 passed`，无抖动 |
| `ruff check .` / `run.py --check-config` | 0 | 通过 / 仍为 v1 四 ID |

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
