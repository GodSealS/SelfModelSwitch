# M00 首个候选最大输入与并发规格 v0.1

状态：3 轮真冷启动探测通过（第 9 节）；runtime 记录、冷启动与内存口径均已裁定。
日期：2026-09-17。配套工具：`scripts/m00_envelope_probe.py`（见第 6 节）。

## 1. 范围

本文件为 M00 的"最大 envelope 输入"探测固定首个候选的配置值，供在目标设备上做
最大输入、三次冷启动与峰值测量；测量通过后这些数字才可进入 M01/M02 的模型登记 envelope。
最小文本推理（`probe-ok`，exit 0，compute_quiescent=true）已完成，但最小推理不构成 envelope 证据。

## 2. 现场事实（2026-09-17 核实）

| 项目 | 值 |
|---|---|
| 设备 | Jetson AGX Orin 64GB，hostname `jtzn-desktop`，L4T R36.4.7，kernel 5.15.148-tegra |
| GPU | CUDA0 Orin（62840 MiB 可用，UUID e6c84ee6-…），统一内存，SM 8.7 |
| runtime | llama.cpp `4bc272fd729bd094c0422e4b8353da8d2fec91f8`，CUDA 12.6，Release，位于 `/opt/self-model-switch/probes/llama.cpp-4bc272…/` |
| 主模型 | `Qwen_Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf`，4683072320 B，SHA-256 `3f4513330aa7f109922bd701d773575484ae2b4a4090d6511260a2a4f8e3d069` |
| projector | `mmproj-Qwen_Qwen2.5-VL-7B-Instruct-bf16.gguf`，1354162912 B，SHA-256 `d1c7588c0bdf6e7889737c01cfd54309240d54042e102275880a514ae979aea3` |
| 资产位置 | `/media/jtzn/sandisk-ext4/models/qwen25vl-7b-q4/`（ext4，只读使用） |

projector 资产此前缺失的说法不成立：文件已在设备上且与本地 `models/` 副本 hash 一致，
文本-only 只是上次探测命令未传 `--mmproj` 的结果。

注意：runtime 目录的 `RUNTIME-SHA256`、`BUILD-METADATA` 与现场二进制 hash 存在不一致
（llama-server 现场 `65a23c6e…`，BUILD-METADATA 记 `2a7131bb…`）。探测 harness 会重新计算并记录
实际 hash，把该不一致作为 anomaly 保留，需在 M00 结论前裁定哪份运行时生效。

## 3. 首轮候选 envelope

单位：token 指模型 token；GiB=2^30。

| 维度 | 值 | 说明 |
|---|---|---|
| 单槽上下文 | 32768 | Qwen2.5-VL-7B 原生训练上下文 |
| 最大输入 | 28672 | 预留 4096 输出 |
| 最大输出 | 4096 | 生成上限 |
| 图像规格 | 1 图/请求，视觉 token ≤1280，输入边长 ≤1024 px | `--image-max-tokens 1280` 显式设置 |
| 并发 | 2 slots × 每槽 32768 | `--parallel 2 --kv-unified-per-slot 32768`（共享池 65536） |
| 组合边界 | 2 并发 ×（1280 图像 token + 27392 文本 token）+ 4096 输出 | 单轮最坏组合，不以拆开小请求代替 |

文本最大输入与并发组合同时适用；任何一项超限的服务请求都应在 M01 之后被 422 拒绝。

### 内存预期（非验收值，仅供异常判断）

权重 4.36 GiB + projector 1.26 GiB + KV 2×1.75 GiB + 视觉/批处理缓冲 ≈ 12–14 GiB 增量。
64GB 设备预期无 swap、无 OOM。实测峰值以 harness 采样为准，R=ceil(measured_peak×1.15) 按 02-scheduler §5 计算。

### 依据

- 上下文与视觉上限取自模型原生规格（32K、动态分辨率 28×28 patch）；不对 128K 扩展做承诺。
- 输出 4096 是当前交互上限的保守预留，输入 28672 正好为 context−输出。
- 图像 1280 token/图对齐常见帧预算，避免单图占用近半上下文（默认 16384 上限过大）。
- 并发 2 在 64GB 统一内存与 Orin 带宽下可控；Orin 单序列生成约 19 t/s，2 并发为吞吐/时延平衡点。

## 4. 探测映射（E0–E3）

| Case | 内容 | 通过判据 |
|---|---|---|
| E0 校准 | 1 图 + 100 token 文本，输出 1 token | 实测 prompt 组成，用于 E3 文本填充精确到 ±32 token |
| E1 文本最大 | `/completion`，prompt=28672 token（token 数组），输出 4096 | prompt_tokens=28672（精确）；输出 4096 完成；记录 t/s |
| E2 图像最大 | 1 图 1024×1024 + 文本，总 prompt 至 28672−1280 附近，输出 4096 | 图像 token ≤1280 且被实际消费；执行设备为 CUDA0 |
| E3 并发组合 | 2 个并发请求，每个 = 1 图 + 填充文本（28672−overhead）+ 4096 输出 | 两请求发送偏差 ≤1000 ms；各自 prompt_tokens≥28640；输出 4096 完成 |

每轮冷启动（容器/进程从未运行状态开始）执行 E0→E1→E2→E3，随后停止并确认静止；
共 3 轮独立冷启动。E1 用 `cache_prompt=false` 保证全量 prompt 计算。
加 `--cold-cache` 时每轮丢弃模型与 mmproj 的页缓存，得到真冷介质加载基线（见 9）。

## 5. 测量、证据与判定

- 采样：`/proc/meminfo` MemAvailable 每 ≤100 ms（含 launch 前 10 s 与 STOPPED 后 10 s）；`tegrastats --interval 100` 同步采集 GPU 活动。
- baseline=launch 前窗口 MemAvailable 中位数；delta=max(0,baseline−运行窗口最小值)；measured_peak=max(delta)。
- 测量有效性：采样缺口 ≤500 ms；delta>0；前后基线差 ≤256 MiB；出现 swap 记为 anomaly。
- 停止证据：SIGTERM 后进程退出（否则记录 SIGKILL）、固定端口无监听、内存回落、无 llama-server 残留、GPU 活动回落。
- 证据目录：`/home/jtzn/self-model-switch-evidence/m00-qwen25vl-envelope-<UTC>/`，逐轮保留 `env.json`、`server.log`、`sampling/`、`cases/*.json`、`run.json`、`stop.json`，失败轮不删除。
- 判定：E1=E2=E3 全部到达规格且 3 轮全部静止通过 → 首轮候选成立；任一轮 OOM、swap、超时或未静止 → 不通过并触发第 7 节降级。

## 6. 工具与命令

harness 为纯标准库 Python（目标机 Python 3.10），不启动生产服务、不写模型盘、不下载资产：

```bash
python3 scripts/m00_envelope_probe.py check                              # 资产 hash、设备、runtime、端口、计划
python3 scripts/m00_envelope_probe.py run --runs 3                       # 默认加载模式，3 轮
python3 scripts/m00_envelope_probe.py run --runs 3 --cold-cache           # 真冷启动（丢弃模型页缓存）
python3 scripts/m00_envelope_probe.py run --runs 1 --load-mode none       # 非 mmap 加载对比
```

`--cold-cache` 在每轮发射前对模型与 mmproj 执行 `POSIX_FADV_DONTNEED`（无需特权）；
`--load-mode` 把加载方式透传给 runtime（该运行时无 `--no-mmap`）。

按 AGENTS.md 流程：本地提交 → 目标机 `git pull --ff-only` 到同一 commit → 目标机运行 → 证据回传评审。

## 7. 降级与升格

降级顺序（任一不通过时按序重测）：并发 2→1 → 图像 1280→768 token → 输入 28672→16384。
升格评估（仅在第一轮全部通过后）：3 轮最小 MemAvailable ≥6 GiB 且无 swap、无异常时延抖动，
下一轮探测评估 parallel=4；context 上限保持 32768，不评估 128K。

## 8. 回填

探测通过后，本文件第 3 节数字加上实测 `measured_peak`、R、加载/推理耗时成为
M01/M02 模型登记 envelope 与 `reserved_bytes` 的输入；未通过前不得用于配置或验收声明。

## 9. 实测回填（2026-09-17）

三轮真冷启动（`--cold-cache`，每轮发射前丢弃模型页缓存）证据：
`/home/jtzn/self-model-switch-evidence/m00-qwen25vl-envelope-20260917T055502Z`
（commit `cbefb5b`，result=passed，3/3 quiescent）。

| 项目 | 实测 | 备注 |
|---|---|---|
| E1 文本最大 | prompt 28672 精确、输出 4096 满额（3/3） | prompt 约 821 t/s、生成约 20.7–20.8 t/s、单请求 232 s |
| E2 图像 | 视觉 1227 token（≤1280） | 1024×1024 输入，`--image-max-tokens 1280` 生效 |
| E3 并发组合 | 2×（28672 prompt + 4096 输出），发送偏差 ≤0.9 ms | prompt 657–715 t/s、生成 15.0–17.1 t/s |
| 内存（MemAvailable） | delta 4.87–4.94 GiB，gaps=0，前后基线差 ≤20 MB | measured_peak=5309693952 B；R=ceil(peak×1.15)=6106148045 B |
| 内存（tegrastats RAM） | 峰值−基线 4.83–4.87 GiB | 与 MemAvailable 口径交叉一致 |
| 加载 | 冷启动 18.07 s（3/3，±0.01 s）；页缓存热 5.02 s | 冷启动前 Cached 9.58→3.96 GiB |
| 停止 | 3/3 quiescent | exit 0、端口释放、内存回收、无残留 PID |
| 执行设备 | CUDA0 | CUDA 库映射 + GR3D 峰值 99% + `--n-gpu-layers 99` |

### 9.1 runtime 记录裁定（已完成）

- 现场二进制与 `RUNTIME-SHA256` 一致：llama-server `65a23c6e…`、llama-cli `ae6ab171…`；
  `BUILD-METADATA` 记 `2a7131bb…`/`4b9cefe0…`，其时间戳 09:13:04 早于二进制替换 09:14:50。
- `llama-server --version` 与 `llama-cli --version` 均为 `0.4.1-dev (build 1, commit 4bc272f)`；
  probe checkout HEAD 同为 `4bc272f…`；`sha256sum -c RUNTIME-SHA256` 11/11 通过。
- 结论：以现场 + `RUNTIME-SHA256` 为生效记录，`BUILD-METADATA` 为陈旧快照。
  harness 已自动分类：阻塞异常为空，陈旧记录进入 `notes`。

### 9.2 加载模式与内存账本

- 该运行时用 `--load-mode auto|none|mmap|mlock|mmap+mlock|dio` 控制加载，**不存在 `--no-mmap`**；
  M02 启动参数与 preflight 必须按此实现。
- 对比测量 `--load-mode none`（单轮，result=passed）：
  `/home/jtzn/self-model-switch-evidence/m00-qwen25vl-envelope-20260917T063212Z`，
  measured_peak=5228285952 B，与 mmap 模式差异 <1.5%。
- 账本分解：MemAvailable 增量 ≈ KV（2×32768 f16 ≈ 3.5 GiB）+ 计算/视觉缓冲 ≈ 1.4 GiB；
  权重 5.36 GiB 与 mmproj 1.26 GiB 无论 mmap 与否都不产生额外净下降（已在基线中或被算作可回收）。
- 给 M01/M02 的登记口径：`reserved_bytes` 按 R=6106148045 B（≈5.69 GiB）服务于 MemAvailable 准入；
  另需登记常驻总量（权重+mmproj+增量 ≈ 10.5 GiB）用于物理内存与多模型共存核算。
- 加载预算：真冷介质首次加载 18.1 s，页缓存热 5.0 s。`--load-mode mlock` 未测（memlock 上限 7.67 GiB，可行但未验证）。

### 9.3 保留的失败证据（不计入结论）

| 目录 | 原因 |
|---|---|
| `…-20260917T045919Z` | harness 缺陷：`/completion` 字段名、EOS 早停、停止窗口时序 |
| `…-20260917T062554Z` | 使用 `--no-mmap`：该运行时为无效参数；改 `--load-mode` 后通过 |

## 10. M00 验收对照

| 05-tasks M00 验收项 | 结果 | 证据 |
|---|---|---|
| 最小推理 | 通过 | `…-20260917T011746Z`（`probe-ok`、exit 0、compute_quiescent=true） |
| 最大 envelope 输入 | 通过 | 第 9 节，3 轮真冷启动 E1/E2/E3 全达标 |
| 峰值/耗时 | 通过 | delta 4.87–4.94 GiB、R=6106148045 B；prompt 821 t/s、生成 20.8 t/s、并发 15.0–17.1 t/s、冷加载 18.07 s |
| 执行设备 | 通过 | CUDA0（CUDA 库映射 + GR3D 峰值 99% + `--n-gpu-layers 99`） |
| 停止证据 | 通过 | 3/3 quiescent（exit 0、端口释放、内存回收、无残留 PID） |

M00 五项验收均有目标设备真实证据且 runtime 身份已裁定，可标记 M00 完成。
M01/M02 的登记输入见第 8 与 9.2 节；本轮不进入 M01 实施。
