# 03 API、内部接口和错误约束

## 1. 通用规则

所有 JSON DTO 使用 reference/contracts.py 的 `extra=forbid, strict=True`；chat 为向后兼容保留原有透传字段。
UTC 用 RFC3339 Z，业务时间用整数毫秒，区间全部半开 `[start_ms,end_ms)`。
整数不能是 bool/字符串；非有限浮点数拒绝；未知字段拒绝；重复 JSON key 和 YAML key 拒绝。
服务端生成 request_id；日志仅记 ID、耗时、状态、模型/产物 hash，不记原音频、图像、完整台词或凭据。
错误体统一：`{"error":{"code":"...","message":"...","retryable":false},"request_id":"..."}`。
4xx/5xx 不返回伪成功 JSON；流已经提交后发生错误关闭流，不伪造 DONE。

## 2. scheduler HTTP（8090）

保留现有 `/live`、`/health`、`/v1/models`、`/v1/chat/completions`、`/v1/embeddings`、`/v1/rerank`、
`/api/status`、`/api/models`、`POST /api/models/{id}/unload`、`POST /api/recover`。
模型列表来自配置，不限定四个 ID；查询不得触发加载。不存在模型404、能力不符422，均不进入队列。
旧三类 API 的参数、响应和 SSE 保持现有测试约束。卸载持有 phase pin/有效execution 的模型返回409 model_busy。
live 只反映进程事件循环；health 只有资源/存储/实例身份可确认且无 BLOCKED 才200，正常队列忙不算不健康。
status 新增 boot_id、active_permit（不暴露完整secret）、execution_count、reserved_model_bytes、reserved_work_bytes、
readiness_reason；这些均来自实际账本，不能硬编码0。

### 2.1 VLM

继续使用 `/v1/chat/completions`，包含图片时必须有 vision 能力。
只允许 `content=[{"type":"text","text":"..."},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}]`。
也接受 PNG data URL；拒绝 HTTP URL、本地路径、SVG、其它 MIME 和非法base64。限制解码后像素/张数并校验实际文件格式。
文字请求仍按 chat 能力；vision 模型需同时配置 chat 才允许纯文字。runner 发图前压缩到 envelope，不能静默截断图片。
请求体默认16MiB（从旧4MiB迁移时明确改变）；解码后的内存也受像素envelope约束，不能只看压缩体积。

### 2.2 音频

新增 `POST /v1/audio/transcriptions`，multipart 字段精确为 model、file、response_format、max_new_tokens。
response_format 仅 `verbose_json`，默认该值；max_new_tokens 默认模型envelope值且不得超过它。
只接收 PCM WAV（16kHz、单声道、16bit），最大音频时长来自envelope（balanced默认1500000ms），最大上传64MiB。
临时文件流式写入受限目录，大小超限413，格式/时长不符422；不把整个文件读进scheduler内存。
返回 `{"model":id,"duration_ms":N,"segments":[{"start_ms":0,"end_ms":1234,"speaker":"S01","text":"..."}]}`。
该路由是有界同步请求，断连可取消；整片任务必须走 runner。它通过相同互斥/内存入口，不能旁路执行。
不同模型timeout从已测部署配置读取；排队deadline和执行deadline分别计时，socket read-idle默认60s，
worker执行采用提交后轮询，不要求60s内生成文本。上游不自动重试。

## 3. runner HTTP（8091）

仅读取受控 input root 已登记的影片；`input_id` 是 registry ID，不是客户端路径或URL。
input registry由本机CLI `python -m video_runner input add --file <absolute-path-under-input-root>` 创建：
检查regular file、每级路径无symlink、hash和metadata前后一致，登记ID。上传影片API不在首版。
create必须提供`Idempotency-Key`（1..128个ASCII可打印非空格字符），作用域为endpoint+key，持久化唯一约束。
相同key+规范化请求hash返回原job（200）；不同请求hash返回409 idempotency_conflict。

| 方法/路径 | 请求 | 成功与行为 |
|---|---|---|
| POST /v1/jobs | CreateJob | 202，Location和JobView；后台任务不依赖连接 |
| GET /v1/jobs/{id} | 无 | 200 JobView，未知404 |
| GET /v1/jobs/{id}/artifacts | 无 | 200 已提交Artifact数组；不返回临时/孤儿文件 |
| GET /v1/artifacts/{artifact_id}/content | 无 | 校验所属job和hash后流式读取；Range首版不支持 |
| POST /v1/jobs/{id}/cancel | `{}` | 202 cancelling；已cancelled返回200；已succeeded返回409 |
| POST /v1/jobs/{id}/resume | ResumeJob | 202 queued；running/cancelling/succeeded/cancelled返回409 |
| GET /live | 无 | 事件循环200 |
| GET /health | 无 | DB、工作盘、scheduler可用才200，否则503 |

JobView严格字段：job_id、state、stage_id(null允许)、completed_units、total_units(null表示尚未规划)、
attempt、candidate_sha256、input_sha256、scope、created_at、updated_at、error_code(null允许)、quality_status。
quality_status枚举 unchecked/passed/failed；job succeeded表示产物已完成且结构验证通过，不表示未标注影片通过人工质量验收。
进度按已提交unit数报告，不按估算耗时虚构百分比；total已知后该stage内不减少。
ResumeJob=`{"allow_external_retry":false}`；默认false，只有 EXTERNAL_UNKNOWN 且true才允许重新发送。
resume必须保持candidate/input哈希不变；变化返回409，需要新job，不复用旧结果。
同一时间只有一个job运行，其余FIFO排队，最多16个非终态job；满429。创建请求体最大64KiB、读取超时10s。
GET断连、POST响应断连均不取消job；显式取消持久化后才回复202；取消生效到确认停止之间一直cancelling。

## 4. scheduler 内部 Unix socket

路径 `/run/self-model-switch/control.sock`，权限0660，owner scheduler、group sms-runner；检查peer UID在配置allowlist。
普通loopback客户端不能通过内部路径提交任意permit；不把该接口暴露到8090。

| 路径 | 请求 | 结果 |
|---|---|---|
| POST /internal/permits | job_id,stage_id,attempt,candidate_sha256 | 202 handle；相同job/stage/attempt幂等 |
| GET /internal/permits/{id} | 无 | phase state、token、expires_in_ms、error_code |
| POST /internal/permits/{id}/heartbeat | PermitToken | 200；已过期409，不复活 |
| POST /internal/permits/{id}/close | PermitToken | 202清理中/200closed；旧token409 |
| POST /internal/executions | ExecutionRequest | 202 execution_id；一次一unit |
| GET /internal/executions/{id} | 无 | ExecutionView |
| POST /internal/executions/{id}/cancel | PermitToken | 202 cancelling/200已终结 |

permit的模型/预算/timeout从服务器candidate+stage映射推导，客户端不能指定bytes、argv、端口或URL。
stage_id=report的permit请求固定422 external_stage_has_no_permit；report不启动media worker。
execute幂等键为(boot,permit,unit_id,attempt)，payload hash一致返回同一执行；不同hash409。
客户端超时后先查询，不盲目创建新执行。关闭permit后不接受新submit；执行unit不能超过phase hard deadline。
ExecutionView：execution_id、token、unit_id、attempt、state、outputs（ArtifactDraft列表）、error_code、compute_quiescent。
state=queued/running/cancelling/succeeded/failed/cancelled/unknown；terminal须quiescent=true，否则视unknown并清理。
输出仅可写attempt临时目录，scheduler/runner验证后提交，worker不能直接改数据库。
ArtifactDraft的精确字段和BackendPort/Operation/Observation/ExecutionView签名见reference/contracts.py；
draft.path仅相对本次execution专属临时根目录，runner补齐provenance后成为Artifact。
InputRef精确为kind(input/artifact)、ref_id、sha256。kind=input解析已登记只读源文件，kind=artifact解析已提交产物；
两者都校验本job归属、hash和无symlink。probe通过kind=input传原片，客户端不能借ID读取另一job或任意宿主文件。

## 5. worker 协议

worker本机固定端口，由scheduler独占调用；容器网络只允许调度侧，不能被runner绕过。
协议版本1，所有响应携带boot_id、generation、instance_id、operation/execution_id。
`GET /health`必须返回已加载资产集合hash、runtime hash、capabilities；ready只在加载及warmup完成后为true。
`POST /executions`提交白名单operation和InputRef，202；`GET /executions/{id}`轮询；`POST .../cancel`幂等取消。
模型名称、操作和参数必须在manifest注册。无任意Python导入、脚本名、shell和用户传入HTTP URL。
资源监视/停止仍由独立Docker observer完成，不信任worker自报已退出。
llama.cpp不能实现上述worker协议时，由LlamaBackend适配HTTP并提供内部ExecutionView，不修改上游协议。

## 6. 错误映射

400 malformed_json；404 unknown_model/job/artifact；409 stale_token/model_busy/idempotency_conflict/candidate_changed；
413 payload_too_large；415 unsupported_media_type；422 invalid_input/capability_mismatch/envelope_exceeded；
429 queue_full；502 backend_failed/invalid_backend_response；503 storage_unready/resource_unknown/backend_blocked；
504 queue_timeout/unit_timeout/stage_timeout。schema验证统一422，JSON语法400。
retryable=true只代表调用者可重新评估提交，不授权网关自动重发；外部计费不确定始终false。
