# M00 首个候选最大输入与并发规格 v0.1

状态：规格已执行 3 轮探测并通过（见第 9 节实测回填）；M00 整体裁定见第 9 节遗留项。
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
python3 scripts/m00_envelope_probe.py check   # 校验资产 hash、设备身份、runtime、端口、打印计划
python3 scripts/m00_envelope_probe.py run --runs 3
```

按 AGENTS.md 流程：本地提交 → 目标机 `git pull --ff-only` 到同一 commit → 目标机运行 → 证据回传评审。

## 7. 降级与升格

降级顺序（任一不通过时按序重测）：并发 2→1 → 图像 1280→768 token → 输入 28672→16384。
升格评估（仅在第一轮全部通过后）：3 轮最小 MemAvailable ≥6 GiB 且无 swap、无异常时延抖动，
下一轮探测评估 parallel=4；context 上限保持 32768，不评估 128K。

## 8. 回填

探测通过后，本文件第 3 节数字加上实测 `measured_peak`、R、加载/推理耗时成为
M01/M02 模型登记 envelope 与 `reserved_bytes` 的输入；未通过前不得用于配置或验收声明。

## 9. 实测回填（2026-09-17，探测 2）

证据：`/home/jtzn/self-model-switch-evidence/m00-qwen25vl-envelope-20260917T052121Z`
（commit `1b87d35`，3 轮独立冷启动，result=passed，3/3 quiescent）。

| 项目 | 实测 | 备注 |
|---|---|---|
| E1 文本最大 | prompt 28672 精确、输出 4096 满额 | prompt 约 821 t/s、生成约 20.8 t/s、单请求 232 s |
| E2 图像 | 视觉 1227 token（≤1280） | 1024×1024 输入，`--image-max-tokens 1280` 生效 |
| E3 并发组合 | 2×（28672 prompt + 4096 输出），发送偏差 ≤0.9 ms | prompt 657–715 t/s、生成 15.0–17.1 t/s |
| 内存 | delta 4.87–4.90 GiB（gaps=0，前后基线差 ≤18 MB） | measured_peak=5263122432 B；R=ceil(peak×1.15)=6052590797 B |
| 停止 | 3/3 quiescent | exit 0、端口释放、内存回收、无残留 PID |
| 执行设备 | CUDA0 | CUDA 库映射 + GR3D 峰值 99% + `--n-gpu-layers 99` |
| 加载 | 5.02 s（页缓存热） | 见遗留项 |

遗留与裁定项（M00 完成前处理）：

- runtime 记录不一致：现场 llama-server 实际 hash 与 `RUNTIME-SHA256` 一致（`65a23c6e…`），
  `BUILD-METADATA` 记 `2a7131bb…`（llama-cli 同理）。建议以 `RUNTIME-SHA256` 与现场为准，
  M01 定契约前确认。
- 5.02 s 加载为页缓存热读数；真冷介质启动基线未测（需 `drop_caches` 或重启后复测），不阻塞 envelope 结论。
- 内存 delta 主要反映 2×32768 KV（f16）与计算缓冲；mmap 权重页进入可回收缓存、不计入 delta。
  M01/M02 登记 `reserved_bytes` 时按 R=5.64 GiB，或按非 mmap 模式复测后调整。
- 首轮 not_passed 证据（前次 harness 缺陷）保留于
  `/home/jtzn/self-model-switch-evidence/m00-qwen25vl-envelope-20260917T045919Z`，不计入结论。
