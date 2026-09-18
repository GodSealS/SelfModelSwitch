# v3 未完成功能执行计划

日期：2026-09-17。核对源码：`63f7d8e13319d57e946e46d5766703e55d75c9a3`。
状态：供审阅和后续实施的详细计划；本次交付不改变运行中的服务，不代表功能或设备验收通过。

## 1. 执行范围、事实与完成定义

本文件细化 [M00—M07](05-tasks-and-acceptance.md)，只覆盖 SelfModelSwitch 模型服务。
视频业务、FFmpeg、人物/声线、影片数据库、视频质量和外部报告均不在范围内。
[根 README](../README.md) 描述当前服务；历史文档中的命令或“已通过”不自动成为本轮指令或证据。
本计划内标为“新增”的文件、命令、字段均须由指定任务交付后才能使用。

### 1.1 已完成与未完成的边界

| 项目 | 核对结论 | 后续动作 |
|---|---|---|
| M00 首个 Qwen2.5-VL/Orin 探测 | [m00-envelope.md §9—10](m00-envelope.md) 已记录真实探测通过；本轮未 SSH 重验原始材料 | 保留结论，P00 核对材料完整性；不重复建设探测器，不外推到新镜像/设备/模型 |
| M01 注册契约第一片 | `contracts_v2.py` 已提交，21 个现有测试通过 | 补齐 P01 的缺口；它尚未接入生产配置、runner 或 scheduler |
| 控制协议测试 | 工作区有未跟踪 `tests/test_control_protocol_v1.py`，对应模块不存在，收集时报 ImportError | P02 接续并评审此草稿；本次不覆盖、不删除、不代为提交 |
| 旧服务 | 固定四 ID，chat/embedding/rerank、切换、取消、存储恢复与旧部署工具已有实现 | 增量复用，不能宣称 M02—M07 从零开始，也不能将旧实现当成 v3 通过 |
| M02—M05 | 动态注册运行接线、通用会话、Blob、通用执行/控制接口缺失 | P04—P20 |
| M06 | 有旧 renderer、report 校验与打包工具；无 v3 原始证据重算闭环 | P03、P21—P28 |
| M07 | 没有最终 v3 candidate 的 S/B/O 全集验收 | P29—P31 |

`plan/05-tasks-and-acceptance.md` 的“全部待办”和 `plan/validation.md` 的“M00仍待”是旧快照；
本计划采用较新的逐项证据。当前未跟踪设计文件和已修改根 README 属用户现有工作，实施前单独确认基线归属。

### 1.2 本次本地核对记录

使用现有 `.venv/bin/python`，版本 **3.13.5**，不是发布要求的 Python 3.12：

| 命令 | 实际结果 |
|---|---|
| `.venv/bin/python -m pytest tests/test_contracts_v2.py -q` | 21 passed，exit 0 |
| `.venv/bin/python -m pytest tests/test_control_protocol_v1.py --collect-only -q` | 缺少 `control_protocol_v1`，exit 2 |
| `.venv/bin/python -m pytest tests -m 'not thor' --ignore=tests/test_control_protocol_v1.py -q` | 226 passed、1 deselected、2 dependency deprecation warnings，exit 0 |
| `.venv/bin/python -m ruff check .` | exit 0 |
| `.venv/bin/python run.py --check-config` | schema_version=1、旧四 ID，exit 0 |

排除草稿的 226 项只用于说明已有实现基线；不等于完整测试集通过。P02 之后不得沿用此 ignore。
本次没有构建、部署、启动模型或重验硬件，不能生成 `software_verified` / `device_backend_ready` 报告。

### 1.3 每个实施任务的统一交付规则

1. 在开发机编辑，使用发布 Python 3.12。先写能揭露违约的测试，再实施；测试不得仅复述内部实现。
2. 每任务最多约 5 个文件；下表文件均为明确落点，新增文件须先创建。若实际超出，先拆同 ID 的后缀任务并写明依赖。
3. 运行该任务指定测试，以及第 7 节通用检查。记录完整 SHA、命令、exit code、解释器、证据目录。
4. 审查并原子提交任务文件，不将工作区其他变更打包。未通过检查不得在任务状态表勾选完成。
5. 按 [AGENTS.md](../AGENTS.md) 将目标干净 checkout fast-forward 到同一提交；硬件行为任务还须目标测试证据。
   无目标条件时标 `software_only`，不是该硬件任务完成。
6. 接口任务产出 schema + 正反例 + 消费者测试；模型任务产出正式 API 到停止证据的闭环。
7. 文档数字是约束或输入，不是通过记录；`blocked` 必须写缺少的具体材料及恢复动作。

## 2. 本计划固定的接口与行为约束

以下是本轮实施采用的设计决定；它们不声称已实现。与旧文件冲突处须由相应任务同步原章节和测试。
不能用“实现时再定”跳过下面的约束。只读 facts 无法确定的设备值必须走第 6 节输入门禁。

### C01：注册、runtime 与范围

- v2 注册 ID 沿用 `contracts_v2` 的小写 1—63 字符规则；使用全字符串匹配，拒绝末尾换行/NUL。
  端口 10001—19999，部署内唯一。配置读取时确定模型集合，不增加在线修改注册表 API。
- `assets` 是逐文件表，按 `path` 唯一；同一 role 可有多文件以支持分片。
  adapter profile 规定角色基数：首个 GGUF profile 恰一 model，vision 恰一 projector；HF 分片格式可登记，
  未有 profile/fixture/实测时不得启动或宣称支持。路径检查须涵盖所有父目录、TOCTOU、regular file 和挂载身份。
- 当前可执行 capability 闭集：`chat|vision|embeddings|rerank`。audio、Torch、ORT 是后续扩展，
  新增前必须补自己的 M00、profile、DTO、fixture、evaluator 和 B 场景；不能因接受 runtime_id 就宣称任意 runtime 可用。
- 每个 RuntimeSpec 新增必填 `profile_id`；profile 是代码注册的有限集合，绑定程序/镜像入口、允许参数、
  取值域、ready/terminal 协议和资产角色基数。未知 profile 422；runtime_id 只作部署内身份。
- `startup_args` 保留为“启用的受支持 flag 名称集合”，不能承载值或 shell 文本。
  profile 给每个 flag 定义值来源：固定常量、配置 envelope、已核验资产、固定端口；没有值来源即拒绝渲染。
  M00 的 profile 固定 `--load-mode auto`，不生成 `--no-mmap`；更换加载模式是新候选。
- 配置 schema v2、control protocol v1、candidate/report v3 相互独立。新增 control 请求要求
  `X-SMS-Protocol-Version: 1`，缺失/不支持返回 400 `unsupported_protocol`，响应回显版本。

### C02：内存只加一次裕量，物理驻留另设门槛

现有 `Book.required_for()` 对 v1 reserved_bytes 再乘 margin；v2 的 `reserved_bytes` 已是 R。
二者不能直接混用。内部统一 `effective_reserved_bytes`，v1 在兼容转换处乘原 margin 一次，v2 原样使用。

```python
# v2 精确整数公式，避免 float 在边界上的取整差异
reserved_bytes = (measured_peak_bytes * 115 + 99) // 100
new_bytes = 0 if instance_already_reserved else reserved_bytes
admit = (
    storage_ready and identity_known and slot_available and sample_valid
    and committed_bytes + new_bytes <= model_budget_bytes
    and mem_available_bytes >= new_bytes + free_floor_bytes
)
```

- 已有 R=6106148045 B，对应 measured_peak=5309693952 B；不再乘 1.15。
  样本 `0 <= now_mono - sampled_at <= 2s`；边界等号通过，差 1 byte 拒绝。
- B 不超过 `MemTotal - system_reserve`；默认 reserve=8 GiB、F=2 GiB；实时不再扣 reserve。
  READY 执行 `new_bytes=0`，仍检查 envelope、槽、freshness、F。
- M00 的约 10.5 GiB 驻留总量是估计，不是可填入生产配置的测量值；其中权重描述也与逐文件 bytes 不完全一致。
  P21 必须从原始采样及实际资产重算，禁止相加一个估计后标 measured。
- v2 ModelSpec 增加 `measurement_ref`（测量材料摘要）、`physical_resident_peak_bytes`（未测为 null）；
  登记可未测，生产候选必须为正整数且绑定材料。定义它为运行窗口中该模型及 adapter 的
  不重复计数物理占用上界；采集方法、统一内存重复计数处理随 policy 固定。
- 增加静态物理门槛：每个未 STOPPED 实例计一次
  `physical_reserved_bytes = ceil(physical_resident_peak_bytes * 1.15)`，总和不得超过 B。
  这是对 MemAvailable 增量低估的独立保护，不能替换实时门槛。
  若采集器不能给出可信物理上界，生产不开放该模型；calibration 用显式临时预算、独占维护环境。
- 首版物理测量方法固定为保守 `system_nonfree_upper_bound_v1`：同运行窗口逐样本取
  `MemTotal - MemFree`，每轮取最大、三轮再取最大，原始样本同时保留这两个字段。
  该上界包含OS、page cache及其他进程，不减基线，不把CUDA指标再加一次；它不是“模型净占用”。
  P21须验证目标统一内存的受管计算占用包含于此Linux口径；无法验证则该方法不适用、生产阻塞。
  跨模型相加可能保守重复计入背景占用，此为首版接受的容量代价；更精确的方法必须新增method版本、材料和候选，不能临时扣除估计值。
- UNKNOWN/ERROR/BLOCKED 保留两套预留；请求结束只还执行槽，实例 STOPPED 才还模型预留。
  内存低于 F 关闭新执行并启动受管计算清理；停止后重新采样，最多等 10s，仍不足保持不可准入。

### C03：身份、端口与异步回写

InstanceIdentity = `(container_id, started_at, deployment_id, model_id, runtime_id,
candidate_digest, image_digest)`；started_at 必须为带 UTC 时区的有效时间。
回写 Fence = `(boot_id, model_id, generation, operation_id, execution_id, attempt)`；
非 execution 操作的最后两项为 null，execution 的 attempt 从 1 起且首版不自动重试。
owner/token 不是实例身份替代品；所有异步完成、超时、取消、observer 更新必须检查自己的 Fence。
P03同时固定 Clock（monotonic/UTC）、MemorySample、LaunchOperation 和 EventSink 端口；
EventSink事件含schema_version/event_id/sequence/UTC/monotonic/Fence/type/payload，sequence在boot内递增。
P06起产生结构化事件，P22只实现落盘collector；observer不得反向读取scheduler内存推断启动者是否退出。

拟新增 `ports_v3.py` 定义以下结构化端口；这些签名固定职责，具体 DTO 在 P03 完成：

```python
class BackendPort(Protocol):
    async def load(self, spec: RegisteredModel, fence: Fence, deadline: float) -> Observation: ...
    async def execute(self, request: ExecutionRequest, fence: Fence, deadline: float) -> ExecutionHandle: ...
    async def cancel(self, handle: ExecutionHandle, deadline: float) -> CancelAck: ...
    async def stop(self, identity: InstanceIdentity, fence: Fence, deadline: float) -> StopAck: ...

class ObserverPort(Protocol):
    async def observe(self, target: ObservationTarget, deadline: float) -> Observation: ...
```

CancelAck/StopAck 只表示命令已处理。Observation 必须带采样时间、完整实例身份（存在时）、
端口状态、启动操作/子进程状态和 `RUNNING|STOPPED|UNKNOWN`。执行终结证据另含完整 Fence、
InstanceIdentity、backend 终结原因、设备同步事实及 `compute_quiescent=true`。
本地未 dispatch 请求可直接终结，使用 `dispatch_state=not_started`，不得伪造容器身份。

STOPPED 的唯一计算：已确认容器退出/不存在 AND 启动操作终结 AND 启动子进程退出 AND 固定端口无监听。
不存在必须来自成功的容器清单/结构化 not-found，不能将任意 `docker inspect` 非零解释为不存在。
停止前用不可变 container ID 及标签全身份核对，禁止按名称杀未知容器。端口被未知进程占用时保持 UNKNOWN。
启动用受控、可观察的子进程组；cancel Future 不等于启动子进程退出。重启按 deployment 标签枚举旧实例和启动者，
核验属于本 deployment 后清理，禁止 `runtime.build_scheduler()` 在观察前批量 bootstrap_stopped。

### C04：会话与执行状态机

| 对象 | 状态/转换 | 约束 |
|---|---|---|
| session | `preparing -> active -> draining -> closed`；preparing 可转 draining；draining 可转 blocked；blocked 确認停止后转 closed | 排队是 preparing 的 `phase=queued`，另有 draining_existing/loading；不新造 ready 状态 |
| execution | `queued -> running -> succeeded|failed`；queued/running 可转 cancelling，后续转 cancelled 或 failed | 无可信停止/终结则保持 cancelling，不超时伪造 terminal |
| model | 复用 unknown/unloaded/loading/ready/evicting/error | 只有 unloaded 可开始 load 并增加 generation |

- 单个 ACTIVE 通用会话独占整个模型服务；会话内最多 `envelope.max_parallel` 个 execution，
  其余进入有界队列。旧交互接口原并发语义保留；会话冻结阶段暂停其新准入，已有 lease 正常完成。
- session queue 容量128、等待1800s；priority 来自服务端 owner 配置，默认0、范围0..1000，客户端不能提交。
  排序键 `(-priority - floor(wait_seconds / 30), sequence)`，sequence 在同一 boot 中严格递增。
- 尝试授予时在同一锁内冻结交互准入，最多 drain 30s；超时解除冻结、保留 waiter 原 sequence/入队时间，
  30s 后才可再尝试，不杀已有 lease；不能重置总等待1800s。pinned/preload 任意冲突返回409。
- `prepare_deadline=min(grant_started+900s, hard_deadline)`；等待队列不计入准备900s。
  hard deadline 从会话创建算起，`session_hard_timeout_seconds` 是配置正整数1..86400，首个 profile 默认3600s。
  queued 阶段截止为 min(created+1800s, hard_deadline)；清理60s是截止后独立清理窗口。
- ACTIVE 起 `expires_at=min(now+30s, hard_deadline)`；建议客户端每10s心跳。
  now>=expires_at 时 heartbeat/submit 必须409，不能续活；心跳不改变 hard_deadline。
- preparing阶段未启用ACTIVE TTL，view的expires_in_ms=0、hard_remaining_ms正常倒计时；
  heartbeat返回409 busy，客户端用GET查询，不因该0值在服务端触发ACTIVE过期清理。
  排队中close只撤销自己的waiter/未派发操作；未持有独占权、无所属实例/启动者/lease时直接closed，绝不停止另一会话的模型。
- close/TTL/hard deadline 在锁内先禁止 submit/renew，然后 cancel 等10s、stop grace30s、总清理60s。
  无法证明停止转 BLOCKED，health503，每5s对账；未清干净不得授予下一会话。
- 同模型尚有其他 lease 时，请求取消先冻结新准入，等待其他有效 lease 结束再停止实例。
  session close 可以取消本 session 全部 execution；独占保证不存在其他 owner 的有效 lease。
- 锁内只改内存状态；生命周期 load/stop 由一个 worker 串行消费。推理可按槽并发，不占住生命周期队列。
  等待执行完成、hash、Docker、sleep、HTTP、磁盘 I/O 全部在锁外，结果按 Fence 回写。

### C05：协议 DTO、错误与幂等

保留 [03-api](03-api.md) 路由；新增接口用严格 JSON，拒绝重复 key、未知字段、非有限数、bool 冒充整数。
嵌套任意字符串必须合法 UTF-8、无 NUL。禁止宿主路径/远程 URL、argv、端口、预算、job/stage/pipeline 输入。

| DTO | 精确约束 |
|---|---|
| SessionCreate | model_id、idempotency_key 必填，correlation_id 可选；key 1..128 UTF-8 字节，correlation 1..128；不接受 owner/priority/deadline |
| SessionView | session_id、state、phase、boot_id、model_id、expires_in_ms、hard_remaining_ms、owner_token、error；剩余值为非负整数；closed token=null |
| ExecutionCreate | session_token、operation、input、parameters、idempotency_key 必填；operation 必须在模型能力表；parameters 规范 JSON <=8192 bytes且按 operation 白名单解析 |
| ExecutionInput | 恰含 inline 或 blob；inline 是非空对象，规范 JSON <=4 MiB；blob 是 BlobRef |
| BlobRef | blob_id、owner、sha256、size_bytes、media_type；sha256 小写64hex，size 1..1 GiB，media_type 小写 type/subtype 无参数；owner必须与服务元数据相等 |
| ExecutionView | execution_id、state、dispatch_state、compute_quiescent、result、error、instance、fence；非terminal的quiescent/result/error为null |
| terminal | succeeded 必须 result、无 error；failed/cancelled 必须 error、无 result；已dispatch必须instance/fence完整且quiescent=true；not_started可无instance，但带Fence且证明未送后端 |

P02 扩展现有草稿测试以覆盖新字段；不得为了保留测试而接受不完整终结证据。
外部 ID 为服务生成的不透明小写 ID；每boot生成随机256bit服务密钥，token固定为
`boot_id + "." + base64url(HMAC-SHA256(boot_key, canonical(boot_id, session_id, model_id, owner)))`。
密钥仅存本进程内存，重启换key；服务存token摘要用于验证，GET可由绑定字段重算同token，解决摘要不可逆问题。
日志/status/持久元数据永不输出token或boot_key。
GET session 只向 owner 返回有效 token；其他 owner 一律404，避免透露对象存在。

幂等索引键 `(boot_id, owner, route_kind, idempotency_key)`；同 key 比较独立的规范 payload hash，
相同返回原对象，不同409 `idempotency_conflict`。hash 不含 token 原文，但以解析得到的 session_id/boot 代替其绑定，
不能把 payload_hash 放进索引主键而漏掉冲突。活跃对象不清理；终结后保留86400s。客户端不得依赖跨重启幂等。
会话/执行重启失效返回409 `stale_token`；对象查询在旧 boot 返回404，不能接管重放。
POST第一次202，幂等重放返回对象现态且不重复副作用；close/cancel 未完202、已完200。

| HTTP | 固定 error.code（retryable） |
|---|---|
| 400 | malformed_json、unsupported_protocol（false） |
| 403 | peer_forbidden（false，仅建连接/无对象操作） |
| 404 | not_found（false） |
| 409 | stale_token、session_expired、idempotency_conflict（false）；busy、blob_in_use（true） |
| 410 | reference_expired（false） |
| 413 / 415 | payload_too_large / unsupported_media_type（false） |
| 422 | contract_violation、envelope_exceeded、capability_mismatch（false） |
| 429 | queue_full、quota_exceeded（true） |
| 502 | backend_failed（false，执行是否已发生不确定，不自动重试） |
| 503 | instance_unknown、storage_unavailable、resource_unavailable、temporarily_unavailable（true） |
| 504 | queue_timeout（true）、execution_timeout（false） |

retryable 表示状态可能恢复；只有确认 not_started 或查询原幂等对象后才能决定重试，服务端不盲重发推理。
通用错误体沿用03-api；request_id 长度1..128字节。队列满不是 health 失败，BLOCKED/UNKNOWN/storage fault 是。

### C06：能力输入和 envelope

- control v1 的 chat/vision inline 使用 `messages`，与受支持 OpenAI chat 消息格式一致；
  Blob 输入对 chat/embeddings/rerank 是 `application/json` 且内容等价 inline。
  vision 的 Blob 输入可为 image/png 或 image/jpeg，parameters 中 text 必填，adapter 构造一条用户消息。
- parameters 闭集按能力定义：chat=`max_tokens,temperature,top_p,seed`；vision 同上加 text（只用于 image Blob）；
  embeddings=`encoding_format`（首版仅 float）；rerank=`top_n,return_documents`。
  正整数/有限数及范围由 schema 写死；temperature 0..2、top_p (0,1]、seed 0..2^31-1。
  max_tokens 默认4096但取 min(4096,模型max_output_tokens)；rerank top_n 默认文档数、不得大于文档数。
- embeddings inline=`input`（非空字符串或非空字符串数组）；rerank=`query,documents`（非空字符串数组）。
  batch/document 上限作为能力 profile 必填正整数，绑定 candidate；没有实测值不得生产开放该能力。
- 文本 token 包含 chat template、特殊 token、全部消息及视觉 token；必须用同 runtime tokenizer/template
  或有证明的保守上界检查，不能用字符数估计。先做有界格式/尺寸验证，再受限计数，最后 dispatch；
  runtime 无法预先确定图像 token 时按登记最坏1280计入输入预算。
- Qwen envelope：每槽ctx32768、输入<=28672、输出<=4096、输入+输出<=ctx、每请求图像<=1、
  每边<=1024、视觉token<=1280、并发<=2；新增 `max_images=1` 明确消除原 schema 漏项。
  其他模型的 max_images 非vision为0；图像按解码后像素检查，压缩体积小不绕过尺寸限制。
- control 的输出统一为 BlobRef：文本/embedding/rerank 为 application/json、保留对应协议 shape；
  SSE 只存在旧chat接口。能力不匹配422，未知模型404；没有新增音频专用路由。

### C07：Blob 持久元数据与暂存生命周期

- POST /internal/blobs 使用二进制 body、Content-Type 和必填 `X-Content-SHA256`；支持有界分块接收，
  Content-Length 可选但若有必须一致；只接受能力表登记的 application/json、image/png、image/jpeg。
  成功201+BlobRef。GET200；过期410；DELETE未持有时204（已删除同owner仍204），执行持有时409。
- owner 由 peer UID 映射，不接受自报身份；上传逐块hash、临时文件独占创建、校验后原子发布。
  `input` BlobRef 中 owner/hash/size 不可信，执行时必须和数据库、打开的文件一致。
- 使用服务独占 SQLite 元数据：blob_id/owner/hash/size/type/state/created_at/expires_at/path；
  仅blob归属/配额/过期元数据持久化，不保存客户端业务任务。完成元数据与文件用恢复日志协调，
  崩溃点测试覆盖文件已rename但事务未提交、事务已提交但文件丢失。
- 输入上传成功起24h过期；输出执行终结起24h过期；执行读取租约内禁止删除/GC，过期后禁止新引用。
  超过过期时刻的活跃租约继续保护文件；最后一个租约释放后清理，保留owner tombstone 24h返回410。
- 默认单owner 4 GiB、全局16 GiB、磁盘余量2 GiB；部署可显式修改并进入candidate摘要。
  upload在接收前预留声明大小，chunked无长度预留1 GiB；输出dispatch前预留profile输出上限。
  并发写入按“已发布+临时+已预留”原子检查配额/余量，不能各自读df后超卖。
- 输出上限来自 profile，最大1 GiB；超限不发布部分结果。取消先标不可提交，晚到结果只清理暂存。
  文件名只用服务生成ID，目录逐级no-follow，拒绝symlink/非regular/逃逸；worker只能访问指定输入和自己的输出目录。
- 启动重新校验已发布文件/元数据hash后开放读取；未完成upload和旧执行输出隔离清理；
  若旧实例未停止，保留相应读取保护直到观察到STOPPED。禁止先清理它仍在使用的文件。

### C08：Unix 身份与单进程双入口

loopback HTTP 和 control.sock 必须共享一个 Scheduler/Book/BlobStore/boot_id/instance_lock。
不允许启动第二个调度进程或两份 lifespan。控制路由不得注册到普通 TCP listener。
control.sock 权限0660，服务账号所有、登记客户端组；owner=`uid:<decimal>`，每个允许UID唯一owner。
本机进程同UID属于同owner；首版不声称提供同UID内部隔离。

control listener 适配器必须从 accepted Unix socket 读取 Linux SO_PEERCRED，将可信 UID 注入服务端 scope；
不能从 X-Owner、X-UID、request body、反向代理头或普通 ASGI client 地址取得身份。
采用同事件循环的 TCP ASGI listener + 独立 Unix HTTP listener 适配器，协议解析复用依赖中的 HTTP 实现；
实现前 P17 的真实 Linux 契约测试必须证明 peer credential 可达 handler，失败则阻塞，不退化为相信请求头。
仅接受HTTP/1.1有界请求，禁止upgrade/proxy；关闭时两入口先停止接收，共享清理只执行一次。

### C09：候选与证据身份

M01 的部署登记digest不是 v3 candidate digest。新增严格文档：

| 文档 | 必填字段族 |
|---|---|
| CandidateV3 | schema_version=3、deployment_id、source_archive_sha256、config_sha256、device、runtime_stack、runtimes、models、measurement_refs、fixture_refs、policy、collector_sha256、evaluator_sha256 |
| Device | machine_id_sha256、architecture、device_tree_sha256、mem_total_bytes、OS/kernel、GPU identity、driver/JetPack、容器版本、电源/时钟模式、模型盘/暂存盘UUID |
| ArtifactRef | relative_path、size_bytes、sha256；不得symlink、绝对路径、重复路径或逃逸 |
| CaseAttempt | case_id、run_id、attempt、candidate_sha256、device_digest、started_at、ended_at、boot_id、events/raw_refs、collector/evaluator身份、exit_code |
| AcceptanceReportV3 | schema_version=3、candidate_sha256、device_digest、run_id、started_at、ended_at、case_attempt_refs、每case最终attempt引用、artifact_manifest；summary不作为判定依据 |

必测集合精确取 [06-acceptance](06-acceptance.md)：S01..S06、每模型六类B、每cap一个B、O01..O06。
允许同case保留失败attempt，但最终映射每case恰一个，所有attempt必须可追溯，不能删掉失败记录。
重复最终结论、未知case、缺case或缺fixture/evaluator一律拒绝。

摘要规范：sort_keys、紧凑分隔、UTF-8、不转义非ASCII、禁NaN、展开默认值；无语义顺序的
模型/runtime/assets表分别按ID/path排序；有语义顺序的输入数组不排序。
配置摘要只去顶层 candidate_sha256；candidate本体不含自身摘要、运行结果、宿主绝对路径、证据输出目录、生成时间。
资产相对路径及fixture/measurement逻辑artifact key是语义输入，必须纳入摘要；外部材料的宿主定位另由manifest映射。
`candidate_sha256=sha256(canonical(candidate_body))`，再回填配置；hash链不能反向引用report。
源码归档用明确清单，只含运行/采集/评价/测试/锁文件，排除生成配置、plan归档、evidence、bundle、模型、凭据。
最终bundle另有file manifest，不回填candidate。改运行代码/配置/runtime/设备/预算/policy/evaluator须新candidate。
纯源码归档由P22独立的 `acceptance source` 生成，不调用需要deployment的旧build-release，避免形成发布循环。
归档输入是clean commit中的 `app.py`、`run.py`、`model_scheduler/`、`scripts/`、`deploy/`、`tests/`、
`pyproject.toml`、`requirements.in`、`requirements-dev.in`、`requirements.lock`、`requirements-dev.lock`；
目录仅收已跟踪regular文件，排除其中任何生成证据/权重/凭据。固定prefix为source、字典序文件清单、
uid/gid/mtime=0、gzip mtime=0；不嵌入commit时间或归档自身摘要，源码字节不变则归档hash不变。

## 3. 依赖图与检查点

下表“依赖”是硬前置；任务只读准备可提前，未满足依赖不能把实现合并为已完成。
每个检查点运行第7节G检查；原M02/M04/M06/M07检查点保留，不以中间checkpoint代替。

```mermaid
flowchart TD
  P00 --> P01 --> P02 --> P03
  P03 --> P04 --> P05 --> P06 --> P06a --> P06b --> P07
  P07 --> P08 --> P09 --> P10
  P03 --> P11 --> P12
  P05 --> P12
  P03 --> P13
  P10 --> P14
  P12 --> P14
  P13 --> P14
  P06 --> P15 --> P16
  P14 --> P16
  P16 --> P17 --> P18
  P18 --> P19 --> P20
  P20 --> P21 --> P22 --> P23 --> P24 --> P25 --> P26 --> P27 --> P28
  P28 --> P29 --> P30 --> P31
```

| 检查点 | 任务结束位置 | 必须能演示的行为 |
|---|---|---|
| K0 | P00—P02 | 基线差异可解释；注册和控制DTO正反例通过，草稿不再阻断收集 |
| K1 | P03—P05 | schema/ports/hash链固定；配置显式迁移后所有资产可核验 |
| K1a | P06—P06b | 真实llama-swap控制契约、动态lab配置与受限启动可用 |
| CP1 / M02 | P07 | 受限启动、身份、迟到启动、独立停止完整闭环 |
| K2 | P08—P10 | 资源单次预留；公平会话；TTL与取消不提前清账 |
| K3 | P11—P13 | Blob上传/重启/删除闭环；幂等无重复副作用 |
| K4 | P14—P16 | 真实协议adapter接线；可信终结与取消/停止证明 |
| CP2 / M04 | P17—P18 | 两UID通过Unix控制完成create→execute→cancel/close，身份隔离成立 |
| K5 / M05 | P19—P20 | 旧API/SSE回归，动态模型和vision envelope生效 |
| K6 | P21—P23 | 实测输入→candidate→真实B执行器，材料可逐项定位 |
| K7 | P24—P26 | O执行器、离线重算、production render/preflight拒绝伪通过 |
| CP3 / M06 | P27—P28 | 锁文件/CI/独立发布包/回滚流程完整，仍不宣称真机通过 |
| CP4 / M07 | P29—P31 | 最终candidate S/B/O全集与现场preflight/发布冒烟通过 |

模块导入方向：纯contracts → config/ports → runner/observer/storage/adapters → scheduler服务编排 → HTTP/CLI composition。
Book/session/execution 是共享锁下的纯状态对象，不调用HTTP/Docker；adapter不导入app/scheduler；
acceptance executor通过正式API使用服务，evaluator只读材料，不依赖活的scheduler。BlobStore通过端口注入execution服务。

**构建顺序与最终运行分开：** P22—P28先交付工具实现及fixture/依赖注入测试，不能要求P22已有P25生产evaluator，
也不能要求P24已有P26/P27最终部署。该阶段使用明确标记test-only的材料测试契约，不生成可发布通过记录。
P28完成后，P29重做source/candidate，绑定全部最终实现hash，才真实执行全集。
O05测试P26的“现场身份核验原语+篡改拒绝”，不要求包含O05自身的最终报告；P31再执行完整production preflight。
O06使用同最终源码的隔离lab release对演练切current、备份/恢复Blob元数据和回滚，记录两份材料身份；
“没有已验收生产旧版时必须拒绝生产回滚并保持停机”是另一个必测分支，不伪造一份旧版通过报告。

## 4. 有序实施任务

所有勾选框初始未完成。Estimated scope：S=1—2文件、M=3—5文件；设备任务按材料集合计，不修改目标源码。
以下 `python` 必须是Python3.12；测试文件标“新增”的命令只在该任务创建文件后执行。

### P00 — 固定可复现基线和可用输入（M00收尾）

**Primary owner:** backend；**Collaborators:** arch；**Dependencies:** 无；**Estimated scope:** S。
**Description:** 保存当前dirty文件清单和hash，核对M00记录的源commit/runtime/资产/原始证据目录；不重跑已通过探测。
**Files likely touched:** `plan/08-execution-plan.md`（执行记录）、`plan/validation.md`（仅追加本轮记录）。
**Acceptance criteria:**
- [x] 单独列出已提交、未提交草稿、旧事实冲突；不将用户草稿误判为已实施。
- [x] Python3.12环境可用；目标硬件按AGENTS命令核验；M00材料在目标或只读副本可核对hash及3轮停止记录。
- [x] 缺材料则标待核验并列恢复来源，不把文档转写成原始证据；不重新触发模型试验。
**Verification:** `git status --short`、`git rev-parse HEAD`、`python --version`；完整测试集预期仅已知草稿收集失败，保存输出。
**本轮执行记录（2026-09-17）:** status=complete；source_commit=`63f7d8e13319d57e946e46d5766703e55d75c9a3`；
python=3.12.11（`/Users/monster/.local/share/selfmodelswitch/venv312`，原 `.venv` 为 3.13.5 不满足 `requires-python`）；
target_commit=`63f7d8e13319d57e946e46d5766703e55d75c9a3`（`/home/jtzn/SelfModelSwitch`，工作区干净）；
M00 通过轮 run1—run3 均 `quiescent=true`，模型资产与 runtime 自校验复核一致；未重跑模型试验。
未提交草稿、旧事实冲突、命令 exit code、基线 manifest 与待核验项见 [validation.md](validation.md) 的 P00 记录。

### P01 — 补齐动态登记契约（M01）

**Primary owner:** arch；**Collaborators:** backend；**Dependencies:** P00；**Estimated scope:** M。
**Description:** 在已有v2契约上实现C01/C02/C06缺项，保留v1运行行为。
**Files likely touched:** `model_scheduler/contracts_v2.py`、`tests/test_contracts_v2.py`、`plan/04-deployment.md`、`plan/02-scheduler.md`。
**Acceptance criteria:**
- [x] 同role分片按path唯一；GGUF profile基数单独校验；新增profile_id、max_images、测量引用和物理峰值字段。
- [x] 所有字符串全匹配、path拒绝NUL/父路径；JSON `1e999`、嵌套非有限值、未知字段均拒绝。
- [x] v2 reserved精确整数计算，无二次margin；measurement为空只能登记/隔离校准，不能生产开放。
**Verification:** `python -m pytest tests/test_contracts_v2.py -q`；补分片、duplicate path、尾换行ID、1-byte和未测生产拒绝案例。
**本轮执行记录（2026-09-17）:** status=complete；起点 commit `62c0036`；python=3.12.11；
`pytest tests/test_contracts_v2.py -q` = 35 passed（实现前 18 项失败）；
`pytest tests -m 'not thor' --ignore=tests/test_control_protocol_v1.py -q` = 240 passed, 1 deselected；
`ruff check .` exit 0；`run.py --check-config` 仍为旧四 ID，v1 运行行为未变。
新增契约：`PROFILES`（`llama-cpp-gguf-v1` 可执行、`hf-sharded-v1` 仅登记）、`RuntimeSpec.profile_id`、
`Envelope.max_images`、`ModelSpec.measurement_ref`/`physical_resident_peak_bytes`、`reserved_bytes_from_peak`、
`physical_reserved_bytes_from_peak`、`effective_reserved_bytes`（v2 原样、v1 兼容处只乘一次原 margin）、
`require_startable_profile`、`require_production_openable`、按 path 唯一的 asset 表。
文档同步：04-deployment §1、02-scheduler §5。未解决：`hf-sharded-v1` 无自身 fixture/实测因而不可启动
（argv 渲染属 P06，测量与 fixture 属 P15/P21）；本任务不产生硬件证据也无此项。目标 checkout 已同步到同一提交。

### P02 — 完成控制协议v1 DTO与错误表（M01）

**Primary owner:** arch；**Collaborators:** backend；**Dependencies:** P01；**Estimated scope:** M。
**Description:** 以C03—C06消除现有测试草稿对应实现缺失，导出可供独立客户端使用的schema。
**Files likely touched:** `model_scheduler/control_protocol_v1.py`（新增）、`tests/test_control_protocol_v1.py`、`schemas/control-v1.json`（新增）、`plan/03-api.md`。
**Acceptance criteria:**
- [x] parser/schema状态、字段、字节限制、版本、错误码一致；succeeded无result/不静止/缺Fence全部拒绝。
- [x] queued取消用not_started证据合法；不得要求伪造未创建的container；扩展草稿中缺失字段测试。
- [x] inline/blob互斥、parameters能力白名单、所有int/bool/NaN/未知字段正反例完备；schema生成可重现。
  新命令 `python -m model_scheduler.control_protocol_v1 export-schema --output schemas/control-v1.json` 只从DTO生成；测试重导出并按字节比较。
**Verification:** `python -m pytest tests/test_control_protocol_v1.py tests/test_contracts_v2.py -q`；全tests收集不再ImportError。到K0。
**本轮执行记录（2026-09-17）:** status=complete；起点 commit `5a8e2d9`（GitHub 推送记录）；
实现提交 `c17eb7b`；python=3.12.11（`/Users/monster/.local/share/selfmodelswitch/venv312`）；
`pytest tests/test_control_protocol_v1.py -q` = 31 passed（草稿期 3 项因缺少新必填字段失败，扩展后全覆盖）；
`pytest tests/test_control_protocol_v1.py tests/test_contracts_v2.py -q` = 66 passed；
`pytest tests -m 'not thor' -q` = 271 passed, 1 deselected（不再需要 ignore，K0 达成）；`ruff check .` exit 0；
`run.py --check-config` 仍为旧四 ID，v1 运行行为未变。
新增 `model_scheduler/control_protocol_v1.py`（严格 DTO、错误表、每能力参数闭集、schema 导出）与 `schemas/control-v1.json`
（16969 B，测试重导出按字节比较）；测试覆盖 terminal 证据（result/error/quiescent/必填 fence、fence 必须指向该 execution）、
dispatch/instance 配对（not_started 不得携带容器身份）、会话视图全字段与 owner_token 恰在 closed 为 null、
参数范围与非有限/bool 拒绝、字节与 UTF-8/NUL 限制、24 码错误表逐项一致。
`plan/03-api.md` 新增 §5 并标注 §2 的已固定部分。HTTP 路由、身份接线与 Blob 存储仍待 M04；本任务不产生硬件证据。

### P03 — 固定内部端口与candidate/report结构（M01）

**Primary owner:** arch；**Collaborators:** backend；**Dependencies:** P02；**Estimated scope:** M。
**Description:** 实现C03/C09的纯类型/严格解析器及材料字段，使下游不通过互相导入业务模块获得类型。
**Files likely touched:** `model_scheduler/ports_v3.py`、`model_scheduler/evidence_contracts.py`、`tests/test_ports_v3.py`、`tests/test_evidence_contracts.py`（均新增）、`plan/06-acceptance.md`。
**Acceptance criteria:**
- [x] observation/terminal/fence可以表达启动中、未知和未dispatch终结；每消费者有fake端口契约测试。
- [x] CandidateV3/CaseAttempt/Report与必测集合唯一映射可解析；结构合法不等于passed。
- [x] canonical/default展开/hash无环有golden例；绝对/逃逸/重复artifact路径拒绝；hash不包含report生成时间。
**Verification:** `python -m pytest tests/test_ports_v3.py tests/test_evidence_contracts.py -q`。
**本轮执行记录（2026-09-17）:** status=complete；起点 commit `0bb8298`；实现提交 `8974a10`；python=3.12.11；
`pytest tests/test_ports_v3.py tests/test_evidence_contracts.py -q` = 28 passed（ports 16 + evidence 12）；
`pytest tests -m 'not thor' -q` = 299 passed, 1 deselected；`ruff check .` exit 0；`run.py --check-config` 仍为旧四 ID。
新增 `ports_v3.py`：Observation（running/stopped/unknown 加端口、启动操作、子进程事实）、LaunchOperation（starting 无终结时间、
terminal 必须有）、MemorySample 与 C02 `0..2s` freshness 边界、EventRecord（sequence 从 1、UTC aware）、ExecutionRequest
（inline/blob 互斥）、ExecutionHandle、CancelAck/StopAck（只表示命令已处理）、TerminationEvidence（dispatched 必须有完整
实例身份与 execution fence；not_started 不得携带容器身份）、BackendPort/ObserverPort/Clock/EventSink 四个 runtime_checkable
端口及 fake 契约测试（含缺方法必须不满足协议），以及唯一 STOPPED 计算 `stopped_is_proven`（容器不存在+启动操作终结+
子进程退出+端口无监听；未知进程占用端口保持 UNKNOWN）。
新增 `evidence_contracts.py`：CandidateV3/DeviceFact/RuntimeStackFact/PolicyV3/CaseAttempt/CaseFinal/AcceptanceReportV3
严格解析（复用 contracts_v2 的模型/runtime 解析器）；必测集合由候选派生（S01—S06、O01—O06、每模型六类 B、每能力 B），
每 case 恰一个可追溯 final、失败 attempt 永久保留、未知/缺 case/重复最终结论/伪造 summary 拒绝、报告起止必须等于
最早/最晚 attempt 时间；policy 只可比 06-acceptance 门槛更严（1800s、100 请求、0.1 错误率等）；candidate/device/artifact
摘要使用规范 JSON（字段全展开、无自摘要、无 report、无生成时间、无宿主路径），artifact 路径拒绝绝对/逃逸/控制字符/重复。
`plan/06-acceptance.md` 补充 schema v3 来源与 case id 语法。HTTP 接线与真实执行仍待后续任务；本任务不产生硬件证据。

### P04 — 配置v2读入与显式v1迁移（M02）

**Primary owner:** backend；**Collaborators:** arch；**Dependencies:** P03；**Estimated scope:** M。
**Description:** 让check-config理解动态登记及运行策略；转换旧四ID，禁止隐式猜测runtime/hash/预算。
**Files likely touched:** `model_scheduler/config.py`、`model_scheduler/migration_v2.py`（新增）、`model_scheduler/deploy.py`、`tests/test_config.py`、`tests/test_migration_v2.py`（新增）。
**Acceptance criteria:**
- [x] 顶层v2必填schema_version、registration、server、scheduler、resources、storage、gateway、control、blobs；
  仅额外允许顶层派生candidate_sha256（草案null，绑定候选后64hex）；registration复用v2登记解析器。
- [x] 保留旧scheduler/heat/thrash/preload/pinned值；新增C04/C07/C08字段有显式类型/默认；candidate回填字段按C09处理。
- [x] 新命令 `python -m model_scheduler.deploy migrate-v2 --input OLD --inventory INVENTORY --output NEW` 不覆盖OLD/已存在NEW；缺项exit2并输出missing字段JSON，不输出可启动的半成品。
- [x] 完整inventory产出配置并通过check-config；旧四ID不重命名，v1仍可检查，迁移不会静默取消pinned/preload。
**Verification:** `python -m pytest tests/test_config.py tests/test_migration_v2.py -q`；对临时v1 fixture完整/缺项迁移并 `python run.py --config NEW --check-config`。
**本轮执行记录（2026-09-17）:** status=software_only；起点 commit `8d4edcd`；python=3.12.11
（`/Users/monster/.local/share/selfmodelswitch/venv312`）；
`pytest tests/test_config.py tests/test_migration_v2.py -q` = 41 passed（实现前 `AppConfigV2` 与 `migration_v2`
均不存在，两个测试文件收集即失败）；`pytest tests -m 'not thor' -q` = 331 passed, 1 deselected（P03 基线 299）；
`ruff check .` exit 0；`run.py --check-config` 仍输出 `schema_version=1` 与旧四 ID，v1 运行行为未变。
`config.py` 新增 `AppConfigV2`：`registration` 直接交由 `contracts_v2.parse_deployment` 解析（未知 profile、
重复 port、资产基数和未测组合仍由登记解析器裁决），模型 port 不得复用 server port，`scheduler.pinned_models`
必须是 `preload_models` 子集且均已登记；C04 会话、C08 控制与 C07 传输各有显式类型/范围/默认（会话默认值取自
`contracts_v2.SESSION_LIMITS`，`hard_timeout_seconds` 默认 3600s），`control.allowed_uids` 与 `blobs.root`
不给默认——分别属身份与站点决策，`resources.model_budget_bytes` 同样必须显式；新增 `config_digest()`
以规范 JSON 计算且排除顶层派生 `candidate_sha256`，满足 C09 回填无环，并拒绝非 JSON 原生值与非有限数。
`load_config` 从此按 `schema_version` 分派，并保持 `AppConfig`/v1 解析路径零改动，`run.py --check-config`
对 v1/v2 均可用（v2 打印 `schema_version=2` 与动态模型集合）。
新增 `migration_v2.py` 与 `python -m model_scheduler.deploy migrate-v2`：只有 v1 输入可迁移，输出已存在即拒绝；
runtime/profile/镜像/adapter/lock/argv、逐文件 asset size/hash、envelope、timeout、是否实测及其测量材料、
`resources.model_budget_bytes`、`control.allowed_uids`、`blobs.root` 全部来自 inventory，缺一项即 exit 2 并输出
`{"missing": [...], "conflicts": [...]}` JSON 且不落盘；asset path/sha256 与 v1 的 file/sha256 不一致，或
`envelope.max_parallel` 与 v1 `max_concurrency` 不一致，或 inventory 声明 v1 不存在的模型，均为 conflicts；
派生且确定的只有 model_id、capability、port（来自 v1 upstream_url）和 `reserved_bytes`（对 v1 原始值按
`effective_reserved_bytes` 乘一次 `resource_safety_margin`），pinned/preload 原样进入
`scheduler.pinned_models/preload_models`，不在两者之一则视为静默取消。
真实端到端核对：以仓库 `config.yaml` 的临时副本为 v1 fixture，`migrate-v2` 产出同名四 ID 的 v2 配置，
`python run.py --config NEW --check-config` exit 0 且 `schema_version=2`；同一 v1 副本 `--check-config` 仍 exit 0；
缺项 inventory（删 `models.qwen-large.timeout_seconds`、`resources.model_budget_bytes`、`blobs.root` 并篡改
asset sha256）返回 exit 2、报告四项 missing/conflicts 且未生成输出文件；重复输出到同一路径返回 exit 2。
**Files touched:** `model_scheduler/config.py`、`model_scheduler/migration_v2.py`（新增）、
`model_scheduler/deploy.py`、`tests/test_config.py`、`tests/test_migration_v2.py`（新增）。
未解决：per-model v1 `priority`/`ttl_seconds` 未带入 v2（v3 的整形 priority 为服务端 owner 配置，属 C04/P09 范围），
迁移前后并发语义只由 `envelope.max_parallel` 保证；产物为草案配置，`candidate_sha256=null`，未绑定任何候选，
也不构成任何硬件或 B/O 证据。
**同步状态（2026-09-17）:** 本地提交 `cea36ca`（作者/远端核对：`origin` 仍为
`https://github.com/GodSealS/SelfModelSwitch.git`）在本环境无法推送：`git push` 报
`fatal: could not read Username for 'https://github.com': Device not configured`，`credential.helper=osxkeychain`
在此 shell 不可达且不能交互输入凭据。按 [AGENTS.md](../AGENTS.md) 标 **待同步**：该提交尚未发布到共享远端，
目标 clean checkout 未 fast-forward 到同一 SHA，因此本次结论为 `software_only`，不能作为最终交付或 M02 完成依据；
待凭据可用后重新执行 §1.3 第 4—5 步的推送与干净 checkout 同步，不得改用其他远端路径、金钥或向目标复制 tracked 文件绕过。

### P05 — 多资产及挂载身份核验（M02）

**Primary owner:** backend；**Dependencies:** P04；**Estimated scope:** M。
**Description:** 从单文件模型校验改为逐文件全量启动核验和1s metadata监测，保持故障关准入。
**Files likely touched:** `model_scheduler/storage_monitor.py`、`model_scheduler/asset_store.py`（新增）、`tests/test_storage.py`、`tests/test_asset_store.py`（新增）。
**Acceptance criteria:**
- [x] 启动/恢复/发布全量hash；运行期1s核挂载UUID、inode/size/mtime，不每秒重复读所有模型hash。
- [x] directory fd逐级no-follow，前后fstat一致；父symlink、文件替换、根盘同名目录、UUID错配、读取阻塞全部关准入。
- [x] hash总deadline为配置 `storage.verify_timeout_seconds`（默认900s），超时工作不能继续打开新资产；保留UNKNOWN。
**Verification:** `python -m pytest tests/test_storage.py tests/test_asset_store.py -q`；Linux用临时挂载命名空间/临时文件制造路径与超时，不动实际模型盘。到K1。
**本轮执行记录（2026-09-17）:** status=software_only；起点 commit `218e7c0`（P04 提交 `cea36ca`，仍在本地待推送）；
python=3.12.11（`/Users/monster/.local/share/selfmodelswitch/venv312`）；
`pytest tests/test_storage.py tests/test_asset_store.py -q` = 35 passed（实现前 `model_scheduler.asset_store` 不存在，
两文件收集失败）；`pytest tests -m 'not thor' -q` = 359 passed, 1 deselected（P04 基线 331）；
`ruff check .` exit 0；`run.py --check-config` 仍输出 `schema_version=1` 与旧四 ID。
新增 `model_scheduler/asset_store.py`：`AssetStore.verify()` 为启动/恢复/发布的全量通过——单次 `verify_timeout_seconds`
全程截止，逐个资产以 directory fd 逐级 `O_DIRECTORY|O_NOFOLLOW` 打开父目录、`O_NOFOLLOW` 打开文件，
hash 前后两次 fstat（并与打开前 lstat）必须一致，否则判 `asset_replaced`；尺寸/hash 与登记不符分别判
`asset_size_mismatch`/`asset_hash_mismatch`；缺失文件 `asset_unavailable`，symlink 文件 `asset_symlink`，
父级 symlink 与 `..`/绝对路径/控制字符一律 `asset_path_unsafe`；findmnt 的 target 不等于挂载点（含根盘同名目录）
判 `mount_not_found`，UUID/fstype 错配判 `mount_identity_mismatch`，模型目录为 symlink 判 `model_directory_unsafe`。
`AssetStore.observe()` 只核对挂载身份与每个文件的 inode/size/mtime_ns，**不重新 hash**（测试用计数 hasher 证明第二次
观察不再读盘），漂移即 `asset_changed` 并使基线失效，下一次进入入口必须重新全量hash。
`StorageMonitor` 改为把逐-file 工作交给 store：`check()` 首次或登记变化时全量hash、其后回到 metadata 观察，
`verify_all()` 供启动/恢复/发布强制全量；新增 `verify_timeout_seconds`（默认 900=P04 的 `storage.verify_timeout_seconds`）、
`clock`、`hasher` 注入参数，v1 的 `StorageSnapshot`/`ModelFile`/错误名“model_file_changed”等契约仅把“内容变化”的
reason 统一改为 `asset_changed`，其余快照结构、`StorageAdmissionGuard` 与既有 preflight 路径保持不变。
结果不再是布尔：normalize 阶段之外的一切均落到 `AssetState.VERIFIED|FAULT|UNKNOWN`——超时
（含读取卡住时每 chunk 检查一次 deadline）判 **UNKNOWN**、reason `asset_verify_timeout`，
并在打开下一个资产前先检查 deadline，因此超时后不会再打开任何新资产，也不会把旧基线当作可信。
**Files touched:** `model_scheduler/asset_store.py`（新增）、`model_scheduler/storage_monitor.py`、
`tests/test_asset_store.py`（新增）、`tests/test_storage.py`。
未在本机（macOS，Python 3.12）执行的部分：真正的 mount namespace/bind mount 隔离、ext4 上的挂载身份复算，
以及与 P06 起的真实 llama-swap/lab 布置联调——这些按计划在目标 Orin 上以临时挂载和临时文件验证，
本轮不动实际模型盘，也没有任何设备侧证据。另：`verify_timeout_seconds` 目前只在 Monitor 构造参数处生效，
尚未由运行时组合处从配置注入（属 P08/P17 范围）。
**同步状态:** 与 P04 相同——本环境无法向 `origin` 推送（首次 `could not read Username ... Device not configured`，
重试为 `Failed to connect to github.com port 443`），P04/P05 提交 `cea36ca`、`dbdab2a` 仅存在于本地 main，
远端与目标 clean checkout 均未同步，因此本任务结论仍为 `software_only`，不是设备侧通过。

### P06 — 受控runtime启动与动态后端路由（M02）

**Primary owner:** backend；**Collaborators:** arch；**Dependencies:** P05；**Estimated scope:** M。
**Description:** 用profile生成argv，按runtime选择adapter；每个load注册可观察的启动操作。
**Files likely touched:** `model_scheduler/model_runner.py`、`model_scheduler/backend_router.py`、`model_scheduler/runtime_profiles.py`（后二者新增）、`tests/test_model_runner.py`、`tests/test_backend_router.py`（新增）。
**Acceptance criteria:**
- [x] 无固定四ID端口表；主模型/projector按验证资产只读挂载，禁止Docker socket/宿主根、任意entrypoint/extra_args。
- [x] M00参数值可重现；未知flag/profile/能力拒绝；restart=no、无自动换模；不同runtime身份互不串用。
- [x] P06/P15真实执行只在隔离lab维护环境；生产profile必须绑定同镜像/参数/envelope的P21测量和最终B证据。
- [x] 启动操作具有PID/process group、Fence、开始/终结状态；timeout后启动者未退出仍不报告STOPPED。
**Verification:** `python -m pytest tests/test_model_runner.py tests/test_backend_router.py -q`；目标使用无权重测试镜像检查只读挂载、信号转发和身份标签。
**目标核验补记（2026-09-18，目标 `jtzn-desktop`）:** 按验收第 1、2 项在目标上用无权重镜像核验，发现并修复一个真实缺陷。
- 缺陷：本目标 Docker 29 + NVIDIA hook runtime **直接拒绝 `--gpus all`**
  （`invoking the NVIDIA Container Runtime Hook directly (e.g. specifying the docker --gpus flag) is not supported;
  please use ... --runtime=nvidia`），而 P06 渲染器沿用了旧的 `--gpus all`。
- 修复（提交 `9f52ad1`）：`render_container_launch` 新增必填部署输入 `container_runtime`（全串校验 `[a-z0-9][a-z0-9_.-]{0,63}`），
  渲染为 `--runtime=<name>`；结构性复核显式拒绝任何 `--gpus`/`--gpus=`；测试新增 `--gpus all` 作为 runtime 名被拒、
  `--gpus` 不得出现、runtime 名缺失/非法被拒等用例。`pytest tests -m 'not thor' -q` = 380 passed；`ruff check .` exit 0；
  `run.py --check-config` 仍为 v1 四 ID。
- 目标身份核验证据（镜像 `sha256:8e572bb99c19defa9218f8c07b7ab30379040f3ead87d64b3e241213597c1bc8`，即 P06a 首候选 lab 镜像）：
  用渲染出的 argv `docker create`（container `4fa3253c59348a4caa610bb6082ad8f0aea554fe65cbc92e2b1e4e6708f88dc8`）后 `docker inspect`：
  `runtime=nvidia`；六个身份标签（deployment/runtime/profile/model/mode/config-sha256）与渲染值完全一致；
  `Mounts=[{Type:"bind",Source:"/media/jtzn/sandisk-ext4/models",Target:"/models",ReadOnly:true}]` 无其它挂载；
  `PortBindings 127.0.0.1:18081→8080`；`RestartPolicy=no`；`Privileged=false`；`Cmd` 与 profile 渲染的 server 参数逐项一致；
  随后 `docker rm`，**未启动容器、未加载模型**。证据目录 `/home/jtzn/self-model-switch-evidence/image-build-20260917T234436Z/`。
- 尚未核验：信号转发（需 P06b 把 `SupervisedLaunch` 接入 runner 入口）与 `stopped_is_proven` 的目标侧闭环，属 P06b/P07。
**本轮执行记录（2026-09-18）:** status=software_only；起点 commit `41656cc`（P05 记录提交，本地 main 仍领先 origin/main 4 个提交）；
python=3.12.11（`/Users/monster/.local/share/selfmodelswitch/venv312`）；
`pytest tests/test_model_runner.py tests/test_backend_router.py -q` = 28 passed（实现前 `model_scheduler.runtime_profiles`、
`model_scheduler.backend_router` 与 `model_runner.SupervisedLaunch` 均不存在，两个测试文件先收集失败）；
`pytest tests -m 'not thor' -q` = 379 passed, 1 deselected（P05 基线 359）；`ruff check .` exit 0；
`run.py --check-config` 仍输出 `schema_version=1` 与旧四 ID，v1 运行行为未变。
新增 `model_scheduler/runtime_profiles.py`：`render_container_launch(registration, model_id, ...)` 只从同一份已解析登记中
取 model+runtime（不接收二者配对参数），按 profile 的 `flag_sources` 渲染启用 flag，值来源为固定常量（`--load-mode auto`、
`--n-gpu-layers 99`、`--flash-attn auto`、`--no-warmup`、`--no-webui`、容器内 `--host 0.0.0.0`/`--port 8080`）或登记 envelope
（`--parallel`、`--kv-unified-per-slot`/`--ctx-size`、`--image-max-tokens`）；M00 登记的 flag→值集合与
`scripts/m00_envelope_probe.py` 的 `build_server_command` 逐项相等，且不生成 `--no-mmap`。enabled 但无值来源的 flag
（如 `--threads`）、必需 envelope flag 缺失（`--host/--port/--parallel/--kv-unified-per-slot`）、非 vision 模型启用
`--image-max-tokens`、无 `--embedding/--pooling` 值来源的 embeddings/rerank、registration-only profile（`hf-sharded-v1`）
全部拒绝渲染。挂载只有 `type=bind,src=<model_directory>,dst=/models,readonly`，容器路径由 asset 逐文件生成；
`--entrypoint`/`--privileged`/`--volume`/docker.sock/非只读 mount 在渲染后被结构性复核拒绝。loopback 端口取
`ModelSpec.port`（测试用 10077 证明未走旧四 ID 端口表），标签含 deployment/runtime/profile/model/mode/config-sha256，
镜像取该 runtime 的 `image_digest`，容器名前缀 `sms-<deployment>-<model>`；`restart=no`、前台子进程、无自动换模入口。
production 分支要求 `require_production_openable`（measured=true 且绑定 measurement_ref 与 physical_resident_peak_bytes），
lab 分支必须显式传入临时 budget 并保留 `mode=lab` 标签；生产携带临时 budget、未知 mode、相对/`/`/含逗号模型目录、
非法 deployment id 与 config 摘要均拒绝。
新增 `model_scheduler/backend_router.py`：`BackendRouter(runtimes, models)` 只接受已登记 runtime，注册时执行
`require_startable_profile`，拒绝重复 runtime、声明了其他 runtime 身份的 adapter、复用到第二个 runtime 的同一 adapter 对象
以及不满足 `BackendPort` 的对象；`backend_for(model_id)` 只返回该模型自身 runtime 的 adapter，无 adapter 时显式拒绝而
不回退到其他 runtime。
`model_runner.py` 新增 `SupervisedLaunch`：以注入的 `popen`/`monotonic`/`killpg` 启动一次性前台子进程，产出的
`LaunchOperation` 带 operation_id/Fence/PID/进程组（`start_new_session` 时 pgid=pid）、开始时间与 starting 状态，
退出码决定 completed/failed 并只写一次终结时间；`wait(timeout)` 超时返回 None 且保持 starting，因此
`stopped_is_proven` 在启动者未退出时不可能为真（测试用超时→未终结→子进程退出后才可证明的顺序验证）；
`terminate()`/`signal_group()` 对记录下的进程组发信号，未启动时拒绝。旧 `docker_run_argv`、
`run_child_with_signal_forwarding`、`docker_stop_argv` 等 v1 入口零改动。
未解决/边界：`model_runner._PORTS` 固定四 ID 表仍只服务于 v1 `docker_run_argv` 兼容路径（P06b 要求旧 schema1 入口继续
通过原测试），新动态渲染器不读取它；`--batch-size`/`--ubatch-size`/`--threads` 有意保留为"无值来源"以拒绝猜测，
embeddings/rerank 需各自 profile、flag 值来源与 fixture 后才能启动。本轮未接线路由与渲染器到 runtime/deploy 入口
（属 P06b/P07/P16/P17），未构建镜像、未启动模型、未产生任何设备或 B 证据；production 正式门禁仍由 P26/P31 实现。
**同步状态（2026-09-18）:** 与 P04/P05 相同。本任务提交 `26e9d6c`（`expected_sha=26e9d6c9e8a41610c8d666847f6b9cf2155fdbe0`）后尝试推送
`origin`（`https://github.com/GodSealS/SelfModelSwitch.git`）失败：`fatal: could not read Username for 'https://github.com':
Device not configured`；`git ls-remote origin refs/heads/main` 仍为 `8d4edcd496423cbeaefe2e94d2f5cd75080d7823`，
即本地 main 有 5 个提交（`cea36ca`、`dbdab2a`、`218e7c0`、`41656cc`、`26e9d6c`）未发布。只读 SSH 核对目标
（`jtzn-desktop`，L4T R36.4.7，aarch64）：`/home/jtzn/SelfModelSwitch` 工作区干净且 HEAD=`8d4edcd4…`，与远端一致、
无法 fast-forward 到本任务提交，因此本轮不执行目标 lab 测试，结论为 `software_only`，不是设备侧通过。
待凭据可用后按 §1.3 第 4—5 步重新推送并同步，不得改用其他远端路径或向目标复制 tracked 文件绕过。

### P06a — 固定llama-swap真实控制契约（M02）

**Primary owner:** backend；**Dependencies:** P06；**Estimated scope:** M。
**Description:** 将当前明确不完整的v217 fixture补齐，并实现run.py要求的CONTROL_CONTRACT；此任务必须早于真实服务闭环。
**Files likely touched:** `model_scheduler/llama_swap_contract.py`（新增）、`tests/fixtures/llama_swap_contract.json`、`tests/test_llama_swap_fixture.py`、`scripts/capture_control_fixture.py`、`tests/test_capture_control_fixture.py`（后二者新增）。
**Acceptance criteria:**
- [x] 核实固定ARM64二进制版本/hash；受控维护探测分别保存running空/非空、真实模型load、unload的request/status/content-type/body与实例证据。
- [x] `python scripts/capture_control_fixture.py --config V2_CONFIG --output EVIDENCE_DIR` 仅访问配置中的loopback端点和登记模型，失败保留材料，不下载资产。
  探测脚本直接调用P06 profile/runner，在独占临时目录生成探测用llama-swap配置与manifest，
  包含部署/镜像/资产/端口/argv；不依赖P06b或v3生产renderer，不写系统安装目录。
- [x] CONTROL_CONTRACT的load路径/响应解析来自真实fixture，现有load_probe=404不能充当成功；未停止不标通过。
- [x] 更新当前“fixture必须不完整”的测试为真实协议正反例；推理流量仍绕过llama-swap自动路由。
**Verification:** `python -m pytest tests/test_llama_swap_fixture.py tests/test_llama_swap_client.py tests/test_capture_control_fixture.py -q`；目标受控探测并记录停止证据。
**本轮执行记录（2026-09-18）:** status=blocked（未开始实现，先做只读材料核对）；起点 commit `2a9ec8b`（P06 记录提交）。
已核实事实（本机 + 目标只读 SSH，未改动目标）：
- 本机：`origin` 取回仍为 `https://github.com/GodSealS/SelfModelSwitch.git`；HTTPS 写通道不可用（`git push` 报
  `could not read Username ... Device not configured`），但 GitHub **SSH 认证已配置且可用**（`ssh -T git@github.com` → `Hi GodSealS!`）。
  已按用户决定把 `remote.origin.pushurl` 设为 `git@github.com:GodSealS/SelfModelSwitch.git`（取回 URL 不变），推送成功：
  `8d4edcd..6ad8b89`，并以 `git ls-remote` 核对远端 main = 本地 HEAD = `6ad8b8912c479dbb57ff3bd3776c8cbce9543d41`。
- 目标 `jtzn@192.168.55.1`：`jtzn-desktop`，L4T R36.4.7，aarch64；`/home/jtzn/SelfModelSwitch` 干净且 HEAD=`8d4edcd4…`（与远端一致，无法 fast-forward）；
  `python3`=3.10.12（`/opt/self-model-switch/toolchains/` 另有 cpython-3.12.14）；docker CLI/Server 29.2.1 可用但 **`docker images` 为空**；
  `/opt/self-model-switch` 下只有 `probes/llama.cpp-4bc272fd729bd094c0422e4b8353da8d2fec91f8` 与 `toolchains/`，**全盘未安装 llama-swap**
  （无二进制、无 `llama-swap_217_linux_arm64.tar.gz` 归档、无 systemd unit、无 `/etc/llama-swap`、无 `/etc/self-model-switch`）；
  模型盘 `/media/jtzn/sandisk-ext4`（`/dev/sda1`，ext4）已挂载；根盘余量 21G；`/home/jtzn/self-model-switch-evidence/` 保留 M00 各轮材料。
缺少的具体材料与恢复动作：
1. **共享远端写权限**（阻塞所有任务的"已发布提交 → 目标 fast-forward → 同 SHA 证据"要求）：由仓库所有者在开发机完成一次 GitHub 认证
   （例如在其自身终端执行一次 `git push` 让 keychain 记录，或配置 token/credential helper）。禁止把凭据写入 URL、脚本或命令历史。
2. **固定 ARM64 llama-swap 发行物**（验收第 1 项）：fixture 记录的 `llama-swap_217_linux_arm64.tar.gz`（sha256 `36c58c…`）在目标上不存在。
   需由维护方按受控流程在目标安装该固定版本，记录版本字符串与二进制 sha256；工具只核对、不自动下载资产。
3. **首候选容器镜像 digest**（本任务探测 manifest 与 P06b lab 渲染的输入；§6 表列为"P06/P15受控runtime构建产物"）：目标无任何镜像。
   需先决定承载上游的固定镜像并构建（含依赖 lock/adapter hash）；若改以已核验的原生 `/opt/self-model-switch/probes/llama.cpp-4bc272…/llama-server`
   作为受控 lab 上游，必须同步修订本任务与 P06b 的 manifest/身份约定，不能两套并存。
4. **独占维护窗口**（探测需真实 load/unload 与停止证据）与后续 P29 切换验收所需的第二个真实模型。
未开始实现的原因：本任务核心交付（`CONTROL_CONTRACT` 的 load 路径与响应解析）必须来自真实 fixture，计划已固定
"现有 `load_probe=404` 不能充当成功"且"不得延后补实现"；在材料 1—3 缺失时编写 contract 或回填 fixture 等于伪造证据，
因此本轮只记录阻塞与恢复动作，不产出任何 contract/fixture 变更。
解除阻塞后的立即动作：① 实现 `scripts/capture_control_fixture.py` 与 `tests/test_capture_control_fixture.py`（仅访问配置内 loopback 端点、
失败保留材料、不下载资产、不写系统安装目录），推送并同步目标；② 目标受控维护环境执行探测，保存 running 空/非空、真实模型 load、
unload 的 request/status/content-type/body 与实例证据；③ 按原始材料回填 fixture 与 `model_scheduler/llama_swap_contract.py`，
并把"fixture 必须不完整"的测试改为真实协议正反例。
**材料核对更新（2026-09-18，第二次只读核对 + 交付链恢复）:**
- **交付链已恢复并验证**：SSH 推送后远端 main = `6ad8b89…`。目标 `origin` 实为其本机裸仓库镜像
  `/home/jtzn/git/SelfModelSwitch.git`（无上游、此前停在 `8d4edcd4…`，与 AGENTS.md 的"两端确认远端 URL"不一致）；本轮把**已发布**的
  同一提交以 git 传输推入该镜像（不复制 tracked 文件），目标 checkout 再 `git fetch` + `git pull --ff-only`，
  `actual_sha=6ad8b891…`、工作区干净（`TARGET_SYNC_VERIFIED`），并可看到 P06 新模块文件。该镜像的更新时间点/责任方需在 M02 检查点复核。
- **目标环境事实**（只读）：docker CLI/Server 29.2.1，`/etc/docker/daemon.json` 已注册 `nvidia` runtime（`nvidia-container-cli` 1.16.2）；
  根盘余 21G、模型盘余 385G；**无外网**（`https://github.com` 与 `registry-1.docker.io` 均超时，无 proxy env）；无 Go 工具链；
  `/opt/self-model-switch` 仅 `probes/llama.cpp-4bc272…` 与 `toolchains/cpython-3.12.14`。
- **首候选镜像（按 §6，用户已选择构建固定镜像）**：因两机均无外网、目标无任何基础镜像，唯一可行路径是**在目标本地离线构建**
  `FROM scratch` 镜像：内容 = 已核验 M00 runtime 的 `RUNTIME-SHA256` 十项文件（逐项 `sha256sum -c` 通过，未列出的常规文件为零）
  \+ 宿主 glibc/libstdc++/openssl/libgomp（路径按 `ldd` 的 NEEDED 布局 `/lib/aarch64-linux-gnu/…`，含 `/lib/ld-linux-aarch64.so.1`），
  `ENTRYPOINT ["/opt/llama-cpp/llama-server"]`，`--network=none` 构建；冒烟项为 `--version`、`--gpus all --list-devices`、
  只读挂载 `/media/jtzn/sandisk-ext4/models` 后被指向 `/models/<缺失文件>` 的报错。构建上下文与命令已定稿，
  但目标侧构建命令本轮三次因**交互审批超时被取消**（`Execution Cancelled: permission request timed out`），因此
  **image digest 尚未产生**，P06a 探测 manifest 与 P06b lab 仍缺该输入。目标 checkout 已同步到 `397a401`。
  可复现命令（在目标上执行，全部写入 `/home/jtzn/` 下，无 sudo、无网络）：

  ```bash
  R=/opt/self-model-switch/probes/llama.cpp-4bc272fd729bd094c0422e4b8353da8d2fec91f8
  B=/home/jtzn/self-model-switch-build/runtime-image-4bc272f-v1
  mkdir -p "$B/rootfs/opt/llama-cpp" "$B/rootfs/lib/aarch64-linux-gnu" \
           "$B/rootfs/usr/lib/aarch64-linux-gnu" "$B/rootfs/usr/local/cuda/targets/aarch64-linux/lib" "$B/rootfs/tmp"
  sha256sum -c "$R/RUNTIME-SHA256"                      # 必须全部 OK
  cp -a "$R/." "$B/rootfs/opt/llama-cpp/"
  rm -f "$B/rootfs/opt/llama-cpp/RUNTIME-SHA256" "$B/rootfs/opt/llama-cpp/BUILD-METADATA"
  for f in libc.so.6 libm.so.6 libstdc++.so.6.0.30 libgcc_s.so.1 libssl.so.3 libcrypto.so.3 \
           libgomp.so.1.0.0 libdl.so.2 libpthread.so.0 librt.so.1; do
    cp -a "/usr/lib/aarch64-linux-gnu/$f" "$B/rootfs/lib/aarch64-linux-gnu/$f"; done
  cp -aL /usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1 "$B/rootfs/lib/ld-linux-aarch64.so.1"
  ln -sf libstdc++.so.6.0.30 "$B/rootfs/lib/aarch64-linux-gnu/libstdc++.so.6"
  ln -sf libgomp.so.1.0.0 "$B/rootfs/lib/aarch64-linux-gnu/libgomp.so.1"
  printf 'FROM scratch\nCOPY rootfs/ /\nENV LD_LIBRARY_PATH=/opt/llama-cpp\nENTRYPOINT ["/opt/llama-cpp/llama-server"]\n' > "$B/Dockerfile"
  docker build --network=none -t sms-llama-cpp:4bc272f "$B"
  docker inspect --format '{{.Id}}' sms-llama-cpp:4bc272f      # 记录 image digest
  docker run --rm sms-llama-cpp:4bc272f --version
  docker run --rm --gpus all sms-llama-cpp:4bc272f --list-devices
  docker run --rm --gpus all --mount type=bind,src=/media/jtzn/sandisk-ext4/models,dst=/models,readonly \
    sms-llama-cpp:4bc272f --model /models/does-not-exist.gguf  # 证明只读挂载可见
  ```

  镜像内容必须同时满足：`RUNTIME-SHA256` 中的每一项逐项通过，且 runtime 目录内不存在未列入该清单的常规文件；
  `--version`、`--gpus all --list-devices`、只读模型挂载三项冒烟全部通过后才可把 digest 作为 P06a 探测/P06b lab 的输入。
- **llama-swap 发行物：本环境无法取得**。开发机与目标均无 HTTPS 出口（`curl https://github.com` / release 下载均超时；
  GitHub 不通过 SSH 提供 release 资产），两机本地与 Spotlight 缓存内均无 `llama-swap_217_linux_arm64.tar.gz`，目标无 Go 无法自建。
  恢复动作：由仓库所有者提供该固定发行物（例如放至开发机 `~/Downloads/llama-swap_217_linux_arm64.tar.gz`），
  我将按 fixture 记录的 sha256 `36c58c…` 核对后再带入目标安装与探测；在它到位前 P06a 保持 `blocked`，P06b—P31 按 §3 硬前置不动。
**材料到位与探测前置条件（2026-09-18，用户在目标机授权下载与配置）:**
- **固定 llama-swap v217 已核实并安装**：目标机直接下载官方发行物 `llama-swap_217_linux_arm64.tar.gz`（6,805,577 B），
  sha256 = `36c58cf69f1422e999acba0b7bff0d47d5b955cb95e8d3875c461d814a74cc29`，**与 fixture 记录逐位一致**；
  二进制为静态链接 aarch64，`--version` = `217 (636b53e70ff7c834e92a97ef2bb556ee60ca2f85)`，sha256 `0f86f5869d167b406c9e81bdc4b268f23dd96a4983ea57b8f2b561ac8bb82e80`，
  已安装到 `/opt/self-model-switch/bin/llama-swap`（root:root 0755）。证据：
  `/home/jtzn/self-model-switch-evidence/llama-swap-217-<UTC>/`（发行物副本、sha256sums、version.txt、material.json）。
  注：此前"两机无外网"的判断**不成立**——实测目标机 DNS 正常且 443 可达（失败源于 curl 走 IPv6/中间盒），
  开发机的 HTTPS 出口仍不稳定，故下载在目标机执行。
- **首候选 lab 镜像已在目标本地离线构建**（`FROM scratch`、`docker build --network=none`）：
  `image_id=sha256:8e572bb99c19defa9218f8c07b7ab30379040f3ead87d64b3e241213597c1bc8`，197,663,169 B，rootfs 27 个文件，
  rootfs manifest sha256 `008c2ef5a0016da4d3d429247b7f87b881abbbc0c3dab01aafe6553f887b8887`；
  内容 = M00 `RUNTIME-SHA256` 十项已核验文件（无未列入清单的常规文件）+ 宿主 glibc/openssl/libgomp
  + `/sbin/ldconfig.real`（NVIDIA CSV hook 的注入前提）+ CUDA 用户态 12.6（`libcudart`/`libcublas`/`libcublasLt`，与 L4T R36.4.7 同源）。
  GPU 通路目标实测结论：`--gpus all` 不被支持、必须 `--runtime=nvidia`；`docker run --rm --runtime=nvidia sms-llama-cpp:4bc272f
  --list-devices` → `CUDA0: Orin (62840 MiB, 57700 MiB free)`；`--runtime=nvidia` 下只读模型挂载与 `--model` 解析正常。
  证据：`/home/jtzn/self-model-switch-evidence/image-build-20260917T234436Z/`（image.json、image-inspect.json、rootfs-files.txt）。
- **仍待办**：`scripts/capture_control_fixture.py` 与 `tests/test_capture_control_fixture.py`（本轮尚未实现），
  随后在目标受控维护环境执行探测并回填 fixture 与 `model_scheduler/llama_swap_contract.py`。因此 P06a 仍为 `blocked`（未完成），
  但已不再是"缺材料"，而是"未实施 + 未探测"。
**本轮执行记录（2026-09-18）:** status=complete（目标受控探测已捕获，契约按原始材料生成）。
实现与提交：`550370e`（采集工具）、`1c30d21`（`--listen` 从命令行取，弃用 `logRequests`）、`739c83f`（按实测解析 `/running` 对象数组）、
`02aa359`（fixture 回填 + `model_scheduler/llama_swap_contract.py` + 测试改造）；python=3.12.11（开发机）/3.12.14（目标 toolchain）；
`pytest tests/test_llama_swap_fixture.py tests/test_llama_swap_client.py tests/test_capture_control_fixture.py -q` = 21 passed；
`pytest tests -m 'not thor' -q` = 393 passed, 1 deselected；`ruff check .` exit 0；`run.py --check-config` 仍为 v1 四 ID
（真实 v2 服务仍未接线，属 P06b/P17）。
探测（目标受控维护，`scripts/capture_control_fixture.py`）：证据目录
`/home/jtzn/self-model-switch-evidence/llama-swap-control-20260918T002718Z/`（capture.json、manifest.json、llama-swap.probe.yaml、
llama-swap.log）；`result=captured`，停止证据 `exit_code=0`、`port_in_use=false`。实测协议事实（已写入 fixture 与契约）：
1. `/health` → `200 text/plain; charset=utf-8`，body `OK`；`/running` 空态 → `200 application/json`，body `{"running":[]}`；
2. **load 路径 = `/upstream/{model_id}/health`**（首个请求启动上游并阻塞至就绪，实测 5.26s；冷加载更长），
   响应 `200 application/json; charset=utf-8`，body `{"status":"ok"}`；
3. `/running` 非空态是**上游对象数组**（键 `cmd/description/model/name/proxy/state/ttl`，`state=ready`、`proxy=http://localhost:5800`），
   旧的"字符串数组"假设被推翻；
4. `POST /api/models/unload/{model_id}` → `200 text/plain; charset=utf-8`，body `OK`（实测 0.926s）；
5. 旧 `GET /props?model={model_id}` → **404**，作为 `rejected_load_probe` 负例保留，`counts_as_success=false`，不得当作成功；
6. v217 的监听地址来自命令行 `--listen`（配置里的 `port` 键被忽略），`logRequests` 已弃用不得写入；
   llama-swap 以 `${PORT}` 替换模型命令中的发布端口并代理到该端口（探测中为 5800）。
失败轮保留：`llama-swap-control-20260918T002328Z`（探测到达前未就绪：`port` 键被忽略 + 无就绪等待）、
`…T002427Z`（就绪等待缺失）、`…T002524Z`（`/running` 解析按旧假设），三轮材料均在，未删除。
未解决：fixture 的 `running_loaded.body_sha256` 记为 `not_recorded_in_this_fixture_revision`（该轮只保留了字节长度与条目结构，
未保留完整 body 的 hash），如需完整 body hash 应在下一次受控探测中补记；"推理流量绕过 llama-swap 自动路由"由 P15/P19 的
gateway/adapter 接线保证，本任务不生成任何自动换模参数，未在此声明已验收。

### P06b — 动态lab渲染与runner入口接线（M02）

**Primary owner:** backend；**Dependencies:** P06a；**Estimated scope:** M。
**Description:** 提前交付后续目标测试所需的lab配置/启动入口，避免P16必须等待P26生产renderer的隐性循环。
**Files likely touched:** `model_scheduler/deploy.py`、`deploy/model-runner.py`、`model_scheduler/runtime.py`、`tests/test_deploy_render.py`、`tests/test_runtime.py`。
**Acceptance criteria:**
- [x] 新增 `python -m model_scheduler.deploy render --config V2_CONFIG --mode lab --output LAB_DIR`，生成动态scheduler/llama-swap/runner manifest；不覆盖非空输出。
- [x] lab manifest绑定配置hash/runtime/profile/资产，显式标记lab；其digest只作测试实例身份，不是production candidate。
- [x] deploy/model-runner与runtime能加载多个动态ID/profile、只读资产及P06a控制契约；旧schema1入口仍通过原测试。
- [x] lab只允许隔离维护启动，有临时预算和现场身份检查；缺测不能通过production分支，P26再实现正式门禁。
**Verification:** `python -m pytest tests/test_deploy_render.py tests/test_runtime.py tests/test_model_runner.py -q`；目标lab配置启动/停止测试实例。到K1a。
**本轮执行记录（2026-09-18）:** status=complete；实现提交 `65877c5`（起点 `4a5733b`）；python=3.12.11（开发机）/3.12.14（目标 lab venv）；
`pytest tests/test_deploy_render.py tests/test_runtime.py tests/test_model_runner.py -q` = 49 passed；`pytest tests -m 'not thor' -q` = 397 passed, 1 deselected；
`ruff check .` exit 0；`run.py --check-config` 仍为 v1 四 ID（v2 服务接线属 P17）；旧 schema1 入口与其原测试全部保持通过。
- `model_scheduler/deploy.py` 新增 `render_lab(config_path, output, deployment_id, container_runtime, mode="lab")`：解析 schema-v2 配置 → 用 P06 渲染器为每个登记模型生成 argv
  → 写出 `manifest.json`（`mode=lab`、`lab_only=true`、`config_sha256`=配置文件字节摘要、`registration_digest`=规范化登记摘要、
  `runtimes{profile_id,image_digest}`、逐模型 `{container_name,registered_port,argv,argv_sha256,probe_argv,image_digest,runtime_id,profile_id,measured,assets}`）、
  `scheduler-v2.json`（配置副本）与 `llama-swap.yaml`（`${PORT}` 探测形态）；非空输出目录拒绝。CLI 为计划规定的
  `python -m model_scheduler.deploy render --config V2 --mode lab --output LAB_DIR --deployment-id ID [--container-runtime nvidia]`，
  旧 `--input` 路径不变；`--mode production` + `--config` 直接拒绝（正式门禁属 P26）。
- `model_scheduler/runtime.py` 新增 `load_lab_manifest(path, config_path=...)` 与 `lab_launch_argv(manifest, model_id)`：强制 `mode=lab`/`lab_only`、
  逐模型校验 `argv_sha256`、可选与配置文件字节摘要比对；未知模型或非 lab 渲染一律拒绝。它是交叉校验器，不替代 P08 的动态账本。
- `deploy/model-runner.py` 新增 lab 分支：`--lab-manifest` + `--temporary-budget-bytes`（缺失即 `parser.error` 退出 2）+ 可选 `--config-sha256`；
  `_lab_start` 先要求可导入的 P06a `CONTROL_CONTRACT`（缺失即拒绝），再校验 manifest、容器名与预算，然后用 P06 的 `SupervisedLaunch` 前台受监督启动；
  `_lab_stop` 走既有身份标签核验的 `docker stop`。旧入口仍只接受固定四 ID。
- **目标核验（`jtzn-desktop`）**：先建 lab venv `/home/jtzn/self-model-switch-build/venv312`（3.12.14 + `requirements.txt`，含 PyYAML/httpx/psutil），
  然后 ①`render --mode lab` 成功；②`deploy/model-runner.py start qwen25vl-7b-q4 --lab-manifest … --temporary-budget-bytes 16000000000`
  启动真实实例：容器 `sms-lab-orin-qwen25vl-7b-q4` Up、llama-server 在容器内 `listening on http://0.0.0.0:8080`（模型已加载）；
  ③`deploy/model-runner.py stop … --lab-manifest …` 返回 0 → 容器消失（计数 0）、端口 18081 无监听、llama-server 日志 `cleaning up before exit...`。
  证据目录 `/home/jtzn/self-model-switch-evidence/lab-run-20260918T004852Z/`（lab manifest、runner-start.log、lab-run.json）。
- 未解决：目标 lab venv 的依赖安装是本轮新增的环境供给（P27 需把它规范成发布 venv 与锁文件安装）；lab 启动为前台受监督进程
  （供 llama-swap 拉起并代理），手工维护时的后台化由操作者负责；`SupervisedLaunch` 仍需在 P07/P16 接入 observer 与停止证据闭环。到 K1a。

### P07 — 独立停止观察与启动恢复（M02）

**Primary owner:** backend；**Dependencies:** P06b；**Estimated scope:** M。
**Description:** 将C03停止条件接入observer/recovery和组合入口，消除启动时盲目bootstrap。
**Files likely touched:** `model_scheduler/process_observer.py`、`model_scheduler/control_recovery.py`、`model_scheduler/runtime.py`、`tests/test_process_observer.py`、`tests/integration/test_process_lifecycle.py`。
**Acceptance criteria:**
- [x] stopped容器未删除也可凭exit/启动者退出/空端口证明；Docker失败、未知占端口返回UNKNOWN。
- [x] 启动先关准入、核验旧deployment实例并以container ID清理；跨deployment不触碰；旧identity不能停止新实例。
- [x] 人为延迟启动到超时之后：停止证据不得提前成功；后续观察实际停止才能清账。
**Verification:** `python -m pytest tests/test_process_observer.py tests/test_control_recovery_port.py tests/integration/test_process_lifecycle.py -q`；目标保留容器ID、PID、端口和退出材料。到CP1。

**本轮执行记录（2026-09-18）:** status=complete；起点 commit `22934b4`（P06b 记录提交）；实现提交 `296577a`；
python=3.12.11（开发机 `/Users/monster/.local/share/selfmodelswitch/venv312`）/3.12.14（目标 lab venv）。
`pytest tests/test_process_observer.py tests/test_control_recovery_port.py tests/integration/test_process_lifecycle.py -q` = 36 passed（实现前三个文件收集即失败）；
`pytest tests -m 'not thor' -q` = 421 passed, 1 deselected（P06b 基线 397）；`ruff check .` exit 0；`run.py --check-config` 仍输出 `schema_version=1` 与旧四 ID。
新增 `process_observer.DockerProcessObserver`：按 `deployment`+`model` 双 label 过滤 `docker ps --all --quiet --no-trunc`，逐 id `docker inspect` 严格解析
（`parse_inspect_payload` 拒绝非数组、缺 id、缺 image、非字符串 label、非 bool Running、缺 Status/ExitCode/StartedAt）；
RUNNING 需容器 running、登记标签齐全（deployment/model/runtime/config-sha256 等）且固定 loopback 端口 listening；
STOPPED 只来自 `ports_v3.stopped_is_proven`（容器已退出或不存在、启动操作终结、启动者进程不再存在、端口 closed）；
docker 列举/解析失败、重复实例、端口 listening 或 unknown、启动操作仍 starting 一律保持 UNKNOWN，`container_absent` 只由成功列举得出。
`canonical_utc` 将 docker 的纳秒 `StartedAt` 规范化成同一 UTC 拼写（`…Z`、去尾零；`+08:00` 等价实例同值），供 `InstanceIdentity` 比较。
新增 `control_recovery.DeploymentRecovery`：`reconcile(close_admission, deadline)` **先**调用关准入回调再执行任何 docker I/O，
按 deployment label 列举本 deployment 容器，逐个核对 inspect 的 `Id` 与 deployment label 后才 `docker stop --time 30 <完整容器ID>`（从不按名字），
再 inspect 复核 not-running；跨 deployment（label 不符）或 stop 失败/无法复核一律进 `remaining_container_ids` 且 `ok=False`；
`stop_instance(identity)` 仅在容器 ID、deployment/model label、`StartedAt` 全部匹配时停止，旧 identity 返回 `stale_identity` 且不发 stop，
`No such object` 结构化 not-found 才算"已不存在"，其他 docker 失败返回 `docker_unavailable`。
新增 `runtime.reconcile_startup(book, recovery, observers, deadline)`：`book.begin_recovery()` 关闭准入 → reconcile 旧实例 → 逐模型观察，
仅当容器级清理成功且每个模型都被独立观察为 STOPPED 时才 `finish_recovery` 清账；`build_scheduler(confirmed_stopped=…)` 取代盲目 bootstrap
（`None` 保留 v1 行为；传入集合时只 bootstrap 有停止证据的模型，其余保持 UNKNOWN，任何 load 都无法准入）。
**目标核验（`jtzn-desktop`，L4T R36.4.7，干净 checkout fast-forward 到 `296577a`）**：证据目录 `/home/jtzn/self-model-switch-evidence/p07-20260918T0140Z/`
（`p07-evidence.json`、`exited-inspect.json`、`running-inspect-after-stop.json`、`container-logs.txt`、探测脚本）：
1. deployment `p07-evidence-exited` 的容器 `5a77ca2e…`（`docker run --runtime=nvidia … --version`，不 `--rm`）以 ExitCode 0 退出后保留 → 观察 `stopped`（port closed、启动者 absent）；
2. deployment `p07-evidence-live` 的真实实例 `5d2e3c92…`（镜像 `sha256:8e572bb99c19defa9218f8c07b7ab30379040f3ead87d64b3e241213597c1bc8`，StartedAt `2026-09-18T01:44:54.625625682Z`，18098→8080）
   加载 `Qwen_Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf`（sha256 `3f451333…`，与本记录前核对一致）后 → 观察 `running`（port listening），instance identity 完整（container/started_at/runtime/candidate_digest/image_digest）；
3. 同容器 ID 但 `StartedAt` 提前的旧 identity → `stale_identity`、`accepted=false`，容器仍在运行（未发 stop）；
4. 真实 identity `stop_instance` → 只按容器 ID `docker stop`，前台启动者退出 143（SIGTERM），容器保留为 `exited (143)`、18098 端口释放 → 再观察 `stopped`；
5. `reconcile` → `ok=true`、`stopped_container_ids=[5d2e3c92…]`、关准入回调先于 docker 调用，另一 deployment 的容器 `5a77ca2e…` 未被触碰（不在停止集合，Id 不变）；
6. 收尾 `docker info ContainersRunning=0`、`docker ps -q` 为空；MemAvailable 52576829440 B → 52582227968 B（+5.4 MiB，未泄漏）。
目标全量 `pytest tests -m 'not thor' -q` = 418 passed、3 failed：均为 `tests/test_release.py` 硬编码 `.venv/bin/python`
（目标 checkout 的 3.12 环境在 `/home/jtzn/self-model-switch-build/venv312`），属 P28 的发布/CI 范围，与 P07 无关。
未解决/边界：①目标探测脚本未登记启动操作，因此容器创建前的首个采样读作 `stopped`（`readiness_observations` 首项为 `stopped`）；
受管路径由 P06 `SupervisedLaunch` 消除，`tests/integration/test_process_lifecycle.py::test_a_timed_out_launch_never_clears_the_books_early`
以真实子进程证明"超时后仍 starting → 不得 STOPPED"，启动期另有 deployment 独占（P17 instance_lock）兜底；
②本任务触碰 6 个文件（多出 `tests/test_control_recovery_port.py`，因为 `DeploymentRecovery` 的单元测试落在其既有文件），略超"约 5 个"；
③v1 `ProcessObserver` 与根 helper `ControlRecoveryClient` 保留未改，旧四 ID 运行行为不变；新 observer/recovery 尚未接入 HTTP/控制路由与 v2 composition（P17/P18）。到 CP1。

### P08 — 动态模型账本和双内存准入（M03）

**Primary owner:** backend；**Dependencies:** P07；**Estimated scope:** M。
**Description:** 统一legacy/v2内部规格并落实C02；Book保持纯同步状态，资源采样独立注入。
**Files likely touched:** `model_scheduler/model_registry.py`、`model_scheduler/resource_monitor.py`、`model_scheduler/runtime.py`、`tests/test_registry.py`、`tests/test_resources.py`。
**Acceptance criteria:**
- [ ] v1原行为不变，v2margin只算一次；UNKNOWN/ERROR/未终结启动计入两套账本。
- [ ] B/F/physical/新模型/READY的等号和1-byte边界、样本2s边界及未来时间全部测试。
- [ ] STOPPED后才释放模型预算；低F关新执行、触发清理及10s重新采样；不把线程cancel当工作已停。
**Verification:** `python -m pytest tests/test_registry.py tests/test_resources.py tests/test_runtime.py -q`。

### P09 — 公平独占会话准入（M03）

**Primary owner:** backend；**Dependencies:** P08；**Estimated scope:** M。
**Description:** 将会话队列、冻结、drain、加载和槽管理接入同一调度权威。
**Files likely touched:** `model_scheduler/session_manager.py`（新增）、`model_scheduler/scheduler.py`、`model_scheduler/request_queue.py`、`tests/test_sessions.py`（新增）、`tests/test_queue.py`。
**Acceptance criteria:**
- [ ] preparing phase/优先级aging/128容量/1800s/30s退让符合C04，重试不刷新序列和截止。
- [ ] 同锁决定交互与session授予；持有交互lease不会被杀；pinned/preload冲突409；等待繁忙health仍200。
- [ ] 生命周期IO串行且锁外；推理槽按max_parallel；两个客户端竞争不能同时ACTIVE。
**Verification:** `python -m pytest tests/test_sessions.py tests/test_queue.py tests/test_scheduler_lifecycle.py -q`；用fake clock和barrier重现竞争，不靠sleep碰运气。

### P10 — TTL、关闭、取消与Fence清账（M03）

**Primary owner:** backend；**Dependencies:** P09；**Estimated scope:** M。
**Description:** 接入session/execution清理状态，修正当前release在ABORTED时先删lease的风险。
**Files likely touched:** `model_scheduler/session_manager.py`、`model_scheduler/scheduler.py`、`model_scheduler/model_registry.py`、`tests/test_sessions.py`、`tests/test_cancellation.py`。
**Acceptance criteria:**
- [ ] 到期瞬间submit/heartbeat拒绝；10/30/60s清理、5s对账；BLOCKED保槽/预算且health503。
- [ ] 断线/异常/取消不提前释放执行lease；其他有效lease完成前不杀共享实例。
- [ ] 旧boot/generation/operation/attempt晚回写无副作用；close幂等；恢复重hash、不自动推理重放。
**Verification:** `python -m pytest tests/test_sessions.py tests/test_cancellation.py tests/test_scheduler_lifecycle.py -q`；覆盖close×heartbeat×late load三方竞争。到K2。

### P11 — Blob上传、读取及租约（M04）

**Primary owner:** backend；**Dependencies:** P03；**Estimated scope:** M。
**Description:** 先交付可独立验证的BlobStore端口，应用C07 owner、配额和文件规则。
**Files likely touched:** `model_scheduler/blob_store.py`、`model_scheduler/blob_metadata.py`、`tests/test_blobs.py`、`tests/test_blob_paths.py`（均新增）。
**Acceptance criteria:**
- [ ] 边接收边hash、原子发布、owner隔离；已发布+临时+预留统一原子计费，超限不残留可读半成品。
- [ ] GET在已核验fd读取，DELETE持有时409；过期410；文件全部父目录no-follow、拒绝TOCTOU换文件。
- [ ] SQLite事务不阻塞事件循环；cancel/disconnect中止上传并回收临时配额。
**Verification:** `python -m pytest tests/test_blobs.py tests/test_blob_paths.py -q`；并发两次写入刚好越配额、checksum错、chunked超限、owner伪造。

### P12 — Blob重启恢复与执行输出提交（M04）

**Primary owner:** backend；**Dependencies:** P11、P05；**Estimated scope:** M。
**Description:** 补齐24h生命周期、崩溃恢复、模型未停时的读取保护和晚到输出丢弃。
**Files likely touched:** `model_scheduler/blob_store.py`、`model_scheduler/blob_metadata.py`、`tests/test_blob_recovery.py`、`tests/test_blob_outputs.py`（后二者新增）。
**Acceptance criteria:**
- [ ] C07每个rename/commit/fsync崩溃点恢复结果确定；重启前后owner不变，hash错blob不可读。
- [ ] GC绝不删活跃lease文件；重启须等旧实例STOPPED才解除旧读取保护；input/output各自24h起点和tombstone正确。
- [ ] 取消先置不可提交，输出发布与cancel按同一Fence裁决；磁盘满不产生成功结果，预留最终可回收。
**Verification:** `python -m pytest tests/test_blob_recovery.py tests/test_blob_outputs.py -q`；真实临时SQLite/文件，进程kill后重启测试。

### P13 — owner身份与幂等仓库（M04）

**Primary owner:** backend；**Dependencies:** P03；**Estimated scope:** M。
**Description:** 在不依赖HTTP的服务层实现授权/幂等，防止路由或重试绕过状态机。
**Files likely touched:** `model_scheduler/control_identity.py`、`model_scheduler/idempotency.py`、`tests/test_control_identity.py`、`tests/test_idempotency.py`（均新增）。
**Acceptance criteria:**
- [ ] owner来自可信PeerIdentity；token绑定boot/session/model/owner，摘要比较；交叉owner读取/修改404。
- [ ] HMAC输入编码固定，GET重算token与创建值相同；boot_key不落盘、重启token失效，比较采用恒定时间函数。
- [ ] 同key并发只创建一次；不同payload409；route命名空间隔离；活跃对象不可被24h清理。
- [ ] 重启旧token无效；对象/索引失效不能重放推理；日志和status没有token原文。
**Verification:** `python -m pytest tests/test_control_identity.py tests/test_idempotency.py -q`。到K3（P11/P12也必须完成）。

### P14 — execution队列与结果提交服务（M04）

**Primary owner:** backend；**Dependencies:** P10、P12、P13；**Estimated scope:** M。
**Description:** 把session授权、Blob读取租约、队列和输出发布组成通用执行纵向切片。
**Files likely touched:** `model_scheduler/execution_service.py`（新增）、`model_scheduler/session_manager.py`、`model_scheduler/scheduler.py`、`tests/test_executions.py`（新增）。
**Acceptance criteria:**
- [ ] 每session execution队列容量128，等待deadline=min(enqueue+1800s,session hard deadline)，未dispatch取消无需假身份。
- [ ] 只有ACTIVE可submit；dispatch前再验Fence/envelope/槽/资源；只获得一个lease、一个Blob引用保护。
- [ ] succeeded须结果发布和可信terminal同时具备；cancel/超时后晚到输出不发布，失败保留清理跟踪。
**Verification:** `python -m pytest tests/test_executions.py tests/test_sessions.py tests/test_cancellation.py -q`；fake BackendPort做乱序/重复回调测试。

### P15 — llama.cpp能力adapter与可信终结（M04）

**Primary owner:** backend；**Dependencies:** P06、P03；**Estimated scope:** M。
**Description:** 第一个真实adapter使用登记profile实现C06，保留llama-swap只控生命周期的边界。
**Files likely touched:** `model_scheduler/adapters/llama_cpp.py`、`tests/test_llama_adapter.py`、`tests/fixtures/llama_cpp_v1.json`（均新增）、`model_scheduler/backend_router.py`、`model_scheduler/gateway.py`。
**Acceptance criteria:**
- [ ] 输入按登记协议消费，输出shape/有限值验证；token/image计数失败时拒绝，不盲dispatch；不跟随重定向访问外部URL。
- [ ] HTTP响应完成不自动等于设备静止：若固定runtime有可信同步/slot终结协议，fixture绑定并验证；
  否则以独立STOPPED作为终结回退，明确其停止/重载成本，不能造quiescent=true。
- [ ] 新runtime无profile直接拒绝；scheduler进程依赖不含Torch/ORT；adapter无业务job/stage。
**Verification:** `python -m pytest tests/test_llama_adapter.py tests/test_gateway.py -q`；目标记录真实协议fixture及同步/停止证据，fixture含敏感输入时先脱敏并保留hash关联。

### P16 — backend load/execute/cancel/stop完整接线（M04）

**Primary owner:** backend；**Dependencies:** P14、P15、P07；**Estimated scope:** M。
**Description:** 让真实adapter、observer与execution服务走同一资源账本，消除fake中看不出的终结空隙。
**Files likely touched:** `model_scheduler/backend_control.py`、`model_scheduler/runtime.py`、`model_scheduler/execution_service.py`、`tests/integration/test_managed_execution.py`（新增）。
**Acceptance criteria:**
- [ ] 完整load→execute→cancel→stop→reload；每步实例/Fence一致；StopAck后容器仍活则不释放。
- [ ] 无单请求同步证明且还有别的lease时冻结新请求，等待已有请求协议终结后停止共同实例，避免互等lease形成死锁。
  “响应已结束但待STOPPED证明”的请求单独标记awaiting_quiescence，不算仍在计算的等待对象。
- [ ] 因终结回退STOPPED后session仍ACTIVE且保留独占权，模型转UNLOADED；下一execution重新准入/load并提升generation。
  reload等待受session TTL/hard deadline约束，旧generation终结不复用；回退期间禁止新dispatch、可继续heartbeat。
- [ ] 模型ready失败、Docker断开、backend断流均保留账本直到独立停止；不从部分输出构造成功。
**Verification:** `python -m pytest tests/integration/test_managed_execution.py tests/test_backend_control.py -q`；目标真实推理/取消一轮，记录停止与内存回收。到K4。

### P17 — Unix peer credential与双入口生命周期（M04）

**Primary owner:** backend；**Collaborators:** arch；**Dependencies:** P16；**Estimated scope:** M。
**Description:** 先用真实socket证明C08身份传递，再接入控制路由，不依赖伪造HTTP头测试。
**Files likely touched:** `model_scheduler/control_server.py`（新增）、`run.py`、`app.py`、`tests/integration/test_control_socket.py`（新增）。
**Acceptance criteria:**
- [ ] 两listener共享同一boot/Book，lifespan启动和清理各一次；TCP/internal/*返回404。
- [ ] run.py明确v1/v2分支：解析→唯一锁→创建一次运行上下文→旧实例reconcile→Blob恢复→开放入口；
  仅依赖llama-swap的profile导入CONTROL_CONTRACT，v2不走旧四模型专用build_backend分支。
- [ ] Linux两UID真实连接：允许UID可访问、非允许UID拒绝；伪X-UID无效；socket0660/目录权限符合部署配置。
- [ ] 单worker/instance_lock；第二进程明确拒绝；半包、超大头、body timeout、断线不泄漏task/FD。
**Verification:** `python -m pytest tests/integration/test_control_socket.py tests/test_instance_lock.py -q`；Linux真实UID测试属S门禁，Mac skip不能计该项通过。

### P18 — 控制HTTP路由闭环（M04）

**Primary owner:** backend；**Dependencies:** P17；**Estimated scope:** M。
**Description:** 暴露session/execution/blob正式API并确保服务层约束不可通过路由绕过。
**Files likely touched:** `model_scheduler/control_api.py`（新增）、`model_scheduler/control_server.py`、`tests/test_control_api.py`、`tests/integration/test_control_roundtrip.py`（后二者新增）。
**Acceptance criteria:**
- [ ] C05/C07所有路由状态码、版本和错误体符合schema；submit/close/cancel并发结果可复现。
- [ ] Unix客户端上传→session→execute→读结果→close→确认STOPPED；相同key重复提交无第二次推理。
- [ ] 非owner查询/取消/blob读取404；过期410；token/超限/不支持媒体负例；disconnect不误判GPU释放。
**Verification:** `python -m pytest tests/test_control_api.py tests/integration/test_control_roundtrip.py -q`；目标跑同一Unix客户端闭环。到CP2。

### P19 — 旧HTTP/SSE与动态模型兼容（M05）

**Primary owner:** backend；**Dependencies:** P18；**Estimated scope:** M。
**Description:** 用新运行接线承载旧API，保留既有消息、错误、SSE和取消约定。
**Files likely touched:** `app.py`、`model_scheduler/api_models.py`、`model_scheduler/gateway.py`、`tests/test_chat_api.py`、`tests/test_admin_api.py`。
**Acceptance criteria:**
- [ ] /v1/models、/api/models来自动态配置且查询不加载；status含boot/state/execution/预留/reason，无token。
- [ ] 未知模型404、能力不符422、有lease/session卸载409；普通队列忙不影响health，BLOCKED503。
- [ ] SSE保持顺序、错误不伪造DONE；disconnect走C04清理；原chat透传兼容不被新control未知字段规则误伤。
**Verification:** `python -m pytest tests/test_chat_api.py tests/test_admin_api.py tests/integration/test_http_disconnect.py tests/integration/test_direct_socket.py -q`。

### P20 — vision输入与embedding/rerank能力envelope（M05）

**Primary owner:** backend；**Dependencies:** P19；**Estimated scope:** M。
**Description:** 将C06运行前输入检查覆盖到兼容API与通用接口，fixture验证实际消费输入。
**Files likely touched:** `model_scheduler/adapters/llama_cpp.py`、`model_scheduler/envelope_validator.py`（新增）、`app.py`、`tests/test_envelopes.py`（新增）、`tests/test_embedding_rerank_api.py`。
**Acceptance criteria:**
- [ ] text/template/image总token、每边尺寸、图像数、batch和并发组合限制一致；精确边界通过，+1拒绝且无dispatch。
- [ ] vision只接data URL/已登记Blob，禁止远程URL；恶意解码尺寸、NaN embeddings、非法rank shape拒绝。
- [ ] 不新增音频/视频路由；每能力fixture映射完整，无fixture的模型不能出现在生产candidate。
**Verification:** `python -m pytest tests/test_envelopes.py tests/test_embedding_rerank_api.py tests/test_llama_adapter.py -q`。到K5。

### P21 — 现场facts、校准和物理内存口径（M06）

**Primary owner:** backend；**Collaborators:** arch；**Dependencies:** P20；**Estimated scope:** M。
**Description:** 将M00输入转为可追溯measurement，补C02物理上界，不能只用summary复制一个数。
**Files likely touched:** `model_scheduler/acceptance/collect.py`、`model_scheduler/acceptance/calibrate.py`、`model_scheduler/acceptance/__main__.py`、`tests/test_calibration.py`（均新增）、`plan/m00-envelope.md`（追加，不抹失败）。
**Acceptance criteria:**
- [ ] collect记录C09设备/栈/盘事实；calibrate关闭生产准入、独占、显式临时budget，所有出口均记录停止或UNKNOWN。
- [ ] 3轮采样间隔<=100ms、gap<=500ms、前后10s、基线中位数、delta>0、基线差<=256MiB；swap/OOM不通过。
- [ ] 按C02 `system_nonfree_upper_bound_v1` 原始MemTotal/MemFree重算物理上界，并确认目标GPU统一内存纳入口径；
  不扣估计背景、不与CUDA字节重复相加；不能证明则阻塞生产、保留软件任务结果。
- [ ] 当前probe image-token允许1.05误差不沿用为正式验收放宽；正式fixture须证明不超过登记envelope。
**Verification:** `python -m pytest tests/test_calibration.py tests/test_m00_envelope_probe.py -q`；目标calibrate按第5节，保存全部原始材料。

### P22 — 候选构建和原始事件采集（M06）

**Primary owner:** backend；**Dependencies:** P21；**Estimated scope:** M。
**Description:** 固定policy/fixtures/测量后生成candidate；实现与executor解耦的collector。
**Files likely touched:** `model_scheduler/acceptance/candidate.py`、`model_scheduler/acceptance/collector.py`、`tests/test_candidate.py`、`tests/test_collector.py`（均新增）、`model_scheduler/acceptance/__main__.py`。
**Acceptance criteria:**
- [ ] C09摘要可重算、同输入可重现；无hash循环；未测/runtime缺fixture/evaluator/policy缺值拒绝。
- [ ] `acceptance source --root REPO --output SOURCE_TAR` 独立生成纯源码归档；脏的允许清单文件拒绝，计划文档变化不改变源码hash。
- [ ] 事件包含monotonic和UTC、run/case/attempt/Fence/instance/设备归属；原始采样非布尔gpu_verified。
- [ ] 文件逐项size/hash；失败材料不删除；collector版本/hash固定进入candidate。
**Verification:** `python -m pytest tests/test_candidate.py tests/test_collector.py tests/test_evidence_contracts.py -q`。

### P23 — 每模型B场景真实执行器（M06）

**Primary owner:** backend；**Dependencies:** P22；**Estimated scope:** M。
**Description:** 执行器只通过正式API施加动作，逐模型/能力产生B证据，不直接改registry。
**Files likely touched:** `model_scheduler/acceptance/backend_cases.py`、`model_scheduler/acceptance/fixtures.py`、`tests/test_backend_cases.py`（均新增）、`model_scheduler/acceptance/__main__.py`。
**Acceptance criteria:**
- [ ] 每模型load/infer/envelope/cancel/stop/reload六类及每cap；>=3独立冷启动和3轮完整重载。
- [ ] 最大输入同轮达到已声明组合边界，不能将文本/视觉/并发拆开替代；provider+归属设备活动+实际输出三者齐备。
- [ ] executor crash也留下attempt和清理记录；未知不能转换为passed；fixture不包含视频质量评估。
**Verification:** `python -m pytest tests/test_backend_cases.py -q`验证执行器逻辑；真实最终 `acceptance run --layers B` 在P29执行，不能把本任务fixture计B通过。到K6。

### P24 — O01—O06运维执行器（M06）

**Primary owner:** backend；**Dependencies:** P23；**Estimated scope:** M。
**Description:** 形成真实混合负载、受限故障与恢复/回滚场景，故障隔离到本deployment。
**Files likely touched:** `model_scheduler/acceptance/operational_cases.py`、`model_scheduler/acceptance/workload.py`、`tests/test_operational_cases.py`、`tests/test_workload.py`（均新增）。
**Acceptance criteria:**
- [ ] O01实际请求>=1800s、arrival>=100、每模型>=3、间隙<=15s、发送偏差<=1000ms；结尾队列/lease/session空、实例STOPPED。
- [ ] O02模型盘故障用本deployment私有mount namespace隔离，不卸载整机共享盘；暂存满用专用小配额文件系统，不填满根盘。
- [ ] O03—O06覆盖Docker通道不可达/陌生端口/stop超时/重启旧token/preflight核验原语篡改/优雅停机及恢复；不破坏其他容器。
  O05不调用依赖最终O05报告的完整production gate，O06隔离lab演练及无已验收旧版拒绝分支按第3节执行。
- [ ] p95/p99 nearest-rank，成功请求含排队+加载+执行；429/504各比例分母是全部发送请求，上限均<=0.1；OOM/非预期500/不安全淘汰/残留为0。
**Verification:** `python -m pytest tests/test_operational_cases.py tests/test_workload.py -q`；此处注入preflight/rollback端口验证编排；P30真实执行，缺条件标not_run。

### P25 — 独立evaluator与离线verify（M06）

**Primary owner:** backend；**Collaborators:** arch；**Dependencies:** P24；**Estimated scope:** M。
**Description:** 从原始事件、采样、输出重算结论，不能相信summary、手填passed或GPU布尔。
**Files likely touched:** `model_scheduler/acceptance/evaluator.py`、`model_scheduler/acceptance/verify.py`、`tests/test_verify.py`、`tests/test_evaluator.py`（均新增）、`model_scheduler/acceptance/__main__.py`。
**Acceptance criteria:**
- [ ] 必测集合全集唯一；失败历史保留；每场景结果从材料计算；缺失/重复/未知/hash错/path逃逸/evaluator缺失拒绝。
- [ ] 报告时间等于case最早/最晚；UTC未来偏差<=5min，从开始起7天，恰到7天过期；重打包不刷新。
- [ ] 离线verify不访问模型、Docker或网络；exit0通过、exit2缺材料/结构错、exit3完整材料语义失败。
- [ ] 改一个原始值后即使重填summary也失败；case绑定设备/候选/工具版本不匹配失败。
**Verification:** `python -m pytest tests/test_verify.py tests/test_evaluator.py -q`；对真实失败/通过材料副本分别离线verify。

### P26 — production render和现场preflight（M06）

**Primary owner:** backend；**Dependencies:** P25；**Estimated scope:** M。
**Description:** 旧deploy入口新增v3模式，只使用同candidate已验证材料，旧报告不能冒充新证据。
**Files likely touched:** `model_scheduler/deploy.py`、`model_scheduler/preflight_v3.py`（新增）、`tests/test_deploy_render.py`、`tests/test_preflight_v3.py`（新增）、`deploy/model-runner.py`。
**Acceptance criteria:**
- [ ] production render先离线verify；缺材料不输出可启动目录，目标非空拒绝；lab输出明显标记不可生产。
- [ ] preflight分两层：无load的verify_environment核对现场身份；production_gate再校验最终S/B/O全集。
  O05只调用前者及完整gate的缺材料/篡改拒绝路径；完整gate的正向验收在P31，避免证据自引用。
  preflight重核源码/配置/模型逐文件/镜像/实际设备/栈/模式/证据及期限；错配在模型启动前阻断，无force。
- [ ] runtime runner每次load验证本次manifest身份；禁止替换已验收镜像/tag；生成路径不硬编码旧模型盘位置。
**Verification:** `python -m pytest tests/test_deploy_render.py tests/test_preflight_v3.py -q`；目标核验原语及篡改副本验证，确认无模型启动；生产完整通过在P31。到K7。

### P27 — 服务部署、优雅停机与回滚（M06）

**Primary owner:** backend；**Dependencies:** P26；**Estimated scope:** M。
**Description:** 部署单一调度进程、双listener和Blob元数据，记录可执行安装/停机/回滚步骤。
**Files likely touched:** `deploy/model-scheduler.service.in`、`deploy/llama-swap.service.in`、`deploy/INSTALL.md`、`docs/operations.md`、`tests/test_deploy_render.py`。
**Acceptance criteria:**
- [ ] service用户、客户端UID/组、socket0660、Blob盘配额/UUID、只读模型映射均可从部署输入渲染；不生成video unit。
- [ ] stop准入→排空→证明旧实例停止→切独立release/venv→preflight→启动→冒烟；未停止不切current。
- [ ] rollback恢复已验收旧代码/配置及相容Blob元数据；元数据升级前备份，downgrade不相容则恢复备份；无可用旧候选则停机修复。
**Verification:** `python -m pytest tests/test_deploy_render.py -q`；目标隔离lab空载安装/停止/元数据恢复演练，记录systemd/PID/socket状态；正式安装在P31。

### P28 — 发布归档、Python3.12/ARM64锁与CI门禁（M06）

**Primary owner:** backend；**Dependencies:** P27；**Estimated scope:** M。
**Description:** 发布包只含模型服务和绑定材料；CI分软件/真实设备门禁，禁止把mock硬件结果计B/O。
**Files likely touched:** `scripts/build-release.py`、`tests/test_release.py`、`.github/workflows/test.yml`、`requirements.lock`、`requirements-dev.lock`。
**Acceptance criteria:**
- [ ] 运行锁和开发锁在Python3.12 ARM64以require-hashes安装；控制listener若需新直接依赖，先独立拆P28a更新requirements.in及锁，不突破五文件边界。
- [ ] P06a固定的ARM64 llama-swap版本、真实控制fixture和CONTROL_CONTRACT随包可安装；不存在则阻止发布，不能在此才首次实现真实控制协议。
- [ ] 源码archive清单与C09一致；bundle含manifest/证据哈希，不含权重/凭据/视频实现；缺文件拒绝打包。
- [ ] CI执行G，Linux peer UID测试通过；保留thor marker兼容命令，不在此隐式改名；硬件job单独报告。
**Verification:** `python -m pytest tests/test_release.py tests/test_llama_swap_fixture.py -q`；ARM64干净venv安装锁并运行G。到CP3。

### P29 — 最终候选冻结与全量S/B验收（M07）

**Primary owner:** backend；**Collaborators:** arch；**Dependencies:** P28；**Estimated scope:** S。
**Description:** 在真实Orin上冻结最终镜像/资产/config/policy，重新执行最终candidate测试，M00不能替代。
**Files likely touched:** `plan/validation.md`、`docs/operations.md`（证据索引，不写设备凭据）。
**Acceptance criteria:**
- [ ] 目标干净checkout与本地release源码SHA一致，硬件/设备树/SM/盘核实；全部登记模型/能力有fixture及性能阈值。
- [ ] S在发布解释器通过；B每模型六类/每cap全部通过，3冷启动/3重载/组合最大envelope证据完整。
- [ ] 任一修复更改candidate，重建并重跑其完整S/B/O，不拼接其他candidate历史通过。
**Verification:** 第5节collect/candidate/run命令；保留环境、每资产SHA、CUDA/runtime、命令exit、耗时、峰值、stop/quiescence。

### P30 — 真实混合负载、故障恢复及离线复核（M07）

**Primary owner:** backend；**Dependencies:** P29；**Estimated scope:** S。
**Description:** 执行O全集，并在不运行模型的环境重算最终证据。
**Files likely touched:** `plan/validation.md`（证据索引）。
**Acceptance criteria:**
- [ ] O01真实>=1800s且流量分布达标；O02—O06失败/恢复路径与停止材料齐备；未测/unknown/skipped都不是通过。
- [ ] 离线verify exit0；篡改副本exit2/3；候选/报告7天有效期与设备绑定复核通过。
- [ ] 最终容器/启动进程/端口/lease/session全部结束，内存与GPU活动回落证据可查。
**Verification:** `acceptance run --layers O` 后独立 `acceptance verify`；不得用测试进程退出代替模型停止。

### P31 — 发布包现场preflight与安装验收（M07）

**Primary owner:** backend；**Dependencies:** P30；**Estimated scope:** M。
**Description:** 生成同candidate发布包，现场preflight和正式API冒烟，记录发布与回滚身份。
**Files likely touched:** `README.md`、`plan/validation.md`、`plan/05-tasks-and-acceptance.md`。
**Acceptance criteria:**
- [ ] production render/preflight通过；包SHA、candidate、报告、设备一致；安装前旧实例已静止。
- [ ] 正式TCP兼容接口及Unix上传/会话/推理/关闭冒烟；单worker、预加载、恢复配置生效；日志无媒体/token。
- [ ] 有旧版时回滚目标为仍有效已验收candidate、演练通过；没有旧版时必须验证拒绝生产回滚并保持停机修复，
  同时P30隔离lab备份/恢复演练必须通过；不因缺旧版伪造历史报告。
- [ ] 只在以上成立时记录device_backend_ready，更新M01—M07完成状态；视频项目状态不参与结论。
**Verification:** 第5节render/preflight/build-release及P27部署步骤，记录每命令exit和最终stop证据。到CP4。

## 5. CLI交付合同（当前多数不存在）

下列是P21—P28必须实现的统一命令，不可现在执行后宣称已经支持。
所有命令拒绝覆盖非空输出；输入严格按schema校验。路径来自本次运行的显式参数，不自动搜索“最近成功”。
公共参数用绝对路径；所有模型动作前必须目标身份核验。已有旧CLI参数兼容保留。

```bash
# P21：只采现场事实，不启动模型
python -m model_scheduler.acceptance collect --output facts.json
# P21：maintenance文件来自已停止生产准入/实例的检查结果，工具仍须现场重验
python -m model_scheduler.acceptance calibrate --config scheduler-v2.yaml --facts facts.json --maintenance maintenance.json --budget-bytes 16000000000 --runs 3 --output calibration
# P22：独立导出纯源码，不依赖候选、验收或deployment
python -m model_scheduler.acceptance source --root . --output source.tar.gz
# P22：纯构建，policy/fixtures不可缺值；source是已生成的纯源码归档
python -m model_scheduler.acceptance candidate --config scheduler-v2.yaml --facts facts.json --measurements calibration --policy policy.json --fixtures fixtures.json --source source.tar.gz --output candidate.json
# P23/P24：可分层执行，但final report必须绑定同candidate且显式merge
python -m model_scheduler.acceptance run --candidate candidate.json --layers S,B --output run-sb
python -m model_scheduler.acceptance run --candidate candidate.json --layers O --output run-o
python -m model_scheduler.acceptance merge --candidate candidate.json --runs run-sb run-o --output final-evidence
# P25：不启动模型；0通过，2缺材料/结构错误，3语义失败
python -m model_scheduler.acceptance verify --candidate candidate.json --evidence final-evidence
# P26：新参数分支，拒绝把旧hardware-report作为v3材料
python -m model_scheduler.deploy render --candidate candidate.json --evidence final-evidence --mode production --output build/deploy
python -m model_scheduler.deploy preflight --manifest build/deploy/manifest.json
# P28：沿用当前打包入口，内部增加v3完整性检查
python scripts/build-release.py --deployment build/deploy --output build/release --release-id RELEASE_ID
```

上例临时budget只是CLI格式示例，**不能作为实际校准输入**；P00/P21根据现场空闲内存和维护policy填值并记录来源。
维护文件是检查记录而非放行凭据，工具须实时确认单实例锁、无生产准入和无其他受管实例。
merge为P25的一部分：只合并同candidate/设备的完整run，case最终attempt需显式唯一；
S/B/O证据目录引用移入统一相对目录并核对原hash，报告起止重新按所有case最早/最晚计算，禁止刷新有效期。

candidate在生成时检查runtime image digest与模型资产已存在；工具不自动pull镜像/下载模型。
collect/calibrate/candidate/run/merge/render/preflight统一：0成功、2输入/材料错误、3执行或语义失败；
run的failed/not_run/unknown必须落盘，异常退出后清理无法确认则非零且报告UNKNOWN。
preflight不load；只要错配，模型启动计数必须为0。

## 6. 不能通过代码猜测的输入与阻塞处理

| 输入 | 获取任务/来源 | 缺失时的确定行为 |
|---|---|---|
| 目标仓库路径、远端、release Python3.12 | P00只读SSH查看进程/systemd/checkout并记录 | 不猜路径，不在错误checkout部署；其他纯契约任务可准备，硬件任务blocked |
| 首候选实际镜像digest/依赖lock/adapter hash | P06/P15受控runtime构建产物 | M00原生binary不是镜像通过；构建新镜像后P21校准/P29最终验收 |
| 其他模型/新的runtime | 管理员登记的资产清单+各自M00 | 不承诺audio/Torch/ORT；只为已登记且完成输入的模型生成candidate |
| 物理驻留可信上界 | P21现场测量及collector算法 | null保留，禁止生产；不得使用约10.5GiB代填 |
| p95/p99/冷加载/推理/最大输入阈值 | P21校准后形成显式policy，在P22运行前冻结 | 输入缺项exit2；不根据正式验收结果倒填宽松阈值 |
| 全部旧四模型的资产与envelope | 迁移inventory | 软件兼容仍需全回归；生产只包含candidate明确登记集合；不得删除用户要求保留的模型来伪装全通过 |
| 第二个真实模型用于切换 | 候选登记+独立探测 | 一个模型只能验load/reload，不能宣称多模型切换；M07切换验收至少两个不同模型，缺失则blocked |
| 固定llama-swap控制协议 | P06a固定ARM64版本+真实HTTP fixture | 无CONTROL_CONTRACT不得启动依赖它的服务；P28仅验证打包，不得延后补实现 |
| 客户端UID/组、Blob根/盘UUID | 部署输入，P17/P27现场检查 | 非允许UID拒绝；无正确盘不写根盘占位目录 |

风险闭合：改变模型、镜像、加载模式、设备、envelope、预算、代码、policy或evaluator就生成新candidate。
同型号换机也必须重测。模型盘现场为 `/media/jtzn/sandisk-ext4`，不能从旧模板的 `/mnt/model-ssd` 推断已迁移。
仅硬件构建且本次核验确为Orin SM8.7时用相应CUDA架构；不复用Thor构建，不改驱动/fstab/磁盘格式。

## 7. 通用验证、提交与证据模板

G（每个实现检查点及发布候选执行）：

```bash
python --version
python -m pytest tests -m 'not thor' -q
python -m ruff check .
python run.py --check-config
git diff --check
git status --short
```

v2任务另对生成的v2 fixture执行 `python run.py --config FIXTURE --check-config`；不能只检查仓库v1示例。
禁止通过ignore/删除失败测试达成G；现有草稿由P02补齐。Python3.13结果仅作开发参考。
不用单元测试假装真机B/O通过；Linux peer UID、ARM64依赖、真实模型分别保留执行环境身份。

提交前审阅diff中的凭据、模型权重、无关改动，只stage本任务文件，原子commit。
目标先查看 `git status --short`，有改动保留并定位；仅干净时 `git pull --ff-only`，核对完整SHA。
没有推送通路时明确标“待同步”，不能reset/复制修改目标tracked文件绕过精确提交要求。

每任务执行记录至少：

```json
{
  "task_id": "Pxx",
  "status": "not_started|software_only|blocked|complete",
  "source_commit": "full commit SHA",
  "candidate_sha256": null,
  "python_version": "actual version",
  "commands": [{"argv": ["python", "-m", "pytest"], "exit_code": 0}],
  "target_commit": null,
  "evidence_directory": null,
  "unresolved": []
}
```

模板字符串必须替换为真实值；它不是验收报告。硬件任务额外记录模型路径/逐文件hash、runtime/CUDA、
设备身份、elapsed/peak、实例ID、进程退出、端口释放、计算静止和最终预留账本。
证据存 `/home/jtzn/self-model-switch-evidence/<task>-<UTC>-<unique>/`；失败轮保留，不在Git保存模型或凭据。

## 8. 计划审阅与开工规则

- 上游 [ADR与Grill Review](adr/decisions.md) 已完成项目拆分和M00前置决策；本计划沿用这些决定。
- 本轮补充决定C01—C09是具体实施合同，尤其物理内存门槛、未dispatch终结和Unix身份接线须在对应任务同步测试/规范。
- 架构检查重点：类型单向依赖、单一Book、生命周期串行但推理并发、observer独立、无停止证据不清账、证据hash无环。
- cs-planning要求的架构fan-out已由独立只读审阅完成（本环境无专用cs-architect工具，子agent承担同职责）：
  已采纳基础ports前置、启动者观察、UNKNOWN启动、内存两种口径、Unix真实UID、幂等索引冲突检查、
  可信terminal、逐profile探测、校准→候选→报告单向链和任务拆分建议；不将该技术审阅写成人工批准。
- 实际计划二轮审阅还修正了token摘要无法恢复、ACTIVE会话STOPPED后重载、源码归档依赖deployment、
  O05验证自身报告、工具开发依赖尚未完成工具、固定控制契约交付过晚等执行歧义。
- 任务全部有owner/依赖/文件/验收/验证；检查点每2—3任务；P28若需要新依赖按规定拆P28a。
- 人工审阅状态：待审阅。本次请求授权生成计划；不把文档生成等同于批准部署或全部实施。
  后续明确要求实施时从P00开始，按依赖推进；若仅要求某任务，则只执行其已满足依赖的范围。
- 本文件交付检查：34个任务均有必填字段，依赖按拓扑顺序且无环，每任务文件清单不超过5项；
  7个本地链接、代码围栏、尾随空白和R的精确整数公式已通过本地脚本检查。

最终功能完成必须以CP4的最终candidate与设备证据为准。此文件及任意勾选本身不证明实现或验收。
