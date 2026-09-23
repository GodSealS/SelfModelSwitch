# Tool calling / thinking 契约 TC01—TC09

状态：未来实现的约束，**并非现有模块或已通过结果**。任务与状态仅在[执行计划](README.md)。
本文件中的Python片段定义接口/算法；CT任务交付前不能import对应新增模块。
“必须/拒绝”是可测试要求；探测值必须绑定固定镜像，不能由实现者猜默认值。

## TC01 能力、策略与信任边界

三个集合明确分离：

```python
MODEL_CAPABILITIES = frozenset({"chat", "vision", "embeddings", "rerank", "tools", "thinking"})
EXECUTION_OPERATIONS = frozenset({"chat", "vision", "embeddings", "rerank"})
GATEWAY_OPERATIONS = frozenset({"chat", "embeddings", "rerank"})
```

- CAPABILITY_MATRIX使用MODEL_CAPABILITIES；tools/thinking所需资产为model，protocol=openai-chat，另验证chat依赖。
- control_protocol_v1的operation解析、PARAMETER_RULES集合断言、schema导出使用EXECUTION_OPERATIONS。
  若保留该模块的CAPABILITIES兼容符号，它只能作为EXECUTION_OPERATIONS别名，不能再次从全局矩阵派生。
- contracts.Capability不扩展tools/thinking；gateway仍走Capability.CHAT。
- evidence_contracts的能力case校验、注册覆盖使用MODEL_CAPABILITIES；禁止误用EXECUTION_OPERATIONS漏掉新case。
- config schema-v1不开放新能力，既有无envelope路径保持原行为；新能力只适用于schema-v2且有envelope的注册。

固定运行时参数策略是服务内部契约，不是新增客户端字段，也不是可自由指定的启动argv：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class RuntimeChatPolicy:
    policy_id: str
    profile_id: str
    image_digest: str
    model_sha256: str
    template_sha256: str
    source_revision: str
    recognized_output_fields: frozenset[str]
    supported_output_fields: frozenset[str]
    effort_values: frozenset[str]
    denied_template_fields: frozenset[str]
    template_request_fields: frozenset[str]
    template_request_constants_json: bytes
    tokenize_options_json: bytes
```

字段约束：所有文本非空且无NUL；hash为小写64hex，image_digest必须固定digest；各JSON bytes按TC02规范序列化为对象。
recognized_output_fields至少含`max_tokens,max_completion_tokens,n_predict`及镜像源码识别的其他别名；
supported必须为recognized子集且包含max_tokens。effort_values只列已证明有语义的值，空集表示不接受显式effort。
denied_template_fields至少含chat_template/chat_template_kwargs/reasoning_format/parse_tool_calls/generation_prompt，
并补齐源码中同类覆盖项；template_request_fields至少含messages/tools/tool_choice/parallel_tool_calls/reasoning_effort。
没有出现在请求中的字段不补给模板接口，除非template_request_constants_json明确给出已验证常量；两者键不得重叠。
模板输入与tokenize选项须从固定镜像实现确认，例如特殊token/生成前缀规则不能写“默认即可”。

策略由CT01取源码/探测材料、CT06以版本化代码常量登记在新增`model_scheduler/chat_counting.py`中，按
(profile_id,image_digest,model_sha256)查找，必须唯一；模板实际hash不匹配则停止计数/派发。
不扩registration schema、不从请求或未审查的外部JSON加载策略。未知runtime组合在新能力路径失败封闭。
旧runtime计数也使用显式策略；其原封套、argv不变，不要求为旧模型加入新能力。
CT10前策略是待验证候选，只有本地模拟和私有lab测试可使用；通过全部必需探测后方可对外宣称支持。

## TC02 有效请求、预算与纯接口

沿用有限JSON解析与重复key拒绝。规范JSON为UTF-8、sort_keys=True、separators=(',', ':')、ensure_ascii=False、allow_nan=False。
不得修改来访payload对象，规范化必须深拷贝；字节序变化不属于“透传字段值变化”。

```python
from dataclasses import dataclass
from typing import Any, Mapping

@dataclass(frozen=True)
class PreparedChat:
    body_json: bytes                 # 完整有效HTTP JSON；唯一派发输入
    body_sha256: str                 # sha256(body_json).hexdigest()
    model_id: str
    image_count: int
    output_field: str
    output_tokens: int
    requires_tools: bool
    requires_thinking: bool
    policy_id: str

class ChatContractError(ValueError):
    def __init__(self, code: str, param: str, message: str):
        super().__init__(message)
        self.code, self.param = code, param

# implementation in envelope_validator; envelope是现有contracts_v2.Envelope
# capabilities来自模型注册，不从客户端取。
def prepare_chat(payload: Mapping[str, Any], *, capabilities: frozenset[str],
                 envelope, policy: RuntimeChatPolicy) -> PreparedChat:
    ...
```

工厂验证所有字段再构造PreparedChat；frozen bytes防止嵌套dict在计数后变动。任何派发JSON只能decode body_json获得。
body_sha256是内部一致性值，不作为用户鉴权；metadata不转发。模版调用仅投影策略允许的模板字段，完整body仍保留无关extra。

预算规范化的确定算法（其输入已通过基础DTO）：

```python
import copy

def normalize_output(payload: dict, envelope, policy: RuntimeChatPolicy) -> tuple[dict, str, int]:
    body = copy.deepcopy(payload)
    keys = sorted(set(body) & policy.recognized_output_fields)
    if len(keys) > 1:
        raise ChatContractError("contract_violation", keys[0], "multiple output budget fields")
    field = keys[0] if keys else "max_tokens"
    if field not in policy.supported_output_fields:
        raise ChatContractError("contract_violation", field, "unsupported output budget field")
    value = body[field] if keys else min(4096, envelope.max_output_tokens)
    if type(value) is not int or value < 1:
        raise ChatContractError("contract_violation", field, "expected a positive integer")
    if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
        raise ChatContractError("contract_violation", "n", "only n=1 is supported")
    effective = min(value, envelope.max_output_tokens)
    body[field] = effective
    return body, field, effective
```

- 缺省补max_tokens，显式支持别名保留原字段名；多个预算字段即使值相同也拒绝。负数无限输出别名拒绝。
- n缺省不强行补字段；stream缺省沿用上游非流式语义，不因DTO默认而改写原请求。
- max_input_tokens约束完整输入；ctx约束 `charged_input_tokens + output_tokens <= ctx_size`，等号通过。
- 本轮只增加上述预算规范化、工具模式缺省parallel_tool_calls=false及明确字段检查；不改采样值/响应格式等无关字段。
- v1无envelope不套新预算策略，也不能注册tools/thinking；缺counter的v2必须503，不能像测试替身一样跳过预算。

## TC03 请求字段与错误优先级

处理顺序固定，发现同一阶段多错误按表中字段顺序、消息/工具数组下标升序、未知键字典序返回第一个。

| 阶段 | 操作 | 错误 |
|---|---|---|
| 0 | 既有HTTP读取、DTO、model查找、服务就绪 | 保留400/404/413/415/503既有映射 |
| 1 | TC02输出预算、n | contract_violation |
| 2 | tools → tool_choice → parallel_tool_calls → reasoning_effort → messages新字段形状 | contract_violation |
| 3 | tools项目数/完整对象字节/数组字节、每条历史调用数量、历史arguments总字节/id字节 | envelope_exceeded |
| 4 | 从整请求计算能力需求、拒绝专用模板覆盖 | capability_mismatch / contract_violation |
| 5 | effort允许值 → tool_choice与当前tools关联 → TC04历史关联 → 既有图片检查 | contract_violation；图片保持既有错误 |
| 6 | acquire→TC05计数→输入/ctx预算 | TC06生命周期错误 / envelope_exceeded |
| 7 | 生成派发及响应生命周期 | 既有gateway/SSE错误 |

新字段形状规则：

| 字段 | 缺省/类型/闭集 |
|---|---|
| tools | 缺省允许，null拒绝；数组0—32；元素仅允许type/function，二者必填，type只能function |
| function | 仅name/description/parameters/strict；name必填；description字符串可空；parameters对象可空；后两者可省略但不可null |
| name | re.fullmatch(`[A-Za-z0-9_-]{1,64}`, name)，不能用re.match接受末尾换行；同tools列表大小写敏感唯一 |
| strict | 缺省/false允许；true首版拒绝；null及其他类型拒绝 |
| tool_choice | 缺省允许；字符串auto/none/required，或仅type/function且function仅name的指定函数对象 |
| parallel_tool_calls | 缺省/false允许，true和非bool拒绝；需要tools能力的请求缺省补false |
| reasoning_effort | 缺省允许；提供必须为非空字符串，具体值在能力判定后检查policy.effort_values；不在集合422 |
| assistant.tool_calls | 提供时必须为非空数组，每项仅id/type/function；type=function，function仅name/arguments且均必填 |
| arguments | 字符串；服务不解析JSON、不执行Schema，客户端负责执行前校验；真实验收要求生成值为有效测试JSON |
| id / tool_call_id | 非空字符串，无NUL、可编码UTF-8、原文≤64字节；不套函数名正则 |
| reasoning_content | 仅assistant上允许；缺省/null/字符串均保留；其他角色携带该字段（包括null）拒绝 |

函数定义parameters内部是开放JSON Schema对象，不递归按函数对象的闭集规则拒绝Schema关键字。
单个完整工具对象≤8192字节，整个tools数组≤65536字节；历史arguments原文UTF-8合计≤262144字节。
每条assistant最多32个调用；整HTTP体上限仍用部署server.max_request_body_bytes。无法编码UTF-8为contract_violation。
messages每项仍允许已有非新字段；非assistant携带tool_calls、非tool携带tool_call_id拒绝。
assistant带合法tool_calls时content允许缺省/null/字符串/已有part列表；其他消息沿用旧content规则。
role=tool的content必须为字符串，空串允许。图片对所有允许parts扫描，仍只接受既有PNG/JPEG data URL。

同tools列表的重名在阶段2检查。阶段2的tool_choice仅检查结构，以下交叉字段在阶段5检查：
tools省略/空时tool_choice只允许省略或none；非空时缺省auto，指定名称必须在当前tools。
空tools+none不需要tools；仅parallel=false也不需要tools。历史函数名不要求在当前tools。
非空tools、指定/required/auto选择或任意tool_calls/tool消息要求tools；其中auto只有tools非空才合法。
显式effort或assistant非null reasoning_content（含空串）要求thinking，null占位不要求thinking。
能力依赖：tools/thinking→chat；存在图片→vision。结构合法但缺能力优先报capability_mismatch，再判断关联或effort支持值。

登记tools/thinking任一能力的模型，其所有chat请求拒绝policy.denied_template_fields（包括普通请求、值为null）。
无新能力模型保留原模板透传范围；此旧路径不声称保证reasoning分离。其它未知顶层extra仍透传；
policy的源码审查必须穷尽该固定镜像可改变模板/输出预算的参数，不能以extra=allow忽略已知绕过项。

## TC04 工具历史状态机

校验只检查提交历史的一致性，不执行工具。状态为`seen_ids:set[str]`和`pending:dict[id,name]`，初始空。
在完成TC03形状检查后逐消息执行：

```python
def validate_tool_history(messages: list[dict]) -> None:
    seen_ids: set[str] = set()
    pending: dict[str, str] = {}
    for index, msg in enumerate(messages):
        where = f"messages[{index}]"
        role = msg.get("role")
        if role == "tool":
            call_id = msg["tool_call_id"]
            if call_id not in pending:
                raise ChatContractError("contract_violation", where + ".tool_call_id", "orphan or duplicate result")
            del pending[call_id]
            continue
        if pending:
            raise ChatContractError("contract_violation", where + ".role", "tool results are incomplete")
        for call in msg.get("tool_calls", []):
            if call["id"] in seen_ids:
                raise ChatContractError("contract_violation", where + ".tool_calls", "duplicate call id")
            seen_ids.add(call["id"])
            pending[call["id"]] = call["function"]["name"]
    if pending:
        raise ChatContractError("contract_violation", "messages", "tool results are incomplete")
```

允许同一批次结果乱序，但每个id恰一次；批次内不能插入system/user/assistant。
允许历史包含多个调用而本轮parallel=false；仅约束当前生成不继续并行，不能错误拒绝历史。
assistant tool_calls=null或[]均按TC03拒绝；客户端应省略无调用字段。普通assistant content=null且无tool_calls仍拒绝。
未闭合的工具调用不能作为新推理请求结尾；客户端拿到第一轮响应后先执行/回填结果再发第二轮。

## TC05 完整计数接口与身份

复用现有Lease与control_protocol_v1.InstanceIdentity，不新造另一套generation或实例权威。
Book在scheduler条件锁下是已接受身份的唯一所有者；adapter观察结果不能覆写Book。

```python
from dataclasses import dataclass
from typing import Protocol
from model_scheduler.contracts import Lease
from model_scheduler.control_protocol_v1 import InstanceIdentity

@dataclass(frozen=True)
class CountReceipt:
    body_sha256: str
    policy_id: str
    generation: int
    instance: InstanceIdentity
    template_sha256: str
    template_tokens: int
    image_charge: int
    charged_input_tokens: int

class ChatCountPort(Protocol):
    async def count(self, lease: Lease, request: PreparedChat, *, deadline: float) -> CountReceipt: ...
    async def validate_binding(self, lease: Lease, receipt: CountReceipt, *, deadline: float) -> None: ...
```

`create_app(..., chat_counter: ChatCountPort | None)`替代旧callable注入；_v2_token_counter改为构造实现此协议的对象。
TC05消费者测试全部迁移，不同时维护两套可任选的计数路径；v1无envelope无需counter。
count入口检查lease model、generation、已接受InstanceIdentity和策略身份；调用完成后重复验证。
validate_binding在gateway.open之前再比对当前Book身份与lease，若失配返回503，**不自动换实例重试**。
实例意外退出仍可能发生于最后检查之后，此时走上游失败/ABORTED；不能宣称跨网络绝对原子。

模板请求等于有效body中存在且属于policy.template_request_fields的键，加固定constants；
固定镜像无法完整支持该投影时，不启用该策略，另做镜像/模板任务，不在计数端另写近似prompt。
apply-template的prompt交给相同tokenizer与policy.tokenize_options_json，计入特殊token和生成前缀。
字段接受但忽略的情况须由D项差分实验排除；正常生成也必须使用同策略绑定的服务模板和启动常量。

Receipt约束：所有token计数为type=int且≥0；image_charge=image_count*max_image_tokens，
charged_input_tokens=template_tokens+image_charge。template_tokens包含完整消息、工具定义、历史和模板占位符成本。
纯文本charged必须等于真实prompt_tokens；图片收费可高于实耗，但不得低估。
如果某镜像会把图像展开token已经包含在返回计数中，禁止再按此公式双算；该策略不属于首版，须修订公式与测试再接入。
坏响应/4xx/5xx/非JSON/非整数receipt均视作计数失败，不转为0，不返回估计值。

## TC06 请求生命周期与错误封装

对于有envelope的兼容请求，严格执行：

```text
read/DTO/catalog → prepare_chat（无I/O） → acquire(model, request_id, deadline)
→ counter.count(同lease、PreparedChat、deadline) → check_chat_budget
→ counter.validate_binding → gateway.open(同lease、decode(body_json)、deadline)
→ 原响应处理/流式所有权 → close/release
```

deadline在原chat入口中创建一次，acquire/count/template/tokenize/gateway均使用它；不重新加timeout。
acquire负责冷加载，计数需要的实例被同一有效lease保护，不再额外warm。取得lease只表示预留，不是生成已派发。
本地422发生前acquire调用数=0；输入token超限可能已加载，但gateway.open调用数必须=0。
count端点不生成token；若固定镜像的计数端点有生成副作用则策略拒绝。

| 路径 | HTTP/code | 取得lease后的Outcome |
|---|---|---|
| 本地形状/交叉字段/历史错误 | 422 contract_violation | 未取得，不release |
| 字节或token/ctx超限 | 422 envelope_exceeded | token超限用REJECTED；其余未取得 |
| 能力缺失 | 422 capability_mismatch | 未取得 |
| acquire队列满/超时 | 429 queue_full / 504 queue_timeout | 未取得 |
| 计数/绑定不可用、receipt不合法 | 503 service_unavailable | ABORTED |
| acquire后count/生成deadline耗尽 | 504 inference_timeout | ABORTED |
| 网关失败/上游坏响应 | 保留GatewayError/502 upstream_protocol_error | 沿用GatewayError outcome，否则ABORTED |
| 非流式正常完成 / SSE完整DONE | 原200 | SUCCESS |
| 客户端取消、SSE超限、缺DONE、流异常 | 未发headers时沿用错误；已发后终止流，不伪造新JSON或DONE | ABORTED |

每次acquire成功对应恰好一次release；将lease移交_StreamingLease后，外层不得重复释放。
所有取消路径shield既有close/release清理，cleanup可以使用既有有限停止期限，但不能延长推理deadline或开启新生成。
计数出错不重试；预算/计数失败不得release SUCCESS。不得吞CancelledError转换成200。

兼容错误形状保持当前app._error，不加入internal的retryable字段：

```json
{"error":{"message":"expected a positive integer","type":"invalid_request_error","code":"contract_violation","param":"max_tokens"},"request_id":"<generated-id>"}
```

type沿用当前函数：<500为invalid_request_error，500为server_error，>500为upstream_error；header带同一X-Request-ID。
param使用TC03指定字段路径，计数服务错误为null；消息不给原工具内容、prompt或堆栈。
C05的retryable=false只描述上述422的语义，不改变现有兼容错误JSON。

## TC07 运行时与启用

新增`llama-cpp-chat-features-v1` profile，仅mode=lab；与既有GGUF资产角色/数量规则一致，新增flag值源固定：

| 模型能力 | 必须启用 | 禁止 |
|---|---|---|
| 无tools/thinking | 无新flag；继续旧profile | 新profile、--jinja、--reasoning-format |
| chat+tools | --jinja | --reasoning-format |
| chat+thinking | --jinja，--reasoning-format deepseek | 未登记值 |
| chat+tools+thinking | 上述两flag，各一次 | 重复flag/未知值 |

vision的projector与image-max-tokens规则不变。首版允许部署的新能力模型ID仅qwen36-27b；四组合单测可用合成模型。
production render发现任何tools/thinking或新profile直接失败，不因有测量材料就放行。
RuntimeSpec.startup_args仍是flag名称集合，不传参数值。FlagSource增加
`requires_any_capability: frozenset[str] = frozenset()`；空集不限制，非空时与model.capabilities交集必须非空。
原requires_capability保留；同时给出时两个条件都必须成立，不能默认为OR。
--jinja要求any={tools,thinking}；--reasoning-format要求thinking；CAPABILITY_FLAGS仍只聚合必需项。

27B必须有独立runtime_id，新值源仅挂新profile；7B继续旧runtime/profile及原封套。
独立runtime仍可使用同一固定镜像，但模板、实际argv、策略hash都纳入候选材料身份。
不改启动参数的全局默认，不把deepseek改成auto/none来绕过不支持；不支持则blocked。
thinking首版还要求该固定模型模板在此配置下默认生成可提取思考；若默认不生成，CT10停止thinking验收，
不得把仅启用解析误称启用生成。额外生成开关或模板变更须先修订本节及策略/测试，再通过正常提交部署。

CT10使用不连接LAN网关的loopback私有lab候选，属于受控验证，不是接受支持声明。
原始证据与策略一致、所有必需用例通过后，CT11才测试对外网关；任何配置/模板/镜像变更使对应证据失效。

## TC08 兼容验收接口与响应

新fixture类型不通过internal DTO：

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class CompatScenario:
    fixture_id: str
    capability: Literal["tools", "thinking"]
    transport: Literal["compat"]
    model_id: str
    first_request_json: bytes
    tool_result_json: bytes | None
    stream: bool
    deadline_seconds: float
    rounds: Literal[1, 2]
```

tools场景rounds=2且tool_result_json必有；thinking场景rounds=2，第一轮普通问答、第二轮回传实际assistant再问追问，
tool_result_json=None。两者均经过公开兼容路由；tools+thinking组合沿用工具两轮并要求reasoning回传。
fixture哈希覆盖所有上述字段、规范化schema和用于断言的固定期望；不是只覆盖prompt。
参数deadline_seconds由site提供的已冻结正数生成，测试中不修改；两轮各自一个HTTP deadline，不共享服务端lease。

CompatDriver.run(scenario, evidence_dir)产生每轮request/response或SSE原始字节、request_id、计数/usage/结束状态、
时间、实例/资源归属、cleanup材料。ToolResult由测试固定数据给出，不联网、不开子进程执行真实工具。
构造第二轮：messages=第一轮原messages+[第一轮实际assistant]+按tool_call_id关联的tool结果，
保留assistant.reasoning_content；tools场景第二轮移除顶层tools、tool_choice、parallel_tool_calls，验证历史能力检查。
第二轮仍保留model、输出预算和stream；其它生成参数复制第一轮；thinking第二轮追加固定追问。

SSE聚合器仅用于测试/验收，不改变服务透传。按choice.index=0、tool_calls[].index聚合；
id/type允许只首片出现，后续重复须相同；name和arguments按出现顺序拼接字符串片段，不能假设name每次都是完整值；
content/reasoning分别拼接，null不添加文本。每个完成调用的id/type/name不能为空。
UTF-8使用增量解码、按SSE空行分隔事件；每个事件合并data行后解析JSON，[DONE]恰一次且结束。
usage-only且choices=[]合法；工具多index在parallel=false的生成验收中失败；历史多调用仍合法。
坏JSON、索引类型错误、矛盾id、DONE后有数据、缺终结finish_reason、缺DONE、重复DONE均验收失败。
工具轮finish_reason=tool_calls且完整arguments可解析；最终轮finish_reason=stop、content非空并包含固定结果标记；
length/content_filter不是通过。实际reasoning用例要求第一轮非空reasoning且最终答案非空，不能只检查字段存在。
模板配置本应分离的思考标记不得出现在本场景的content（测试prompt不要求引用这些标记）。

capability case覆盖四变体：非流式/SSE × 热态/轮间卸载重载；每个变体独立材料，case成功要求全部通过。
已有report-v3的case id继续为B:<model>:cap:tools|thinking，每个case一个最终attempt，变体是该attempt的材料，不能重复最终case。
lab附加run使用`lab-chat-report-v1`，见acceptance；不填production通过标志，不绕过06的完整候选门禁。

## TC09 回滚与兼容边界

回滚单位为完整部署组合：代码SHA、model能力集/runtime_id、startup_args/值来源、profile、镜像、模板、封套及渲染产物。
删除能力但保留新profile/flag必须被渲染拒绝，不能当回滚成功；禁用reasoning提取也不能当禁用思考。
保留独立已验收的CT02预算修复。若它自身需回退，必须先关闭受影响入口，不能恢复可绕过封套的服务宣称。
正常回滚撤回新能力部署组合，保留验收工具代码以采集恢复后的事实；验收driver本身不进行git/部署变更。
按AGENTS在开发机审查git revert、检查、提交/push、目标ff-only同SHA；不直接改目标配置或拷贝tracked文件。
恢复后验证7B普通chat/vision、预算约束、SSE、冷加载、新能力拒绝，停止后清理证据齐全。

新能力模型的普通请求也受模板变化影响，必须回归27B普通chat/vision；7B没有新flag仍需共享路径回归。
内部execution operation/schema保持原闭集；既有embeddings/rerank不改；未测试客户端不宣称支持。
