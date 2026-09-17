# 02 调度、许可、内存与恢复

## 1. 对象分离

| 对象 | 所有者 | 生命周期 | 持久化 |
|---|---|---|---|
| Job/Stage/Unit attempt | runner | 整片/阶段/音频块或镜头 | SQLite，显式恢复 |
| PhasePermit | scheduler | 阶段驻留、工作区和互斥槽 | 否；重启失效 |
| Execution lease | scheduler | 真实计算，包括取消后未结束部分 | 否；重启先清理 |

HTTP 断开不是 job 取消；permit 过期不是计算停止；对象析构不是 GPU 释放。
permit 绑定随机 boot_id、permit_id、job_id、stage_id、attempt、model_id（媒体阶段 null）。
提交/续租/清理及回写核对 token；旧 boot 或旧 attempt 为 stale，不修改状态。每次进程启动生成新 UUID boot_id。

## 2. 串行政策

首版 pipeline 的全局 workload_slots=1、unit concurrency=1。交互模式保留配置内请求并发，
但 pipeline permit 与全部交互、preload、后台加载互斥，覆盖加载和真实计算。
队列键 `(-priority-floor(wait_seconds/30), sequence)`，priority 0..1000、pipeline 默认50；容量128，phase 等待上限1800s。
授予 phase 前：同锁设置 drain 意图 → 拒绝新交互准入 → 等既有 lease 结束（最多30s）→ 停其它模型并证实停止。
drain 超时撤销冻结，原 waiter 保留，30s 后重试，不能杀死有效请求。
随后原子预留模型/工作预算、占槽、启动目标；只有此 phase 可执行。结束必须确认清理，不能只靠 TTL 切换。
balanced-v1 不允许 pinned/preload 模型，配置加载即报错。每阶段 hard deadline 固定于 profile，heartbeat 不能延长它。
并行需要新 profile 和组合峰值/公平性验收，首版不支持白名单绕过。

## 3. 模型状态

UNKNOWN/UNLOADED/LOADING/READY/EVICTING/ERROR 沿用当前核心。只有 UNLOADED 可 begin_load 并加 generation。
跨 IO 回写核对 boot/epoch、generation、operation_id。一个 lifecycle worker，锁内没有 IO 或 sleep。

| 事件 | 确定行为 |
|---|---|
| 启动未知实例 | UNKNOWN，关闭准入、保持配置预留，清理成功才 UNLOADED |
| load 完成 | 实例身份+ready 探测一致才 READY，单独 HTTP200 不足 |
| load 超时/失联 | ERROR，保留预算，迟到成功不得覆盖 |
| 正常淘汰 | 无 execution lease、无 phase pin 才允许 |
| stop 成功但进程仍活 | 保持 EVICTING/ERROR 及预算 |
| STOPPED 证据完整 | UNLOADED，释放实例预算 |

STOPPED=容器已退出/不存在 AND 无未完成启动操作/启动子进程 AND 固定端口无人监听。
Docker 不可达、端口未知占用、解析失败均 UNKNOWN。身份包括 container ID、StartedAt、deployment_id、
model/runtime ID、candidate digest、image digest；禁止按名称停止未知容器。
取消交互请求不能杀掉同模型其它有效 lease；先冻结新请求，等其它 lease 完成再清理。

## 4. Phase 与 execution

Phase：PREPARING → ACTIVE → DRAINING → CLOSED；无法确认清理为 BLOCKED。
准备超时也进入 DRAINING。ACTIVE 每10s heartbeat，TTL30s；`now>=expires_at` 即过期。
PREPARING初始expires_at=min(准备开始monotonic+900s,phase hard_deadline)，不使用30s心跳TTL；
activate先检查准备期限，过期的迟到load成功只能清理，不能复活许可。ACTIVE续租不能延长hard_deadline。
过期/主动 close/job cancel：原子关 submit/renew → DRAINING → cancel execution（等10s）→ stop专属容器（grace30s）。
总清理期限60s；之后未确认停止则 BLOCKED，保留全部预算和槽，health503，每5s对账。
STOPPED 才 CLOSED；重复 close 幂等。成功阶段也按此清理，不跨阶段保留模型。
worker 在 CUDA 同步完成后才能报告 execution terminal；terminal 有 generation/attempt，未知响应当未终结。
执行完成可释放 execution lease，但 phase/model reservation 仍保留。取消后迟到成功不能提交产物。

## 5. 内存

单位 byte，GiB=2^30。Linux MemAvailable 为唯一实时准入指标；sample 最大年龄2s，未来时间戳无效。
`R_m=ceil(measured_model_peak*1.15)` 包含权重、KV、推理 workspace；
`R_w=ceil(measured_work_peak*1.15)` 只含额外解码/编码/帧缓冲；归属写入测量，不重复计。
`C`=未证实停止的模型+未关闭phase承诺；`B`=受管总预算；`F`=瞬时余量（默认2GiB）。
新负载增量 N=尚未预留的 R_m+R_w；准入条件同时满足：

```text
sample valid AND storage ready AND identities known AND workload slot free
C + N <= B
MemAvailable >= N + F
```

另有静态约束 B<=MemTotal-system_reserve（默认8GiB）；实时门槛不再重复减 system_reserve。
每 unit 开始检查输入在测量 envelope 内、sample新鲜、MemAvailable>=F；不重复扣已预留内存。
采样低于 F 停新 unit，取消当前计算并清理；不能保证采样间峰值不会OOM，仍需最大负载实测。
停止后重新取得新 sample 才准入，内存未回收则等待最多10s，失败保持不可准入。
没有测量时仅可 exclusive calibration：所有其它受管负载停止，显式临时预算；不开放服务。

## 6. 恢复

- scheduler 重启：新 boot_id，先关闭准入、清理该 deployment 所有旧受管实例，禁止接管旧租约。
- runner 重启：运行中的 attempt → INTERRUPTED，job → PAUSED，等待显式 resume。
- runner 消失：permit 到期由 scheduler 清理；恢复不得绕过旧实例停止确认。
- 模型盘故障：撤销permit、关准入并清理；根盘空目录不能当作恢复。
- 工作盘故障：禁止 artifact commit，job PAUSED，停止模型；校验 DB/产物后显式恢复。
- 外部服务不确定：EXTERNAL_UNKNOWN，不自动重发可能重复计费的调用。

SQLite 位于系统盘 `/var/lib/self-model-switch/jobs.sqlite3`；模型盘和工作盘分角色配置。
全局恢复提升 epoch/使旧operation失效，确认停止、重新hash后才开放；不自动重放任何推理。
