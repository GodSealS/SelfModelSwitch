# v3 成立问题修复方案

日期：2026-09-22。依据：[逐项事实复核](20260922-v3plan-review-verification.md)。
状态：建议方案，未实施；不代表产品范围决策、原报告 resolved 或硬件验收通过。

已细化为[可执行Plan（19项任务）](20260922-v3plan-execution-plan.md)及[接口/状态契约（K1—K8）](20260922-v3plan-execution-contracts.md)。后续实施按这两份细化文档执行；本文件保留修复思路来源。

## 执行顺序

| 顺序 | 工作包 | 覆盖原 ID | 交付边界 |
|---|---|---|---|
| 1 | R2 有界验证和可信观测 | design-F02；code-F01/03/04/05/06/07/08/09/10；security-N01；test-N02 | 先明确C03契约，修时间与写回边界，再收紧测试。 |
| 2 | R3 已证STOPPED的账本处理 | code-F02；backend-N01；code-F05/08、test-N02相关部分 | 与R2保持三态一致，独立验证预算释放与过期操作拒绝。 |
| 3 | R4 完整生产候选 | design-F06成立部分 | 在候选生成阶段更早拒绝不完整集合，复用已有生产门禁。 |
| 4 | R1 C02产品决策 | design-F01成立部分 | 可与前3项独立准备；生产候选模型集合定稿前必须闭合。 |
| 5 | R6 双入口就绪屏障 | design-F09成立部分 | 失败入口不能留下另一入口继续提供业务。 |
| 6 | R5 导航与边界文档 | design-F05、design-F07成立部分 | 随各包同步文档，最后核对入口和证据索引。 |

不为重复 finding 分别修改同一代码，也不因审查数量多而扩大到视频项目、能力扩展或全仓重构。

## R1：保留当前安全门禁，闭合35B的生产决策

当前立即可执行的规则：保留 `system_nonfree_upper_bound_v1` 与安全余量；现有35B材料不能让该记录设备通过准入；27B估算值仍只属于lab。更正记录中的预留为 **75,029,940,839 B**，注明来源材料与设备身份。

决策有两条路径，应形成 Proposed ADR，再由产品要求确定 Accepted 分支，本次不代为选择：

- **保留现有方法**：冻结本次生产模型清单，35B不列入；27B只有完成有效测量、预算/生产规则通过后才能加入。保留35B历史失败证据和未来重新评估入口。产品决策本身不要求重测，但最终生产候选的S/B/O仍必须按规则完成。
- **必须支持35B**：先给出新的物理占用上界定义、归属和保守性证明，再新增 method 版本、原始采集字段、collector/evaluator实现与候选绑定，重新校准所有受影响模型。不能临时扣估算page cache或直接把 MemAvailable delta 当作物理上界。

修改位置：`plan/adr/decisions.md`、08/C02、02/§5及validation中的“当前决策”指向；历史测量正文保留，不伪装成新结果。
如果改变的只是测量method而安全余量仍为15%，`physical_reserved_bytes_from_peak` 的整数公式无需机械改动。只有公式本身改变时才版本化变更它。

验收：所有文档指向同一个已接受决策；候选模型与决策一致；超预算拒绝测试保持通过。若选择新method，必须重新核验实际目标硬件、三轮材料和最终候选证据，不以本次算术复核替代。

## R2：有界轮询、截止屏障与观测验证

涉及：`backend_control.py`、`process_observer.py`、`scheduler.py`、`control_recovery.py`、`runtime.py`，以及确有运营需求时的配置schema/renderer；规范在08/C03，C02内存规则保持独立。

### 契约与实现

1. **区分加载与加载后见证。** adapter当前已经请求控制面并探测health/slots；只有返回RUNNING才进入独立observer见证。明确adapter UNKNOWN是否应重试及由谁负责，不能仅修改 `_verify_running` 就声称覆盖了全部启动瞬态。
2. **独立验证窗口。** `verify_deadline = min(caller_deadline, now + verify_window)`。10s仅作为待实测的默认候选，不能用C02“停止后内存回收最多10s”冒充加载验证规范。正常可见性延迟应落在窗口内，缺observer等不可恢复配置错误应立即失败。
3. **三处检查时间。** 发起每次observe之前、observe返回后、sleep前均检查剩余时间；截止或无合格终态时显式返回UNKNOWN/超时原因，不返回未经验证的 `last`。sleep裁剪到剩余预算。load和stop两条路径一起修；保留各自终态条件。
4. **约束单次观测成本。** Docker调用timeout取默认上限与剩余预算的较小值；端口探测和第二个Docker调用前重新计算。可以对await加超时兜底，但 `asyncio.to_thread` 被取消不会终止底层线程，需同时限制底层阻塞调用并避免超时后继续发起下一轮，不能宣称套一层timeout就已回收线程/子进程。
5. **独立恢复预算。** 验证失败保持保守账本状态，必要恢复使用明确的新清理deadline；不能继续沿用已过期的请求deadline。恢复任务仍需去重、关闭准入、遵守lease排空和总清理期限，不无限延长原请求，也不提前释放未证停止的预留。
6. **控制轮询频率。** 先采用清晰、可测的固定间隔与总窗口，1s可作为候选；是否需要更短初始间隔、退避或额外采样次数上限，以延迟/调用次数数据决定。暴露配置时让schema、运行时装配、渲染和候选摘要一致，并拒绝0、负数、NaN、无穷值。无需同时引入所有调度机制。
7. **身份验证。** 已知身份时核对容器ID、StartedAt及登记身份；冷启动 `identity=None` 合法，按deployment/model/runtime/image及本站约定的candidate/config digest核对观察到的身份。不要加 `assert identity is not None` 破坏首次加载；不要只比较容器ID或随意混同candidate digest和config digest。
8. **观测新鲜度。** 在C03定义采样时刻含义、最大年龄和未来样本拒绝规则，再在写回前执行。当前2s常量属于C02内存样本；若C03选同值，应明确依据。相同时间戳仍在有效窗口内时，不应仅因“不严格递增”就误判；过时/未来/不匹配/截止后的样本不能写回实例账本。
9. **原因码与类型。** 使用有限稳定原因，如 `observer_missing`、`observer_failed`、`observation_unknown`、`verify_deadline_exhausted`、`observation_identity_mismatch`、`observation_stale`；STOPPED保留为状态。检查现有错误码映射，避免内部码未经转换破坏公共API。`identity`标注可空、返回类型明确、运行循环获取方式统一。

共享轮询helper仅在减少重复且保持load/stop语义清楚时采用；它不是验收条件。

### 必须通过的回归

- UNKNOWN→合格RUNNING：在第二次观察即成功，调用数**恰为2**；再取样应被严格夹具检测，而不是吞掉夹具异常后拖到超时。
- 持续UNKNOWN、observer抛异常、缺observer：分别断言状态、原因、次数/终止时点和预算保留。现有重复末项夹具适合持续UNKNOWN，无需删除。
- 剩余预算短于poll：截止后不发起observe；观测本身跨截止：拒绝迟到RUNNING；不以几十毫秒墙钟断言代替可控时钟。
- 陈旧/未来时间、错deployment/model/runtime/容器代际、身份为空的合法冷启动都有覆盖。
- 验证超时后恢复拿到新的有界预算，不能以请求已过期而跳过清理；无法证STOPPED时预算仍不释放。
- 负例用短的验证window或可控时钟，**不能只调小poll_seconds**；持续UNKNOWN仍会一直等到deadline。
- 真机O03再记录Docker观测次数/耗时、验证延迟、恢复启动时点、停止与静默证据；不把本次fake复现称为硬件结果。

## R3：把STOPPED事实送到账本，保持操作栅栏

涉及：`backend_control.py`、`scheduler.py`、`model_registry.py`及对应测试；同步C03和S02。

先满足R2中的身份、采样有效性和截止要求，再处理三态：

| 已验证结局 | bridge处理 | scheduler / Book处理 |
|---|---|---|
| RUNNING | 记录合格完整身份，返回健康RUNNING | 校验当前operation/epoch/generation后转READY。 |
| STOPPED | 提交停止采样和加载失败原因；仅在当前操作被接受后清除此代身份 | 在当前LOADING操作、无有效lease且停止证据归属一致时释放预留；记录加载失败，不能当作推理成功。 |
| UNKNOWN / 不合格样本 / 截止 | 不写回新身份，不伪造停止 | ERROR/关闭准入并保预算；通过有界恢复或已存在cleanup路径处理。 |

`Book.stopped()`目前只接受EVICTING。首选复用已有受控cleanup转移：在同一调度锁内验证原加载operation，转入允许清理的状态，取得合法cleanup operation，再消费仍有效的STOPPED证据；若现有转换不能安全表达，才增加一个范围很窄的“加载已证停止”账本方法。禁止为省事直接扩大 `stopped()` 接受所有状态，禁止绕过 `_operation` 栅栏。

STOPPED被当前操作接受后不能保留旧 `_instances`，但仅修改bridge返回值也不够；scheduler当前仍会将它送入 `book.failed`，这两个层次必须一起改。身份清理必须与操作接受协调，迟到的旧STOPPED不能先在bridge清掉新一代身份、再由Book拒绝旧操作。

验收：LOADING→已证STOPPED后双账本恰释放一次、身份清空、等待者可再次正常调度；UNKNOWN保留预留；旧operation/epoch/generation的STOPPED不能释放新一代实例；有lease不能释放；加载失败错误仍可诊断；不再为同一已证停止启动不必要的全局恢复。

## R4：补候选完整性，复用生产拒绝规则

涉及：`acceptance/candidate.py`、候选CLI/契约和相关测试；必要时与render/preflight共享纯校验函数。

- 第一片：生产候选生成时逐模型要求已测、物理峰值为正、材料引用完整匹配；在写出candidate前拒绝任何混合未测集合。保留lab登记/独占校准入口，避免把正常校准也禁掉。
- 当前candidate builder要求恰好一个measured模型；短期可以明确仅生成一个模型的完整候选。若发布范围要求多个模型，则下一片改为按model_id接收独立材料集合，校验覆盖完整、无重复/孤儿材料、身份与上界匹配。
- 复用 `require_production_openable` 和现有材料校验；render逐模型拒绝和preflight模式校验保持。若增加evaluator防御检查，应明确它用于证据一致性，而不是假定此前生产毫无门禁。
- 不用“任意一个模型已测”替代“每个生产模型已测”，也不因要发布而删除失败材料、伪填峰值或把lab manifest改个mode。

验收矩阵：全未测拒绝；已测+未测混合拒绝且不产生candidate；已测缺材料/错hash/错model拒绝；一个完整已测集合正常生成；支持多模型后完整两模型成功、缺任一份材料失败；lab仍可做显式预算的隔离校准，production仍拒绝lab产物。

## R5：修导航与已声明边界，保留历史证据

- `plan/README.md`补08入口，标注哪些是当前规范、哪些是基线快照、哪些是验收记录；05顶部链接当前进度。08开头的旧源码基线/“模块不存在”等也标为历史，不能只将README重定向到旧08 §1.1。
- 各Cxx约束与01–06保持明确引用关系；06仍是必测集合与发布门槛的规范来源，不把所有01–06降成“只有意图”。validation和P任务记录只记证据/状态，不隐式覆盖契约。
- 在C08补“文件系统权限允许连接且应用UID白名单允许服务”的关系；0660包括服务owner和group访问，不要求所有允许UID都属于客户端组。内核peer UID决定owner；同UID边界沿用现有明确声明，不新增未被需求要求的进程凭据体系。
- 区分连接层非白名单关闭与进入API后的`peer_forbidden`错误；既有Linux测试已经规定前者不返回HTTP，不将它机械改为403。

验收：规范链接有效；“当前/历史/未验收”能从入口辨认；同UID限制只有一致定义；保留历史SHA、失败记录和硬件范围。文档变更不宣称设备通过。

## R6：双入口共同就绪与失败清理

涉及：`run.py:serve_v2`、`control_server.py:ControlServer.start/stop`、TCP服务生命周期装配及集成测试。

目前启动顺序已有代码约定，但control会先accept。修复应明确：共享上下文/锁/boot_id初始化和恢复完成后，两入口都准备成功才开放业务请求；任何一个失败都撤销开放并清理另一个。
可以延迟accept，或用共享ready门屏蔽所有业务dispatch；具体方式按现有listener接口选择，不引入第二份lifespan。

Unix listener还应在服务请求前完成chmod/chown；启动失败路径包含权限、组解析和TCP绑定失败。最终cleanup保持幂等，连接任务、socket文件和共享清理各自有唯一归属。

验收：注入TCP端口占用、Unix权限/组错误、第二入口启动异常及启动期间取消；失败时另一入口不能执行模型/Blob业务，退出后无连接任务或socket残留、共享清理恰一次；正常启动两入口仍共享一个boot_id/Book/BlobStore。

## 不采纳的原修法与保留的验收工作

- 不因design-F03降低O01门槛；使用现有arrival builder，先做可复现的计划与真实运行，再据证据优化。
- 不因design-F04扩展audio case或自动延后embeddings/rerank；按既有闭集修实际失败的fixture。边界token应通过各能力实际模板/计数路径核算，不能硬套文字模板开销。
- design-F08可选补状态表及“tombstone持久化后重开数据库”的组合测试；不改已有活跃租约/重启保护优先级。
- 不因test-N01废弃重复末项observer；新增严格有界脚本和可控时钟以覆盖不同测试意图。
- 不把原审查“接受degraded才能签发”当成本次修复的前提，不需要先签发旧报告才可实施已授权的修复。

## 后续实施的交付要求

每个代码包使用Python 3.12，运行针对性回归及仓库规定的完整 `pytest tests -m 'not thor' -q`、`ruff check .`、`run.py --check-config`。
审查差异和敏感信息后在开发机原子提交；按AGENTS规定push、核对remote SHA、目标干净checkout fast-forward到相同SHA后测试。硬件相关包必须先核验实际设备，模型只读并验hash，记录运行命令、环境、内存、停止与静默证据，失败材料保留。
R1选新method或修改候选身份时，旧硬件报告不能自动继承；R2/R3至少复验正常冷启动、未知观察/恢复、停止和重载；R6在Linux重验双入口失败路径。
本次文档方案仅本地提交，不push、不同步目标，不将用户现有补丁纳入提交。
