# 06 模型服务独立验收

## 1. 范围和结论

SelfModelSwitch 只有模型服务产品范围，必测层为 S（软件）、B（实际模型后端）、O（运维与切换）。
software_verified 只说明 S 通过；device_backend_ready 要求最终候选 S/B/O 全通过。
视频 P/Q/E、影片质量、声线一致性、人工关联和外部关系报告不进入本项目 gate。
视频项目另做 [集成与产品验收](video-analysis/04-acceptance.md)，不能从模型服务通过推导分析效果。
旧 reference、报告字段检查和 health200 均不构成真机通过。

待实施的tools/thinking扩展采用[专门诊断与验收规格](tool-calling-and-reasoning/acceptance.md)，
通过兼容HTTP执行真实多轮场景。其lab附加报告不替代本文件的完整S/B/O发布门禁；
能力case语法及fixture/evaluator的代码扩展由[CT03、CT07—CT08](tool-calling-and-reasoning/README.md)交付后才生效。

## 2. 执行和证据

executor 通过正式API施加动作，collector 采集真实实例/资源/事件，evaluator 从原始材料重算，gate检查全集。
不能直接改registry制造结果，不能把手填passed、GPU布尔或summary数字当依据。
候选摘要覆盖04列明的模型服务范围；每个注册模型、每项能力必须有对应fixture和evaluator，缺映射拒绝候选。
每个场景恰有一个最终结论；历史失败保留，not_run/skipped/unknown/failed均不通过。
新报告schema v3由M01/P03固定在 `model_scheduler/evidence_contracts.py`（严格解析、必测集合唯一映射、失败attempt保留、无summary字段），不直接使用归档的混合Candidate/AcceptanceReport。

原始包关联candidate、run/case/attempt、实际设备、代码/collector/evaluator版本、boot/generation/实例身份。
材料逐文件bytes/hash及相对路径；拒绝symlink、路径逃逸、缺失材料、hash错配、重复/未知场景。
各case时间由原始事件核对，报告开始/结束为最早/最晚case时间；允许未来偏差最多5min，从开始起有效7天。
重新包装不能刷新有效期；参数/设备/代码变化重测。hash证明完整性，现场采集环境仍为信任边界。

## 3. 必测集合

| 层 | 必测内容 |
|---|---|
| S01 | 动态模型/runtime/多资产、严格schema、旧配置迁移、无业务耦合 |
| S02 | load失败/迟到、旧boot/generation、stop返回但实例仍活、UNKNOWN保预算 |
| S03 | C+N=B及差1byte、MemAvailable=N+F及差1byte、过时/未来采样、不重复计账 |
| S04 | 交互/会话/preload竞争、drain不杀有效lease、TTL/硬期限、取消、队列和幂等 |
| S05 | 兼容API、blob owner/hash/配额/过期/重启、路径与请求限制、迟到输出不可误用 |
| S06 | 证据缺失/重复/伪passed/篡改、换设备/candidate、过期/未来时间、evaluator缺失 |
| B:<model>:load/infer/envelope/cancel/stop/reload | 每个模型执行全部六类真实场景，至少3次独立冷启动及3轮完整重载 |
| B:<model>:cap:<capability> | 每项声明能力实际消费专用输入，检查协议/shape/有限值/范围和执行设备；tools/thinking 走兼容两轮（见下），不用 legacy 单次 execute 顶替 |
| O01 | 至少1800秒所有模型真实请求、切换、等待、取消；最终队列/lease/session为空且实例停止 |
| O02 | 模型盘失效/恢复、暂存盘满/不可写、无根盘假目录写入、恢复重hash |
| O03 | Docker不可达、未知实例/端口、stop超时；UNKNOWN/BLOCKED保预算并health503 |
| O04 | 服务重启/残留实例、旧token拒绝、清理后准入、无自动推理重放 |
| O05 | 正确及篡改candidate/证据/资产/设备的preflight，错配在加载前拒绝 |
| O06 | graceful shutdown、日志容量/脱敏、配置/证据/暂存元数据备份恢复和回滚 |

case id 语法（P03 固定）：`S01`—`S06`、`O01`—`O06`、`B:<model_id>:<load|infer|envelope|cancel|stop|reload>`、
`B:<model_id>:cap:<chat|vision|embeddings|rerank>`。必测集合从候选登记集合派生；报告对每个case恰映射一个存在的最终attempt，
失败attempt永久保留且可追溯，缺case、未知case、重复最终结论、伪造summary一律拒绝。结构合法不等于passed：判定只来自evaluator对原始材料的重算。

**新能力case语法与材料责任（CT07 起）**：`tools`/`thinking` 仍是 `B:<model_id>:cap:<capability>`，但它们是 chat 的细化，
不是 internal execution operation，因此经公开兼容路由跑两轮（`transport=compat`）：第一轮只含用户消息（不得静态预填 tool call），
第二轮用**上游实际返回**的 assistant 消息（含 `reasoning_content`）与真实 `tool_call_id` 关联固定 tool 结果；工具场景第二轮去掉
`tools`/`tool_choice`/`parallel_tool_calls`，只允许固定假工具结果，不执行真实工具、不开子进程。每轮的原始请求/响应字节、
request_id、usage、finish_reason 与逐文件 digest 都落盘，由 evaluator 从原始材料重算关联（删掉第二轮或改 id 后重算 hash 仍失败）。
驱动由能力选定：chat/vision 继续 legacy driver 与其封套 fixture，tools/thinking 走 compat driver；没有 compat 驱动时该 case 记
`unknown`，不得转为 passed。既有 B 生命周期场景不因新能力减少，`L:` 前缀的 lab 附加报告不进入本表必测集合。

SSE 两轮材料（CT08 起）：服务透传不重写分片，聚合由验收侧负责——增量 UTF-8 解码、按空行切事件、合并 data 行后解析 JSON，
`[DONE]` 恰一次且结束；按 `choice.index=0` 与 `tool_calls[].index` 聚合，`id`/`type` 只首片出现（后续不同值即矛盾），
`name`/`arguments` 按到达顺序拼接，`content` 与 `reasoning_content` 分别拼接且 null 不添加文本；`usage`-only 且 `choices=[]` 合法。
坏 JSON、索引类型错、矛盾 id、DONE 后有数据、缺/重复 DONE、缺终结 finish_reason、parallel=false 下多生成 index 均为失败。
evaluator 只从原始材料重算（不读任何存储的 status）：工具轮 `finish_reason=tool_calls` 且 arguments 可解析为固定值，
最终轮 `finish_reason=stop` 且答案等于固定标记，`length`/`content_filter` 不是通过，思考用例要求第一轮 reasoning 非空，
答案不得残留模板思考标记；删掉第二轮、替换 `tool_call_id` 或改动 request 后重算外层 hash 仍被拒绝。
lab 附加报告（`lab-chat-report-v1`）只提供本扩展的原始材料，不能伪装为完整 report-v3 发布结论，也不能降低本表门槛。

S可用可控fake/event/时钟；B/O须真实目标设备，不用mock代替。维护故障只影响本deployment，不制造整机OOM或破坏其他磁盘/容器。
模型infer成功只说明能力运行及基本输出契约成立，不声称转写/人脸/声纹/视频质量达标。

## 4. 测量和性能policy

每轮停其他受管实例，至少10s基线；baseline=MemAvailable中位数，delta=max(0,baseline-运行窗口最小MemAvailable)。
采样从launch前到STOPPED后10s，间隔<=100ms、缺口<=500ms，至少3轮，measured_peak不得小于最大delta。
delta=0、前后基线差>256MiB或无关重负载使测量无效；不通过填1制造正预算。
每轮最大输入同时达到配置的context输入+输出、图像/音频规格、batch/concurrency组合边界，不以拆开小请求代替。
执行设备需provider、可归属实例的设备活动和真实输出共同支持；显式CPU能力按CPU验收。

policy在运行前固定各模型冷启动/推理/最大输入上限、内存、session期限、p95/p99、混合负载与错误率。
O01 arrival清单>=100项、每模型>=3项、计划间隙<=15s、发送偏差<=1000ms，不能空等30分钟。
p95/p99按nearest-rank，包含成功请求的排队+加载+执行；429/504比例分母为全部发送请求，各上限<=0.1。
OOM、非预期500、不安全淘汰、最终残留均须0；停止后重新采样确认可再次准入。
缺真实设备、资产、fixture或性能值则not_run/输入不完整，不填写占位通过值。

## 5. 发布门禁

最终候选身份/环境一致 AND S/B/O全集唯一且通过 AND 原始材料完整有效且可重算 AND 性能达标 AND 最终实例STOPPED。
verify不启动模型，只重算材料；缺材料/结构错误退出2、完整证据语义失败退出3、通过退出0。
production render只生成同一已验证候选的文件；现场preflight再次比对代码/资产/设备/栈/模式/证据。
模型服务发布包只含本项目代码、配置、证据和服务部署文件；视频项目使用自己的候选和发布流程。
