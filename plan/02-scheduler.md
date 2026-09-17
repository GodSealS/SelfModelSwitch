# 02 模型状态、资源与切换

## 1. 三类对象

| 对象 | 用途 | 终结条件 |
|---|---|---|
| 模型实例 | 已加载权重、runtime 及模型工作区 | 独立 STOPPED 证据 |
| 请求 execution lease | 覆盖真实推理与取消后未结束计算 | 可信终结且 compute_quiescent=true，或独立停止证据 |
| 通用 ModelSession（拟议） | 客户端连续使用一个模型，固定驻留与独占准入 | 关闭提交、清理 execution、确认模型停止 |

会话不理解影片阶段，也不持久化业务任务。进程重启产生随机 boot_id，旧 session/token 失效。
异步回写同时核对 boot_id、generation、operation_id、execution attempt；迟到结果不得修改新实例。
时间预算使用 monotonic；记录和报告使用 UTC。

## 2. 模型状态与停止

沿用 UNKNOWN/UNLOADED/LOADING/READY/EVICTING/ERROR。只有 UNLOADED 可开始新 load 并提升 generation。
启动未知实例时关准入并保留预留；实例身份和 ready 探测一致才 READY，HTTP 200 单独不足。
加载超时/失联进入 ERROR，保留预算；迟到成功不能复活失败操作。
有效 execution/session 存在时拒绝普通卸载。只有证明停止才释放预算。

STOPPED = 容器已退出/不存在 AND 无未完成启动操作及启动子进程 AND 固定端口无人监听。
Docker 不可达、端口未知占用、解析失败均为 UNKNOWN。
身份包括 container ID、StartedAt、deployment_id、model/runtime ID、candidate digest、image digest。
禁止按容器名称停止未知实例；shutdown、recover、取消和切换使用同一证据规则。

## 3. 通用独占会话

会话 PREPARING → ACTIVE → DRAINING → CLOSED；清理不能确认则 BLOCKED。
授予前同锁冻结新交互准入，等待已有 lease 结束（最多30s），然后停止其他模型并确认。
drain 超时撤销冻结，保留 waiter，30s 后可重试；不得为新会话杀死有效请求。
显式 pinned/preload 与独占会话冲突时拒绝会话，不能暗中删除配置；客户端部署可选择关闭这些配置。
会话队列容量128，等待上限1800s，键为 (-priority-floor(wait_seconds/30), sequence)，priority 0..1000。
锁内只改状态，无 IO/sleep；生命周期 IO 由单一 worker 串行执行。

准备期限最多900s且不超过配置 session hard deadline；ACTIVE 每10s heartbeat、TTL30s。
now>=expires_at 即失效，heartbeat 不延长 hard deadline；到期后的 load 成功只触发清理。
关闭/过期原子禁止 submit/renew，取消执行等10s，stop grace30s，总清理期限60s。
超时为 BLOCKED，保留槽和预算，health503，每5s对账；确认 STOPPED 才 CLOSED。
正常结束也完成相同清理；重复关闭幂等。到期不代表 GPU 已释放。
会话期限由模型服务配置控制，客户端不能无限延长；独立视频项目按剩余期限分批申请会话。

## 4. 请求取消与恢复

HTTP 断开只能触发该请求的取消，不能推断计算停止或取消客户端自己的持久任务。
同模型有其他有效 lease 时，冻结新请求并等其他 lease 完成后再清理，不杀死它们。
worker 在设备同步完成后报告可信 terminal；响应缺身份或 quiescent=false 视为未终结。
取消后迟到输出标为不可提交；客户端独立验证 attempt 并决定产物提交。

scheduler 重启先关准入，清理该 deployment 旧实例，确认停止后开放，不接管旧 session/lease。
模型盘故障关闭准入并清理；recover 重新确认挂载、全量 hash 和停止状态，不自动重放推理。
客户端断线由会话 TTL 触发资源清理；它的业务恢复由客户端负责。

## 5. 内存准入

单位 byte，GiB=2^30。R=ceil(measured_model_peak*1.15)，包含模型、KV、推理 workspace 和 adapter 预处理。
v2 的 `reserved_bytes` 已经是 R，用精确整数 `(measured_peak*115+99)//100` 计算，避免 float 在边界上的取整差异；
内部统一 `effective_reserved_bytes`：v2 原样使用，v1 只在兼容转换处按原 margin 乘一次，任何路径都不二次乘 margin。
C 为未证实停止的所有模型预留，B 为受管预算，F 为实时余量（默认2GiB），N 为尚未预留的新增 R。
同时满足：存储就绪、身份明确、槽可用、C+N<=B、MemAvailable>=N+F。
MemAvailable 样本最大年龄2s，未来时间戳无效；静态 B<=MemTotal-system_reserve（默认8GiB）。
实时不重复扣 system_reserve，已驻留模型执行前只检查 envelope、样本新鲜、MemAvailable>=F。
低于 F 停新执行并取消清理当前受管计算；不能保证采样间无 OOM，必须实测最大组合输入。
停止后重新采样，内存未回收等待最多10s，仍不足则保持不可准入。

实时门槛之外另有静态物理门槛：每个未 STOPPED 实例按 `physical_reserved_bytes=ceil(physical_resident_peak_bytes*1.15)`
计入，总和不得超过 B。`physical_resident_peak_bytes` 是该模型及其 adapter 在运行窗口内不重复计数的物理占用上界，
未测保持 null。首版采集方法固定为 `system_nonfree_upper_bound_v1`：同窗口逐样本取 `MemTotal-MemFree`，
每轮取最大、三轮再取最大；它包含 OS、page cache 和其他进程，不是“模型净占用”，跨模型相加可能保守重复计入背景占用。

本项目不维护视频工作区 R_w/阶段预算。其他进程消耗通过实时余量约束反映，客户端另外约束自身峰值。
无测量值只允许关闭服务准入的独占 calibration，使用显式临时预算；测量后形成正式 candidate 再验收。
未测登记只能停留于登记与隔离校准：`measurement_ref` 为空或 `physical_resident_peak_bytes` 为 null 时不得生产开放。
