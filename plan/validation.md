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
| target_commit | `63f7d8e13319d57e946e46d5766703e55d75c9a3`（`/home/jtzn/SelfModelSwitch`，`git status --short` 为空） |
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

按 [AGENTS.md](../AGENTS.md) 使用 `-i ~/.ssh/selfmodelswitch-target-agent -o IdentitiesOnly=yes` 只读 SSH；
本轮未在目标机执行任何写操作，未加载模型，未重启服务。

| 项 | 实测 |
|---|---|
| 设备 | `jtzn-desktop`；`Linux 5.15.148-tegra` aarch64；L4T `R36.4.7`（与 m00-envelope §2 一致，未换机） |
| 目标解释器 | `python3` 3.10.12（probe harness 口径；本任务不在目标机运行发布解释器） |
| 仓库 | `/home/jtzn/SelfModelSwitch`，branch `main`，`origin` = `/home/jtzn/git/SelfModelSwitch.git`，HEAD `63f7d8e…`，工作区干净 |
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
5. 本地 GitHub `origin` 落后 3 个提交；本地到目标机当前无 GitHub 推送通路。目标 checkout 已精确在同一 SHA，
   按 08-execution-plan §7 记“待同步”，未 reset、未复制修改目标 tracked 文件。
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
  "evidence_directory": "/home/jtzn/self-model-switch-evidence/m00-qwen25vl-envelope-20260917T055502Z",
  "unresolved": [
    "m00-envelope.md 第5节的 stop.json/顶层 run.json 命名与目标材料不符",
    "证据根有3个未登记目录与4个 console log",
    "plan/README.md 索引缺 08-execution-plan.md 且源码基线过旧",
    "本地 .venv 为 3.13.5，不满足 requires-python；发布锁为 Linux ARM64，macOS 无法复现",
    "本地 GitHub origin 落后3个提交，本地到目标无推送通路（待同步）",
    "tests/test_control_protocol_v1.py 草稿收集失败，由 P02 接续"
  ]
}
```

本记录的命令 exit code 2 是 08-execution-plan §1.2 已预期的草稿收集失败，不是 P00 未通过；
P01 的起点为同一 `source_commit`，本任务结束不改变任何 tracked 生产文件。
