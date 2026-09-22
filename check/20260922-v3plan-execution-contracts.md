# 修复执行契约：接口、状态与拒绝条件

日期：2026-09-22。状态：**K1—K8 已由 RP00 冻结；K1—K5 同步进 `plan/08-execution-plan.md` 的 C03 增量，K7 进 C08 增量，K6 分阶段范围进 C09 增量，K3/K5 同步进 `plan/02-scheduler.md`。实现仍未开始。**
任务、依赖与验收见[执行Plan](20260922-v3plan-execution-plan.md)。现行规范仍为 `plan/01–06`、08/Cxx；本文件由RP00把选定增量同步进对应规范，不能靠审查记录隐式覆盖规范。

## K0. 基线、来源与归属（RP00 记录）

事实复核基线提交 `79d44d22b225e955e3fb2083c6d2982ec8c83ab7`（短 `79d44d2`）；契约与执行计划冻结基线
`c1d547aa42d37adb13d58ac02dc3bc86b8a33688`（短 `c1d547a`，只新增 `check/` 文档）。

工作区另有**用户未提交**的两个文件，不属于任何 RP 的交付，由 RP04 整合，本轮不 reset、不覆盖、不混入无关提交：

| 文件 | 工作区 blob（`git hash-object`） | `git diff` 的 SHA-256 |
|---|---|---|
| `model_scheduler/backend_control.py` | `a6c0b0744af631cf4cb03d46854cdde3b525f7b5` | `45de54f485d0fe8c98191eb45b18860338f2a353a886b65fdb89bb311d196a30` |
| `tests/test_backend_control.py` | `05cfb29b50ab02d6dfa6d7e0d9555392fffb24f2` | `1ffaad7d3b189eb6043c8ff8eaf8ff1c9282b0ccc2bb3cdb3e6e1e04072d243e` |

内容是把加载路径的单次观察改为轮询见证（`ManagedLifecycle._verify_running`）及其测试，等价于被 `a6497fd`
回退的 `142746c`。它与 K1 重合，RP04 以有界策略和注入时钟重写，不直接沿用其 `asyncio.get_event_loop()` 与
墙钟 sleep。

[事实复核](20260922-v3plan-review-verification.md)**整体不是需求**：只有映射到 RP00—RP17 的条目（执行Plan 第 4 节）
才是本轮范围。RP00 的签名核实结论：下列符号在当前源码中**不存在**，均为本轮新增——`LifecyclePolicy`、
`ExpectedInstance`、`DeadlineDocker`、`probe_loopback`、`Book.instance/load_stopped/retry_stopped_load`、
`Runtime.load_stopped_generation`、`DeploymentRecoveryPort`、`ServingGate`、`TcpServerAdapter`、
`ControlServer.prepare/activate`、`measurements_index`、`require_instance_identity`、`lifecycle_policy`、
`build_managed_execution(expected_instances=...)`、`build_v2_context(config_sha256=...)`。
已存在且被本轮改签名的：`LlamaCppAdapter.release(model_id)` 增加 `deadline`；`build_candidate` 的
`measurements_dir` 由必填改为可选并新增互斥的 `measurements_index`。
`build_v2_context` 位于 `run.py:86`（不在 `runtime.py`）；`require_production_openable` 位于
`contracts_v2.py:568`（不在 `candidate.py`）。

## K1. 策略和时钟

本轮使用内部不可变策略，定义于`ports_v3.py`，不增加配置schema、环境变量或CLI阈值开关。生产只能使用同一源码版本的默认值；测试显式注入较短值。策略值随source archive进入候选身份，改值必须重新生成候选及受影响证据。

```python
@dataclass(frozen=True)
class LifecyclePolicy:
    verify_window_seconds: float = 10.0
    poll_seconds: float = 0.5
    observation_timeout_seconds: float = 2.0
    observation_max_age_seconds: float = 2.0
    recovery_seconds: float = 60.0
```

约束：每项为有限、非bool、正数；`poll_seconds <= verify_window_seconds`；`observation_timeout_seconds <= observation_max_age_seconds <= verify_window_seconds`。构造时违反即`ValueError`，不裁剪错误配置。10/.5/2/2/60是本Plan明确的初版软件值，**不是引用旧C02得出的硬件已验证参数**；RP17验证不足时改策略和测试，不能现场暗调。

- 使用同一单调时钟域，生产默认`time.monotonic`；测试注入`now: Callable[[], float]`及`sleep: Callable[[float], Awaitable[None]]`。UTC只用于证据，不参与deadline。
- 调用方传入绝对`caller_deadline`，`now >= deadline`即过期。不得把过期时间重置为“再给一次机会”。
- adapter控制阶段使用原调用deadline；adapter返回后独立验证使用 `verify_deadline=min(caller_deadline, now()+verify_window_seconds)`。
- 单次观测使用 `sample_deadline=min(verify_deadline, now()+observation_timeout_seconds)`，各I/O阶段共用此绝对时间，不为每阶段重新给2秒。
- `sampled_at_monotonic`定义为本次事实采集**开始**时间。RUNNING/STOPPED被接受需 `0 <= now-sampled_at <= observation_max_age_seconds`，且`now < valid_until`。不能用采集结束时间掩盖已过时的起始事实。
- 截止限制的是新工作派发和结果接受；超时后的子进程终止/回收仍须完成并记录。不能承诺操作系统调度意义的严格零毫秒超时，也不能在回收未完成时声称资源静默。

## K2. 生命周期结果与唯一身份所有者

沿用 `contracts.Observation` 的原五个位置参数，**只在末尾新增带默认值的字段**：

```python
@dataclass(frozen=True)
class Observation:
    presence: Presence
    instance_id: str | None
    healthy: bool
    observed_at: float
    detail_code: str | None = None
    instance: InstanceIdentity | None = None   # 新增，完整身份
    valid_until: float | None = None           # 新增，验证结果最后接受期限
```

字段只是内部DTO，不自动新增HTTP字段、不改变control-v1 schema。`InstanceIdentity`复用 `control_protocol_v1`，不得再造相同结构。纯DTO依赖为 `contracts -> control_protocol_v1 -> contracts_v2`，不允许反向引用scheduler/runtime。

```python
class Book:
    def instance(self, model_id: str) -> InstanceIdentity | None: ...
    def loaded(self, operation: Operation, now: float, *,
               instance: InstanceIdentity | None = None) -> None: ...
    def load_stopped(self, operation: Operation, now: float,
                     code: str = "load_proven_stopped") -> None: ...
    def retry_stopped_load(self, model_id: str, *, expected_epoch: int,
                          expected_generation: int) -> None: ...

class ManagedLifecycle:
    # 必需注入，不提供私有identity字典的fallback。
    # instance_lookup: Callable[[str], InstanceIdentity | None]
    def instance(self, model_id: str) -> InstanceIdentity | None: ...
    async def load(self, operation: Operation, deadline: float) -> Observation: ...
    async def stop(self, operation: Operation, deadline: float) -> Observation: ...
```

- 唯一已接受身份为`Book.Runtime.instance`，新增可空字段默认None。`ManagedLifecycle._instances`删除，`instance()`仅调用只读lookup；adapter的identity回调继续经此入口取得同一份身份。
- load/stop及observer不修改Book或身份；结果只是待接受事实。不得另加prepared/committed身份缓存、待提交映射或回调中的写账本行为。
- `ModelScheduler(..., require_instance_identity=False, lifecycle_policy=...)`显式区分legacy；managed composition固定传True。禁止依据`getattr(result,"instance",...)`或有无某方法猜模式。
- managed的RUNNING需完整instance、healthy=True、有限observed_at/valid_until，`instance_id == container_id + ":" + started_at`；STOPPED允许instance=None（实例不存在），但同样需要有效时间、归属和停止证明；UNKNOWN不写身份。
- legacy保持原五字段构造/返回兼容，RUNNING允许没有完整身份；默认模式也必须检查原caller deadline，不能接受已过期成功。
- 成功`loaded`写READY和identity；已证`stopped/load_stopped/finish_recovery`清identity；`failed/begin_recovery/UNKNOWN`保留最后接受的身份，直到可信停止被接受。`bootstrap_stopped`保持空身份。

调度器接受动作必须在同一`_condition`内完成：

```python
# 示意顺序；不引入第二份事务管理器。
validate_current_operation(operation)  # epoch/generation/operation_id
validate_result(result, deadline=min(caller_deadline, result.valid_until), now=now)
# 全部可能拒绝的检查在下面首次写状态之前结束。
book.loaded(operation, now, instance=result.instance)  # 或load_stopped/stopped
condition.notify_all()                # 中间没有await、I/O或外部回调
```

`valid_until`由bridge给出，RUNNING/STOPPED为 `min(verify_or_stop_deadline, sampled_at+max_age)`。scheduler重新获取now核实，结果字段无权延长原deadline。旧operation（epoch/generation/operation_id不再匹配）不得更新身份、释放预留、清错误、重置重试计数或触发恢复。当前operation仅因结果时间过期时，拒绝迟到成功/停止，但应按UNKNOWN记失败、保留预算并允许K5的新预算恢复；不能把这类超时静默丢弃。
`_finish_load` finally只在 `_loads.get(model_id) is asyncio.current_task()` 时pop，避免旧任务删除后继任务。

## K3. 已证STOPPED仍是加载失败

新增 `Runtime.load_stopped_generation: int | None = None`，仅表示“该加载代际已证停止”，不是第二份身份/预算账本。

| 方法/输入 | 前置条件 | 单次同步写入 |
|---|---|---|
| `load_stopped(op, now)` | 当前op，LOADING，无lease，now有限，调用者已验可信STOPPED | ERROR、admission_blocked=True、reservation=0、instance=None、operation_id=None、stopped_at=now、last_error=load_proven_stopped、marker=当前generation。 |
| `failed(op, code)` | 当前op | ERROR、关闭准入，保留reservation及identity，清marker。 |
| `retry_stopped_load(id, expected_epoch, expected_generation)` | 不recovering，epoch/generation一致，ERROR、reservation=0、identity=None、operation=None、无lease、marker==本代 | UNLOADED、解除block、消费marker；last_error可保留至新begin_load，不发Docker卸载。 |
| `begin_load` | 现有C02准入全部成立 | 新generation/operation及预留，并清marker和旧stopped_at；必须先使用旧stopped_at完成本次sample_after_stop校验，再清除。 |
| `begin_recovery` | 沿用现有恢复约束 | 清marker，旧停止证据不能跨恢复epoch授权重试。 |

ERROR+reservation=0仍是错误状态；当前Book的两种committed都按reservation计算，因此预留已释放。**不能只凭ERROR+零预留或历史stopped_at来识别本代load已证停止。**

重试行为固定：

1. 普通`acquire`在ERROR仍抛`ModelUnavailable(last_error)`，不新增透明重试。
2. `warm/preload`沿用`load_retry_limit`，默认首次加最多3次重试；limit=0为首次失败后拒绝。`_reclaim_failed_model`在同一锁内重查额度，且只有成功取得重试转换/cleanup operation才计数一次。
3. 有有效marker的ERROR0直接调用`retry_stopped_load`；未知ERROR走既有cleanup+真实STOPPED路径。失败计数不因cleanup/recovery而清零，只在**当前op的健康RUNNING被接受后**清零，不能看到未验证READY就清零。
4. 两个并发warm不能重复消费marker或超额领取重试；旧generation/epoch/重复结果不计数、不释放资源。
5. 停止后的下次内存准入重新采样，样本时间不得早于接受停止事实的`stopped_at`；不把观测开始时间当作停止后内存采样边界。

## K4. 控制派发、观察与错误语义

### 控制阶段

本轮明确选择：**adapter返回UNKNOWN/STOPPED或控制异常时，不进入新的load-witness轮询**。bridge返回UNKNOWN（`load_unverified`或`load_failed`），保守保预算，交给恢复/受控cleanup。只有adapter返回RUNNING才轮询；这保留当前行为，不顺带重新设计adapter readiness重试。

控制调用也必须有界，不能只给observer限时：adapter.load/stop、孤儿release使用原绝对deadline，内部HTTP请求取剩余时间。

```python
class ManagedAdapterPort(Protocol):
    async def load(self, spec: ModelSpec, fence: Fence, deadline: float) -> V3Observation: ...
    async def stop(self, identity: InstanceIdentity, fence: Fence, deadline: float) -> StopAck: ...
    async def release(self, model_id: str, deadline: float) -> bool: ...
```

该内部能力显式由managed adapter实现；不得依赖`getattr(adapter,"release",None)`静默跳过。legacy的`BackendControl`不新增此要求。
控制HTTP超时、task取消、StopAck以及“端口此刻关闭”都**不是launch终结证明**。`task.done()`同样不是：它只说明协程结束，不说明启动子进程或容器启动已退出。

**可提供证明的来源（RP00 已核实，实现者不得猜测）**：

| 角色 | 位置 | 现状 |
|---|---|---|
| 事实类型 | `ports_v3.LaunchOperation`（`ports_v3.py:78`），`is_terminal` 即 `state != "starting"`，`state ∈ {starting, completed, failed}` | 已存在 |
| 生产者 | `model_runner.SupervisedLaunch`（`model_runner.py:45`），`operation` 返回 `LaunchOperation`，launcher 子进程退出前保持 `starting` | 已存在；只被 `deploy/model-runner.py:106`、`scripts/capture_control_fixture.py:251` 及测试使用 |
| 消费方 | `DockerProcessObserver.observe` 的可选 `launch_lookup`（`process_observer.py:309/322/330`），结果进入 `stopped_is_proven(launch_operation_terminal=...)`（`process_observer.py:350`） | 已存在；默认 `None` |

已核实的**两处缺口**：

1. `process_observer.py:348` 在 `launch is None` 时取 `launcher_terminal = True`——把“没有启动记录”等同于
   “启动已终结”。生产装配 `run.py:139-142` 只传 `deployment_id, model_id, port`，因此恒为 `launch is None`。
   本轮固定：**不能**因 lookup 为 None 默认 `terminal=True`。
2. v2 managed 加载路径 `LlamaCppAdapter.load`（`llama_cpp.py:299-317`）经 HTTP 派发给控制面，不创建
   `SupervisedLaunch`，返回的 `launch_operation` 恒为 `None`（`llama_cpp.py:316`）。即 managed 装配下
   **根本没有 LaunchOperation 生产者**。

结论：**当前固定控制协议（control-v1 的 load/unload HTTP）不能提供 launch 终结证明**。该分支固定为失败封闭，
并带可测试的失败行为：

- 未派发——本 boot 从未对该 target 发起启动**且观察者能正面确认**——才可证明“无 launch”；
- 派发后失联必须保持 `launch_unresolved` → UNKNOWN，禁止用 `task.done()` 或超时代替；
- 该分支下 bridge 不得返回 STOPPED，`DeploymentRecoveryPort.recover` 必须 `ok=False`；
- RP04 断言该分支返回 UNKNOWN + `launch_unresolved` 且 STOPPED 次数为 0；RP07 断言该分支 `ok=False`。

让控制面暴露逐 load 的启动操作状态，或让 managed 加载改走 `SupervisedLaunch` 监督路径，是**新的控制协议能力**，
另立任务；本轮不得伪造接口实现。

### 观察I/O与轮询

为v3观测/恢复新增显式有deadline的Docker调用接口，保留legacy同步`run_docker`的现有调用兼容入口：

```python
class DeadlineDocker(Protocol):
    def __call__(self, argv: Sequence[str], *, deadline: float) -> tuple[int, str, str]: ...

def probe_loopback(port: int, *, deadline: float) -> str: ...
```

实现每次`subprocess.run`之前计算剩余timeout；ps、inspect、stop后的inspect共用上层deadline；port timeout为min(0.5,剩余)。在worker中执行一个完整有界观察/恢复操作，不能cancel worker后继续发下一轮并叠加线程。消费返回结果前再次检查deadline/新鲜度。
如果外层取消，记录并跟踪尚未退出的worker/control任务，完成回收或保留不可准入状态；task.cancel不等于子进程/受管计算退出。测试用可控阻塞runner验证“不再派发下一阶段”，墙钟允许回收开销，不以deadline后补出的样本更新状态。

轮询算法：先检查时间→observe→再查时间和样本→验证身份/终态→普通UNKNOWN按min(poll,remaining) sleep。observer异常可在验证window内重试；缺observer、错身份、过旧/未来样本立即返回UNKNOWN及对应原因，不继续接受同轮后续样本。截止已到优先返回截止码。stop遇到合格RUNNING立即返回不释放，遇到普通UNKNOWN继续到调用deadline。不添加指数退避、额外随机抖动或可无限重设的窗口。

### 身份归属

在`ports_v3.py`定义不可变期望值，构造时验证ID及digest格式：

```python
@dataclass(frozen=True)
class ExpectedInstance:
    deployment_id: str
    model_id: str
    runtime_id: str
    image_digest: str
    identity_digest: str
    digest_kind: Literal["config"] = "config"

def build_v2_context(config: AppConfigV2, *, config_sha256: str,
                     env=None, ports: dict | None = None) -> RunContextV2: ...
```

`main`对已经完成读取一致性检查的`config_bytes`计算SHA-256并传入；不重新序列化config对象求摘要，不新增环境变量。build_v2_context用现有必需站点deployment_id、config.models中的model_id/runtime_id、对应runtime.image_digest及该config_sha256构建精确模型全集的mapping。
`build_managed_execution`新增必需keyword参数`expected_instances: Mapping[str, ExpectedInstance]`，将mapping交给bridge；run同时把对应值传给`DockerProcessObserver(..., expected=...)`。managed装配缺摘要/少模型/多模型/错误runtime立即抛RuntimeCompositionError，不能退回弱校验；测试必须显式构造自己的期望值。

当前lab和production renderer均生成CONFIG_LABEL（`runtime_profiles.py:189`、`deploy.py:450`），故本轮固定`digest_kind="config"`。observer在原始labels尚未丢失时要求CONFIG_LABEL完全匹配且CANDIDATE_LABEL不存在，取消现有`candidate or config`回退；bridge再次核对identity字段。旧DTO字段`candidate_digest`名称保持兼容，其当前值按config摘要比较。未来增加candidate标签模式另立协议任务；本轮不加入未实现分支，也不从observer结果反推期望值。
已知instance时还需container_id和canonical StartedAt相同；初次加载lookup为None合法。STOPPED不要求虚构instance，但须对当前deployment/model/目标容器及launch、subprocess、port四事实进行归属验证。

### 内部错误码

| 情况 | 内部detail_code | 生命周期结果 |
|---|---|---|
| 无observer | observer_missing | UNKNOWN，立即返回 |
| 验证window耗尽 | verify_deadline_exhausted | UNKNOWN；事件中另保留last_observation_error |
| 观察异常/未知事实 | observer_failed / observation_unknown | 中间诊断；window内可重试 |
| 错身份、旧/未来采样 | observation_identity_mismatch / observation_stale | UNKNOWN，立即返回对应码；不等待剩余window |
| 启动派发不能证终结 | launch_unresolved | UNKNOWN，禁止用STOPPED释放 |
| 加载时已证停止 | load_proven_stopped | STOPPED，由K3消费 |

内部码进入有限结构化事件，不包含控制响应body或凭据。对外沿用已有`ModelUnavailable`/`backend_failed`等映射和HTTP状态；不得将新增内部码直接塞入control-v1未知枚举。详细诊断使用现有status last_error/日志路径，新增公开字段另立契约任务。

## K5. 自动恢复接口与总预算

源码事实：当前v2 `RunContextV2.recovery`是同步`DeploymentRecovery`，用于启动reconcile；scheduler默认`recovery=None`。本轮明确新增适配与装配，不能把两个接口混用。

```python
class DeploymentRecoveryPort:
    # 依赖：DeploymentRecovery、按model映射的ObserverPort、Clock/Policy。
    # 禁止依赖Book、Scheduler或直接修改账本。
    async def recover(self, deadline: float) -> RecoveryResult: ...
```

`DeploymentRecoveryPort` 是本轮新增的**适配器类名**；它必须满足**既有** `ControlRecoveryPort` Protocol
（`contracts.py:106`，同样只有 `async def recover(self, deadline: float) -> RecoveryResult`），因为
`ModelScheduler.__init__` 的 `recovery` 参数类型就是它（`scheduler.py`），`runtime.py` 也经
`scheduler_kwargs` 注入。不得再定义第二份同形 Protocol，也不得用同步 `DeploymentRecovery` 冒充该端口。
现有 `DeploymentRecovery`（`control_recovery.py:173`）是同步对象，只有 `reconcile(*, close_admission, deadline)`
与 `stop_instance(identity, *, deadline)`，仅在线程边界被适配器调用。

适配器在worker中调用带deadline的reconcile，只清理本deployment容器；之后逐模型独立采样STOPPED，校验K1/K4。`ok=True`必须同时满足helper成功、所有登记模型已证停止、无未知launch/worker、无未证停止的容器；允许已停止但未删除的容器，不新增docker rm。`stopped_models`为模型ID全集而非container_id集合。缺一项返回ok=False，不能用“Docker返回0”填成功。

调度状态机：

```text
当前加载UNKNOWN被接受
  -> 锁内创建唯一恢复任务、deadline=now+60、begin_recovery一次，保存epoch
  -> 立即停止新load/lease/session dispatch
  -> 在同一deadline内等现有lease可信归还、已派发load/eviction/cleanup终结
  -> 没排空：失败并保持recovering/预留/identity/lease，不执行全局stop
  -> 排空：调用port.recover(同一deadline)
  -> 锁内校验epoch及完整stopped_models后finish_recovery，清身份，通知等待者
```

- 触发者自身的load task从`_loads`安全摘除后才进入等待，避免恢复等待自己；旧动作集合通过epoch固定，不容许后来任务偷偷加入。
- 合并重复恢复触发，不延长deadline；不能在排空后再调用会重复`begin_recovery`的公开recover入口。
- 租约到期只请求既有可信终结流程；删除“drain超时后直接book.release(ABORTED)”作为恢复手段。未知计算仍活着时必须保留租约和预留。
- 显式人工recover可在旧恢复失败、无活动恢复task且租约/控制动作已排空后重试当前关闭状态；创建新的有界尝试，不把人工重试自动循环化。epoch变化的迟到结果无效。
- shutdown加入外部更早截止时取min；到期仍未静默则记录失败，不能finish_recovery。成功恢复不自动重放推理，不清空加载重试计数。
- legacy继续使用现有ControlRecoveryClient，managed显式注入新适配器。启动reconcile也移出事件循环阻塞路径，但接受Book结果仍在宿主事件循环，不让worker操作Book。

## K6. 完整生产候选及多模型材料

第一阶段RP10：现有`--measurements DIR`只允许配置中**恰好一个模型且已测**。解析完整配置后、读取昂贵资产/写输出前，逐模型复用`require_production_openable`。混测集合返回输入错误exit2，output不存在或保持原内容不变；不删历史候选，不改变lab校准CLI。

若最终生产模型数>1，RP11/RP12必须执行；不满足时不能靠从配置删掉模型伪装完成原发布范围。

```python
def build_candidate(*, config_path: Path, facts_path: Path, policy_path: Path,
                    fixtures_path: Path, source_path: Path, deployment_id: str,
                    output: Path, measurements_dir: Path | None = None,
                    measurements_index: Path | None = None) -> dict: ...
# 两者必须恰好一个；CLI使用required mutually-exclusive group。
```

`--measurements-index`指向严格JSON：

```json
{"schema_version":1,"models":[
  {"model_id":"model-a","directory":"calibration/model-a"},
  {"model_id":"model-b","directory":"calibration/model-b"}
]}
```

directory相对index父目录，只是材料定位，不进入candidate语义摘要。拒绝未知键、重复JSON键/模型、绝对路径、`..`、symlink及所有路径逃逸；模型集合必须等于配置集合。逐模型检查summary.model_id、measured、正物理峰值、登记值、材料manifest digest、实际bytes/hash及有效测量结论。

复用schema-v3的flat `measurement_refs`，不增加第二个候选schema：index模式的artifact key统一为`measurements/<model_id>/<原相对路径>`；按model_id和原路径排序。每个模型的`measurement_ref`仍按去掉上述namespace前的局部artifact清单计算，保持校准材料的原摘要语义；全局candidate摘要绑定加namespace后的引用。legacy单模型flat路径保持原样；不可混用flat与namespace，也不可猜测材料属于哪个模型。

RP12提供统一分组/校验函数供candidate和verify使用。index模式材料按上述key复制进证据包，并进入report artifact manifest；verify离线重验分组覆盖、局部摘要、bytes/hash和模型身份。run/merge必须保留这些材料，缺材料exit2、完整但测量语义失败exit3；所有模型最终仍需各自完整B能力/包络及O/S门禁。不能仅靠candidate构建时读一次就宣称发布时仍完整。

已有摘要中的passed只作为构建前置输入，不能替代正式evaluator重算。C02物理门槛及内存实时门槛不放宽，最终预算依赖RP15的产品决策。

## K7. 双入口与唯一lifespan

新接口明确区分资源准备与开放：

```python
class ControlServer:
    async def prepare(self) -> None: ...   # bind，不accept；chmod/chown完成
    async def activate(self) -> None: ...  # 只能在prepare成功后调用
    async def stop(self) -> None: ...      # 幂等，只清理自己拥有的资源

class ServingGate:
    ready: bool = False
    def open(self) -> None: ...
    def close(self) -> None: ...

class TcpServerAdapter:
    ready: asyncio.Future[None]            # startup成功/失败都必须settle
    async def serve(self, prepared_socket: socket.socket) -> None: ...
    async def stop(self, deadline: float) -> None: ...
```

ControlServer保留`start()`为`prepare(); activate()`的兼容便利入口，正式双入口装配不得调用它。ready门是进程内共享对象，不能由客户端header/body控制。

`serve_v2`唯一所有者顺序：

1. gate关闭；沿用main已持有的instance lock，不在serve_v2重复获取；准备TCP原生socket和Unix listener（`start_serving=False`）；先验证组并完成权限，才进入运行生命周期。任一bind失败不启动preload。
2. 启动reconcile/Blob recover通过后，进入TCP应用唯一lifespan；复用现有应用启动/退出逻辑，Uvicorn设`lifespan="off"`，control app不跑lifespan。
3. 启动薄TCP适配器，用ready Future等实际startup成功；Unix activate成功后才gate.open。任一失败执行统一finally。
4. 两个业务入口的ASGI包装都在**dispatch前**检查gate。未ready时业务请求返回已有503错误格式，不读取body、不创建Blob、不排队/加载；TCP `/live`可维持存活诊断，`/health`必须503且不触发业务。Unix非白名单仍在HTTP解析前关闭。
5. 正常停止/失败/取消：gate.close→停止两边accept→按截止排空/取消入口连接→退出唯一lifespan执行共享scheduler shutdown一次→回收自有socket/clients。不能先关闭共享Book/Blob再让另一入口继续请求。

薄适配器封装当前锁定Uvicorn 0.53.0的startup完成与异常通知，禁止其他模块直接读/改其内部server列表。验证startup抛`OSError/SystemExit/CancelledError`均使Future失败并清理；不用固定sleep推测就绪。应用lifespan提取为具名context manager供外层调用，避免依赖多个入口各跑一次startup。

Unix清理记录本次bind后的inode/所有权，只unlink仍属于本次启动的socket；遇已有live listener直接拒绝，不能删除其socket。`grp.getgrnam`的KeyError也走finally。旧入口兼容测试和Linux真实peer UID测试都保留。

## K8. C02产品决策与未授权内容

35B要求仍独立于上述代码修复，未收到产品选择时标为`UNDECIDED`，不默认“已接受放弃35B”。可执行的默认安全行为始终是拒绝不满足现有C02的模型生产准入。

RP15输出Proposed ADR，记录两种分支及生产model_id清单：

- 保留现方法：登记本轮排除的模型和材料依据；35B预留复算为75,029,940,839 B。明确排除只是当前候选范围，不是模型永久不可用。
- 必须生产支持35B：先提出新method版本、字段、collector/evaluator及统一内存上界论证，再单独形成测量实现Plan与三轮实测。**当前材料不足以给出一个可信净驻留公式**，因此此分支不是实施者自行减page cache的许可。旧15%整数函数在余量不变时无需修改。

旧门槛、O01、能力闭集、同UID边界不自动更改；不实施视频业务、驱动/磁盘操作、模型下载、无关的全仓文档重构。
