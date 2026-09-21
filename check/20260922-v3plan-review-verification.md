# v3 审查报告事实复核

日期：2026-09-22。对象：[原最终草案](../tasks/team-review/20260921-v3plan-mixed-a6497fd/reviews/final-review.draft.md)及同目录 `findings.json` 的全部 23 个 ID。

结论：**不能原样采纳“全部确认”。7 项成立、12 项部分成立、4 项不成立。**
确有加载验证、deadline 和 STOPPED 处理缺陷，但原报告混淆了实验配置与生产门禁、缺少验收与设计矛盾，并把若干已明确的契约重复报为缺口。
本次只提供复核与[修复方案](20260922-v3plan-fix-plan.md)，没有实施业务修复，也没有签发或改写原团队审查。

## 1. 基线与证据边界

- 源码基线：`a6497fdf57f338a26ef0abfaf930ea278872b091`，加现有未提交的 `backend_control.py` / `test_backend_control.py` 补丁。原审查的 14 个 snapshot 文件与当前工作区逐字节一致。因此以下差异来自补查证据和纠正推理，不是快照过期。
- 原报告代码范围仅为这两个文件；本次补查调度器、账本、真实 observer、生产 renderer、候选构建、能力解析、Blob 持久化和 listener 装配。
- 知识库使用：直接解析 `graphify-out/graph.json`，查询 `ManagedLifecycle`、`physical_admissible`、`require_production_openable`、`required_case_ids`、`BlobStore` 及相邻调用边。图有 5,799 个节点，同时包含原审查文字和 snapshot；引用时区分当前源码节点与审查观点节点，未把后者当独立佐证。
- 抽查上述两个补丁文件、C03/06 计划、`contracts_v2.py`、`acceptance/candidate.py` 的当前 MD5 与 manifest 的 AST/semantic hash 一致。知识库可查询，这六个文件内容匹配；不据此声称所有图关系都正确或完整。所有关键结论再读源码核实，未重建知识库。
- 本地验证使用 Python 3.12.11；未 SSH、未加载模型、未重跑 Orin 实测。35B 数字是对历史文档输入的算术复算，不是新的硬件测量或性能结论。
- Git：HTTPS fetch 连接超时，随后通过 origin 已配置 push URL 对应的同一 GitHub 仓库，以单次 URL 覆盖成功 fetch；未修改 remote 配置。基线比 `origin/main` 多一个既有 revert 提交，本次不发布它。

判定含义：**成立**为核心事实有证据；**部分成立**为真实问题夹杂过度归因、错误边界或重复项；**不成立**为原断言被反证，或把待验证事项误当已证明缺陷。成立不等于认可原严重度或原修法。

## 2. 逐项判定

| 原 ID | 判定 | 核实结论 / 修复方案编号 |
|---|---|---|
| design-F01 | 部分成立 | 按记录输入，35B 的物理预留确实超过记录中的 MemTotal，产品决策未闭合。但这证明该测量/设备/方法组合不能准入，不证明模型永久不能用，也不是内存安全门禁失效；27B lab 配置不能作为生产绕过证据。R1。 |
| design-F02 | 部分成立 | C03 没有明确独立验证时间窗、轮询节奏和终态处理，值得补齐。但“一次采样”并非已有硬规定，补丁也只在 adapter 返回 RUNNING 后轮询；adapter 返回 UNKNOWN 不进入轮询。R2。 |
| design-F03 | 不成立 | 原推导把“至少1800秒”当成上限，把“间隙≤15秒”当成下限，并把 arrival 与串行切换耗时相加。已有 arrival builder 和测试。真实设备达标仍待验收，不能推导门槛矛盾；不据此降低门槛。见 §3.3。 |
| design-F04 | 不成立 | C01 明定四能力闭集，解析器拒绝 audio，case 集合按每模型实际声明能力派生。06 已要求真实输出，明确结构合法不等于通过。P29 fixture 失败应修 fixture，不能证明 cap 语法有裂缝。见 §3.4。 |
| design-F05 | 成立 | `plan/README.md` 未索引 08，仍称新增功能均待实现；05 与 08 开头也保留旧基线。导航和当前状态应明确分开，不能简单将真源改指同样过时的 08 §1.1。R5。 |
| design-F06 | 部分成立 | “measured:false 可绕过生产门禁”没有成立：生产启动渲染逐模型拒绝未测登记。另有真实缺口：candidate builder 允许一个已测模型混入未测模型，并仅支持一份已测模型材料，未满足 C02 的完整生产候选要求。R4。 |
| design-F07 | 部分成立 | C08 已明确同 UID 同 owner、不提供同 UID 内隔离；不能称“未声明”。文件系统权限与 UID allowlist 的关系可补清，代码已先取内核 UID 并拒绝非白名单。原方案要求非白名单必返403与实际连接层关闭行为不符。R5。 |
| design-F08 | 不成立 | C07 已写明活跃租约优先、旧实例未停继续保护、tombstone 保留24h；SQLite 持久保存 state/expires_at/leases，已有过期和重启保护测试。可增加组合测试/状态表，但没有证据支持现有语义缺失或文件泄漏结论。见 §3.5。 |
| design-F09 | 部分成立 | boot_id 初始化与共享不是未知，TCP 退出也会 finally 停 control；但 control 先开放、TCP 后启动，尚无共同就绪屏障。原“顺序启动+失败回滚”不能保证另一入口从未接收请求。R6。 |
| code-F01 | 成立 | 验证继承 load 的完整 deadline；UNKNOWN 可耗尽它，随后 `_start_recovery(deadline)` 复用到期时间。实际客户端先返回 `control_recovery_timeout`，不一定调用到 helper 的 `recovery_timeout`。10s 仅是修复候选值。R2。 |
| code-F02 | 成立 | `_verify_running` 的 STOPPED 被 `load()` 改写为 UNKNOWN。且 scheduler 对全部非 RUNNING 都 `book.failed`，因此只改返回状态仍不会释放预算。R3。 |
| code-F03 | 成立 | 未裁剪 sleep，且采样前没有截止检查，能够越界再采样；不是每次成功路径都必然越界。观测耗时不受该循环约束，所以超时不止一个 poll 周期。R2。 |
| code-F04 | 部分成立 | 默认0.05s、生产装配无配置位确实存在；但不是固定20Hz或每次必定两个 Docker 子进程：周期包含观测耗时，空列表或 ps 失败不会 inspect。缺少退避本身不等于性能故障。R2。 |
| code-F05 | 部分成立 | `>=2` 断言过弱，缺 STOPPED-during-load/身份/截止边界精确断言成立；“超时与异常均无覆盖”不实，已有全 UNKNOWN 与 BrokenObserver 用例。降低 poll 间隔不会缩短等待完整1s deadline的负例。R2/R3。 |
| code-F06 | 成立（建议） | 两个循环重复 deadline/sleep 逻辑。可同步修复，也可在保持终态差异清楚的前提下抽取很小的内部 helper；不必为两个循环建立通用框架。R2。 |
| code-F07 | 部分成立 | 可空 identity、缺返回标注和同文件两种loop API混用属实；冷启动确实可能为 None。但这是类型/一致性改进，异步运行上下文未证明会触发弃用故障，不应误写为已有运行时失败。R2。 |
| code-F08 | 成立（建议） | 验证失败统一 `instance_unverified`，无法区分缺 observer、异常、持续未知、STOPPED 或截止。应保留终态和有限的原因码，兼容上层映射。R2/R3。 |
| code-F09 | 部分成立 | 占用和采样放大与 F01/F04 重复；身份直接写回确有防御缺口，但补丁前就这样写回，不是本补丁新引入的外部攻击面。ObserverPort 是进程内可信依赖。R2。 |
| code-F10 | 部分成立 | 桥接层不校验时间戳，陈旧 RUNNING 可被接受；不过 `SAMPLE_MAX_AGE_SECONDS` 明标 C02 内存样本，不能直接说 C03 已强制2s。C03 应先规定自己的新鲜度与截止语义。R2。 |
| security-N01 | 部分成立 | 新鲜度/身份缺口可复现，与 F09/F10 合并；“超时 return last 泄漏 RUNNING”在当前提前返回结构中不成立，不能把未来可能修改后的分支当现有漏洞。R2。 |
| test-N01 | 不成立 | 单项 UNKNOWN 脚本会重复 UNKNOWN，最终按 deadline 退出；现有负例已这样做。缺精确时钟断言不等于超时不可构造。见 §3.6。 |
| test-N02 | 部分成立 | ManagedLifecycle 的 load 负例确实没断言原因码；文件前部 legacy load 测试已断言 `control_load_failed`，不应概括全部 load 测试。并入 F08 修复。R2/R3。 |
| backend-N01 | 成立（重复） | 就是 F02 的同一控制流事实，不另立独立缺陷/提交。R3。 |

## 3. 关键证据与反证

### 3.1 C02：数值成立，定级和“永久”需收窄

[contracts_v2.py](../model_scheduler/contracts_v2.py) L301–304 使用整数公式：

```text
(65,243,426,816 × 115 + 99) // 100 = 75,029,940,839 B
记录中的 MemTotal = 65,893,224,448 B
```

原摘要写成 `75,029,940,838`，少1 byte；`findings.json` 中 test 专家其实已纠正，最终草案未同步。
[C02](../plan/08-execution-plan.md) L98–110 已明确首版包含 OS/page cache、不减基线，无法证明或超预算不生产开放。
[Book.physical_admissible](../model_scheduler/model_registry.py) L146–158 按预留总量拒绝，规则本身并不悬空。
[validation](../plan/validation.md) L1743–1746 确有待决产品选择，L1752–1756 明确27B是 lab。

合理结论是“现有35B材料不能用于这台记录设备的当前规则准入，生产范围/新方法决策待闭合”。没有“35B必须生产”的已确认硬需求时，不据此宣称整个设计 Critical 或所有模型均不安全。复核不代用户接受放弃35B，也不改内存口径。

### 3.2 生产拒绝已存在，候选完整性仍有真实缺口

直接证据链：

1. [candidate.py](../model_scheduler/acceptance/candidate.py) L223–242 仅要求至少一个、且恰好一个 measured 模型；随后将配置模型集合纳入 candidate。用其现有 `_site` 测试夹具构建，得到 `embedding=false, qwen-small=true`，构建成功。
2. [deploy.render_v3](../model_scheduler/deploy.py) L430–452 先 verify，再对**每个模型**调用 `render_container_launch(..., mode="production")`。
3. [runtime_profiles.py](../model_scheduler/runtime_profiles.py) L175–180 调用 [require_production_openable](../model_scheduler/contracts_v2.py) L568–580，拒绝未测/缺材料/缺物理峰值的模型。本次对上述 embedding 的直接调用确实抛出生产拒绝错误。
4. [preflight_v3.py](../model_scheduler/preflight_v3.py) L117–118 明确拒绝 lab manifest 作为生产部署；[生产渲染测试夹具](../tests/test_deploy_render.py) L415–418 也明确删除未测模型后才构造可生产集合。

因此成立的是“生产候选生成太宽、支持多模型材料不足”，不是已证明的“未测模型能生产发布”。是否直接手工运行 v2 配置是另一入口治理问题，原报告没有提供绕过受支持发布路径的证据。

### 3.3 O01：已有可执行计划，未证明性能通过

[workload.py](../model_scheduler/acceptance/workload.py) L70–108 已提供 builder/validator；[test_workload.py](../tests/test_workload.py) 使用120项、1800秒策略。
本次构造两个模型、120项、1800秒，最大间隙15秒，每模型60项，validator 返回空问题列表。
这仅反证“没有可执行 arrival plan”和简单算术不可能性，**不证明 p95/p99、429、504 或真实切换达标**。
到达可排队，不要求每个 arrival 都独立冷启动；O01 是至少1800秒，429与504是各自≤0.1，原草案 TOP 将其写成合计也不准确。
继续用正式候选与实际 fixture 做 P30；若失败，先诊断真实排队/加载/执行数据，不从旧切换时延直接调整门槛。

### 3.4 能力闭集已有明确绑定

[C01](../plan/08-execution-plan.md) L65、[contracts_v2.py](../model_scheduler/contracts_v2.py) L97及L501–513、
[evidence_contracts.required_case_ids](../model_scheduler/evidence_contracts.py) L649–657 分别定义闭集、拒绝未知能力、逐模型逐能力派生。
本次传入 `audio`，得到 `unknown capability 'audio'`。已有 `test_unknown_capability_is_rejected` 与 `test_required_case_ids_follow_the_candidate`。
[06](../plan/06-acceptance.md) L35、L45、L55 已要求实际消费输入、真实输出及联合边界；修复失败 fixture 是既定验收工作，不需要为 audio 扩展 case 语法，也不应自动缩减产品范围。

### 3.5 身份、Blob 与双入口需区分已实现部分

- [C08](../plan/08-execution-plan.md) L264–269 已声明同 UID 边界。[control_server.py](../model_scheduler/control_server.py) L190–198 设置文件权限/组，L241–243 检查内核 UID；非白名单在 HTTP 解析前关闭。[Linux双UID测试](../tests/integration/test_control_socket.py) L379–414 已覆盖“可连接但UID被拒”。本次未在 Linux 重跑。
- [C07](../plan/08-execution-plan.md) L252–260 已写活跃租约、tombstone、重启保护；[blob_metadata.py](../model_scheduler/blob_metadata.py) L31–48、L272–303 持久保存状态并跳过有租约文件。[test_blob_recovery.py](../tests/test_blob_recovery.py) L96–108 和 [test_blobs.py](../tests/test_blobs.py) L134–158 覆盖核心保护行为，本次均通过。tombstone 跨关闭数据库重开的组合测试仍可补强，但不证明当前语义缺失。
- [run.serve_v2](../run.py) L279–301 先 reconcile/Blob recover，再构造共享对象；`await control.start()` 后才 `await server.serve()`，finally 停 control。真实缺口是两个入口缺共同就绪屏障和失败注入测试，boot_id 共享已明确。

### 3.6 生命周期与测试的最小复现

以现有 `tests/test_backend_control.py` 的 `ManagedFakeAdapter`、`ScriptedObserver`、`v3_observation` 和 `lifecycle` 为夹具；不启动 Docker。
deadline 边界实验替换轮询时钟与 sleep 为可控时钟：起点0，deadline=0.02，poll=0.05，sleep只推进虚拟时间。

| 输入 | 实际输出 | 说明 |
|---|---|---|
| observer 单项 STOPPED | UNKNOWN、`instance_unverified`、调用1次 | F02/N01 成立。 |
| RUNNING、时间戳0 | RUNNING，写回 chat 实例 | 桥接层缺新鲜度检查。 |
| RUNNING、实例 model_id/container_id 改为 other | RUNNING，chat 键下记录 other | 桥接层缺身份一致性验证；这是可信依赖的防御性测试，不是远程攻击复现。 |
| 单项 UNKNOWN，重复末项 | 调用时间 `[0.0, 0.05]`，0.05返回 UNKNOWN | F03 成立，同时直接反证 test-N01“超时不可构造”。 |
| UNKNOWN→RUNNING，不遵守 deadline 的 fake observer | 同样在0.05接受 RUNNING | 循环没有迟到结果屏障；真实 observer 对开始时已到期的调用会先返回 UNKNOWN，不能将此 fake 结果冒充真实 Docker复现。 |
| `ControlRecoveryClient.recover` 传入过去时间 | `control_recovery_timeout` | 过期恢复预算在客户端就被拒绝。 |

[backend_control.py](../model_scheduler/backend_control.py) L125–140、L170–214 与以上结果一致；
[scheduler.py](../model_scheduler/scheduler.py) L247–280 将非健康 RUNNING 全部转为 `book.failed` 并复用 deadline；
[model_registry.py](../model_scheduler/model_registry.py) L348–356 的 `stopped()` **仅接受 EVICTING 且无租约**，不能直接用于 LOADING。

[process_observer.py](../model_scheduler/process_observer.py) L326–360 只在采样开始检查 deadline，然后执行线程中的端口/Docker操作；L282–284 在无容器时跳过 inspect。
因此原修复“一行裁剪sleep即消除硬违约”不充分，观测调用耗时和返回后的截止检查也要处理。

## 4. 本次实际验证

```bash
uv run --no-project --offline --python 3.12 \
  --with pytest --with pytest-asyncio --with-requirements requirements.txt \
  python -m pytest tests/test_backend_control.py tests/test_candidate.py \
  tests/test_deploy_render.py tests/test_contracts_v2.py \
  tests/test_evidence_contracts.py tests/test_workload.py \
  tests/test_blob_recovery.py tests/test_blobs.py -q -p no:cacheprovider
```

结果：**130 passed in 11.66s，exit 0，Python 3.12.11 / pytest 9.1.1**。通过现有测试并不消除上文额外实验揭示的缺口。
额外实验为临时脚本，使用上述夹具、`dataclasses.replace` 构造错配身份、可控时钟与 candidate `_site/_build`；输入和输出见 §3，可据此复现。没有将实验脚本加入业务代码。

环境副作用：首次未带 `--no-project` 的uv探测自动重建了本地 `.venv`。随后已恢复原Python 3.13.5解释器并按 `requirements-dev.lock` 重装开发依赖，移除本次生成的临时 `uv.lock`；原环境可能另装的额外包未留清单，未确认全部恢复。正式复核命令改用上述独立Python 3.12环境。

本次交付为两份 Markdown，完成链接、23个ID覆盖、敏感信息与 `git diff --check` 检查；未运行完整运行时套件、未执行硬件验收。
原草案的隔离/签发流程只约束该次团队审查，不是本次查证成立问题或给出方案的前置条件；保留原产物不变。
