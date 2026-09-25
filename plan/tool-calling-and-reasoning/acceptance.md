# Tool calling / thinking 诊断与验收规格

本文件是[执行计划](README.md)的测试输入真源；行为定义见[TC01—TC09](contracts.md)。
状态：用例未执行；表格的“通过”是条件，不是结果。不得从本文件生成真实passed材料。

## 1. 验证分层与边界

- 软件层：假HTTP、可控时钟、模拟lease/identity，证明规范化、拒绝、计数投影、错误清理、SSE及材料校验。
- 后端层：目标模型、固定镜像、真实兼容HTTP与GPU归属，证明功能、资源边界和重载；模拟材料无资格通过。
- 客户端层：SDK和每个宣称支持的客户端版本经真实网关闭环。
- 回滚层：完整部署组合恢复，证明旧路径和新能力拒绝，以及最终静默。

A01—A12与Idea审查编号保持对应，但本文件是实施用例真源。
表中的边界通过指该维度校验通过；若构造的8192字节合法工具仍超过token封套，单元测试注入足够封套隔离字节测试，
不能要求超token的请求真机200。真机组合材料必须同时满足全部限制。
输出上限断言验证completion_tokens（包含reasoning）≤有效预算；短答案200不足以证明预算执行。
边界实验须有一次实际耗尽已请求预算的有界生成，否则记not_proven；不得无限生成或自动提高部署封套。

## 2. SiteInput v1 与材料身份

`site-input.json`由CT00在证据目录创建并人工核实，严格拒绝未知字段；它是测试输入，不注入服务运行配置。
字段表中的对象按子表闭集解析，数字不接受bool；URL不允许内嵌凭据，base_url不含/v1后缀。
输入相对路径以site-input.json所在目录为根，读取时校验后复制到本次新输出目录，报告中的相对路径统一以输出目录为根。

| 字段 | 类型/约束 |
|---|---|
| schema_version | 严格整数1 |
| checkout | 目标绝对仓库路径，必须已存在，remote/branch/SHA守卫先通过 |
| remote_url / branch / expected_sha | 实际remote、交付分支、完整40hex提交；https和SSH形式须指向同一已确认仓库 |
| deployment_id / mode | 实际部署ID；mode必须lab |
| phase | baseline或candidate，与probe命令--phase必须相同；candidate/gateway suite用candidate，rollback suite用baseline |
| service_base_url | 目标loopback HTTP地址；不得自动替换为模型上游 |
| gateway_base_url | string或null；CT10必须null，CT11才填已确认网关地址 |
| auth_env | string或null：测试进程读取的环境变量名称，值不进材料或日志 |
| hardware_file | 相对证据根的硬件原始记录路径，禁止symlink/逃逸 |
| models | 非空对象，键为实际model_id，首版仅7B/27B；值为下表 |
| timeouts | 对象：connect_seconds/read_idle_seconds/total_seconds，均为有限正数，运行前冻结 |
| request_limit | 严格正整数，每次probe/run最多发出的HTTP生成请求数；按选定用例展开后预检足够，否则退出2 |
| policy_source_sha256 / fixture_set_sha256 | 审查后的策略源码、fixture集合摘要，小写64hex；inspect/baseline probe尚无新增策略时允许null，candidate probe和所有run禁止null |
| rollback_input | 已验证回滚部署输入的绝对路径，只读；原始文件hash也记录在材料manifest |
| rollback_sha | 回滚步骤预期的完整40hex代码SHA；candidate/gateway时可null，rollback时必填且等于expected_sha，保留已交付预算修复 |
| client_evidence | 数组，默认空；每项仅name/version/evidence_root，用于gateway附加IDE客户端的脱敏原始材料；SDK由driver实测 |

models的值必须包含：capabilities/runtime_id/profile_id/image_digest/model_path/model_sha256/projector_path/projector_sha256/
template_sha256/upstream_base_url/envelope/launch_argv。projector的path/hash必须同时存在或同时null；vision时不可null。
capabilities是已核实注册的无重复字符串数组，取TC01模型能力闭集，不能根据欲测试的场景自行补能力。
model/projector路径是immutable输入，只读取并校验；不得下载、写入或修复挂载。
envelope复制完整登记封套，不由探测器重新猜数值；launch_argv为实际容器argv字符串数组而非shell片段。
template_sha256在baseline inspect前可null，但inspect必须提取并保存；candidate必须非null且与实际一致。
upstream_base_url只用于显式诊断，不作为兼容验收URL。对两个地址混用必须退出2。

inspect原始材料至少含：hostname、uname、nv_tegra_release、Python、CUDA/runtime、df、mount、nvidia-smi或不可用理由、
tegrastats、git状态/remote/SHA、逐资产sha256、镜像inspect/build/help、实际argv/相关环境和props、模板字节。
不输出整个进程环境，只采集影响llama模板/预算的非敏感项；认证值、完整用户对话、私钥不得收集。
未能取得模板字节/hash、镜像源码版本或关键预算语义→candidate probe不能通过。

## 3. ProbeReport v1：探测定义与判定

固定case结果集合：`passed | unsupported | needs_candidate | failed | not_run`。
baseline允许needs_candidate但不是能力通过；unsupported只在固定镜像源码/明确错误证明不支持时使用。
“200但字段是否生效未知”为failed或needs_candidate，不能标passed/unsupported。
candidate必需项任何非passed即退出3；输入不合法/缺原始文件退出2。

每个case行闭集字段：id、phase、status、source_ref、request_files、response_files、observation_files、
expected、actual、reason、started_at_utc、ended_at_utc；actual记录实测值，不支持手填passed覆盖status计算。
source_ref含镜像build/源码commit、路径/符号及相关行内容摘要；上游master URL仅作导航。
报告顶层：schema_version=1、site_sha256、phase、code_sha、cases、artifacts；artifacts为相对path/size/sha256数组。
passed必须同时有原始材料和满足下表的实际值；不是status文本自身构成证据。

| ID | 操作与对照 | 通过条件 / 失败分支 |
|---|---|---|
| D01 身份 | 读取固定镜像help/version、实际argv/环境、props和模板 | 身份与Site一致；新flag源码存在不等于已启用；身份错配停止 |
| D02 预算 | 分别发送max_tokens=32/64及每个源码识别别名，记录原请求/usage/finish_reason；冲突只在受控直连诊断 | 哪个字段限制总输出、默认与优先级有明确证据；32的约束须被耗尽证明；未知则阻塞CT02 |
| D03 工具选择 | 按§5强制指定get_weather；再测required、auto、none、parallel=false | 指定/required产生标准调用，none不调用；auto两种行为均合法；false不生成多个调用；纯文本且length优先判预算截断 |
| D04 工具模板计数 | 相同消息分别无tools、短tools、加长description；apply-template+tokenize与chat usage对照 | 有工具时模板包含定义，变更影响计数；纯文本精确相等；被忽略或低估失败 |
| D05 历史计数 | 使用真实call/result历史，增大arguments和工具结果；reasoning回传对照 | 所有实际进入模板的字段计费；模板丢弃的历史reasoning必须有源码/渲染证据，不能臆加或漏算 |
| D06 thinking | 默认生成、解析为deepseek；对源码接受effort枚举逐值测试或确认无效 | 思考与答案分离、总输出受预算限制；只有具有源码与实测语义的effort进入允许集；没有有效effort可用空集 |
| D07 图片组合 | 27B/7B各自真实vision，27B再加工具历史和思考；记录完整模板与usage | 模板文本与图片收费不低估；不能仅验证text工具路径就开放混合请求 |
| D08 服务合成历史 | null/缺省/空串assistant+调用+结果三份输入，全部经service_base_url | 基线只记录各自HTTP码和错误阶段，不要求通过；candidate要求合法历史通过、错误历史422且不生成 |
| D09 参数覆盖 | 枚举源码中模板/解析/预算覆盖字段；对有效请求计数/派发录制 | 模板影响字段被完整计数或按TC03拒绝；预算所有别名受控；unknown随机字段200不是这项通过证据 |

只读取已在本机的固定镜像，不执行docker pull；help使用无模型、无GPU占用的短进程并确认退出。
需要新flag的探测在baseline记needs_candidate，CT10使用已实现渲染器的私有lab部署完成；不得临时手改目标启动脚本。
直连生成只能在独占窗口、调度器加载的单一目标实例上进行，记录实例身份与停止证据，不作为A08两轮兼容证明。
request_limit用尽立即停止并保留已有材料；不足以完成必需项时结果失败，不偷偷多发请求。
D02固定user文本为`Print positive integers starting from 1, separated by spaces. Continue until the generation limit.`；
若固定镜像源码确认支持ignore_eos则附true，否则不添加。每次仅发送一个被测预算字段；冲突探测单独标识。
未耗尽预算记not_proven（case status=failed、reason=not_proven），禁止把短输出推断为字段有效。

## 4. A01—A12 软件与硬件用例矩阵

公共软件fixture：envelope.ctx_size=8192、max_input_tokens=4096、max_output_tokens=1024、max_images=1；
图片细项用既有有效vision fixture。合成policy同时支持max_tokens/max_completion_tokens，识别但不支持n_predict；
effort允许集为{"none","low"}，仅用于单元测试，不能当作目标镜像支持声明。
每个负例检查具体status/code/param及acquire/count/gateway/release调用次数。

| ID | 必须包含的测试 | 硬件证明 |
|---|---|---|
| A01 | 缺省→1024；4096→1024；64→64；0/-1/null/true/1.5/"64"→422且param=max_tokens；原payload不变；两个stream值 | 实际上游收到有效字段；强制有界长输出耗尽预算，usage不超过限制 |
| A02 | max_completion_tokens单独裁剪；与max_tokens同现即使值相同仍422；n_predict单独422；n=1/缺省通过，0/2/true/null拒绝；sampling/未知普通extra值保留 | 每个实际支持别名的约束相同，显式关闭/冲突不产生生成 |
| A03 | 名称1/64字符通过、65/点号/末尾换行拒绝；大写/下划线合法；description/parameters缺省通过；strict=false/缺省通过，true/null拒绝；tools32/33、对象8192/8193、总65536/65537；tool_choice所有取值与指定名称存在性；parallel=true拒绝 | 每种接受的tool_choice实际生效；none不调用；false最多一个生成调用 |
| A04 | assistant content缺省/null/空串通过；tool_calls null/[]拒绝；非字符串arguments拒绝；id64/65字节、arguments262144/262145边界；重复/孤立/未完成结果拒绝；历史多调用串行结果通过；省略当前tools仍检查能力；null reasoning占位与空串区分 | 第二轮使用真实id/arguments/reasoning，能在无当前tools时继续；畸形请求不派发 |
| A05 | 完整模板投影包括tools/choice/parallel/effort；token4096通过、4097拒绝；用专门封套/计数构造input+output=ctx及+1；错误receipt类型、hash、generation、模板、模型身份全部拒绝；deadline不重置 | D04—D07通过，工具/历史/图片/思考组合边界有原始模板、计数与usage证据 |
| A06 | reasoning仅assistant；null/缺省保留，不要求thinking；空串/非空/effort要求thinking；不支持effort422；新能力模型的普通请求也拒绝模板覆盖；原字段不拼入content | 第一轮reasoning非空、最终content非空，历史回传不丢；length不判通过；真实生成控制与提取分开记录 |
| A07 | 无/chat+tools/chat+thinking/两者组合；缺chat、缺flag、错误专用flag、错profile、production全部拒绝；并集flag只渲染一次；删除能力残留flag不能当回滚 | 27B独立runtime的实际argv与render相同；7B运行身份/镜像/模板/封套/argv不变 |
| A08 | fixture必须transport=compat；internal DTO/schema不变；缺fixture/driver/evaluator拒绝；只能用实际响应构造第二轮；删除第二轮或改变id再重填hash仍失败 | 非流式和SSE的两轮经过服务，至少一组经真实网关，固定结果被最终答案使用 |
| A09 | Unicode按字节切块、JSON跨chunk、仅首片id/name、arguments片段、usage-only、DONE；矛盾id、多生成index、缺/重复DONE、坏JSON拒绝；客户端断连/事件超限/计数取消/gateway失败每个lease恰释放一次 | SSE原始字节和完成/停止材料一致，未完成流不执行工具；cleanup与设备活动可关联 |
| A10 | 7B/27B普通chat/vision、预算边界、SSE、冷启动计数；v1旧路由与internal consumer回归；7B no-tools仍处理已知可选none/false占位 | 共享路径真机回归，budget-boundary同时施加登记max_parallel个单choice请求，不擅改并发/封套；固定seed仅辅助，按结构/固定答案断言 |
| A11 | 两轮历史存于客户端；首轮后卸载、下一轮重载仍回填原id；模拟超时不重放工具；request_limit和四变体展开可预检 | 热态/重载 × 非流式/SSE全过；客户端版本、发送字段、实际读超时入材料 |
| A12 | 证据缺失/路径逃逸/symlink/hash错误/身份错配/未知case/重复final拒绝；failed材料保留；回滚输入完整性校验 | 完整组合回滚后旧chat/vision可用，新工具请求422，最终队列/lease清零、实例停止与设备静默 |

本地测试函数名以A编号前缀，如`test_a01_budget_reaches_upstream`，便于按ID定位；不以私有函数单测代替HTTP consumer测试。
边界fixture生成可用同runtime计数搜索长度，搜索次数纳入request_limit；不能用字符/token比例猜测已达上限。
若该模板无法精确命中某token值，保存搜索轨迹并记not_proven，停止相关验收；不把“接近上限”换成passed。

## 5. 固定两轮工具场景

以下第一轮JSON中的model在执行时替换为Site已登记的27B ID，其他字段是固定fixture；max_tokens由fixture冻结为
`min(1024,envelope.max_output_tokens)`，不得在看到失败后临时增大。首轮stream按变体为false/true。

```json
{
  "model": "qwen36-27b",
  "messages": [{"role":"user","content":"Call get_weather with city Beijing. After the tool result, reply with exactly its marker."}],
  "tools": [{"type":"function","function":{
    "name":"get_weather",
    "description":"Return a fixed weather fixture for a city.",
    "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"],"additionalProperties":false}
  }}],
  "tool_choice":{"type":"function","function":{"name":"get_weather"}},
  "parallel_tool_calls":false,
  "max_tokens":1024,
  "stream":false
}
```

第一轮通过条件：HTTP200；choice0恰一个function调用、name=get_weather、id非空；
arguments解析后严格等于`{"city":"Beijing"}`；finish_reason=tool_calls，无length；不要求content非空。
生成arguments不合法即失败，不能补写参数以使测试继续成功。
固定tool结果字符串为规范JSON：`{"city":"Beijing","marker":"SMS_WEATHER_OK_27","temperature_c":23}`。
第二轮追加实际assistant消息和`{"role":"tool","tool_call_id":实际id,"content":固定结果字符串}`；
顶层省略tools/tool_choice/parallel_tool_calls，其他按TC08；最终content.strip()必须等于`SMS_WEATHER_OK_27`且finish_reason=stop。
如果模型不能稳定遵守这个固定简单任务，保存失败并评估模型能力，不把断言改为“任意非空回答”。

thinking场景第一轮用户为`Compute 19 * 23. Finish with RESULT=437.`，第二轮回填实际assistant后询问
`Using the previous result, add 1. Finish with RESULT=438.`。两轮content分别包含对应完整RESULT标记；
第一轮reasoning.strip()非空，第二轮发出的历史字段与第一轮原文一致；本场景不提供tools。
组合场景复用工具fixture，额外要求第一轮非空reasoning、第二轮完整回传、两个content不含模板思考标签。
能力独立性由软件四组合验证；首版真机部署chat+vision+tools+thinking，不能据此外推其它模型。

重载变体：首轮结束并释放HTTP lease后，经既有管理API卸载（不改registry），等待真实STOPPED和设备静默；
下一轮经兼容API自动冷加载，保持客户端历史不变，记录新的generation/instance并证明每轮各自计数/派发身份一致。
同一轮中的实例变化必须失败，不能与合法轮间重载混淆。

## 6. SSE测试原始输入构造

软件夹具至少把合法事件序列按每字节切块一次、按完整事件一次，并覆盖跨UTF-8边界：

```text
data: {"choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"查询天气"},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_fixture_1","type":"function","function":{"name":"get_weather","arguments":"{\"city\":"}}]},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"Beijing\"}"}}]},"finish_reason":null}]}

data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}

data: {"choices":[],"usage":{"prompt_tokens":123,"completion_tokens":45,"total_tokens":168}}

data: [DONE]

```

这是软件夹具，不是实际模型输出或硬件计数证据。服务透传不重写分片；验收聚合出的arguments必须精确为
`{"city":"Beijing"}`，reasoning为`查询天气`。以这些原事件生成断连/重复DONE/矛盾id等负例。

## 7. LabChatReport v1 与发布边界

新增CLI的run输出`lab-chat-report.json`，顶层严格字段：schema="lab-chat-report-v1"、suite（candidate/gateway/rollback）、
site_sha256、code_sha、policy_source_sha256、fixture_set_sha256、candidate_report_sha256、started_at_utc、ended_at_utc、attempts、final、artifacts。
candidate_report_sha256在candidate suite为null，在gateway/rollback绑定已通过candidate报告的原文件hash。
不允许summary、production_ready或device_backend_ready。verdict由verify重算并打印，不存手填通过结论。
attempt行字段为case_id/variant/attempt/status/reason/request_files/response_files/observation_files/cleanup_files/start/end；
status仅passed/failed/not_run，时间为UTC ISO8601；final每个必需case/variant恰引用一个存在的attempt号。
历史failed attempt及其材料永远保留；删除失败后重新编号不作为同一次run的合法修改。

candidate suite固定集合：B:<27B>:cap:tools与B:<27B>:cap:thinking各四变体（json-hot/sse-hot/json-reload/sse-reload）；
L:<27B>:tools-thinking也四变体；L:<model>:legacy对7B/27B各含chat-json/chat-sse/vision-json/cold-count/budget-boundary五变体。
gateway suite固定集合：L:<27B>:gateway两变体json/sse，以及client_evidence所宣称的每个客户端
L:client:<name>各json/sse变体。name为唯一小写ASCII字母/数字/连字符串。
rollback suite固定集合：L:deployment:rollback一个full变体。driver只验证已由开发机同步的回滚部署，不执行git或部署动作。
内置SDK由gateway两个场景覆盖，IDE材料由操作者在该冻结部署测试后提供；缺原始材料或版本即拒绝支持声明。
首版交付要求27B登记两能力、7B登记chat/vision且无新能力，candidate缺任何上述项不能通过。
L前缀是lab附加报告内部ID，不能写入report-v3的必测case列表。所有ID在CT07以常量/上述确定规则派生，
未知/重复/缺失拒绝，不提供--skip或静默N/A。每个run仅证明本suite，gateway/rollback run必须校验candidate原始材料。
CT09冻结的候选输入在`plan/tool-calling-and-reasoning/ct09-candidate/`：候选配置与渲染、fixture集、site输入副本与
`freeze.json`；site的实际路径、摘要、base URL、超时、request_limit、预算上限与回滚输入以`freeze.json`为准
（`check_freeze`为空），登记、模板或镜像变更后必须重新冻结，不能沿用旧摘要。

每个artifact字段为path/bytes/sha256；相对路径必须在证据根内、regular file、无symlink；缺失/hash错配退出2。
结构完整但HTTP/语义/身份/静默不满足退出3；全部满足退出0。evaluator从原请求/响应重新计算工具关联与结果，
从采样重算内存/设备归属，不信任attempt.status；tamper同时更新hash也不能隐藏语义失败。
最终verify必须同时提供candidate/gateway/rollback三目录，缺任一是输入不完整（退出2）。candidate和gateway的
源码/策略/fixture/硬件及27B注册身份必须相同，允许site差异仅为gateway地址、auth_env、client_evidence、rollback_sha。
rollback报告允许phase/expected_sha/注册/模板/argv/策略摘要变为明确的回滚输入对应值，必须逐项核对已审查的回滚材料；
硬件与模型文件hash不得改变，时间必须在gateway测试之后。预算修复与验收工具保留在回滚代码中，不能回退到工具尚不存在的提交。
除此受控转换外，任何换SHA/设备/runtime/模板沿用证据均拒绝；通过回滚不能覆盖候选阶段的原始失败。

lab附加报告可为既有S05/S06及B新能力case提供原始材料，但不能单独替代report-v3；接入时保持每case唯一final，
同时满足06的身份、有效期与全部原始材料要求。不降低原load/infer/envelope/cancel/stop/reload和O01—O06门槛。
CT12结论必须写“指定27B lab能力通过/未通过”，不能因为该文件verify=0宣称整个系统生产通过。

## 8. 执行记录格式

每完成一个CT任务，在README唯一状态表填写SHA/材料，并在validation追加实际记录：
`CT-ID、源码SHA、Python版本、命令、exit、UTC起止、remote/branch、目标SHA与前后干净树、site/policy/fixture摘要、
软件与硬件结论、失败材料、最终部署状态、未完成项`。
源文件、模板、注册、策略、fixture或实际设备改变时重新判定受影响证据；不以补写文档日期刷新测量有效期。
本次仅生成计划，无硬件结果；本页所有材料示例不能直接填进validation的通过记录。
