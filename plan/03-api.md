# 03 模型服务接口

## 1. 现有兼容接口

保留 `/live`、`/health`、`/v1/models`、`/v1/chat/completions`、`/v1/embeddings`、`/v1/rerank`、
`/api/status`、`/api/models`、`POST /api/models/{id}/unload`、`POST /api/recover`。
现有参数、响应、SSE 和取消行为由既有测试保护；模型列表来自配置，查询不加载模型。
未知模型404、能力不符422、有效租约/会话阻止卸载409；队列忙不等于 health 不健康。
status 展示实际 boot_id、模型状态、execution 数、预留字节和 readiness_reason，不暴露完整控制 token。

工具调用/思考兼容增量见[执行计划](tool-calling-and-reasoning/README.md)及
[TC01—TC09接口契约](tool-calling-and-reasoning/contracts.md)。其中输出预算兼容例外已由 CT02 交付：
兼容 chat/vision 请求先经 `envelope_validator.prepare_chat` 规范化并生成唯一派发体（原 payload 不被修改），
预算缺省补 `min(4096, envelope.max_output_tokens)`、显式支持的别名字段保留原名并裁剪到封套上限、
多个预算字段/非正整数/`n≠1` 一律 422 `contract_violation`；计数与网关消费同一字节体，见 TC02。
模型能力与internal operation分离、工具/思考字段规则仍为待实施规范，
控制协议v1不因此增加工具operation。

## 2. 拟议通用控制接口

以下协议的 DTO、限制与错误表已由 P02 固定（见 §5），HTTP 路由、身份接线与 Blob 实现仍待 M04；本节不代表接口已可访问。
本机 Unix socket `/run/self-model-switch/control.sock`，0660，服务账号所有，客户端组控制访问并检查 peer UID。
接口语义与视频领域无关，其他本机客户端使用同一协议。

| 操作 | 输入 | 行为 |
|---|---|---|
| POST /internal/sessions | model_id、idempotency_key、可选 opaque correlation_id | 202 session handle，服务推导预算/期限并排队加载 |
| GET /internal/sessions/{id} | 身份验证 | 状态、owner 专属 token、到期剩余毫秒、错误 |
| POST /internal/sessions/{id}/heartbeat | session token | 200续租，过期/旧boot409 |
| POST /internal/sessions/{id}/close | session token | 202清理中或200closed |
| POST /internal/executions | session token、operation、input、parameters、idempotency_key | 202 execution_id；仅登记能力和 envelope 内请求 |
| GET /internal/executions/{id} | 身份验证 | 状态、结果引用、错误、compute_quiescent |
| POST /internal/executions/{id}/cancel | session token | 202 cancelling或200已终结 |

session token 绑定 boot/session/model/owner；execution 再绑定 generation 和 attempt。
相同 owner+幂等键+规范 payload hash 返回原对象，不同 payload 返回409；服务重启后旧 token 失效，客户端不能盲重发。
所有查询和变更检查归属；operation、参数、输出格式由 model adapter 白名单定义，调用方不能自选 argv、URL、端口和字节预算。
M01 必须锁定 DTO、限制、幂等保留期、协议版本、结果引用生命周期和错误映射，再开始跨项目接入。
该通用协议替代旧 v2 的 job/stage/phase permit 契约。

## 3. 模型数据访问

兼容 JSON API 保持原传输方式。通用 worker 输入采用有界内联数据或预登记 BlobRef，禁止任意宿主路径/远程 URL。
拟议本机 Blob API：POST /internal/blobs 有界流式暂存，GET /internal/blobs/{id} 校验 owner/hash 后读取，DELETE 显式释放。
BlobRef 绑定 owner、sha256、size、媒体类型和 opaque ID；模型服务验证根目录、无 symlink、regular file、配额与剩余空间。
执行持有 blob 读取租约，期间禁止删除；结果写入服务受限暂存区，经校验后发布引用。
默认结果终结后保留24小时，过期410；未终结执行的引用不清理，空间不足拒绝新写入。
客户端应及时复制到自己的持久产物库并校验 hash；服务重启保留已完成 blob 的元数据和归属，重新校验后开放读取。
这是通用传输暂存，不能保存电影 job 或充当视频检查点。临时目录/配额纳入模型服务部署和故障验收。

vision adapter 可扩展既有 chat data URL 输入；audio/embedding 等能力按实际登记 adapter 提供有界执行。
不在此定义切片、字幕、人物、声纹聚类或镜头结果结构；这些是客户端契约。
新 HTTP 专用路由是否提供由 M01 能力矩阵决定，不把旧混合方案的 audio API 当作已交付接口。

## 4. 通用错误和响应

新增 DTO 严格拒绝未知字段、重复 key、bool 冒充整数和非有限数；旧 chat 透传兼容行为保留。
错误体 `{ "error": { "code": "...", "message": "...", "retryable": false }, "request_id": "..." }`。
400语法错误、404对象不存在、409冲突/旧token/忙、410引用过期、413负载过大、415格式不支持、422契约或envelope错误、
429队列满、502后端失败、503资源/存储/实例未知、504排队或执行超时；错误响应不能伪造成功或 SSE DONE。
上游不自动重试不确定执行；terminal 必须携带完整实例身份且 quiescent=true。
日志记请求 ID、模型、hash、耗时和状态，不记原始媒体、完整台词或凭据。

## 5. 控制协议 v1 已固定契约（M01/P02）

`model_scheduler/control_protocol_v1.py` 是 DTO、限制、错误码表与 schema 的唯一来源；下列数字由该模块与
`tests/test_control_protocol_v1.py` 共同锁定。HTTP 路由/身份/Blob 实现仍待 M04（P17/P18）。

- 版本：`PROTOCOL_VERSION=1`；新增 control 请求须带 `X-SMS-Protocol-Version: 1`，缺失或不支持返回 400 `unsupported_protocol`。
- 限制（按规范 JSON 字节计）：inline 输入 ≤4 MiB、parameters ≤8192 B、Blob 1 B–1 GiB、幂等键/关联 ID/请求 ID ≤128 字节、
  结果引用保留 86400 s。
- 会话视图字段：session_id、state（preparing/active/draining/blocked/closed）、phase（null/queued/loading/draining_existing）、
  boot_id、model_id、expires_in_ms、hard_remaining_ms、owner_token、error；owner_token 恰在 closed 时为 null。
- 执行视图字段：execution_id、state（queued/running/succeeded/failed/cancelling/cancelled）、dispatch_state
  （not_started/dispatched）、compute_quiescent、result、error、instance、fence。terminal 必须 quiescent=true；
  succeeded 必须带 result，failed/cancelled 必须带 error；dispatched 必须带完整实例身份，not_started 不得携带容器身份
  （排队取消不需要伪造容器）。
- fence：(boot_id, model_id, generation, operation_id, execution_id, attempt)；execution 的 attempt 从 1 起，
  非 execution 的最后两项为 null；执行视图的 fence 必须指向该 execution。
- 参数闭集：chat=`max_tokens`、`temperature`(0..2)、`top_p`((0,1])、`seed`(0..2^31-1)、`ignore_eos`(boolean，验收用：
  请求必须真正消耗声明的输出预算，模型自然提前停止会让边界无法证明)；vision 另加 `text`；
  embeddings=`encoding_format`（首版仅 float）；rerank=`top_n`、`return_documents`。默认值与 envelope 截断由执行层应用。
- 错误码：24 个固定码，HTTP 状态与 retryable 以模块 `ERROR_STATUS`/`RETRYABLE_ERROR_CODES` 为准；
  错误体中的 retryable 必须与表一致，否则拒绝。
- schema 导出：`python -m model_scheduler.control_protocol_v1 export-schema --output schemas/control-v1.json`；
  文件只从 DTO 与常量生成，测试重导出并按字节比较。
