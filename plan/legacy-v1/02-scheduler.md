# 02 状态机与调度算法

## 1. 不变量

1. 同一模型只有一个 generation；仅从确认 UNLOADED 开始新 load 时加一。
2. 每个请求最多一个 lease；lease 使用随机唯一 ID，不以 `in_flight -= 1` 作为释放操作。
3. 所有有效 lease 必须登记在模型 runtime；`in_flight = len(leases)`，不维护第二个计数。
4. READY 可以有 0..max_concurrency 个 lease；对外 `active` 仅是 READY 且计数大于零的展示值。
5. 自动淘汰和手动卸载都不能停止持有有效 lease 的模型。已取消的请求不再有完成权，允许清理终止其后台计算。
6. 未确认停止的模型保留预算；瞬时内存读数增加不能替代停止确认。
7. 一个生命周期 worker，最多一个 load 或一批 unload 在执行；慢 IO 不持有 condition。
8. 推理不经 llama-swap 自动路由，不自动重试；只有 begin_load 成功后才能发启动命令。
9. 排队、加载、连接、流未开始、流中断、服务退出等所有路径，都必须移除 waiter 或幂等释放 lease。
10. SSD、内存采样、受管实例身份未知时拒绝新准入；已有请求按故障/退出规则收尾。

## 2. 状态转换表

| 当前 | 触发与前置条件 | 下一状态 | 预算 |
|---|---|---|---|
| UNKNOWN | 启动清理得到 STOPPED 证据 | UNLOADED | 释放 |
| UNLOADED | 内存检查通过，分配 op/generation | LOADING | 立即计入 |
| LOADING | op 匹配、容器身份匹配、direct /health=200 | READY | 保留 |
| LOADING | 超时/启动失败/不确定 | ERROR | 保留，进入控制面恢复 |
| READY | grant/release 成功请求 | READY | 保留 |
| READY | 未知实例、传输异常、取消推理 | ERROR | 保留；现有其他 lease 不删除 |
| READY | 整批候选原子锁定，lease=0 | EVICTING | 保留 |
| EVICTING | stop 未发送，批处理回滚 | READY | 保留 |
| EVICTING | 已发送 stop 且结果不确定 | ERROR | 保留 |
| EVICTING | STOPPED 证据确认 | UNLOADED | 释放 |
| ERROR | lease=0，开始清理 | EVICTING | 保留 |

ERROR 不是“可以直接重试加载”的状态；先清理至 UNLOADED。
release 不可将 ERROR 自动改回 READY。过期 operation 回写抛 StaleOperation 并日志告警，不改变状态。
后台探测捕获 epoch、generation、operation_id、instance_id、采样起始时间；回写前校验一致且不早于最近已应用的探测。
不将 READY 的旧探测用于更新已经开始新一轮加载的模型。

## 3. lease 与取消的确定所有权

### 3.1 队列握手

每个队列项有 `WAITING / GRANTED / CLAIMED / CANCELLED / EXPIRED` 状态。
`request_id -> waiter` 字典为权威，排序列表只用于选取；最大 128 个 WAITING。
grant 在 condition 内完成：检查未取消/未过期 → `Book.acquire_ready` → 存 lease → 标记 GRANTED → 唤醒等待者。
等待协程被唤醒后，在同一 condition 内将 GRANTED 改 CLAIMED 并取走 lease。
grant 与 cancellation 的竞争由这个状态机决定，不依赖 Future.cancel 的时序。

```python
# acquire() 外层语义，具体数据结构见上文；取消回收要在屏蔽取消的清理任务中执行。
try:
    return await wait_and_claim(request_id, deadline)
except BaseException:
    # WAITING: 删除；GRANTED: 回收未被路由领取的 lease，按 REJECTED；
    # CLAIMED: 由路由持有者清理；每种状态仅一个所有者。
    await finish_cleanup(cancel_or_reclaim(request_id))
    raise
```

GRANTED 还未发送上游，因此取消回收使用 REJECTED，不隔离健康模型。
队列 deadline 从 enqueue 开始，**包括等待共享加载的时间**，直到 CLAIMED；默认 1800 秒。
过期先于 grant：在 `now >= deadline` 时只能 EXPIRED。边界不得同时得到 lease 和 504。
等待中的模型即使 pinned，也只按已设置 priority 排队，客户端不能覆盖优先级。

### 3.2 已领取 lease

路由取走 lease 后立即安装外层 finally，先于任何后续 await。
上游未发送就取消：REJECTED；上游已开始发送后取消/超时/协议失败：ABORTED。
ABORTED 先关闭对应上游响应，撤销 lease，模型进入 ERROR 并阻止新请求；不假设后台计算已经停止。
其他有效 lease 允许自然结束；最后一个结束后清理容器。pinned 也允许这种故障清理，然后重新 preload。
这一策略刻意牺牲一次取消后的模型复用，避免未结束的后台生成与下一请求争用 slot。

首版 `max_concurrency=1`；允许配置更大值，但必须同步 llama.cpp `--parallel` 并重新测量预算。
并发数定义为有效网关 lease 数；后端可能短暂还有已取消计算，但 ERROR 阻止新增请求，cleanup 终止遗留计算。

清理通过被持有强引用的 asyncio.Task 执行，屏蔽客户端任务取消；关闭响应上限 5 秒，随后仍须执行 registry release。
若关闭失败，按 ABORTED 处理。不能只 `await asyncio.shield(coro)` 后忘记保存和等待子任务。
lifespan shutdown 必须 join 所有清理任务；超出退出期限时进程退出，下次启动清理所有受管容器。

## 4. 内存预算

采用系统统一内存；`resources.provider=psutil`，Linux 上取 `psutil.virtual_memory().available`。
tegrastats/nvidia-smi 是诊断值，不参与准入、不与系统 RAM 求和、不做自动口径回退。
原 `total_memory_bytes=0` 迁移为读取物理总内存；非零旧值迁移需显式改为新预算，禁止假装改变物理 RAM。

```python
R = ceil(reserved_bytes * (1 + resource_safety_margin))
B = total_system_bytes - system_reserve_bytes - min_free_memory_bytes
can_load = (
    sample_age <= resource_sample_max_age_seconds
    and available_bytes >= min_free_memory_bytes + R
    and committed_bytes + R <= B
)
```

默认：安全裕量 0.15，系统预留 8 GiB，空闲硬底线 2 GiB，采样周期 1 秒，最大采样年龄 2 秒。
`committed_bytes` 是各模型 R 的和，包含未证实停止的 UNKNOWN/ERROR/LOADING/EVICTING。
两个门槛是独立的 AND：不能把 R 再从 MemAvailable 减一次，也不能用预算剩余替代实时内存。
READY 的新 lease 不重复预留 R；它仍须满足依赖健康、SSD 有效、资源快照有效及 available >= 硬底线。
R 要覆盖该模型配置上下文、batch、KV cache、并发的峰值；预热后的内存不是完整运行预算。
这是保守准入策略，不是 OS 内存隔离；无权限阻止其他宿主机应用突然分配大量内存。

单模型 R>B：配置校验失败，部署不启动。临时外部内存压力：请求等待到 queue deadline。
不通过停止 active/pinned 模型强行腾空间；状态提供 blocked_reason。

### 淘汰决策

实时缺口：`max(0, floor + R - available)`。
预算缺口：`max(0, committed + R - B)`。
目标释放量：两个缺口的最大值。候选按 core.py 的分数及稳定 model_id 排序，取满足目标的最小前缀，上限 8 个。
选不出完整前缀时不进行部分无意义淘汰，等待请求结束/内存变化。
在 condition 内一次性验证并将整个前缀标记 EVICTING，然后锁外顺序停止。
每个 STOPPED 确认才释放其预算；任何失败：失败项 ERROR，未发 stop 的后续项 rollback_unsent；结束本批。
成功后每 0.2 秒重采样，最长 10 秒等待内存归还；超时不 load，继续有界等待，不拿估计释放量替代实测。
下一次 load 前必须用新快照再次同时检查两个门槛。

## 5. 有界队列、公平性和切换意图

排序键见 core.py：`(-(priority + floor(wait_seconds / 30)), enqueue_sequence)`。
每次唤醒重新按当前 monotonic 时间排序，不能把变化的优先级永久塞进静态 PriorityQueue。
入队、release、操作完成、资源变化会 notify；timer 至少每秒运行一次，处理 deadline、TTL 和老化。
队列满返回 429 `queue_full`，Retry-After=1；队列过期返回 504 `queue_timeout`，从字典删除。

快速准入循环：先清除过期项；按排序为 READY 且未冻结、未满并发的请求发 lease。
冷加载进行中仍允许其他 READY 模型获得 lease。

生命周期循环优先级：全局恢复/故障清理 → 手动卸载 → 已建立的切换意图 → 最高排序冷请求 → TTL。
冷请求即 WAITING 且目标 UNLOADED。LOADING 的等待者共享一次 load，不重复排 load。
预热视为 priority=1000 的内部需求，最多每模型一个，不占用户 128 项容量。

### 防止热门模型让冷模型永久无法加载

对最高排序冷请求建立唯一 switch intent；需求存在且未过期期间不被新到请求替换。
如果空闲候选不够，按同一评分把**允许淘汰但仍有 lease**的模型也纳入计算；满足完整缺口后冻结这组候选的新准入。
冻结不停止现有请求；只设 admission_blocked。计算中排除 target、pinned、evictable=false、非 READY 模型。
冻结后快速准入不能绕过该组模型；其他 READY 模型照常服务。
最多冻结 30 秒。到期未排空：解除冻结，该目标设置 next_switch_attempt_at=now+30 秒，允许其他冷目标获得意图。
旧目标下一次 eligible 时按原 enqueue 时间继续竞争，不能重置其老化。
全部需求取消/超时：立即解除冻结并删除意图；已开始的 load 仍由生命周期 worker 收尾。
资源外部压力导致无完整候选集：不冻结任何模型，记录 waiting_for_memory。
冻结模型都空闲后，重新采样及重新检查，原子 begin_eviction；不复用过期缺口。

## 6. TTL、热度、pinned 和切换限速

- `pinned=true`：禁止资源淘汰、TTL、用户手动卸载；必须 `evictable=false`、`ttl_seconds=0`、`preload=true`，否则配置报错。
- `preload=true`：启动恢复完成后发内部加载需求；模型故障清理后重试，间隔 5/10/20/30 秒，随后固定 30 秒；只保留一个重试定时器。
- `evictable=false`、非 pinned：不自动淘汰/TTL，用户可以在空闲时手动卸载。
- 手动卸载 pinned 返回 409 `model_pinned`；无 force 参数。加载/卸载/冻结/有效 lease 存在返回 409 `model_busy`。
- TTL 从最后一个有效 lease 的结束开始；从未使用过的模型从 loaded 时间开始；TTL=0 禁用。
- 同一 tick 中用户需求优先于 TTL；存在该模型 WAITING 项时不执行 TTL。
- heat 独立保存 last_updated；成功 grant 记一次 request_weight；成功完成只加 tokens×token_weight。
- release的tokens=None表示usage未知，tokens=0表示后端明确返回零；未知不增加token热度，不从字节数猜token。
- total_requests在grant时加一（含GRANTED后被取消、尚未发送的请求）；成功完成tokens为None才增加usage_unknown_requests。
- REJECTED/ABORTED不计usage_unknown或total_tokens；重复release不重复计数。排队即取消且未grant不计total_requests。
- 删除旧 active_bonus 配置：active 已不是候选，不需要奖励；迁移检查要说明这个字段被移除。
- 成功冷加载时间进入 deque；保留最近 10 秒。若已有 3 次，设 `not_before=max(last_success+15, oldest+10)`；到时重算。
- READY 准入、取消、健康探测不等待 cooldown；停止/故障清理不受限速。加载失败按故障退避，不计成功切换数。

## 7. 外部状态确认与迟到启动

`process_observer` 通过 `docker inspect` 读取固定容器名，核验项目标签、model_id、镜像摘要、配置指纹和 StartedAt。
仅允许检查 manifest 中的名字，命令以 argv 数组执行；不得拼 shell 或扫描后批量操作其他项目容器。
RUNNING 必须容器身份匹配、容器 Running=true、direct /health=200；/running 只作控制面交叉检查。
STOPPED 必须容器不存在或 Running=false、固定端口未被占用、控制面对应操作已终结。
只要控制面有潜在未结束启动，则 STOPPED 证据不成立，即使本次 inspect 恰好没有容器。

**加载超时/启动结果不确定统一触发全局控制面恢复，不立即重试单模型。**
原因：超时的 HTTP 不一定取消 llama-swap 内部启动任务，简单 stop+立即重试无法排除旧任务迟到创建容器。
恢复步骤写死如下：

1. 在condition内先结算失败load所属模型的WAITING：load超时504 model_load_timeout，其他load失败502 upstream_unavailable；
   然后ready=false，拒绝新请求503，其余WAITING失败503 service_unavailable。无失败load的恢复统一使WAITING失败503。
   同一请求只由先发生的完成事件结算一次；若queue deadline已先到期，保留原queue_timeout。
   调用Book.begin_recovery()递增epoch、封闭所有准入、清除旧operation_id；预算及有效lease保留。
2. 现有有效 lease 继续至完成，最多 shutdown_grace_seconds=30 秒；到期撤销剩余请求并关闭上游。
3. 回收所有 lease 后，取消并join旧epoch的control/probe任务（清理上限10秒；超时记错误但旧epoch不能回写），
   调用受控恢复 helper 停止 `llama-swap.service`，确认 unit inactive 且 cgroup 无进程。
4. helper 只停止带本项目 deployment_id 标签的四个指定容器；不匹配标签则退出并保持错误，不停止未知容器。
5. helper确认全部目标容器停止、端口释放，重启没有preload的llama-swap，返回stopped_models；scheduler核验/health、配置和模型清单。
   在condition内调用Book.finish_recovery(epoch, frozenset(stopped_models))；必须覆盖全部模型且lease为空，才原子重置UNLOADED和预算。
6. 重新执行 preload，成功后 ready=true。恢复失败采用 5/10/20/30 秒退避，不开放准入。

加载失败若已被 adapter 证明“根本未发送请求”，可直接 ERROR 后清理，但实现首版允许统一走上述保守恢复。
正常 READY 模型 stop 返回超时也走全局恢复。已取消推理的单模型清理，在控制面明确健康且无 pending load 时可正常 stop。
全局恢复与关机是唯二可在 grace 到期后终止其他请求的管理事件，必须记录原因；正常自动淘汰不可这样做。

## 8. 启动、健康和关闭

启动顺序：严格加载配置 → 进程锁 → 创建后台恢复task并让lifespan立即yield，先开放HTTP listener。
后台执行依赖/SSD探测 → **复用第7节全局恢复helper**（停旧控制cgroup→清容器→重启），不能只清容器而留下旧pending start。
然后finish_recovery → preload → readiness。lifespan不得await恢复或preload完成才yield，否则/live无法在依赖故障时响应。
配置不合法直接退出 78；运行依赖暂不可用时保持 /live=200、/health=503 并按退避重试。
启动阶段不收推理，模型状态 UNKNOWN；UNKNOWN 全预算只是防御账本，清理完成前不参与新请求决策。

后台每 2 秒对账，受管容器消失或身份变化：模型 ERROR，阻止新增 lease；原请求通过传输错误/自身 deadline 结束。
llama-swap 全局不可用或出现未知受管实例：进入全局恢复。
资源快照无效/过期：readiness=false；恢复有效采样后可恢复，不必重启容器。
SSD mount UUID 变化、掉盘或文件不可读：readiness=false，待处理项503，取消所有在途请求，关模型；禁止回退根盘目录。
挂载恢复后必须再次核对 UUID、文件元数据、manifest；文件变化重新校验 SHA256 后才 preload。

SIGTERM：拒绝新请求、所有队列项503 → 最多30秒排空 → 取消剩余 lease并清理 → 停止受管模型 → join任务并关闭httpx → 退出。
systemd TimeoutStopSec=120；如果外部控制面卡住被 systemd 终止，下次启动继续清理，不能假定退出时已释放模型。
