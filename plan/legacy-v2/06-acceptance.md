# 06 验收执行、原始证据与发布门禁

本文件规定待实施的验收机制。当前 `tests/thor/test_acceptance.py` 的报告格式检查不构成真实验收；
本次设计及 `plan/reference/` 自检不能生成设备通过结论。状态、内存和取消规则由 [02](02-scheduler.md) 唯一规定，
输入输出与质量指标由 [07](07-pipeline.md) 唯一规定；本文件规定如何证明这些规则成立。

## 1. scope、层级与必测集合

报告 `schema_version=3`。部署候选必须在执行前固定 scope，运行失败后不得自动降级 scope。
改变 scope 是新的候选，须重新计算必测集合；较小 scope 的通过不能替代原承诺。

| scope | 必测层 | 成功标签 | 可以宣称的能力 |
|---|---|---|---|
| scheduler-only | S、B、O | device_backend_ready | 候选声明的模型调度和推理能力 |
| local-pipeline | S、B、P、Q、O | local_pipeline_ready | 07 的本地产物；无外部关系报告 |
| full-pipeline | S、B、P、Q、O、E | full_pipeline_ready | 本地产物及有证据引用的外部关系报告 |

`software_verified` 仅要求 S 全部通过，允许没有目标设备，不能发布设备能力。
独立软件自检绑定源码/工具链摘要即可，不生成完整部署 v3 报告；正式验收中的 S 必须重新绑定最终 candidate。
`device_backend_ready` 要求 S/B/O 全部通过；pipeline scope 可以展示此中间标签，但发布仍须对应 scope 全集通过。
`local_pipeline_ready` 要求 S/B/P/Q/O 全部通过；`full_pipeline_ready` 还要求 E 全部通过。
不允许将参考代码单元测试输出转成 B/P/Q/O/E 通过记录。

必测集合由 `required_scenarios(candidate)` 纯函数推导，调用方不能传入自选的必测列表：

```text
所有 scope: S01..S08 + O01..O06
对 candidate.models 中每一个 model_id:
  B:<model_id>:load
  B:<model_id>:infer
  B:<model_id>:envelope
  B:<model_id>:cancel
  B:<model_id>:stop
  B:<model_id>:reload
  对其每个 declared capability: B:<model_id>:cap:<capability>
local-pipeline / full-pipeline: 再加 P01..P08 + Q01..Q06
full-pipeline: 再加 E01..E03
```

model ID 和 capability 来自通过严格校验的候选注册表。未映射 evaluator 的 capability 是配置错误，不能静默跳过。
每个必测 ID 必须恰有一个最终场景结论；一个场景内部允许多个规定子用例和重复测量。
历史失败、重试和中断仍保留在证据目录，最终场景明确引用实际使用的 attempt；不能覆盖失败文件伪造首次通过。
collector 发现了未知/多余的场景 ID 或重复最终结论时拒绝报告，避免拼写错误掩盖缺测。

## 2. 四个职责与真实执行流程

| 组件 | 职责 | 禁止行为 |
|---|---|---|
| executor | 通过正式 API/控制接口执行输入、断连、取消、故障和恢复动作 | 直接改 registry/SQLite 或替换模型输出来制造成功 |
| collector | 在目标环境采集实例、请求、资源、产物、环境与系统事件 | 将用户填写的 `gpu_verified=true` 当证据 |
| evaluator | 读取原始材料，按场景算法和固定 policy 算指标与结论 | 只读取 summary 中的 passed 或指标数字 |
| release gate | 验证身份、完整性、有效期、必测覆盖及 evaluator 结论 | 遇缺测/异常降级为通过 |

执行顺序固定：生成候选 → 严格校验 → 采集现场环境 → 比对候选 → 检查维护模式 → 执行必测集合 →
封存原始材料 → evaluator 重算 → 生成 v3 报告 → 独立 verify → production render → 现场 preflight。
候选没有实测预算时，先按 02 进入独占 calibration；测得预算后形成新的完整候选，再执行正式验收。
calibration 的结果仅用于定值，不能替代最终候选的 B/P/O 场景。

验收器是待实现的 `acceptance/` 包；建议 CLI 合约如下，具体参数类型纳入生产 CLI 测试：

```bash
python -m acceptance collect --output build/facts.json
python -m acceptance run --candidate build/candidate/candidate.json --scope full-pipeline --output build/evidence --maintenance
python -m acceptance verify --candidate build/candidate/candidate.json --evidence build/evidence
```

`collect` 只读，不启动模型，可选 `--candidate` 只增加现场比对。`run --scope` 必须等于 candidate.scope，并通过 scope 推导全集；开发时可选子集，但该输出标记 incomplete，不能用于发布。
`verify` 不启动模型、不执行故障、不访问外部 LLM；验证成功退出 0，缺材料/结构错误退出 2，指标失败退出 3。
场景失败也必须封存证据，不能仅以非零退出而丢失失败原因。

`plan/reference/acceptance.py` 只约束必测集合、报告封套、身份与文件 hash 等可离线校验规则。
它不实现真实 executor、GPU 检测、性能统计、质量算法或事件语义。
参考 `verify` 必须显式注入 trusted evaluator；缺少 evaluator 默认拒绝，恒真 evaluator 只可用于标为 synthetic 的测试。
生产 `acceptance/evaluators/` 必须真实实现下述每个场景的重算；参考门禁通过不能自动生成生产 release report。

## 3. 候选身份与证据失效

候选使用严格类型与确定性序列化计算 SHA256，算法以 `reference/acceptance.py:canonical_digest` 为准，候选字段以 `reference/contracts.py` 为准。
最终摘要至少覆盖下列语义字段，不能仅 hash 模型列表：

- 发布 scope、profile、所有 scheduler/runner/worker 配置、资源预算、队列/TTL/超时/输入 envelope、阶段 DAG、外部 provider 配置。
- scheduler、runner、adapter、collector、evaluator 的代码产物 digest；源码 commit 仅为辅助定位，不能替代部署文件 digest。
- 全部镜像 digest、llama-swap 二进制 hash、依赖锁和模型资产清单（每文件角色、路径、大小、SHA256）。
- 实际硬件型号、compatible、物理设备标识摘要、MemTotal、JetPack/L4T、内核、CUDA/驱动/容器运行时版本。
- 电源模式、时钟策略及 GPU/CPU 锁频设置；测试媒体和标注的 manifest hash。
- acceptance policy、性能 SLO、质量阈值、指标算法版本、外部 provider/model/prompt 版本。

机器身份按04固定为现场 `/etc/machine-id` 原始去尾换行字节的 SHA256，写入 `DeviceIdentity.machine_id_sha256`。
同型号第二块板必须具有不同 machine-id；克隆系统须先完成独立身份初始化，不能沿用另一机器的验收身份。
设备树型号不能充当唯一物理设备标识，machine-id 也不得每次测试临时重建。

不包含报告路径、证据目录绝对路径、报告摘要自身及报告生成时间，避免 candidate→report→candidate 循环。
这些字段不能影响运行；任何影响运行的字段不得借此排除。严格报告只包含 `AcceptanceReport` 定义的字段，
其中存 candidate digest、各 `EvidenceRef` 的文件 hash；证据 manifest digest 存独立 manifest/发布清单，不能加进严格报告。

采集开始、结束及 production preflight 均读取实际环境并核对；测试中发生栈/模式/配置变化，整次 run 无效。
任何上述语义字段变化均使旧报告不适用，包括提高内存预算、降低质量阈值、更换 mmproj、修改超时或同型号换机。
仅移动证据目录，在相对路径和文件内容一致时不要求重跑。

严格报告没有 `generated_at` 字段；`started_at` 必须等于本报告所有必测场景最早开始时间，
`finished_at` 必须等于最晚结束时间，生产 evaluator 从原始事件检查二者。两者均为 UTC 且 started_at<=finished_at。
验证时允许最多 5 分钟的未来时钟偏差；参考 verifier 检查 finished_at 不超过该上限，结合先后约束同时限制 started_at。
参考 verifier 按 started_at 计算 7 天有效期，超过即拒绝；不得重新包装旧证据改变最早开始时间刷新有效期。
ScenarioRecord.started_at/finished_at由evaluator与raw run事件核对；report开始=min(case开始)，结束=max(case结束)，
reference独立检查聚合等式。重包同时改case时间仍因raw不符失败。
性能耗时使用同一 boot 内 monotonic 差值；不能用墙钟差计算超时，跨 boot 记录分段并分别绑定 boot_id。

## 4. 证据包与判定

证据包包含 environment、candidate、原始场景事件、资源采样、请求响应/产物、系统日志片段、manifest、v3 report。
正式验收的所有场景材料至少关联 candidate digest、run ID、scenario ID、attempt、执行环境身份和采集器版本。
S 记录实际开发/测试环境；B/P/Q/O/E 必须同时关联候选绑定的物理目标设备。S 环境与目标设备不同不构成 GPU 证据。
进程事件还关联 boot_id、operation/permit/execution ID、generation、container ID/StartedAt；GPU 使用必须能够归属到目标实例。
事件按 source 的递增 sequence 排序，记录 monotonic/UTC；不同 boot 不能凭 monotonic 大小拼接顺序。

manifest 对每个相对路径记录 bytes、SHA256 和媒体类型。禁止绝对路径、`..`、符号链接、逃出证据根目录的路径。
报告枚举其所需全部材料；无引用的文件不能补足缺失证据。验证读取实际 bytes 重算 hash，不相信文件名或 sidecar hash。
原始敏感视频/音频可以存受控路径，但验证者必须能读取并重算引用材料；只有不可访问的 hash 不足以验证结果质量。
API token 不进入 candidate 明文、日志或 evidence；外部调用记录认证身份标识，不记录 secret。

严格 `ScenarioRecord.disposition` 枚举为 executed/failed/not_run/skipped/unknown，没有 passed。
executed 仅表示已执行；只有 executed 且可信 evaluator 从原始材料重算为 True 才能进入通过集合。
任何缺测、异常、采样缺口导致无法判定、哈希错误、设备不匹配、未知状态、子用例失败都不通过。
`passed`、指标摘要及 `reported_metrics` 若需展示，仅写独立 summary 文件，不能添加到 extra=forbid 的严格报告。
展示摘要必须与 evaluator 重算结果一致；禁止用 summary 或 disposition=executed 替代计算。

hash 提供材料完整性，不单独证明材料由真机产生。collector、现场执行权限与验收维护环境是受信边界；
正式 release bundle 必须记录这一信任来源。软件 fake、手工 JSON、mock GPU 指标不得标记 hardware evidence。

## 5. S：软件与协议，所有 scope 必测

S 可以在开发机通过可控 fake backend、时钟和资源采样执行；还须运行既有应用单元/集成回归。
每个 ID 的全部动作均须通过，不能以一个示例代表整个 ID。

| ID | 动作 | 必须满足 | 原始证据 |
|---|---|---|---|
| S01 | 加载动态模型、每类 runtime、合法/非法资产；迁移旧四模型配置；未知字段/能力/版本 | 严格拒绝歧义及非法值；迁移显式；无固定四 ID 假设 | 参数化输入、异常代码、迁移前后语义比对、测试结果 |
| S02 | READY/load失败/迟到回调/epoch变更/stop响应成功但进程活着 | 状态遵循02；陈旧操作不回写；UNKNOWN不释放预算 | 按序状态事件与fake observation |
| S03 | 测试 C+N=B、超过1byte、MemAvailable=N+F、少1byte、过时/未来样本 | 边界包含关系与02一致；不重复减reserve、不重复计算workspace | 逐步账本、样本、准入决定 |
| S04 | 交互lease、phase、preload、后台load争用；队列满、老化、drain超时 | 统一互斥；无饥饿规则绕过；drain不杀有效请求；队列和许可无泄漏 | 并发事件、授予/拒绝/完成记录 |
| S05 | heartbeat到期、重复close、HTTP断连、迟到成功、旧attempt/boot提交 | 拒绝旧token；HTTP不取消durable job；取消后不提交；停止确认前保留槽/预算 | permit/execution状态及提交拒绝记录 |
| S06 | 在临时写/rename/DB提交边界注入崩溃；修改已提交产物；resume | 仅复用已提交且hash一致产物；孤儿不当成功；损坏阻断或重跑规则符合07 | DB快照、文件树/hash、故障点、恢复事件 |
| S07 | API非法输入、幂等冲突、非法路径、任意URL、过大负载、旧接口回归 | 03的确定状态码和错误码；无路径逃逸/任意后端访问；兼容API通过 | 请求响应、文件访问/网络fake记录 |
| S08 | 缺ID/重复ID/skip/改证据/换设备/改candidate/过期/未来报告 | verifier fail closed；不能凭passed或布尔GPU声明通过 | 正反例包、重算输出、退出码 |

## 6. B：每个注册模型和 capability 的目标设备验收

B 必须在候选绑定的真实目标设备执行，使用发布镜像和实际模型资产；CPU fallback 不能替代声明 GPU 的能力。
若某 capability 在部署定义为 CPU，则明确按 CPU 验收，报告不能称该能力通过 GPU 验收。
每次测量前确认其它受管实例停止，记录基线；采样持续覆盖启动、推理、取消及停止，不只采稳定驻留值。
峰值按候选中的测量归属计算，采样间隔和容许最大间隙由 acceptance policy 固定。

测量算法：停止其它受管实例后采集至少10秒基线，baseline为窗口MemAvailable中位数（偶数取两中值算术平均）。
每轮从launch前覆盖到STOPPED后的10秒，每100ms或更密采样，最大缺口500ms；更稀疏数据不能冒充该policy。
`delta=max(0,baseline-min(运行窗口MemAvailable))`向上取整数；至少3轮，measured_peak_bytes不得小于最大delta。
delta为0、窗口前后基线差>256MiB或存在无关重负载，测量无效重测，不用max(1,...)制造正预算。
不清空page cache伪造冷启动；冷指无受管模型实例，记录缓存状态未知及首轮/后续耗时。
R_m覆盖模型容器及预处理/推理scratch；R_w单独测非模型媒体/runner额外峰值，不重复加模型容器RSS。
完整stage实际统一内存delta还须<=R_m+R_w；组件过关但组合超额仍失败，修预算后创建新candidate。
最大图像数/像素、context输入+输出、batch/concurrency按配置同时达到，不能以分开轻请求冒充组合边界。
request token总数必须<=context，违反时执行前拒绝。

| ID 后缀 | 动作 | 必须满足 | 原始证据 |
|---|---|---|---|
| load | 冷启动实际模型；至少3次独立冷启动，每次前确认停止 | 实例/镜像/资产/健康一致；冷启动耗时满足policy；实际执行provider匹配 | 启动argv、实例inspect、worker provider自检、加载日志、资源序列 |
| infer | 最小有效输入和常规输入真实推理 | 返回schema正确、结果有限、非空要求满足；执行设备可归因；SLO满足 | 输入hash、输出、execution事件、provider及实例关联证据 |
| envelope | 运行声明支持的最大音频时长/采样率、图片数量/尺寸、context、batch、并发等边界，另提交越界输入 | envelope内可完成且峰值在预留范围；越界在执行前拒绝；不得缩输入后冒充最大配置 | 完整边界矩阵、实际输入尺寸、资源序列、OOM/退出事件 |
| cancel | 计算正在进行时取消，模拟响应丢失并等待迟到结果 | 取消不立即释放预算；迟到结果不提交；总清理期限/未知状态按02；不得遗留实例 | 时间戳、cancel/stop请求、进程/端口观察、账本、产物检查 |
| stop | idle及完成推理后分别停止；观察控制返回与真实退出 | STOPPED所有条件成立后才清账；端口关闭和启动入口封闭 | 独立inspect、端口、控制进程/子进程证据、账本事件 |
| reload | load→infer→stop→reload→infer，至少3个完整循环 | generation/实例更新；旧execution不能影响新实例；无逐轮单调泄漏 | 各轮实例/结果/峰值/回收基线及token拒绝事件 |
| cap:<capability> | 对该能力执行专用fixture，包括有效输入和协议错误输入 | 对应03/07输入输出契约全部成立，不能用health或另一能力推理代替 | 能力专用输入输出、schema检查、执行事件 |

能力 evaluator 的最小内容：chat验证普通/流式及终结；embeddings验证数量、维度、有限值和encoding；
rerank验证索引唯一、范围、排序和top_n；vision验证实际图片输入确被消费并返回07结构；
transcribe验证音频内容对应的文本、段内说话人和时间范围；face detection/embedding、depth验证07规定的shape、坐标、dtype与范围。
准确率不在 B 中宣称，交由 Q。capability 的实际枚举以 contracts 为准；枚举映射至这些专用 evaluator 不允许空实现。

GPU证据至少包含目标模型采用的实际执行provider、关联到目标实例的设备活动及一次真实输出。
只有系统GPU利用率上升不足以排除其它进程，只有配置 `device=cuda` 不足以排除实际fallback。
S的模拟资源测试、第三方bench、模型文件大小与B证据不可互换。

## 7. P：完整流水线、恢复与产物

P 使用同一个固定输入 manifest；P01长片fixture为07规定的120min±5min、1080p视频，音轨及帧时间可解码。
不可用循环一段短视频代替长片fixture；fixture必须包含07验收需要的镜头、画外音、多人/无人、遮挡和跨块情况。
长片必须跑完全部本地阶段；完成后另执行 O01 的30分钟混合负载，两者不能相互折抵。

| ID | 动作 | 必须满足 | 原始证据 |
|---|---|---|---|
| P01 | 新建job并完成完整长片所有本地阶段 | 阶段顺序与07一致，只有一个movie及一个阶段模型；时长/磁盘/SLO满足policy | 全job/stage/unit事件、输入probe、资源序列、DB和产物manifest |
| P02 | 同一创建幂等键重复提交；创建HTTP断连后查询；不同输入复用key | 不重复创建；断连仍执行；冲突确定拒绝 | HTTP事件、job ID、DB唯一约束观测 |
| P03 | 在音频块/镜头中途结束runner并重启后resume | job PAUSED，旧permit清理；已验证unit不重复、未提交unit重跑 | 两个boot事件、attempt链、产物hash及调用计数 |
| P04 | 活跃stage时重启scheduler，再显式resume runner | 新boot拒绝旧token；停止旧实例后才能重启；无旧结果污染 | supervisor/实例事件、token拒绝、恢复调用链 |
| P05 | 在准备/计算/合成时分别取消job | 先CANCELLING后确实停止才CANCELLED；不能发布完整成功产物 | 状态序列、STOPPED证据、最终产物清单 |
| P06 | 检查所有片段偏移、镜头边界、人物/说话人关联及未知项 | 全片时间基准统一、无越界；段内speaker不直接跨段合并；关联可追溯 | 逐段时间映射、引用图、未知项、schema与规则重算 |
| P07 | 校验原分辨率打码视频、音视频同步、逐帧mask引用和深度产物 | 输出可完整解码；尺寸/时长/PTS/相对深度语义符合07；无缺失帧产物 | ffprobe、全量解码日志、frame/mask/depth manifest |
| P08 | 同一candidate/input从清洁状态重跑；保留中间结果后逐项检查 | 全部hash引用闭合、无悬空引用/跨job污染；结果schema与质量门槛成立 | 两次完整manifest、规范化结构比对、artifact验证报告 |

非确定性模型输出不要求逐字节相等；P08 要求处理输入、版本、结构、引用和质量可复核。
不得将随机差异当成失效缓存的理由；缓存键变化规则按07执行。

## 8. Q：结果质量

Q 的数据集清单、标注格式、算法、匹配策略、阈值与分母由07唯一规定。
以下场景不允许 `human reviewed=true` 代替数值计算。指标不可计算（例如分母为零）应判数据集无效，不能算满分。
所有片段指标包含总体值和07要求的分层值，不能以总体平均掩盖某个强制子集失败。

| ID | 动作/指标归属 | 必须满足 | 原始证据 |
|---|---|---|---|
| Q01 | 分镜边界匹配与过分割/漏分割 | 07的边界容差和指标门槛 | 标注边界、预测边界、匹配对和逐片段计数 |
| Q02 | ASR文本及分段说话人/跨段关联 | 07的文本误差、说话人指标及未知处理规则 | 文本标注、正规化结果、对齐、speaker映射与逐段错误 |
| Q03 | 人物检测、跟踪、聚类和出现区间 | 07身份与时间区间指标；不猜真名 | 逐帧标注/轨迹、预测、聚类匹配及区间误差 |
| Q04 | 打码覆盖及整帧兜底比例 | 07逐帧覆盖、A/V/PTS及整帧兜底比例<=1%；按声明覆盖范围报告 | 人脸标注、mask、逐帧覆盖率、漏码片段索引及兜底帧计数 |
| Q05 | 相对深度结构与标注点对远近排序 | 07相对深度指标；不宣称米制距离或未定义的时序稳定性 | 标注点对、预测图、尺寸/有限值检查和序关系计算 |
| Q06 | VLM镜头描述的事实与证据引用 | 07字段正确性、幻觉/未知和引用指标 | 逐镜头参考事实、模型JSON、匹配记录与判定依据 |

Q04通过只证明验收数据及声明检查范围达到门槛，不证明任意后续影片绝无漏码。
人工标注/审定可以构成固定ground truth；执行前封存标注hash，不能观察本轮输出后修改答案。
更换标注、算法或门槛均重新形成candidate并运行Q，不得只改报告。

## 9. O：运维和故障，所有 scope 必测

故障注入必须在目标设备维护模式进行：停止外部接入、确认没有其它job、记录恢复步骤及受影响挂载点。
只能影响该deployment受管资源。不能在共享宿主机直接制造整机OOM、拔除他人SSD或终止未知容器。
磁盘故障用该deployment的独立测试挂载/可恢复访问故障；记录实际注入方式，纯fake结果只能计S。

| ID | 动作 | 必须满足 | 原始证据 |
|---|---|---|---|
| O01 | 长片后连续不少于1800秒混合实际模型请求；scheduler-only独立运行同等时长 | 覆盖所有模型，包含切换和等待；无OOM/非预期500/不安全淘汰；终态queue/lease/permit全空、实例停止；429/504率及延迟符合policy | 请求ledger、资源/温度/系统事件、状态及最终STOPPED证据 |
| O02 | 模型盘访问失效和恢复、工作盘满/不可写 | 关闭准入/禁止commit；无根盘假目录写入；显式恢复后重hash与恢复 | mount/UUID事件、错误、文件路径/DB、恢复hash |
| O03 | worker/docker控制不可达、stop超时、未知实例/端口冲突 | UNKNOWN/BLOCKED期间保留预算与槽；health503；不杀未知实例；修复后独立观察才放行 | 控制故障、inspect/端口、账本、健康检查及恢复事件 |
| O04 | 主机服务重启及受管模型残留场景 | 启动先封闭入口；新boot清理旧实例；runner PAUSED等待resume；无自动重放 | systemd、boot、实例、API和job状态 |
| O05 | 用正确及被篡改的candidate/report/资产/设备身份执行production preflight | 完整匹配通过；任一错配在启动模型前失败；不接受v2报告或过期v3 | preflight输入、现场facts、退出码、确认无launch事件 |
| O06 | graceful shutdown、日志/证据容量限制、备份恢复验证 | 停机遵循02；日志不泄secret且可追踪；DB/artifact备份恢复可校验；恢复操作不自行触发推理 | 停机事件、脱敏检查、存储用量、备份manifest和恢复校验 |

scope 为 scheduler-only 时，O02 执行模型盘故障及调度服务状态目录不可写，O04 不含 runner 恢复，
O06 备份恢复对象为候选/配置/证据；pipeline scope 额外执行表中的工作盘、runner、SQLite/artifact 分支。
这些分支由 scope 固定推导，在证据中列明已执行断言；不得由执行人员自行关闭，O01..O06 场景本身均不可跳过。

混合负载的请求总数、各模型请求数、arrival schedule、输入分布和允许的429/504上限在policy中必填。
禁止仅运行一个请求然后空等30分钟。压力导致期望的拒绝可接受，但零OOM、零不安全淘汰、零残留租约不可放宽。
若无硬件温度/功耗计数器，明确记录不可采集的可选观测；内存、进程、请求事件等判定必需材料不可缺失。

## 10. E：外部关系报告，仅 full-pipeline

full-pipeline 必须使用候选声明的真实provider/model，mock只用于S回归。
E02中可能重复计费的故障由受控代理/传输层制造；不得自动重发不确定请求。

| ID | 动作 | 必须满足 | 原始证据 |
|---|---|---|---|
| E01 | 以P完成的结构化本地产物生成关系报告 | provider/model/prompt一致；每条关系可追溯07证据；未知不编造；质量门槛满足07 | 脱敏请求、provider请求ID、响应、引用验证与指标 |
| E02 | 外部超时/响应丢失/限流；显式处理EXTERNAL_UNKNOWN | 不盲重试可能已计费调用；本地产物保留；显式恢复/重试有新attempt和审计关联 | 调用尝试ledger、代理故障事件、状态与人工决定记录 |
| E03 | 验证发送字段白名单、credential脱敏、外部结果写盘及恢复 | 仅发送07允许的信息；不发原视频/人脸图/声纹；无secret泄露；成功报告可校验恢复 | 实际出站payload检查、日志扫描、产物hash、恢复记录 |

仅SDK可连接、API key有效或收到HTTP200均不能替代E通过。
外部模型别名若provider无法锁定revision，记录实际返回版本/调用日期和此限制；仍遵守7天有效期，不声称永久可复现。

## 11. policy、最终门禁和交付

性能policy必须在执行前填实值并绑定candidate：各模型冷启动/常规与最大输入推理上限、各阶段hard deadline、
整片最大耗时、峰值内存和磁盘上限、采样间隔/最大缺口、混合负载请求数/分布、p95/p99与错误率上限。
单位分别为 byte、毫秒或秒，类型及单位不得混用；缺值、NaN、无限值或占位符拒绝运行正式验收。
这些部署相关性能目标不能凭附件估算自动填写通过值。质量门槛和算法固定于07，policy仅引用其版本/摘要。
精确类型是reference/contracts.py的AcceptancePolicy，candidate.acceptance_policy_sha256绑定其规范JSON。
模型SLO键集合必须恰等于候选模型集合。cold_load/inference/envelope_seconds为对应每次请求最大值；
p95/p99按nearest-rank，对O01该模型成功请求的排队+加载+推理总耗时计算；拒绝和超时另计比例，不从日志删除。
429/504比例分母为全部实际发送请求，各上限<=0.1；非预期500、OOM、不安全淘汰和最终残留必须0。
arrivals是固定相对毫秒+model_id+fixture_sha256列表，>=100项、每模型>=3项、计划到达间隙<=15秒；
执行器按计划发送，不因拥塞降低负载，实际发送偏差>1000ms使该轮无效。
没有公开推理API的模型通过维护期fixture job申请正常permit执行，不绕过调度启动；
fixture job由acceptance注册且绑定candidate fixture hash，只在maintenance可用，正式runner不开放任意stage调用。

production release gate 的通过条件是以下条件的逻辑 AND：

1. 严格schema、scope、candidate与现场身份匹配，报告及全部必需材料在有效期内。
2. 该scope动态推导的必测集合完整且唯一，每项 disposition=executed，不含failed/skipped/unknown/not_run。
3. 全部材料hash正确、归属正确，生产evaluator重算全部场景通过，摘要数字与重算相符。
4. 全部性能policy与07质量门槛通过，结尾queue/lease/permit为空，所有验收启动的受管实例STOPPED。
5. production render引用该candidate与证据摘要；现场preflight再次比对代码、资产、设备、栈、模式和报告。

最终交付包括candidate、环境facts、完整可验证证据包、v3报告、验证器版本和退出码、渲染清单及现场preflight记录。
软件开发可以在无设备时完成并标记software_verified；不得填充虚构的峰值、GPU证据、长片指标或passed使设备门禁通过。
