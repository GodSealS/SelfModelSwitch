# 兼容面支撑 tool calling 与 thinking（reasoning_content）：方案草案

创建：2026-09-23；修订：2026-09-24。类别：enhancement，含输出预算约束的前置 bugfix。
状态：**方案已修订，代码未实施，目标镜像能力与硬件验收待验证**。
源码证据基线：`854f39e1bb34ec3ce8678010d9b363a8021c6854`；本轮为开发机源码审查。
此前关于 lab 部署、端口和性能的记录仅为历史线索，本轮未重新验证目标机运行状态或三端 SHA。
范围：模型服务的 `/v1/chat/completions` 兼容面、C06 封套、能力注册、运行时渲染及对应验收。
本文件是独立的新能力方案，不属于 RP00—RP17 修复轮；保留在 `Idea/`，不代表正式规范或生产验收。

实施入口已整理为 [plan 执行计划](../plan/tool-calling-and-reasoning/README.md)。后续接口、任务状态与验收分别以该目录
contracts、README、acceptance为真源；本文件保留设计与审查来由，不再平行维护实施契约。

## 0. 修订结论与审查闭环

| 审查问题 | 修订后的决定 | 验收 |
|---|---|---|
| 预检裁剪预算、转发仍用原值 | 先修复有效请求一致性，明确这是透传兼容的行为修复 | A01—A02 |
| 只补 tools 仍可能漏算模板输入 | 计数覆盖完整有效请求的模板依赖，不能可靠计数则不启用 | A05 |
| 能力表被误当成实际 flag 选择器 | 27B 使用独立 runtime 注册，回滚完整注册组合 | A07、A12 |
| thinking 生成与响应提取混淆 | 分开定义生成、预算、提取、历史回传，不承诺透传字段被忽略 | A06 |
| 现有 B 层验收走 internal 协议 | 新能力采用兼容 HTTP 验收通道，统一 thinking 命名 | A08 |
| 第 0 步存在循环依赖 | 拆分运行时诊断、合成历史探测、实施后的真实闭环 | D0、A08 |
| 工具契约误拒及能力检查遗漏 | 独立名称规则、可选字段、历史关联、全请求能力判定 | A03—A04 |
| 仅凭 7B 渲染不变豁免回归 | 增加共享路径回归及证据复用判定 | A10 |

保留 `ChatRequest.extra="allow"`；不增加 OpenAI 与 llama.cpp 之间的通用字段转换层。
封套规范化和首版明确限制的字段属于有记录的兼容例外，不能再承诺所有请求无条件原样转发。
能力名统一为 `tools`、`thinking`，验收名为 `cap:tools`、`cap:thinking`；首版仅在 27B lab 注册启用。
正式启用前必须完成预算检查、计数、消息校验和兼容 HTTP 验收设施，不能先发布能力再补门禁。

## 1. 目标与范围边界

**目标行为**：客户端通过兼容接口提交函数工具定义，收到结构化调用，回填工具结果后收到最终答案；
27B 在已验证的配置下将思考放在 `reasoning_content`，最终答案放在 `content`，历史回传不被服务丢弃。
服务在推理派发前拒绝不合法、未登记能力或超封套请求，保证核算的请求就是实际发送的请求。

**本轮纳入**：共享兼容接口的输出预算修复；工具/思考的形状、能力与资源检查；固定运行时的模板计数；
27B lab 独立 runtime 注册；非流式与 SSE；7B 共享路径回归；证据采集和可验证回滚。

**不纳入**：工具执行、函数代理、客户端业务编排；新增模型、视频或音频项目；鉴权模型变更；
`/internal/executions` 的工具参数扩展和新增 execution operation；跨请求会话租约或驻留承诺；
通用 JSON Schema 验证/远程 `$ref` 获取；通用请求/响应映射；生产开放新能力。
镜像升级、自定义 chat 模板另列评估任务，不因上游探测失败而自动实施。

7B 本轮不登记 tools/thinking，这是支持范围决定，不是“模型绝不可能支持”的结论。
保留旧 sampling 等字段的透传；预算修复、能力检查及本方案明示的字段限制须列入兼容说明。

## 2. 当前行为与证据边界

链接用于基线源码定位；实施契约使用接口与行为，不依赖固定行号。

| 编号 | 已核实事实 | 证据入口与含义 |
|---|---|---|
| F1 | ChatRequest 使用 extra=allow、strict=True | [ChatRequest](../model_scheduler/api_models.py)：未知顶层字段可通过 DTO；通过 DTO 不代表通过后续检查或获得上游支持 |
| F2 | 消息检查不接受 null/缺省 content | [collect_image_sizes / _message_parts](../model_scheduler/envelope_validator.py)：assistant 带 tool_calls 且 content 为 null/缺省时失败；空串能通过此检查，不能称“第二轮必然失败” |
| F3 | 启动白名单与渲染规则尚未登记新 flag | [注册](../model_scheduler/contracts_v2.py)、[渲染器](../model_scheduler/runtime_profiles.py)：未显式传 --jinja 不证明它未启用，默认值取决于固定镜像 |
| F4 | 兼容接口丢弃预检返回的有效 max_tokens，转发原 payload | [chat 处理器](../app.py)、[HTTPXGateway.open](../model_scheduler/gateway.py)：预检裁剪不等于实际限制输出；internal adapter 构造请求是另一条路径 |
| F5 | 兼容响应没有裁掉工具或 reasoning 字段 | [chat 处理器](../app.py)：非流式检查基本结构；SSE 还受事件大小、完成和租约生命周期约束，仍需端到端测试 |
| F6 | 计数请求只携带 messages | [count_chat_input / _count_chat_tokens](../model_scheduler/adapters/llama_cpp.py)、[_v2_token_counter](../run.py)：顶层 tools 未计入；tool 结果已在 messages 内，正确渲染取决于模板 |
| F7 | 能力矩阵为闭集 | [CAPABILITY_MATRIX](../model_scheduler/contracts_v2.py)：新增能力须走 M01 并审查所有消费方；protocol 当前无业务消费不代表验收链路无需修改 |
| F8 | 实际 flag 来自 runtime.startup_args | [_render_flags](../model_scheduler/runtime_profiles.py)：CAPABILITY_FLAGS 聚合必需参数；删除能力不会自动移除已启用 flag，也可能导致渲染拒绝 |
| F9 | 现有 B 层 driver 走 internal executions | [driver](../model_scheduler/acceptance/driver.py)、[adapter](../model_scheduler/adapters/llama_cpp.py)：当前参数闭集和 _chat_payload 不发送 tools；只加 fixture 不能证明兼容面通过 |
| F10 | 输出预算与消息形状已有最小复现 | 开发机：请求 max_tokens=4096、封套上限1024，预检结果1024但原 payload仍为4096；assistant content=null失败、空字符串通过图片检查 |

历史 lab 记录：7B ctx/in/out/parallel 为 32768/8192/4096/2，27B 为 8192/4096/1024/1，
两者 max_images=1；27B 曾在较短输出预算下返回空答案。D0 必须重新绑定实际注册、镜像和硬件。
空答案或较多 completion tokens 不能单独证明模型产生了可提取思考。

上游参考：[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)、
[function calling](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md)、
[OpenAI Chat 字段定义](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。
这些参考用于解释字段语义，不证明目标镜像能力；D0 须补与镜像对应的源码提交、文档版本和实际结果。

## 3. 启用前必须取得的运行时证据

| 证据 | 必须回答的问题 | 缺失或失败时 |
|---|---|---|
| 固定运行时身份 | 镜像 digest、llama.cpp build/commit、模型/projector SHA-256、模板来源/hash、实际 argv、相关环境覆盖和封套 | 不解释性能或启用能力 |
| 工具行为 | 强制指定函数、required、auto、none 的实际效果；false 是否限制一次回答的调用数量 | 基础工具契约不满足则不启用 tools |
| thinking 行为 | 生成开关、effort 取值、解析格式、历史保留策略及思考/答案共用预算语义 | 不登记 thinking，不猜测关闭方式 |
| 计数一致性 | apply-template 是否实际使用 tools 和所有模板参数；特殊 token、生成前缀及图像收费是否对齐推理 | 不用字节估算替代，另评估同版本计数实现或升级 |
| 输出预算参数 | max_tokens 及固定镜像接受的别名、优先级、默认值和总输出计数语义 | 禁止预算覆盖绕过；S1完成前不启用新能力 |
| 客户端行为 | 实际客户端/SDK版本、发送字段、历史回填、SSE重组和超时 | 只宣称已通过客户端的支持范围 |

`/props` 提供模板和配置线索；未知字段得到200/400不能枚举完整参数语义。
每个受支持取值都需要有预期行为的探测，不能以 HTTP 200 代替“字段生效”。

## 4. D0：受控诊断与实施后闭环

### 4.1 硬件与执行边界

遵守根 [AGENTS.md](../AGENTS.md)：先用指定 SSH key 核实 Orin 硬件、目标绝对 checkout 路径、
remote、branch、SHA 和干净树；模型测试前采集磁盘、挂载、GPU/tegrastats。
使用独立外部证据目录，记录命令、退出码、耗时、峰值内存和停止后进程退出/compute quiescent。
不在目标修改 tracked 文件，不绕过调度器私自再启动占用 GPU 的模型进程。
help/version 和配置读取属于检查；生成请求可能加载、切换模型，属于受控诊断，不能称纯只读。

区分服务监听地址、客户端网关地址与 ModelSpec.port 对应的模型上游地址。
服务地址从部署产物确认，上游端口从注册确认；历史8090/8091只作线索，ModelSpec.port不是服务端口。
诊断允许对调度器已加载并受控的模型做 loopback 直连；结果只证明上游行为，不能代替服务验收。
诊断使用独占 lab 测试窗口，完成后通过既有生命周期接口停止并采集静默证据。

### 4.2 三个独立检查

1. **运行时探测**：固定镜像与实际配置，读取 help/version/props，逐项探测§3参数。
   强制指定函数验证结构化调用，分别记录 auto/none/required；auto 返回普通文本是允许结果。
   记录输出是否耗尽、解析器与模板配置，不能从一次纯文本结果直接认定模板不支持。
2. **基线服务探测**：构造合法合成历史，提交到兼容接口：user → assistant(tool_calls，content分别为null/缺省/空串)
   → tool(tool_call_id，字符串结果)。不依赖第一轮生成成功；标注为合成材料，仅证明校验/计数路径。
   分别记录本地422、计数失败503和上游错误，不混为同一结论。
3. **实施后真实闭环**：S1—S5完成后，经服务取得真实tool_calls；测试客户端回填固定结果，第二轮保留实际调用与reasoning，
   得到使用结果的最终答案。非流式和SSE分别执行；这才是能力验收，不得用直连或合成历史替代。

| 诊断结果 | 后续路线 |
|---|---|
| 上游可用，基线服务拒绝合法历史 | 进入契约与实现修复；基线拒绝不阻塞修复本身 |
| 需要调整已支持启动配置 | 通过开发机提交、同步后的独立lab runtime受控验证，不直接改目标checkout |
| 无法证明字段或模板生效 | 能力保持关闭，另评估镜像/模板变更，不由auto文本输出直接决定换模板 |
| 无法可靠计数 | 阻塞新能力启用，无字节近似例外 |
| 实施后真实闭环失败 | 不声明lab可用，保留失败证据，修复后同SHA复验 |

## 5. 冻结的行为契约

工具/思考字段的形状、能力和资源检查由 envelope 预检承担；ChatRequest 保持 extra=allow。
增加个别 DTO 字段本身不必然影响其他 extra，但本方案集中预检职责，避免两层规则漂移。

### 5.1 有效请求与输出预算

校验后形成一份有效请求，计数、预算检查、派发和测试断言均引用它；不能从原payload恢复已规范化字段。
网关传输/鉴权职责不变，由兼容处理器交付有效请求。

- 输出预算必须为严格正整数；bool、null、0、负数、小数拒绝。
- 缺省为 `min(4096, envelope.max_output_tokens)`；显式预算裁剪至封套上限，必须显式写入实际发往上游的请求。
  输入+有效输出仍须≤ctx_size。
- D0登记固定镜像接受的全部输出预算别名。本轮保留客户端使用的已支持字段名并规范化值；多个预算字段同时出现一律422。
  已知但镜像不支持的别名422，不静默忽略；完全缺省时补max_tokens。未知预算覆盖行为未查清前不开放新能力。
- 首版单choice：n缺省视为1，显式n仅接受严格整数1，防止多份输出破坏预算假设。
- 这些规则适用于带封套的兼容chat/vision，包括7B。补缺省、超限裁剪、预算别名冲突和n限制是明确兼容变更。
  temperature/top_p/stop等无关字段保持原JSON值，不将extra改为forbid。

### 5.2 工具定义、选择与体积

首版是已登记的function-tool子集。字节限制控制解析/传输负载，token限制控制模型预算，两者独立。
字节使用 `canonical_json_bytes`：键排序、紧凑分隔符、UTF-8、非ASCII不转义、禁止非有限数。
限额先作为兼容契约常量实现，不增加registration schema；后续每模型配置另走schema变更。

| 字段 | 首版契约 |
|---|---|
| tools | 可省略，非null数组，0—32项；每项type=function、function为对象 |
| function.name | 必填，独立规则 `^[A-Za-z0-9_-]{1,64}$`，大小写敏感且同一tools列表唯一；不复用模型ID正则 |
| description / parameters | 均可省略；提供时分别为字符串/JSON Schema对象；省略parameters表示无参数函数，保留缺省形态 |
| function.strict | 可省略；提供时必须为bool；首版不支持true，422；false透传，不承诺Schema强约束 |
| 扩展成员 | 已识别工具对象内的其他成员首版422；顶层extra仍allow，明确声明此兼容子集 |
| 字节限额 | 每个完整工具对象（含description、parameters等）≤8KiB，整个tools数组≤64KiB，同时满足 |
| tool_choice | tools缺省/空数组时缺省语义none，仅接受省略或none；非空时缺省auto，允许auto/none/required或 `{"type":"function","function":{"name":"..."}}`，指定名称必须存在于当前tools |
| parallel_tool_calls | 可省略或false，true首版422；工具模式下缺省在有效请求补false；限制一次回答的多调用，与max_parallel无关 |

Schema仅检查对象形状与资源限额，不执行工具、不访问`$ref`，不保证模型arguments符合Schema。
契约取值还须通过固定镜像探测；基础工具契约不满足则停止启用并先修订支持声明。

### 5.3 历史消息与能力检查

- assistant.tool_calls必须为非空数组；每条含非空id、type=function、合法function.name、字符串arguments。
  id/tool_call_id为不透明字符串，UTF-8≤64字节；每条消息≤32个调用，完整请求中的调用id唯一。
  全部历史arguments字符串UTF-8原文总量≤256KiB；完整HTTP body仍受既有总大小限制。
- 带合法tool_calls的assistant允许content缺省、null、字符串或已支持的part列表；图片扫描将null/缺省视为无parts，
  不改原消息。无tool_calls的普通消息继续使用原content规则。
- role=tool必须有tool_call_id和字符串content；只能引用前一未完成assistant调用批次，每个调用恰好一个结果。
  孤立/重复结果、未完成批次中插入user/assistant、请求结尾仍有待返回结果均422。
  允许串行回填已有多调用历史；parallel_tool_calls=false只限制本轮生成。
- 历史函数名不要求存在于当前tools，后续轮可以不再提供工具定义。
- reasoning_content若出现只能位于assistant，值为字符串或null；缺省/null/空串/非空值原样保留。
  模板是否保留旧reasoning须由D0绑定并计数，服务不自行拼入content。
- 图片沿用既有data URL、媒体、数量和像素限制；工具与图片组合只有在组合计数、验收通过后才能开放。

**完整请求能力判定**：非空tools、非none的tool_choice、assistant.tool_calls或role=tool任一出现均需tools。
空tools、tool_choice=none、parallel_tool_calls=false本身不要求工具能力。
显式reasoning控制或非null的assistant.reasoning_content（含空串）需thinking；null占位不表示启用思考。
两能力均依赖chat，含图片另需vision。无能力422 capability_mismatch，不能通过省略顶层tools绕过。

顺序：HTTP/JSON/DTO → 字段形状/体积 → 能力 → 历史关联/图片 → 计数 → 总预算 → 派发。
可本地确定的拒绝不得触发加载或生成。

### 5.4 Thinking：生成、预算、提取、历史

1. **生成**：27B lab默认思考状态绑定固定runtime/模板；不从flag缺省或模型名推定。
   普通请求也可能受新模板影响，须回归27B既有chat/vision。
2. **控制与预算**：reasoning_effort由固定版本上游解释，不承诺“接受但忽略”。D0冻结允许枚举及实际语义；
   未验证值422。若不支持effort控制，显式字段422，但仍可验证默认thinking输出。
   思考和答案共用有效输出预算；总预算语义无法确认时不启用thinking。
3. **提取**：采用实测reasoning-format将思考分离到reasoning_content。改变/删除提取设置不是关闭思考，
   不能用关闭提取缓解截断，也不实现服务端字符串剥离。
4. **历史**：服务保留回传字段；模板对历史的取舍必须记录并与计数一致；工具闭环保留实际返回的reasoning。

登记tools/thinking任一能力的模型，其全部chat请求（包括没有新字段的普通请求）首版拒绝未经验证的
chat_template、chat_template_kwargs、reasoning_format、parse_tool_calls、generation_prompt等模板/解析覆盖字段；
D0补全固定镜像实际识别的同类列表，防止通过省略tools绕过默认thinking的字段分离约束。
开放任何覆盖参数前须证明计数与派发一致且不破坏字段分离。未登记新能力的旧模型保留既有透传范围；
新能力模型的额外限制明确列入兼容变更。不能可靠关闭思考时撤回thinking声明并恢复已验证部署，不臆造关闭开关。

### 5.5 完整输入计数

计数接口接收有效请求和同一请求deadline，覆盖全部messages、tools及允许的模板影响参数。
同步更新TokenCounter、兼容处理器包装、_v2_token_counter、adapter计数入口，不能只给底层加tools。
计数与派发绑定同一模型/runtime/模板身份；实例重建或切换后若身份改变，重新核算或拒绝。

- 纯文本/工具/思考：预检token与真实prompt_tokens一致，特殊token及生成前缀规则相同。
- 图片按C06的max_image_tokens保守收费，单独核对文本模板与图片占位；可高于实际消耗，但不得低估。
  图像上界差额不能扩展成任意文本估算误差。
- definitions、description、历史arguments、tool结果、reasoning按实际完整模板计费；
  不能把工具JSON单独tokenize再相加替代完整渲染。
- apply-template接受字段却静默忽略不算支持；用有/无定义、长描述、历史结果对照证明计数有效。
- 不能可靠计数则失败封闭，不退回字节/字符近似。另评估同版本同模板实现或镜像升级，验证前不启用。
- 冷模型仅在原deadline内由调度器warm后重数；本地格式错误不warm，确定模板不支持不盲目warm重试。
  计数失败零生成，不自动重试已经不确定执行的请求。

### 5.6 响应与错误

非流式保留完整message、tool_calls、reasoning_content、finish_reason、usage；SSE保留delta原分片，
不要求每个delta都有完整id/name/arguments/reasoning，不在服务端重组改写。
验收客户端按choice/tool index拼接，成功结束时验证arguments为可解析JSON且名称、参数符合测试期望。
finish_reason=length不算成功闭环，不能执行未完成arguments；thinking成功须同时有可识别思考与最终答案。
覆盖UTF-8/JSON跨chunk、usage-only事件、DONE、断连、事件限额及租约释放，不伪造成功结束。

| 情况 | HTTP / code |
|---|---|
| 已知字段形状、历史关联、首版不支持取值 | 422 / contract_violation |
| 工具/历史字节限额、输入/ctx封套超限 | 422 / envelope_exceeded；显式输出先按§5.1裁剪 |
| 能力缺失 | 422 / capability_mismatch |
| HTTP总body超限、图片媒体不支持 | 沿用现有413及415映射 |
| 冷启动后仍无法计数 | 沿用兼容503 / service_unavailable，内部记录具体原因 |
| 上游协议错误、执行超时 | 沿用兼容502/504，不伪装成本地422 |

前三项retryable语义为false，沿用C05；兼容错误封装与internal C05不是同一套，不借机统一整个协议。
新增错误字段/码须先登记规范和契约测试；错误说明定位到实际字段，不能全部标记为messages。

### 5.7 能力注册与运行时隔离

通过M01新增tools、thinking，required_asset_roles均含model，protocol使用openai-chat，并要求同模型已登记chat。
它们是chat可选能力，不是新的internal execution operation。审查配置、控制协议、状态输出、fixture/candidate等消费方，
防止全局能力表扩展意外扩大operation闭集。

27B绑定独立RuntimeSpec，不能给7B共用runtime直接追加参数。
同步登记ALLOWED_STARTUP_FLAGS、profile.allowed_flags、LaunchRules.flag_sources、CAPABILITY_FLAGS、
运行器argv校验和部署产物；能力表只描述必需flag，实际渲染仍取startup_args。
--jinja可由tools或thinking任一要求；--reasoning-format的值绑定已验证thinking profile。
渲染器须表达“任一能力”约束，不能误用单一requires_capability要求两能力同时存在。
无能力却启用专用flag、缺必需flag、未知flag/value均拒绝渲染，不静默删参数。

矩阵/渲染代码可提前落地，真实27B能力注册须在S1—S5门禁通过后启用。
7B镜像、模板、启动参数、封套保持原注册；共享软件路径仍需回归。
每个能力均须有fixture；名称取并集不会自动提供多轮场景、组合测试或兼容通道。

### 5.8 专门验证兼容接口

新增兼容HTTP验收driver，经服务/v1/chat/completions执行cap:tools、cap:thinking，复用证据与硬件采集设施；
至少一组经真实客户端网关验证。旧chat/vision的internal B层用例保留，不能替代兼容预算及新字段验证。
新fixture明确transport=compat、请求序列、每轮边界和预期结果；单请求Fixture不足时扩展有界多轮场景，
不能把tools塞进internal inline_input后假装透传成功。
同步更新fixture闭集、候选覆盖、capability_output_problems、driver选择和证据核验；缺材料失败封闭。
仅tool_calls且content=null的第一轮不按旧chat“非空文本”规则误判。
功能场景证明调用/最终答案，边界场景证明工具、历史、reasoning、图片的输入总量和输出/ctx限制；
小请求成功不能宣称覆盖封套边界。

### 5.9 兼容性与驻留

7B需渲染不变、普通chat/vision、预算边界、SSE、冷启动计数回归；27B启用前后同样回归并加新组合。
固定seed只辅助复现；模拟上游用于确定性JSON值/SSE字节透传断言，真实模型不按文本逐字相等判兼容。
真实模型验证结构、参数、最终答案使用固定结果、思考标签不污染content、输出上限及静默。
旧硬件证据逐项记录沿用/重跑理由，共享路径改变的相关用例必须重跑，不能以argv不变豁免。

兼容chat每请求携带完整历史，不承诺跨请求驻留。轮间TTL卸载/切换通常意味着重载和延迟，不能直接推定会话丢失。
首版单客户端闭环，并覆盖一次轮间卸载重载后的历史回放；客户端持有历史，超时不能盲目重复有副作用工具。
测试工具只返回固定值，无外部副作用。

## 6. 实施切片与启用顺序

顺序 **D0 → S0 → S1 → S2 → S3 → S4 → S5 → S6**。
每片可由本地模拟/单元/集成独立验证，不等于每片可以提前开放真实能力。

| 切片 | 可审查结果 | 验收与门槛 |
|---|---|---|
| S0 契约接入 | 将冻结行为接入plan/03、C06、M01矩阵、plan/06；记录D0固定版本支持表 | 字段/错误/能力名/预算别名一致，未测不写成支持 |
| S1 输出预算修复 | 有效请求规范化，缺省/裁剪/别名冲突/n限制；仍不登记新模型能力 | A01—A02，捕获实际上游请求并跑共享路径回归，可独立交付bugfix |
| S2 请求契约与能力定义 | 字段、历史、体积、thinking检查和闭集能力定义；真实注册保持原能力 | A03—A04、A06负例；测试构造能力实例，合法缺省通过 |
| S3 完整模板计数 | 全链路有效请求与统一deadline，工具/思考/图片组合核算 | A05；无可靠核算则阻塞启用，不增加近似例外 |
| S4 运行时规则 | 独立27B runtime、flag门禁、组合规则、完整回滚配置 | A07；生成/测试产物，不在运行部署启用；7B产物对比通过 |
| S5 验收设施与透传 | 兼容HTTP多轮fixture、S/B覆盖、SSE断言、证据核验、7B回归 | A08—A10本地项通过；模拟材料不得代填真机证据 |
| S6 受控lab启用与复验 | 同SHA启用27B，通过服务/网关/客户端实测与回滚演练 | A01—A12适用真机项通过才宣称lab可用；失败恢复已验证部署并保留材料 |

按接口行为定位ChatRequest、兼容chat处理器、check_chat_input/TokenCounter、runtime计数入口、注册/渲染器和driver。
每片覆盖合法缺省、非法值、上下限、错误路径及旧请求；不能把所有可选字段缺失都列为负例。
代码/配置变更遵守AGENTS.md：Python3.12全量本地检查、审查、原子提交、push、目标守卫式ff-only、同SHA测试。
新能力默认未登记；S6受控启用不是生产发布。

## 7. 风险与回滚

| 风险 | 缓解与恢复条件 |
|---|---|
| 新模板影响普通27B请求/vision | 启用前后回归；失败恢复完整runtime/模板/注册，不仅删除能力名 |
| 新flag误用于7B | 独立runtime、错误组合渲染拒绝、产物对比；误共享阻塞启用 |
| 思考耗尽输出 | 记录reasoning/答案及finish_reason；lab评估预算/真实生成开关，不能用关闭解析器替代 |
| 工具/历史/图片超输入 | 派发前真实核算并422，不截断历史或改用字符近似 |
| 客户端参数绕过约束 | 固定版本参数表、有效请求一致、别名冲突拒绝、模板覆盖限制；未知行为阻塞启用 |
| 7B共享路径回归 | 本地与真机相关回归，逐项评估旧证据适用性 |
| 轮间卸载/切换/超时 | 完整历史重放和deadline验证，不承诺驻留，工具执行由客户端负责 |

按AGENTS.md在开发机审查并git revert，提交/push后目标ff-only，保留失败证据。
恢复项：能力集、runtime_id、startup_args及值来源、镜像digest、模板来源/hash、lab封套及渲染产物。
独立验收过的计数/输出预算修复应保留，不能为撤回tools重新引入预算漏洞；共享实现有回归时单独审查revert范围，
停用受影响路径直到约束恢复。目标同步后验证实际argv/身份、普通chat/vision、旧客户端、新能力拒绝及停止静默。
lab封套上调须记录内存/ctx余量、同SHA测试与回滚值，不修改生产封套或目标tracked文件。

## 8. 客户端验证

历史网关地址http://192.168.1.100:8091仅供定位；D0确认实际URL、认证方式和模型名，凭据不进入文档、历史或证据。
仅用实际开放的兼容端点；管理操作经既有管理路径。客户端版本、开关与发送字段由脱敏请求确认。

- CodeBuddy、Cursor、SDK分别记录实际测试版本，未测不宣称兼容；保留独立SDK闭环区分客户端/服务问题。
- 验证强制调用、id/arguments、结果回填、最终答案；thinking开启时验证实际历史回传和SSE重组。
- 不保留“CodeBuddy v2.94.3+ 已修”等无版本材料支撑的保证，以本轮请求/响应为准。
- 检查客户端是否发strict、parallel_tool_calls、预算别名和模板扩展，拒绝须指出具体字段与原因。
- 超时由本轮加载/切换/生成实测确定，分别记录连接、首字节、流间隔、总时长限制；历史300秒建议不是保证。
- 工具测试使用确定性假数据，不调用真实有副作用工具。

## 9. 交付与可执行验收

以下是**实施完成条件**，本轮文档修订不将其标记为通过。

| ID | 独立验收条件 |
|---|---|
| A01 | 输出上限1024时，缺省/请求4096实际向上游发送1024，请求64发送64；null/bool/0/负数/小数422且无派发；非流式/SSE一致 |
| A02 | 支持的预算别名单独裁剪；多别名/已知未支持别名422；n缺省/1通过，其余拒绝；无关sampling字段原值保留 |
| A03 | 大小写/下划线/连字符名称通过，点号/长度65拒绝；可选字段缺省通过；重复名称/未知指定工具/strict=true/parallel=true拒绝；32项、8KiB、64KiB及+1逐项验证，中文按UTF-8 |
| A04 | 合法assistant缺省/null/空串content工具历史通过；缺失/重复/孤立结果/未完成批次拒绝；历史id64字节及arguments256KiB边界通过、+1拒绝；无顶层tools历史仍检查能力；空tools+none不误拒普通请求；本轮parallel=false不拒合法多调用历史 |
| A05 | 定义、长description、arguments、tool结果、reasoning、图片组合全部核算；文本模板token与真实prompt_tokens一致，图像收费不低估；输入/ctx上限及+1拒绝；计数失败零生成，冷启动恢复不延长原deadline |
| A06 | 默认thinking和每个允许effort有实测语义；未支持值/模板覆盖422（新能力模型的普通请求也检查）；reasoning类型/角色错误拒绝，null占位与非null能力规则分别验证；实际reasoning与答案分离、历史回传不丢；length不算成功，总生成量受有效预算约束 |
| A07 | tools-only/thinking-only/两者/均无四种组合可测；缺chat/必需flag、错误专用flag拒绝；27B独立runtime，7B镜像/模板/argv/封套不变；完整回滚组合可启动 |
| A08 | 新S/B用例确走兼容HTTP；缺fixture/transport/原始轮次材料不能通过覆盖核验；cap:tools保留真实两轮且答案使用结果；能力名统一cap:thinking |
| A09 | 非流式字段保留；SSE跨chunk重组正确，覆盖tool索引/arguments/reasoning/usage-only/DONE/断连/事件限额；成功失败均有正确租约释放与静默证据 |
| A10 | 7B普通chat/vision、输入输出边界、SSE、冷启动计数回归；27B既有路径及新组合回归；真机用结构/语义断言，模拟验证确定性透传 |
| A11 | 热态及轮间卸载重载的单客户端真实闭环通过；至少一组经真实网关；记录所宣称客户端的版本及脱敏请求/响应，截断/超时不执行不完整调用 |
| A12 | 证据含硬件、remote/branch、开发/推送/目标SHA、前后干净树、模型hash、镜像/build、模板hash、argv、CUDA/runtime、命令/退出码/耗时/峰值内存、停止静默；完整组合回滚复验通过，保留失败材料 |

交付物：正式API/C06/M01/验收规范增量、S1—S6代码测试、固定版本支持表、兼容多轮fixtures、
lab启用/回滚部署材料、7B回归及历史证据复用表、客户端记录、plan/validation.md真实证据索引。
通过lab不等于生产验收；生产候选仍按ADR-05，不能用27B实验结果替代生产证据。

## 10. 待实测清单与停止条件

设计选择已在§5冻结；剩余项是启用前的证据任务，不由实现者猜测。

| 任务 | 所属切片 | 关闭条件 |
|---|---|---|
| 硬件、checkout、镜像/模型/模板/注册/地址确认 | D0 | 原始材料定位到同一部署与SHA |
| 工具/reasoning开关、字段取值/默认值/覆盖参数 | D0/S0 | 固定版本源码或文档依据与行为探测，未支持值有拒绝规则 |
| 输出预算别名/优先级/思考总预算 | D0/S1 | 有效发送值与实际输出限制一致，含非流式和SSE |
| 完整模板计数及图像收费差异 | D0/S3 | A05通过，不可可靠核算则继续关闭新能力 |
| 独立runtime与回滚组合 | S4 | A07通过且保存启用前后可重现产物 |
| 客户端、真实闭环、共享路径回归 | S5/S6 | A08—A12全部适用项通过，未验证项不宣称 |

镜像升级、模板定制、并发工具生成、internal协议扩展和生产开放另立方案；
不得以记录近似例外、模拟成功或仅看渲染不变解除停止条件。
