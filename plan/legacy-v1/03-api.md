# 03 HTTP 与内部接口契约

内部 Protocol、DTO 和枚举见 [contracts.py](reference/contracts.py)。API 只使用 SchedulerPort，不操作 runtime。
本文件定义的 HTTP DTO 在实现时使用 Pydantic v2；对外 OpenAPI 由这些真实路由生成，测试保存 schema 快照。

## 1. 通用规则

- 默认 `http://127.0.0.1:8090`，JSON UTF-8；JSON 成功与错误 Content-Type 为 application/json。
- 请求顶层必须对象，拒绝重复 JSON key、NaN、Infinity、非法 UTF-8 和无效 JSON，400。
- JSON POST Content-Type 必须 application/json，允许 charset=utf-8；否则415；无body的 unload 不要求 Content-Type。
- 请求体最多 4 MiB，既检查 Content-Length 又累计分块读取大小，超限413；body读取最长10秒，超时408。
- model 必须匹配 `[a-z0-9][a-z0-9_-]{0,63}`，大小写敏感；未知 ID 404；没有能力400；这两类不得入队。
- 禁止隐式类型转换：`"false"` 不是 bool，`true` 不是整数；所有校验失败400，覆盖 FastAPI默认422。
- 每请求生成 UUID4 request_id；所有响应头含 X-Request-ID；不信任、不回显客户端给的同名头。
- 不返回用户输入原文、模型绝对路径、上游错误全文或堆栈；详细错误仅日志中用安全错误码和request_id关联。
- 路由不存在404、方法错误405、未捕获错误500也统一错误结构；405保留Allow响应头。
- 服务端不跟随上游重定向；不传递客户端 Authorization/Cookie/Host 或 hop-by-hop headers。

```json
{
  "error": {
    "message": "Model does not support embeddings",
    "type": "invalid_request_error",
    "code": "unsupported_capability",
    "param": "model"
  },
  "request_id": "ae34f2ca-68aa-4fd1-ae3b-b4eebf99da98"
}
```

type 固定为 `invalid_request_error / resource_error / upstream_error / server_error`；param 为字段路径字符串或 null。
error.message 是英文简短固定模板，客户端分支只依赖 code。

## 2. 路由表

| 方法 / 路径 | 作用 | 成功 |
|---|---|---|
| GET /live | 当前进程事件循环可响应，不访问外部依赖 | 200 `{"ok":true}` |
| GET /health | 完整服务readiness，读取最新后台快照 | 200或503，结构见下文 |
| GET /v1/models | 全部启用模型（含未加载），model_id升序 | OpenAI list形状 |
| GET /api/models | 全部启用模型的运行状态 | model_id为key的对象 |
| GET /api/status | 资源、队列、存储、模型及控制状态 | 200，即使readiness=false也可诊断 |
| POST /api/models/{model_id}/unload | 同步等待单模型卸载 | 200 `{"ok":true,"model":"qwen-small"}` |
| POST /v1/chat/completions | 聊天非流/流推理 | JSON或SSE |
| POST /v1/embeddings | 单文本/批量文本向量 | embedding list |
| POST /v1/rerank | 本项目自有重排序契约，不宣称OpenAI标准 | 排序结果 |

不实现 `/v1/completions`、`/v1/responses`、`/rerank` 等隐式别名，未知路径正常404。

### health

```json
{"ok":false,"checks":{"llama_swap":true,"storage":true,"resources":true,"preload":false,"control":true},"reason":"preload_pending"}
```

checks 固定五个bool；ok=全部checks为true且未shutdown。
reason 为 `null / starting / llama_swap_unavailable / storage_unavailable / resource_sample_invalid / preload_pending / control_recovering / shutting_down`。
同时失败时优先级：shutting_down→storage→control→llama_swap→resources→preload→starting。
不能因上游返回非200但没有抛异常就返回HTTP200；resources沿用2秒采样时效；
其他依赖探测缓存超过 `2 * poll_interval_seconds + 1`（默认5秒）则对应check=false，避免2秒轮询的正常耗时造成误抖动。

### models / status

`/v1/models` 不访问推理后端，也不触发load；created固定0，owned_by固定self-model-switch：

```json
{"object":"list","data":[{"id":"embedding","object":"model","created":0,"owned_by":"self-model-switch"}]}
```

`/api/models` 每个模型固定字段：

```json
{"embedding":{"state":"ready","generation":1,"in_flight":0,"waiting_requests":0,"capabilities":["embeddings"],"reserved_bytes":4294967296,"effective_reserved_bytes":4939212391,"priority":100,"evictable":false,"pinned":true,"preload":true,"max_concurrency":1,"ttl_seconds":0,"idle_seconds":12.5,"heat":0.0,"total_requests":0,"total_tokens":0,"usage_unknown_requests":0,"admission_blocked":false,"last_error":null}}
```

state在READY且in_flight>0时输出active，其他直接输出内部State.value；generation和计数均非负整数。
idle_seconds有lease时null；last_error是安全错误码或null；累计统计按进程生命周期计，不伪装持久化。

`/api/status` 固定顶层字段：

```json
{
  "ready": true,
  "resources": {"source":"psutil","total_bytes":137438953472,"available_bytes":90000000000,"used_bytes":47438953472,"utilization":0.345163,"sample_age_seconds":0.2,"model_budget_bytes":126701535232,"committed_bytes":4939212391},
  "storage": {"ready":true,"reason":null},
  "queue_size":0,
  "queue":{"capacity":128,"oldest_wait_seconds":0,"blocked_reason":null},
  "control":{"phase":"idle","target_model":null,"cooldown_remaining_seconds":0},
  "models":{}
}
```

数值仅展示结构，不代表本机测量。models等于/api/models；queue_size只计WAITING，已GRANTED不计队列。
blocked_reason固定枚举 `null / waiting_for_memory / model_concurrency / switch_drain / cooldown / model_loading / dependency_unavailable`；按最高排序WAITING项决定。
control.phase为 `idle / loading / evicting / draining / recovering / shutting_down`。

## 3. 请求类型定义

以下代码规定核心验证边界；生产实现增加整体body大小、模型能力和列表长度验证。

```python
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, field_validator

class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    model: StrictStr
    messages: list[dict[str, Any]]
    stream: StrictBool = False

class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: StrictStr
    input: StrictStr | list[StrictStr]
    encoding_format: Literal["float", "base64"] = "float"

class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: StrictStr
    query: StrictStr
    documents: list[StrictStr]
    top_n: StrictInt | None = None  # 仅缺省可为None；JSON显式null拒绝
    return_documents: StrictBool = False

    @field_validator("top_n", mode="before")
    @classmethod
    def reject_explicit_null(cls, value: Any) -> Any:
        # 未启用validate_default；缺省不会执行本validator。
        if value is None:
            raise ValueError("top_n must be an integer when supplied")
        return value
```

### Chat

messages非空，最多256项；每项必须有非空字符串role；不限制role枚举；content、tool_calls等由上游验证。
保留额外字段如tools、tool_choice、response_format、temperature、stream_options，按原始JSON转发，不通过model_dump丢字段或偷偷加默认值。
model ID与模型容器 `--alias` 一致，不做别名隐式转换。
非流成功要求完整有效JSON对象，choices为数组、model为字符串；上游usage存在时必须为非负整数计数，非法成功响应502。
保留合法上游响应全部字段，不包装第二层data。

### Embeddings

input是非空白字符串，或1..256个非空白字符串组成的数组；验证用strip判断，转发保留原文本。
不支持token ID数组、dimensions、user和未知字段，遇到直接400。
支持float/base64；默认float。base64应表示同一向量的little-endian float32字节，返回标准RFC4648编码。
上游固定请求float，网关负责base64转换，避免依赖不同llama.cpp构建的编码行为。
成功响应必须包含与输入数量相同的data，index恰好为0..N-1，无重复；各embedding为非空且维度一致的有限数数组。
网关按index升序返回，model固定请求ID，usage缺失时不伪造token数：省略usage并计usage_unknown。

```json
{"object":"list","data":[{"object":"embedding","index":0,"embedding":[0.1,-0.2]}],"model":"embedding"}
```

### Rerank

query与documents文本非空白，documents为1..256项，允许重复文本但保留不同index。
top_n缺省为文档数；显式传值必须为严格整数且1<=top_n<=文档数。return_documents缺省false。
后端路径固定 `/reranking`；发送 `{model,query,documents,top_n:len(documents)}`。
固定版本fixture必须证明能返回全部文档的score；否则版本/模型不符合本项目rerank后端验收，不改成猜测兼容。
要求后端results里每个index恰好一次，score为有限数，允许负数或大于1；缺项/重复/NaN均502。

```python
ranked = sorted(results, key=lambda item: (-item["relevance_score"], item["index"]))[:top_n]
public = [
    {"index": item["index"], "relevance_score": item["relevance_score"],
     **({"document": {"text": documents[item["index"]]}} if return_documents else {})}
    for item in ranked
]
response = {"model": model_id, "results": public}
```

本项目不承诺rerank usage、响应id或其他上游扩展字段；响应仅model/results。
同分按原index升序。return_documents=false时省略document，不能返回null占位。

## 4. 超时和状态码

| 阶段 | 默认上限 | 精确定义 |
|---|---:|---|
| 请求体 | 10s | 第一次读取到完整body |
| 等待lease | 1800s | 入队至CLAIMED，包含等待共享load |
| 后端load | 900s | 发warmup至容器身份和direct health确认；独立共享操作 |
| 控制stop | 45s | 发stop至STOPPED证据 |
| inference | 900s | 开始发送上游至响应完整结束，包含整个流 |
| connect/pool | 各5s | 建连、连接池等待；不替代绝对deadline |
| read/write idle | 各60s | 两次读取/写入之间等待；不替代绝对deadline |
| cleanup response close | 5s | 关闭单个上游响应 |

时间取monotonic，使用asyncio.timeout_at绝对期限；阶段超时与已有deadline同时生效时取最早者。
一个客户端queue到期不取消其他客户端共享的load。load失败使该模型当时全部等待者504或502，随后进入02恢复。
上游已经执行也不能自动重试；只有客户端提交新请求才有下一次尝试。
InferenceGateway.open失败只能抛contracts.GatewayError(http_status, code, outcome)，路由据此决定HTTP错误及release方式。
发送前的本地参数失败使用REJECTED；完整有效4xx/429使用REJECTED；传输/协议失败和5xx使用ABORTED。
取消发生在open执行期间由gateway记录是否已开始发送请求；已发送或无法确认使用ABORTED，确认未发送可用REJECTED。
读取错误body超64KiB、读取不完整、响应关闭失败一律ABORTED，不沿用原4xx的REJECTED。

| 情况 | HTTP / code |
|---|---|
| 参数/JSON错误、能力不匹配 | 400 invalid_request / unsupported_capability |
| 本地未知模型 | 404 model_not_found |
| 请求body超时/过大/类型错误 | 408 request_body_timeout / 413 request_too_large / 415 unsupported_media_type |
| 手动卸载busy/pinned | 409 model_busy / model_pinned |
| 队列满 | 429 queue_full，Retry-After:1 |
| queue/load/inference期限 | 504 queue_timeout / model_load_timeout / inference_timeout |
| 连接、协议、非法成功响应 | 502 upstream_unavailable / upstream_protocol_error |
| 上游401/403/404/3xx | 502 upstream_configuration_error |
| 上游400/422 | 400 upstream_invalid_request（不回传其详情） |
| 上游413 | 413 upstream_request_too_large |
| 上游429 | 429 upstream_rate_limited；Retry-After只接受1..60的整数，否则1 |
| 上游503 | 503 upstream_unavailable |
| 上游其他4xx/5xx | 502 upstream_error |
| 依赖、SSD、资源采样、关闭中 | 503 service_unavailable |
| 未捕获程序错误 | 500 internal_error |

上游错误body最多读取64KiB供解析/分类，不把原文写到普通日志。
非流成功body累计最多16MiB，超限返回502 upstream_response_too_large并按ABORTED隔离；
流不设总字节上限，但受900秒绝对期限和1MiB单事件上限约束。不得无界buffer整个流。
完整有效的4xx/429按REJECTED释放；连接不确定、超时、5xx及非法成功响应按ABORTED隔离。
单模型暂时内存不足先排队，到期504；全局采样不可信立即503，二者不能混为一谈。

## 5. StreamingResponse 所有权

路由必须在发送客户端响应头之前执行gateway.open并确认HTTP200、Content-Type=text/event-stream。
响应对象接管lease和OpenedResponse；不是只让生成器负责清理。

```python
# 核心结构；close_and_release实现02中的有界、屏蔽取消、幂等清理。
class LeasedStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        outcome = Outcome.ABORTED
        try:
            await super().__call__(scope, receive, send)
            outcome = self.parser.outcome  # 只有收到合法[DONE]才SUCCESS
        finally:
            await self.owner.close_and_release(outcome)
```

响应对象创建失败/接管前取消由路由finally清理；接管后即使iterator从未运行也由__call__finally清理。
使用应用自定义ASGI响应发送包装层，确保返回响应后、进入__call__前的取消仍由owner注册表回收；shutdown join该注册表。
非流请求同样主动监听http.disconnect；body读取完成后只有一个disconnect watcher读receive，不能与body reader竞争。
客户端断开不尝试发送499，仅日志记录client_disconnected。

SSE按原始字节转发；旁路解析以事件边界而非chunk边界处理UTF-8和data行，事件缓存上限1MiB。
只从合法usage.total_tokens记录token热度；最后一个有效累计值为准，不把增量误相加。
有效 `[DONE]` 后认定完成；EOF而没有DONE按ABORTED。解析异常或超限同样终止流并隔离。
HTTP已提交后不能修改状态码、追加JSON错误、伪造DONE或重试；日志带request_id和阶段。
响应头设置Cache-Control:no-cache、X-Accel-Buffering:no；不显式设置Connection。

## 6. 手动卸载

请求不接受参数和force字段；有非空body返回400。
UNLOADED幂等200；未知404；pinned/busy/loading/evicting/frozen返回409。
非pinned且空闲时，通过同一生命周期worker调用begin_eviction(automatic=False)。
处理上限45秒；超时504并触发控制面恢复。客户端取消管理请求不取消已开始的stop，其操作由worker收尾。
200必须实际停止确认；失败不得仅修改UI状态后返回200。

## 7. 连接池及日志

lifespan分别创建control与inference的httpx.AsyncClient，trust_env=False、follow_redirects=False；不用用户代理环境改变本机路径。
inference连接数为 `sum(max_concurrency)+8`，keepalive最多同值；control最多8；退出时统一关闭。
结构化日志字段：timestamp、level、event、request_id、model_id、generation、operation_id、state_from、state_to、duration_ms、error_code。
不记录prompt、messages、embedding内容、完整模型路径、认证信息。单次请求结束必须有且仅有一个完成事件。
