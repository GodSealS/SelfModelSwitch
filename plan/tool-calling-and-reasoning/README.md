# Tool calling / thinking 执行计划

日期：2026-09-24。类型：兼容接口增强 + 前置预算修复。状态：**CT00—CT08 已执行（CT01 含未补齐的 27B 材料，见下；CT02 的 lab 真机预算上限、CT05 的同租约计数均在目标机以同 SHA 回归通过；CT06 为本地软件候选，A07 硬件列留给 CT10），CT09—CT12 未执行**。
设计来源：[已修订方案](../../Idea/20260923-tool-calling-and-reasoning-plan.md)。源码基线：`854f39e1bb34ec3ce8678010d9b363a8021c6854`；
方案基线：`4e5f51e`。本计划不代表真机、客户端或生产通过，也不替代 RP 系列剩余任务。

## 1. 阅读顺序、范围与真源

1. 本页：唯一任务状态表、依赖、命令和交付流程。
2. [contracts.md](contracts.md)：TC01—TC09 是本扩展的规范性契约，包括 Python 接口、决策算法、错误与运行时规则。
3. [acceptance.md](acceptance.md)：唯一用例规格、诊断输入、真实两轮场景和证据核验规则。

Idea 文件保留设计来由，不再作为实施任务或新增字段的平行真源。本目录细化并接入
[03 API](../03-api.md)、[08 C01—C09](../08-execution-plan.md)、[06 验收](../06-acceptance.md)；
只对本扩展明确列出的兼容变更生效。原有生命周期、资产、权限、生产发布门禁继续适用。
本扩展不授权改变 ADR-05 的生产模型集合；任何未列出的冲突先记为 blocked，不自行放松既有门禁。

**完成行为**：27B lab 经兼容 HTTP 完成真实工具调用→结果回填→最终答案，支持思考字段分离与历史回传；
所有兼容 chat/vision 请求的预算与实际派发一致，7B共享路径回归通过，能恢复已验证部署。
不做工具执行器、通用映射、internal工具协议、跨轮驻留、模型/驱动下载、视频业务、生产新能力开放。
输出预算裁剪/缺省补值、预算别名冲突、n=1、已知新字段校验和新能力模型的模板覆盖限制是显式兼容变更。

## 2. 已核实实现边界

| 边界 | 当前实现 | 本轮落点 |
|---|---|---|
| HTTP DTO | ChatRequest.extra=allow；messages为开放对象列表 | 保留DTO透传，把新字段规则放在envelope预检 |
| 预算 | app.py丢弃check_chat_input返回的有效max_tokens | PreparedChat成为计数和派发的唯一请求来源 |
| 计数 | run包装重新计算deadline；adapter仅向apply-template发送messages | 同一deadline、完整模板输入、持有lease计数 |
| 模型租约 | Lease已有model_id/generation；scheduler.acquire可加载并给租约 | 本地拒绝在acquire前；计数在acquire后，生成在预算检查后 |
| 能力闭集 | control_protocol_v1.CAPABILITIES来自全局矩阵，并与PARAMETER_RULES集合比较 | 先分离EXECUTION_OPERATIONS，否则新能力导致模块导入失败 |
| 启动参数 | 实际argv由startup_args产生；CAPABILITY_FLAGS只定义必需项 | 独立27B runtime，显式any-of能力条件，完整回滚 |
| 验收 | B driver走internal，chat payload不发送tools；case与fixture按能力派生 | transport=compat多轮场景、专用driver/evaluator，保留旧driver |
| 证据 | report-v3派生S/B/O必测集合；27B未测量为生产候选 | lab附加证据不升级为production/device_backend_ready |

本轮读取了上述源码、03/06/08、ADR-05及根AGENTS。Graphify索引在源码基线提供定位，最终用源码核实；
未发现需引用的SysDocs库，不创建新库。目标配置和镜像本轮未读取，CT00负责确认，不沿用历史端口/性能。

## 3. 状态、依赖与停止规则

状态枚举：`pending → in_progress → software_verified → target_verified → done`；可从任一步进入`blocked`。
文档任务无运行时影响时可由审查直接done；含硬件的任务必须有target_verified，不得只凭软件测试done。
blocked记录缺少的具体输入、失败证据及恢复动作；not_run/skipped/unknown都不是通过。

| ID | 任务 | 依赖 | 状态 | 实际SHA / 证据 |
|---|---|---|---|---|
| CT00 | 基线、硬件与部署输入确认 | 无 | done | `854f39e1…`；目标证据 `ct00-tool-calling-baseline-20260924T040501Z/`，`site_input_sha256=51b970e2…` |
| CT01 | 固定镜像探测工具与基线材料 | CT00 | done | `7f1fdf78…`；目标证据 `ct01-baseline-20260924T050824Z/`：`inspect-a`（exit 0、missing 0）、`probe-e`（38/64）；D01/D04 passed |
| CT02 | 输出预算有效请求纵向修复 | CT01的预算字段清单 | done | `29a894f5…`；目标证据 `ct02-budget-20260924T105148Z/`：显式/别名预算 7B、27B 均耗尽 32→32，缺省上限 7B=4096、27B=1024 精确耗尽；本地 1080 passed |
| CT03 | 模型能力与execution operation分离 | CT02 | done | `a90494e0…`；本地 1087 passed、目标同 SHA 149 passed；schema 字节一致；证据 `ct03-contract-20260924T121318Z/` |
| CT04 | 工具/思考预检与历史状态机 | CT03 | done | `4b7303b1…`；本地 1156 passed、目标同 SHA 297 passed；A03/A04/A06-local 全矩阵 + 本地零调用断言；证据 `ct04-contract-20260924T144322Z/` |
| CT05 | 持租约完整计数与错误清理 | CT04 | done | `3f60bc2…`；本地 1175 passed、目标同 SHA 普通 chat/vision 回归 3/3 200；证据 `ct05-chat-regression-20260924T160538Z/` |
| CT06 | 27B独立runtime与能力/flag门禁 | CT03、CT01的flag源码证据 | software_verified | `2b3257df…`/`7f0524e…`；本地全量 1179 passed（12 项既有日期型失败与 HEAD 基线逐项相同）；目标同 SHA 复跑 122 passed；pending：A07 硬件列与 effort 登记归 CT10 |
| CT07 | 兼容HTTP多轮fixture和driver | CT05、CT06 | software_verified | `681d59892a59df4c9c5e14c6aba375a36cc8f3b0`；本地全量 1194 passed（12 项既有日期型失败与基线逐项相同）；pending：真机两轮与 SSE 聚合/evaluator 归 CT08/CT10 |
| CT08 | SSE、evaluator及证据反篡改 | CT07 | software_verified | `027820e…`；本地全量 1204 passed（12 项既有日期型失败与基线逐项相同）；pending：`run --suite` CLI 与 lab-chat-report-v1 属 CT10 前补齐 |
| CT09 | 7B/27B共享路径本地回归与候选冻结 | CT08 | software_verified | `8a6230ea…`（代码+候选输入；记录 `ct09-candidate/freeze.json` digest `fec0630a…`）；本地全量 1221 passed（12 项既有日期型失败与基线逐项相同）、目标同 SHA 51 passed；候选 site `09bc1428…` 已在目标解析通过；pending：真机 A01—A07 列与 27B 策略登记（登记后需重冻）归 CT10 |
| CT10 | 私有lab候选探测与真机验收 | CT09、CT00 | pending | — |
| CT11 | 网关/客户端闭环与完整回滚 | CT10 | pending | — |
| CT12 | 文档、材料索引与交付结论 | CT11 | pending | — |

执行顺序按表；CT06独立于CT04/05，但本计划不要求多agent并行。
CT01只探测基线配置，某项必须换启动flag才能验证时记`needs_candidate`，允许继续软件任务；
该状态只由CT10的候选实测解除，不能伪填passed。基础字段/flag在固定镜像源码中不存在时记`unsupported`，
阻塞依赖它的能力；升级镜像或定制模板另立任务。CT02所需预算语义不明则CT02也阻塞。
CT10前不修改运行部署的能力集。CT10的私有lab试验允许候选能力登记，但不连局域网网关、不宣称可用；
CT11完成后才允许登记为“已验证lab能力”。硬件探测失败不阻止保存已审查的软件代码，但不得关闭硬件任务。

## 4. 任务卡

### CT00 基线和输入

- 结果：外部证据目录的`site-input.json`与源码/硬件原始记录；确认开发分支、remote、目标绝对checkout路径。
- 操作：按根AGENTS检查本地与目标状态、upstream历史、硬件身份；读取目标部署产物确认服务/网关/模型三个地址、
  服务单元、model/runtime注册、封套、镜像digest、模型与projector文件hash、CUDA/runtime版本及Python3.12。
- 接口：site-input字段按acceptance §2；凭据只从既有受限文件读取，不写入JSON、argv或材料。
- 验证：`hostname; uname -a; cat /etc/nv_tegra_release`及挂载/磁盘检查，模型hash核对；记录完整命令与exit。
- 完成：必填项无占位值，硬件匹配，目标干净且无divergence。无法连接/dirty/硬件不符即blocked。
- 文件/文档：不改运行代码；本页记录材料位置，validation只记实际发生的核实。

### CT00 基线材料与已核实输入（2026-09-24 已执行）

- 证据目录（目标外部证据根）：`/home/jtzn/self-model-switch-evidence/ct00-tool-calling-baseline-20260924T040501Z/`；
  `site-input.json`（sha256 `51b970e2da18f30c667c62fba9a40d57bc0d6c0c947096232316280a92407ce2`）、`hardware-raw.txt`、
  `model-sha256.txt`、`materials.json`（11 项 path/bytes/sha256，校验 `validation_problems=none`）。
- 分支与路径：开发分支 `chore/tool-reasoning-execution-plan-20260924`（HEAD `17fe9a1`，本轮仅文档、未推送）；
  已部署交付分支 `chore/verify-v3-review-20260922` @ `854f39e1bb34ec3ce8678010d9b363a8021c6854`；
  共享远端 `https://github.com/GodSealS/SelfModelSwitch.git`；目标 checkout `/home/jtzn/SelfModelSwitch`（干净、无 divergence）。
- 三个地址：service `http://127.0.0.1:8090`（`/live` 200、`/v1/models` 200）；模型上游 7B `http://127.0.0.1:10002`、
  27B `http://127.0.0.1:10003`，llama-swap 控制面 `http://127.0.0.1:8080`；LAN 网关 `deploy/gateway.py` 监听
  `0.0.0.0:8091`（CT11 才填 `gateway_base_url`，基线为 null）。
- 部署身份：`deployment_id=sms-orin-lab2`、`mode=lab`、`config_sha256=3a966deb…`（与 `deploy2/scheduler-v2.json`、
  `config.yaml` 实测 hash 相同）；镜像 `sms-llama-cpp@sha256:8e572bb99c19…`（本机固定镜像，不 pull）；
  runtime `llama-cpp-1` / profile `llama-cpp-gguf-v1`；7B 与 27B 共用同一 runtime_id，能力均为 `chat,vision`
  （基线无 tools/thinking，CT06 负责 27B 独立 runtime 与能力登记）。
- 模型与 projector：四个文件实测 sha256 与登记完全一致（`model-sha256.txt`）；模型盘 `/media/jtzn/sandisk-ext4`
  （`/dev/sda1`，ext4，可用 340G）；未下载、未写入、未修复挂载。
- 硬件/运行时：`jtzn-desktop`，`Linux 5.15.148-tegra aarch64`，L4T R36.4.7、JetPack 6.2.1+b38、
  Driver 540.4.0 / CUDA 12.6.11；Python 3.12.14（`/home/jtzn/self-model-switch-build/venv312`）。
- 冻结输入：`timeouts` connect 5 / read_idle 60 / total 900（与网关及模型 `timeout_seconds` 一致）；`request_limit=64`；
  `policy_source_sha256`、`fixture_set_sha256`、`template_sha256`、`rollback_sha` 按 acceptance §2 暂为 null，
  分别由 CT01（策略/模板）、CT06/CT07（fixture）、CT09/CT11 冻结。
- 已知偏差与注意（不自行放松，CT01 起按此实现）：
  1. 目标 checkout 的 `origin` 是本地镜像 `/home/jtzn/git/SelfModelSwitch.git`（非 GitHub），当前 refs 与 GitHub 一致
     （main `142746c`、`chore/verify-v3-review-20260922` `854f39e`）。交付路径为 dev→GitHub **且**
     dev→镜像（`GIT_SSH_COMMAND='ssh -i ~/.ssh/selfmodelswitch-target-agent -o IdentitiesOnly=yes'`
     推送 `ssh://jtzn@192.168.55.1/home/jtzn/git/SelfModelSwitch.git`）→ 目标 ff-only；CT01 的 remote 守卫必须把
     “镜像与 GitHub 对同一分支 ref 相同且等于 `expected_sha`”作为通过条件，只比对其一不算通过。
  2. 在线服务不是 systemd 单元：llama-swap（pid 16492，`llama-swap.lab4.json`）与调度器（pid 290846，
     `deploy2/scheduler-v2.json`）为手工进程；`rollback_input` 取 `deploy2/manifest.json`（完整渲染组合），
     并在 `materials.json` 同行记录 `scheduler-v2.json`/`llama-swap.lab4.json`/`config.yaml` 的 hash。
  3. 在线 `llama-swap.lab4.json` 与渲染产物 `deploy2/llama-swap.yaml` 的差异仅为 `--publish` 端口占位符实例化
     与 JSON 化（`proxy`/`ttl`）；argv 与 manifest 的 `argv`/`argv_sha256` 一致。
  4. `/health` 当前 503（`checks.preload=false`，其余四项 true），`/live` 与 `/v1/models` 200；probe 的就绪检查
     不得依赖 `/health` 的 preload 项。

### CT01 探测工具与基线材料

- 新增`model_scheduler/acceptance/chat_probe.py`和`tests/test_chat_probe.py`，CLI见§5；不复用生产验收的passed字段。
- inspect只读取固定镜像版本/help、props及配置；probe在独占测试窗口对已由调度器加载的模型发送有限请求，
  输入按acceptance D01—D09，禁止新增GPU进程、自动拉镜像或更改目标tracked文件。
- 产物：`probe.json`与逐请求原始响应、参数来源、对照prompt/count、失败记录；解析为TC01 RuntimeChatPolicy的候选值。
- 预算清单包含max_tokens/max_completion_tokens/n_predict及该镜像源码识别的其他别名，逐项证明优先级和限制语义。
- 验证：`$SMS_PY -m pytest tests/test_chat_probe.py -q`；假HTTP测试证明未知字段200不等于支持、失败不能写passed；
  按AGENTS提交/同步工具后执行baseline probe。通过条件是记录真实完整，非要求基线已经支持新能力。
- 文档：回填D项状态与源版本；不得用master文档作为固定镜像的唯一证据。

### CT01 探测工具与基线材料（2026-09-24 已执行）

- 交付：`model_scheduler/acceptance/chat_probe.py`（`inspect` / `probe` 两个子命令）与 `tests/test_chat_probe.py`；
  分支 `feature/ct01-chat-probe`，目标 checkout 同步到 `7f1fdf78d369de7bbd3b05001f1eed2d9938782e`（干净）。
- 目标材料：`/home/jtzn/self-model-switch-evidence/ct01-baseline-20260924T050824Z/`
  - `site/site-input.json`（`site_sha256=6b1baf519eb676f7665c9582967270fb09fbd8b54a79c745bc9dae78932598c4`，
    `expected_sha=7f1fdf78…`，phase baseline，硬件与模型 hash 沿用 CT00 已核实值）。
  - `inspect-a/`（exit 0、`missing=0`）：镜像 `version: 0.4.1-dev (build 1, commit 4bc272f)`、help 全量、
    两个模型的 GGUF 模板字节与 hash、逐资产 sha256、git 状态、硬件原始记录、容器 argv。
  - `probe-e/`（最终跑，38/64 请求，未发无预算请求）：`probe.json`、`policy-candidates.json`、逐请求/响应/观测原始文件。
- D 项结论（`probe-e`，逐项均带原始材料）：

| 项 | 状态 | 实测结论 |
|---|---|---|
| D01 身份 | passed | 镜像 digest、`Id` 与登记一致；版本/commit `4bc272f`；模板可读；四个资产 hash 一致；checkout 干净且等于 `expected_sha` |
| D02 预算 | needs_candidate | `max_tokens`、`max_completion_tokens`、`n_predict` 在 7B 与 27B 上均被**耗尽证明**限制输出（32→32、64→64、finish_reason=length）；无预算缺省**未在基线测量**（见下） |
| D03 工具选择 | needs_candidate | 基线未登记 tools：五个 tool_choice 变体均未产生工具调用 |
| D04 工具模板计数 | passed | apply-template+tokenize 与 chat `prompt_tokens` 一致；工具定义变化影响计数 |
| D05 历史计数 | needs_candidate | 服务以 422 `message content must be a string or part list` 拒绝带 `tool_calls` 的 assistant；CT04 负责该形状 |
| D06 thinking | not_run | 27B 在本用例期间无法服务（见下）；**effort 允许集未取得，不可当作空集结论** |
| D07 图片组合 | failed | 7B vision 通过且图片被计费（53 > 23）；27B vision 502、27B 组合 422，同因于 27B 被置 error |
| D08 服务合成历史 | needs_candidate | 基线仅记录三个合成历史的 HTTP 码与失败阶段 |
| D09 参数覆盖 | needs_candidate | 五个模板/解析覆盖字段与一个未知字段全部被转发（200），基线不执行拒绝策略；未知字段 200 未被当作支持证据 |

- 策略候选（`probe-e/policy-candidates.json`，供 CT06 登记，仍是待验证候选）：
  `source_revision=4bc272f`；help 中存在 `--jinja`、`--reasoning-format`、`--reasoning-effort`、`--reasoning-budget`、
  `--ignore-eos`、`--chat-template-kwargs`（存在≠已启用，实际 argv 未含 `--jinja`/`--reasoning-format`）；
  recognized/supported 输出预算字段均含 `max_tokens,max_completion_tokens,n_predict`；
  模板 hash `qwen25vl-7b=a0bc6f6f…`、`qwen36-27b=e84f32a2…`；`effort_values` 未取得。
- **部署行为发现（对 CT05/CT06/CT10 有直接影响）**：
  1. 任一请求在上游失败（502）后，调度器把该模型置为 `State.ERROR` + `admission_blocked=true`，
     此后该模型的请求一律 503 "Service is not ready"，**只有重启调度器才能清除**（`/api/recover` 与
     `/api/models/{id}/unload` 均不能，unload 返回 409 `model_busy`）。本轮三次触发、三次以同配置重启恢复。
  2. 基线不发送无预算请求：实测该类请求会无界生成、以 abort 结束并把模型留在 error 状态。
     probe 仅在 **candidate** 相位发送它（CT02 给出有效缺省后才是安全的），基线记为
     `needs_candidate` + `not_measured`。
  3. 恢复后 smoke 显示 27B **默认就产生 `reasoning_content`**（TC07 "默认生成可提取思考" 的候选证据，
     但那是 smoke 观测，不是 D06 用例结果）。
- 未完成项（不得当作已验证）：27B 的 D06/D07 材料未取得，CT10 候选阶段必须补齐；
  thinking、27B vision 与工具+思考组合**未经验证**；`effort_values` 未取得，CT06 不得按"空集"登记。

### CT02 输出预算纵向修复

- 涉及`model_scheduler/envelope_validator.py`、`app.py`、`tests/test_envelopes.py`、`tests/test_chat_api.py`；
  按TC02增加PreparedChat及预算规范化，传给counter和gateway的请求相同，原payload不被修改。
- 此片可保持旧计数签名，TC05再整体替换；普通消息行为不因临时适配改变。
- 验证：上述两测试文件；必须先有捕获实际上游HTTP JSON的失败测试，再让A01/A02通过。
  覆盖预算缺省、裁剪、别名冲突、bool、n、SSE；仅测试effective_max_tokens返回值不算完成。
- 真机：基线7B/27B分别验证实际发送预算和总生成上限；输出长度未达到上限不证明限制，使用有界边界输入。
- 文档：03标记预算兼容例外、08 C06同步实际发送约束；不启用新能力。

### CT02 输出预算有效请求纵向修复（2026-09-24 已执行）

- 交付：`29a894f512a6b2b3ab46537b4401fdc568dc1a09`（分支 `feature/ct02-output-budget`，基于 `e10f168`；
  该代码提交已推送 GitHub 与目标镜像，其后仅有 `plan/` 文档回填提交，运行时代码不变；目标 checkout 停在该 SHA 且干净）。
  `model_scheduler/envelope_validator.py` 新增 `PreparedChat`/`prepare_chat`/`normalize_output`/`canonical_json_bytes`
  与固定镜像策略 `DEFAULT_OUTPUT_POLICY`（recognized/supported 预算字段取 CT01 `probe-e/policy-candidates.json`：
  `max_tokens,max_completion_tokens,n_predict`）；`app.py` 兼容 chat 路由先 `prepare_chat` 得到唯一 `body_json`，
  计数与 `gateway.open` 消费同一份体，来访 payload 不被修改。`tests/test_envelopes.py`、`tests/test_chat_api.py`
  按 A01/A02 增补捕获实际派发体（`RecordingGateway`）；`tests/test_config.py` 新增公共软件 fixture `V2_CHAT_FIXTURE`
  （envelope 8192/4096/1024，供 HTTP 级用例复用）。旧计数签名保留（TC05 再整体替换），v1 无 envelope 路径行为不变。
- 软件验证（开发机 Python 3.12.11）：`pytest tests/test_envelopes.py tests/test_chat_api.py tests/test_config.py -q`
  → **86 passed**；全量 `pytest tests -m 'not thor' -q` → **1080 passed、1 skipped、1 deselected**；
  `ruff check .`、`run.py --check-config`、`git diff --check` 通过。覆盖：缺省→1024、4096→1024、64→64；
  `max_completion_tokens` 单独裁剪且保留原字段名；`n_predict`（识别但不支持）拒绝；多预算字段/0/-1/null/true/1.5/"64" → 422
  且零派发；`n=1` 通过、其余值拒绝；采样式与未知 extra 保留；SSE 与非 SSE 同预算。
- 真机（目标 checkout `29a894f`，干净；Python 3.12.14；证据 `ct02-budget-20260924T105148Z/`，检查工具与逐例原始请求/响应同目录）：
  调度器以 CT02 代码重启（pid 322176，`/live` 200，7B/27B 均 unloaded 起步），全部请求经 `service_base_url`
  `http://127.0.0.1:8090`，使用 D02 固定有界文本；SSE 用例以镜像支持的 `stream_options.include_usage=true` 取精确 usage：
  1. **显式预算到达上游并被耗尽**：7B/27B 的 `max_tokens=32` 与 `max_completion_tokens=32` 全部 200、
     `finish_reason=length`、`completion_tokens=32`（7B 1.7–7.4 s、27B 7.4–29.1 s，含冷加载）。
  2. **27B 缺省上限**：无预算 SSE → 200、`finish_reason=length`、`usage.completion_tokens=1024`
     （= `min(4096, envelope.max_output_tokens)`，248.0 s）；独立增量材料为 1024 个 reasoning 增量。
  3. **7B 缺省上限**：无预算 SSE 自然停止（`finish_reason=stop`，30.3 s）**不构成上限证明**，按 not_proven 保留材料；
     改用 `ignore_eos: true`（镜像 help 含 `--ignore-eos`，先以 32 预算证明字段被接受并耗尽）重跑无预算 SSE →
     200、`finish_reason=length`、`usage.completion_tokens=4096`（= `min(4096, 4096)`，196.9 s）。
  4. 测试后状态：7B/27B 均 `ready`、无 blocked、无 last_error，queue 0、admission 开放，`/live`、`/v1/models` 200；
     设备 RAM 29597/62841 MB、GR3D 空闲档、温度 55–61 °C，与测试前一致；llama-swap（pid 16492）本轮未改动。
- 文档：`03-api.md` 标记预算兼容例外（已交付）；`08-execution-plan.md` C06 记录兼容路径的实际发送约束。
- 边界与未完成项：`DEFAULT_OUTPUT_POLICY` 仍是固定镜像候选策略，CT06 才按 `(profile_id,image_digest,model_sha256)` 绑定；
  并发/批量预算边界（A10）归 CT09/CT10，本轮不宣称生产通过。

### CT03 能力与operation分离

- 涉及`contracts_v2.py`、`control_protocol_v1.py`、`envelope_validator.py`、`tests/test_contracts_v2.py`、
  `tests/test_control_protocol_v1.py`；新增EXECUTION_OPERATIONS按TC01替代控制协议的全局矩阵推导。
- 注册tools/thinking及chat依赖；CAPABILITY_INPUT_KEYS补messages/tools需求描述；不为tools新增控制参数或operation。
- 验证：模块可导入；合法能力组合解析、缺chat拒绝；internal operation=tools/thinking返回既有契约错误而非KeyError/500。
  导出的control-v1 schema与原文件字节相同；若不相同说明边界外溢，必须修复后继续。
- 文档：08 C01和03区分模型能力与execution operation；无真实模型注册变更。

### CT03 能力与operation分离（2026-09-24 已执行）

- 交付：`a90494e0cf1c9442e51fcc22b13c5604e260fcf5`（分支 `feature/ct03-capability-operations`，基于 `c10efd8`）。
  `contracts_v2`：能力矩阵新增 `tools`/`thinking`（`required_asset_roles=("model",)`、protocol `openai-chat`），
  导出 `MODEL_CAPABILITIES`（即矩阵键集合）与 `CAPABILITY_DEPENDENCIES`（两者依赖 chat，注册解析时校验）。
  `control_protocol_v1`：新增 `EXECUTION_OPERATIONS=chat|vision|embeddings|rerank`，`CAPABILITIES` 仅作兼容别名；
  operation 解析、PARAMETER_RULES 断言与 schema 导出统一使用该集合，不再从全局矩阵派生。
  `envelope_validator.CAPABILITY_INPUT_KEYS` 增 `tools: {messages, tools}`、`thinking: {messages}`；
  `evidence_contracts` 的能力用例校验与注册覆盖改用 `MODEL_CAPABILITIES`。
- 验证（RED→GREEN）：新增用例先失败（`MODEL_CAPABILITIES`/`EXECUTION_OPERATIONS` 不存在、`chat+tools+thinking` 注册被拒、
  `cap:tools` 未识别、fixture 覆盖断言不平衡）；实现后开发机定向 **149 passed**，
  全量 `pytest tests -m 'not thor' -q` → **1087 passed、1 skipped、1 deselected**（942.13 s）；
  `ruff check .`、`run.py --check-config`、`git diff --check` 通过。
- 契约检查：模块导入正常（导入期 operation/参数表断言未触发）；`export-schema` 与 `schemas/control-v1.json` **字节相同**；
  internal `operation=tools|thinking` 返回既有 `contract_violation`（非 KeyError/500）；`chat+tools+thinking` 合法注册、缺 chat 拒绝；
  v1 配置闭集仍拒绝新能力；候选用例派生包含 `B:<model>:cap:tools|thinking`。
- 目标同 SHA（`a90494e…`，干净；Python 3.12.14；证据 `ct03-contract-20260924T121318Z/`）：
  受影响五文件 **149 passed**（2.41 s）、`import ok`、`schema_byte_identical=yes`；
  未改运行部署（调度器仍以 CT02 代码在线、`/live` 200，CT03 不改变当前注册的运行路径）。
- 已知慢用例（非本轮引入）：`tests/integration/test_control_socket.py::test_v2_tcp_app_serves_the_legacy_surface_and_never_the_control_routes`
  等待兼容 chat 路由的 900 s deadline 后按断言返回 503，单独复跑 `1 passed in 900.55 s`，整套耗时因此约 942 s。
- 边界：可启动/可服务仍由 profile 与 flag 门禁决定，`tools`/`thinking` 的启动支持属 CT06。

### CT04 字段校验、历史状态机与能力检查

- 涉及`envelope_validator.py`、`tests/test_envelopes.py`、`tests/test_chat_api.py`；按TC03/TC04实现检查。
- 校验输出PreparedChat；分离纯本地准备与运行时token检查；EnvelopeError补可选param，旧调用默认messages。
- 验证：A03/A04/A06-local全矩阵；无能力但历史含tool调用、null/缺省assistant、重复/孤立id、可选字段、UTF-8字节边界。
  本地拒绝必须观察acquire/warm/counter/gateway调用数全部为0，不能只看HTTP422。
- 文档：03字段/错误指向TC03，禁止把全部未知顶层extra收紧。

### CT04 字段校验、历史状态机与能力检查（2026-09-24 已执行）

- 交付：`4b7303b14217c35bd5ab20f77c5e6efef8363902`（分支 `feature/ct04-request-contract`，基于 `1137bed`）。
  `envelope_validator.prepare_chat` 成为唯一的本地判定入口，按 TC03 的固定阶段执行：
  ①输出预算与 `n`（TC02）；②新字段形状（`tools`/`tool_choice`/`parallel_tool_calls`/`reasoning_effort`/messages 新字段）；
  ③字节与计数上限（tools ≤32 项、单对象 ≤8192 B、数组 ≤65536 B，每条 assistant ≤32 调用，
  历史 arguments 合计 ≤262144 B，call id ≤64 B）；④整请求能力需求（tools/thinking→chat）与新能力模型的模板覆盖拒绝；
  ⑤`effort` 允许值、`tool_choice`↔`tools` 关联、TC04 工具历史状态机与既有图片检查。
  `EnvelopeError` 携带具体 `param`；`FixedOutputBudgetPolicy` 增 `effort_values`/`denied_template_fields`
  （denied 取 CT01 D09 清单；`effort_values` 空集是 fail-closed 默认，D06 未取证，CT06 必须补测后登记）；
  `collect_image_sizes` 允许带 `tool_calls` 的 assistant 省略 content。
- 验证（RED→GREEN）：新增用例先失败（策略字段不存在、校验缺失、CT02 的投影用例按新语义重写）；
  实现后定向 `pytest tests/test_envelopes.py tests/test_chat_api.py -q` → **127 passed**；
  相邻受影响 8 文件 → **197 passed**；全量 `pytest tests -m 'not thor' -q` → **1156 passed、1 skipped、1 deselected**（944.16 s）；
  `ruff check .`、`run.py --check-config`、`git diff --check` 通过。
- 关键语义：本地拒绝发生在 `acquire` 之前——HTTP 级断言 acquire/warm/release 未发生、counter 调用数 0、gateway 未打开；
  结构合法但缺能力优先 `capability_mismatch`（tools→`tools`，effort→`reasoning_effort`，历史→`messages`）；
  无 tools/thinking 的模型保留原模板透传；顶层未知 extra 仍透传（`03-api.md` 已注明不得收紧）。
- 目标同 SHA（`4b7303b1…`，干净；Python 3.12.14；证据 `ct04-contract-20260924T144322Z/`）：
  受影响八文件 **297 passed**（14.08 s）；导入与 `DEFAULT_OUTPUT_POLICY` 字段核对通过；
  未改运行部署（调度器仍以 CT02 代码在线、`/live` 200；CT04 对当前 chat/vision 注册的普通请求行为不变）。
- 边界：A03/A04/A06 的真机列（tool_choice 实际生效、false 单调用、真实两轮 id/arguments/reasoning）属 CT10 候选阶段。

### CT05 同租约计数与清理

- 涉及`app.py`、`run.py`、`adapters/llama_cpp.py`，新增`model_scheduler/chat_counting.py`；
  新增`tests/test_chat_counting.py`验证TC05接口与TC06顺序，同步`tests/test_chat_api.py`、
  `tests/test_envelopes.py`、`tests/test_llama_adapter.py`和`tests/test_runtime.py`中的相关consumer测试。
- 使用PreparedChat、同一deadline及现有Lease；acquire完成加载后再计数，删除兼容路径“任意异常→warm→重试”分支。
- apply-template→tokenize使用固定版本的模板参数规则；CountReceipt校验身份与request hash后才允许生成。
  未知模板参数拒绝；计数失败不降级、不重试生成；保留internal adapter原行为及其consumer测试。
- 验证：A05/A09-cleanup，注入切换、deadline消耗、计数4xx/5xx、断连、取消；每个已取得lease恰释放一次。
  本地全量后目标普通chat/vision回归；新能力真实计数在CT10验收。
- 文档：08 C06明确“本地检查在acquire前，token检查在同lease下且生成前”；取得lease不构成生成派发。

### CT05 同租约计数与清理（2026-09-24 已执行）

- 交付：`3f60bc2`（分支 `feature/ct05-lease-bound-count`，含 `b39467f` 与策略解析修复）。
  新增 `model_scheduler/chat_counting.py`：`RuntimeChatPolicy`（TC01 全字段 + 校验）、按
  `(profile_id,image_digest,model_sha256)` 的 `POLICY_REGISTRY`/`resolve_policy`（含 CT01 材料的两个 lab 条目）、
  `CountReceipt` + `validate_receipt`、`ChatCountPort` 协议与 `RuntimeChatCounter`（模板投影 +
  同租约计数 + 入口/复检身份 + `validate_binding`）。适配器新增 `count_template`（投影请求 + 策略
  tokenize 选项，内部执行路径不变）；`app.py` 改为 `chat_counter` 端口注入并按 TC06 顺序接线
  （prepare_chat → acquire → count → check_chat_budget → validate_binding → gateway.open），
  删除“任意异常→warm→重试”，计数/超限/绑定失败分别 503/422/503 且 ABORTED/REJECTED 恰释放一次；
  `run.py` 从注册表解析每模型策略并从 Book 读取身份/代数。
- 验证（RED→GREEN）：`tests/test_chat_counting.py` 13 项（投影、常量重叠拒绝、receipt 矩阵、图片计费、
  入口/复检身份、切换不重试、deadline 映射、绑定复核）；HTTP 级零调用/释放断言与 `count_template` 单测同批更新；
  定向 185 passed、相邻受影响一致；全量 `pytest tests -m 'not thor' -q` → **1175 passed、1 skipped、1 deselected**（69.27 s）；
  `ruff`、`run.py --check-config`、`git diff --check` 通过。
- 目标同 SHA（`3f60bc2`，干净；证据 `ct05-chat-regression-20260924T160538Z/`）：以该 SHA 重启调度器后，
  三个普通用例全部 200——7B `Reply with exactly OK.`（stop、prompt 24/completion 3、"OK."）、
  7B vision（1×1 PNG，stop、41/3、"Red."）、27B chat（length、15/16、reasoning 62 字符）；
  7B/27B 均 `ready`、无 blocked/last_error、queue 0，调度器日志无错误行。
- 文档：`08-execution-plan.md` C06 增“本地检查在 acquire 前、token 检查在同 lease 下且生成前；取得 lease 只表示预留”。
- 边界：`effort_values` 仍为 fail-closed 空集（CT06 补测）；未注册策略的模型按请求 503（失败封闭，不猜默认值）；
  新能力真实计数与 A05/A09 真机项归 CT10。

### CT06 启动策略与回滚产物

- 涉及`contracts_v2.py`、`runtime_profiles.py`、`deploy.py`、`model_runner.py`；按TC07实现。
  使用`tests/test_contracts_v2.py`、`tests/test_deploy_render.py`、`tests/test_model_runner.py`，新增`tests/test_runtime_profiles.py`。
- 新profile `llama-cpp-chat-features-v1`仅lab，27B绑定独立runtime_id；固定--jinja和thinking的--reasoning-format deepseek。
  值不支持则blocked，不在命令行自动替换auto/none；原7B profile不变。
- 同时扩展flag白名单、值来源、any-of约束和运行器argv校验；production render拒绝tools/thinking。
- 验证：A07四组合与负例；新旧7B完整render产物比较（排除仅由全局产物摘要变化引起的元数据，须逐字段列白名单）。
  不允许排除镜像、模板、argv、封套、资源预算差异；完整回滚配置可渲染。
- 文档：04记录独立runtime与lab限制；输出为本地候选产物，不操作部署。

### CT06 27B独立runtime与能力/flag门禁（2026-09-25 已执行，本地软件候选）

- 交付：`contracts_v2.py` 把 flag 词表拆为 `LEGACY_GGUF_FLAGS` + `CHAT_FEATURE_FLAGS`（`--jinja`、`--reasoning-format`），
  旧 GGUF profile 的 `allowed_flags` 冻结为前者（新 flag 在旧 profile 上解析即拒），新增可执行、仅 lab 的
  `llama-cpp-chat-features-v1`（资产角色基数与 GGUF 一致）；`require_production_openable` 拒绝该 profile 与
  任何 tools/thinking 注册。`runtime_profiles.py` 新增该 profile 的值源：`--jinja`（any={tools,thinking}，
  无值开关）与 `--reasoning-format`（固定 `deepseek`，any={thinking}）；`FlagSource` 增 `requires_any_capability`
  （与 `requires_capability` 同时给出时两个条件都必须成立），`CAPABILITY_FLAGS` 补 `tools: (--jinja,)`、
  `thinking: (--jinja, --reasoning-format)`；渲染前 `_require_chat_feature_registration` 强制
  “新 profile ⇔ tools/thinking 且 mode=lab”，首版按常量 `CHAT_FEATURE_MODEL_IDS={qwen36-27b}` 限制登记模型。
  `deploy.py` 的 v3 渲染把 per-model 渲染错误包成 `DeployError`（含 “cannot render the <mode> launch”）。
  `model_runner.py` 未改：运行器 argv 校验由 profile 派生（`_assert_server_arguments`），新 flag 自动纳入。
- 验证（RED→GREEN）：新增 `tests/test_runtime_profiles.py`（14 项 A07）先失败（`CHAT_FEATURES_PROFILE` 等不存在），
  实现后通过；`tests/test_deploy_render.py` 增两项（独立 lab runtime 的完整 render 绑定；v3 渲染遇新能力
  报 `DeployError` 且不写出可启动目录）。全量 `pytest tests -m 'not thor' -q`（Python 3.12.11 临时 venv）
  → **1179 passed、1 skipped、1 deselected**（45.9 s）；`ruff check .`、`run.py --check-config`、`git diff --check` 通过。
- A07 覆盖：四组合（无/chat+tools/chat+thinking/两者）→ 只渲染必需 flag 且各一次并集不重复；缺 chat 依赖、缺必需 flag、
  tools 上出现 `--reasoning-format`、无能力模型用新 profile、旧 profile 登记 tools/新 flag、production 渲染、
  非 `qwen36-27b` 登记新能力全部拒绝；THINKING 值固定 `deepseek` 且不出现 auto/none 替换；
  7B 旧 profile 两次渲染的 argv 差异仅为白名单内的 `config-sha256` label（并以改 envelope 的反例证明比较非空转）。
- 既有失败（与本任务无关，基线可复）：`test_verify.py`/`test_preflight_v3.py`/`test_deploy_render.py` 共 12 项，
  原因是 fixture 冻结的报告日期 `2026-09-18`/`2026-09-01` 已超过 7 天有效期；以 HEAD 原样的临时工作树复跑，
  失败集合与本改动后**逐项相同**（另有 1 项同族用例因时间边界偶发）。
- 未完成项（不宣称已验证）：A07 硬件列（27B 独立 runtime 的实际 argv 与 render 相同、7B 运行身份/镜像/模板/封套/argv 不变）
  需 CT10 候选部署后取证；`chat_counting` 的 27B 新 profile 策略条目与 `effort_values` 仍为 fail-closed 空集，
  待 CT10 的 D06 实测登记前不得计入；因此本页 CT06 记为 `software_verified`，不是 `target_verified`。
- 目标同 SHA 复跑（Python 3.12.14，`2026-09-25T11:33:14Z`—`11:33:20Z`）：checkout `/home/jtzn/SelfModelSwitch`
  → 守卫流程 → `verified_target_sha=7f0524e9ad58603835192ed457e12e62326ece40`、树为空；受影响五文件
  **122 passed**（2 项同为上述日期型既有失败），与开发机结果一致。GitHub 与目标镜像 ref 相同。
  **未重启调度器、未渲染安装**：CT06 不触碰运行部署，A07 的实机列仍在 CT10 候选部署时取证。

### CT07 兼容多轮fixture/driver

- 新增`model_scheduler/acceptance/chat_compat.py`、`tests/test_chat_compat_acceptance.py`；修改fixtures/driver选择。
- 按TC08新增CompatScenario与CaseSpec，不扩展internal execution DTO；旧chat/vision走旧driver，新能力走compat。
- driver只执行acceptance规定的固定假工具；保存原始请求/响应，不能静态填第一轮call或丢掉reasoning字段。
- 验证：A08，模拟上游真实两轮数据关联，role/tool_call_id、transport与fixture缺失/错误即拒绝。
- 文档：06登记新能力case语法和材料责任；existing B lifecycle场景不减少。

### CT07 兼容多轮fixture/driver（2026-09-25 已执行，本地软件候选）

- 交付：新增 `model_scheduler/acceptance/chat_compat.py`——`CompatScenario`（TC08 全字段 + 固定期望，`digest()`
  覆盖每个字段与期望值）、`CaseSpec`（case/variant 绑定，variant 与 stream 必须一致、case id 由能力派生）、
  `CompatTransport`/`CompatAggregator` 端口、`CompatDriver.run`（两轮 + 逐轮原始材料）、`build_second_round`
  （只用实际响应构造第二轮：真实 assistant 原文含 reasoning、真实 `tool_call_id` 关联固定结果，工具场景第二轮移除
  `tools`/`tool_choice`/`parallel_tool_calls`）、`compat_material_refs`/`link_problems`/`verify_material`。
  固定假工具：`get_weather` → `{"city":"Beijing","marker":"SMS_WEATHER_OK_27","temperature_c":23}`；thinking 两问固定
  `RESULT=437`/`RESULT=438`；first round 只允许 user 消息（静态预填 tool call 即拒绝）。
- driver/fixture 选择：`fixtures.py` 的 legacy fixture 跳过 tools/thinking（未知能力仍拒绝），
  `backend_cases.CaseExecutor` 增 `compat`/`compat_variant`：`cap:<feature>` case 交给 compat 路由，无 compat 驱动时记
  `unknown`（带问题），既有 load/infer/envelope/cancel/stop/reload 与 `cap:chat|vision` 不变。
- 验证（RED→GREEN）：新增 `tests/test_chat_compat_acceptance.py`（15 项 A08）先失败（`chat_compat` 不存在），实现后通过；
  全量 `pytest tests -m 'not thor' -q`（Python 3.12.11）→ **1194 passed、1 skipped、1 deselected**（41.64 s）；
  失败 12 项与 CT06 基线逐项相同（冻结报告日期过期的既有失败）；`ruff`、`run.py --check-config`、`git diff --check` 通过。
- 覆盖要点：transport=compat 与 rounds=2 的闭集校验；缺 transport/scenario/SSE aggregator 与非空证据目录拒绝；
  fixture 摘要对任何字段与期望变化敏感；第二轮随上游 id 变化（不静态填）；thinking 保留 reasoning 与固定追问；
  两轮原始字节与 usage/finish_reason 落盘；删除第二轮或改 id 后重算 hash 仍被 `link_problems`/`verify_material` 发现；
  internal execution operation 闭集与 `schemas/control-v1.json` 未变。
- 目标同 SHA 复跑（Python 3.12.14）：checkout `/home/jtzn/SelfModelSwitch` 走守卫流程 →
  `verified_target_sha=64caa99c6d0d9a5b5215620d56568ad3605585cc`、前后树为空；CT07/CT06 受影响四文件 **76 passed**，
  与开发机一致；GitHub 与目标镜像 ref 相同，未重启调度器。
- 未完成项（不宣称已验证）：真实网关/真机两轮与 SSE 聚合（CT08 的 aggregator/evaluator、CT10 候选部署）；
  `run --suite candidate` 的 CLI 与 `lab-chat-report-v1` 属 CT08；27B 新 profile 策略与 `effort_values` 仍待 CT10。

### CT08 SSE、evaluator与证据核验

- 涉及acceptance目录的chat_compat、backend_cases、materials、verify；使用`tests/test_chat_compat_acceptance.py`、
  `tests/test_backend_cases.py`、`tests/test_verify.py`、`tests/test_candidate.py`及`tests/test_evidence_contracts.py`，实现TC08聚合器和独立evaluator。
- 同时更新evidence_contracts/candidate/fixture覆盖，保证case由完整模型能力集合派生；不得仅修改输出检查函数。
- 验证：A08/A09，参数分片、非ASCII跨chunk、多id、重复DONE、缺DONE、usage-only、断连/超限的正负例；
  删除原始第二轮、替换tool_call_id、修改request hash并重算外层hash仍被evaluator拒绝。
- 文档：06新增能力材料与派生规则；lab报告不伪装为完整report-v3发布结论。

### CT08 SSE、evaluator与证据反篡改（2026-09-25 已执行，本地软件候选）

- 交付：`chat_compat.py` 增 `SseAggregator`（增量 UTF-8 解码、按空行切事件、合并 data 行后解析 JSON、
  `[DONE]` 恰一次且结束、按 `choice.index=0` 与 `tool_calls[].index` 聚合、id/type 首片为准且矛盾即拒绝、
  name/arguments 顺序拼接、content/reasoning 分离拼接且 null 不添加、`usage`-only 合法、事件上限）、
  `evaluate_case`（只从原始材料重算：链接、固定工具与参数、两轮 finish_reason、固定标记、thinking 非空 reasoning、
  模板思考标记残留，且不读存储 status）、`write_fixture_material`/`fixture_set_digest`（候选形状的 fixture 条目与集合摘要）。
  driver 改为**每轮新建聚合器**（工厂可调用对象或类），流式轮由聚合器重组；`compat.json` 增加 `capability` 字段。
- 验证（RED→GREEN）：CT08 新增 10 项用例先失败（`SseAggregator`/`evaluate_case`/`fixture_set_digest` 不存在）；
  实现后 `tests/test_chat_compat_acceptance.py` **25 passed**；全量 `pytest tests -m 'not thor' -q`（Python 3.12.11）
  → **1204 passed、1 skipped、1 deselected**（42.25 s）；失败 12 项与基线逐项相同；`ruff`、`run.py --check-config`、
  `git diff --check` 通过。
- A09 覆盖：逐字节切块的 SSE 重组、非 ASCII 跨字节边界、仅首片带 id/name 的 arguments 片段、usage-only 与 null delta；
  坏 JSON、索引类型错、多生成 index、缺/重复 DONE、DONE 后有数据、缺终结 finish_reason、空 call id、事件超限全部拒绝。
- A08 覆盖：删掉第二轮即拒绝；替换 `tool_call_id` 并重算全部 hash 仍被 evaluator 发现；`length` 不算通过；
  缺固定标记、thinking 无 reasoning、答案残留 `<think>` 均被记为问题。
- 未完成项（不宣称已验证）：`run --suite candidate/gateway/rollback` 的 CLI 与 `lab-chat-report-v1` 仍待实现（CT10 前）；
  真机/真实网关两轮与 lease 释放计数归 CT10/CT11；`chat_counting` 的 27B 策略与 `effort_values` 待 CT10。

### CT09 回归与候选冻结

- 所有本地测试（包含7B及27B普通路径、v1兼容、internal不变）通过，覆盖A01—A10的软件项。
- 固定受测代码SHA、独立lab输入、RuntimeChatPolicy源码版本/摘要、fixture与模板hash；记录具体变更例外。
- 冻结CT10用的base URL、超时、最大请求数、预算上限及回滚输入；不使用伪造的实测passed完成冻结。
- 验证：§5通用检查和render对比；候选中不存在未定义取值、浮动镜像或模型占位hash。
- 文档：本页记录software_verified；记录7B旧证据逐项复用/重跑理由，不标真机passed。

### CT09 回归与候选冻结（2026-09-25 已执行，本地软件候选）

- 交付（候选代码+输入提交 `8a6230ea74ce8127eb71476b9102fe9ef166c582`，分支 `feature/ct09-freeze-candidate`）：
  - `model_scheduler/acceptance/chat_freeze.py`：CT10 冻结记录的闭集 schema（`parse_freeze`）、可冻结性检查
    （`check_freeze`：浮动镜像、占位/全零 hash、未定义 `effort_values`、模板表与模型条目不一致、预算上限缺项、
    请求预算低于 suite 最小量、没有记例外的冻结全部拒绝）、`minimum_requests`（按注册能力、variant 与轮次展开候选
    suite 的最小生成请求数）与 `freeze_digest`。
  - `model_scheduler/chat_counting.py`：`policy_source_digest()`——`source_revision` 加每条已注册策略（TC01 字段）的
    规范化摘要，作为 `policy_source_sha256` 的可复算来源。
  - `model_scheduler/acceptance/chat_compat.py`：`tools_thinking_scenario`（组合 fixture：复用工具两轮，另要求第一轮
    reasoning）与 evaluator 的组合判定（第一轮 reasoning 非空、第二轮历史逐字段回传第一轮 assistant 原文）。
  - `plan/tool-calling-and-reasoning/ct09-candidate/`：候选配置 `scheduler-v2.json`（目标已部署 lab 配置的最小改动：
    新增仅 lab 的独立 runtime `llama-cpp-chat-features-4bc272f`，27B 能力 chat+vision+tools+thinking 并切到该 runtime）、
    渲染产物 `manifest.json`/`llama-swap.yaml`、6 个 fixture 文档、site 输入副本与 `freeze.json`。
- 冻结值（`ct09-candidate/freeze.json`，digest `fec0630afcccec510e514e8e6e9e8a1bad10f4a894d5193dc118b6cee736a5e6`）：
  `code_sha=8a6230ea…`、`policy_source_sha256=1324dcde…`、`fixture_set_sha256=cb8544ca…`、
  模板 hash 7B `a0bc6f6f…` / 27B `e84f32a2…`、`service_base_url=http://127.0.0.1:8090`、超时 5/60/900、
  `request_limit=64`（≥ suite 最小 35）、预算上限 7B 4096 / 27B 1024、回滚输入
  `…/qwen36-27b-lab/deploy2/manifest.json`（sha256 `1c04f3c7…`）、两模型身份与 `max_parallel`；`check_freeze` 为空。
- 验证（RED→GREEN）：`tests/test_chat_freeze.py` 11 项、`tests/test_chat_compat_acceptance.py` 组合 2 项、
  `tests/test_ct09_candidate.py` 4 项（记录可冻结、策略与 fixture 摘要同时与代码和已提交文档一致、候选配置重渲染与
  已提交 manifest 字节相同、site 输入与记录/渲染/自身摘要一致）在实现前失败。
  全量 `pytest tests -m 'not thor' -q`（Python 3.12.11，`15:18:41Z`—`15:19:25Z`）→
  **1221 passed、1 skipped、1 deselected、12 failed**（43.06 s）；失败 12 项与 CT06 基线逐项相同（冻结报告日期过期的
  既有失败）；`ruff check .`、`run.py --check-config`、`git diff --check` 通过。
- render 对比：候选配置重渲染与已提交 manifest/llama-swap 字节相同；27B argv 末尾为 `--jinja --reasoning-format deepseek`
  且各一次，runtime/profile 为独立 lab 组合；7B 与目标已部署 manifest 的 argv 差异只有白名单内的 `config-sha256` label
  （身份、镜像、资产、封套、端口不变）；production 渲染仍拒绝（既有 CT06 用例）。
- 7B 旧证据复用理由（本轮不复测真机）：7B 的 runtime/profile/模板/封套/启动 argv 均未变（上一条渲染对比），
  CT02 的预算耗尽材料（7B 4096）与 CT05 的普通 chat/vision 三例对同一代码路径继续有效。
- 目标同 SHA（`8a6230ea…`，干净；Python 3.12.14，`15:19:32Z`—`15:19:33Z`）：checkout 走守卫流程 →
  `verified_target_sha=8a6230ea…`、前后树为空；
  `tests/test_chat_freeze.py tests/test_chat_compat_acceptance.py tests/test_chat_counting.py` **51 passed**（0.60 s）；
  候选 site 输入在目标以该 SHA 的代码解析通过（`phase=candidate`、两个模型、模板/策略/fixture 摘要与冻结值一致）。
  证据目录 `/home/jtzn/self-model-switch-evidence/ct09-candidate-20260925T151642Z/`：`site-input.json`（`09bc1428…`）、
  `hardware-raw.txt`（`ebab29ab…`）。**未重启调度器、未改运行部署**。
- 例外（同时记录在 `freeze.json.exceptions`，不在文档里淡化）：27B 的 `llama-cpp-chat-features-v1` 策略条目仍未注册
  （fail-closed；CT10 的 D06 实测登记会更新 `policy_source_sha256` 与 `code_sha`，登记后必须重冻）；`effort_values`
  空集不是测量结论；CT01 遗留的 27B thinking/vision/组合未测项由 CT10 关闭；`rollback.code_sha` 待 CT11 的已审查回退提交；
  候选 suite 的 L 前缀 case id 与 `lab-chat-report-v1` CLI 仍是 CT10 前的补齐项。
- 边界与未完成项（不宣称已验证）：本页只记 software_verified；A01—A07 真机列、工具/思考/组合的真实两轮与计数身份、
  `needs_candidate` 关闭均属 CT10；网关/客户端闭环与完整回滚属 CT11。
- 记录提交的边界：`code_sha` 之后只允许 `plan/tool-calling-and-reasoning/` 与冻结测试的增量，CT10 部署前用
  `git diff --name-only 8a6230ea… <head>` 复核；运行代码、候选配置与渲染产物都停在该 SHA，之后的重冻必须显式替换记录。

### CT10 私有lab真机验证

- 使用已push且目标同SHA的候选，停用局域网网关接入，只在loopback测试；先验证干净树和实际硬件/资源。
- 若候选与现有部署不能安全共存，按既有生命周期停止旧受管实例并证明静默后再切换，禁止额外GPU进程。
- 重跑candidate probe关闭needs_candidate；按acceptance场景完成A01—A10硬件项及A11重载项。
  对输入、输出、ctx的边界材料逐个核实，不因短答案200豁免；保存所有失败与cleanup记录。
- 支持失败或计数差异→停止候选、恢复基线，标blocked；不得自动升封套/换模板/升级镜像求通过。
- 软件修改必须回开发机、重测、提交/push/sync后重跑受影响场景；最终共享SHA材料才能汇总。
- 文档：validation写真实材料索引，仍仅lab；不更改06的生产S/B/O全集门槛。

CT10子项（依赖顺序；父项验收全集不变，未全部通过不得进入CT11）：

| 子项 | 交付 | 依赖 | 状态 |
|---|---|---|---|
| CT10a | `chat_compat run --suite candidate` CLI + `lab-chat-report-v1` + 真实compat HTTP/SSE transport + 卸载/重载端口 + `request_limit` 预检与退出码 | CT08、CT09 | software_verified（`3b00e226…`，前两片 `ac98b22`/`fee17ef`；本地全量 1240 passed；端到端 22 用例全 passed、实际 35 请求=冻结下限；详见 validation CT10a） |
| CT10b | 目标候选切换（按生命周期停旧实例、证明静默、按冻结配置启动）与 `probe --phase candidate`（关闭 needs_candidate） | CT10a | blocked（根因已复现）：切换与两次 probe 均执行（exit 3）；当前基线上受控复现证明**受管切换回切失败**——加载 27B 时调度器主动 kill 7B（`docker events`），随后再请求 7B 即 503 且 7B `state=error`、admission 阻塞，探针服务侧 503 级联由此产生；默认预算注入与单模型路径已证正常；待开发机 TDD 修复切换回切路径、全量回归、重冻、目标同 SHA 重跑 probe（详见 validation） |
| CT10c | 27B `llama-cpp-chat-features-v1` 策略登记（D06实测值）→ 重冻 `freeze.json` → 受影响场景复跑 | CT10b | in_progress：策略已登记并重冻（`3aee26ec`/site `d8b26be2…`/记录 `65a5a644…`）；D06 的 effort 语义仍需候选复跑测量（登记为 fail-closed 空集），复跑未通过前不得视为完成 |
| CT10d | candidate suite 运行、A01—A11 硬件项取证、validation 材料索引与状态回填 | CT10c | pending |


### CT11 客户端与回滚演练

- CT10全部必需项通过后，按既有网关控制开放lab测试窗口；验证SDK及每个宣称支持的客户端实际版本。
- 先运行gateway suite；完成开发机回滚提交及目标同步后，再运行rollback suite。各suite及身份关联见acceptance §7。
  验收driver不执行git变更、push、镜像下载或配置复制，部署切换严格由开发机执行AGENTS流程。
- 按A11记录发送字段、SSE重组、历史reasoning及读超时；不要求修客户端产品本身，失败则不宣称该客户端支持。
- 按TC09恢复完整基线组合，证明新能力拒绝、旧chat/vision正常、最终lease为空和实例STOPPED。
- 重新启用已验候选若有需求须再次按同SHA部署检查；不把回滚后基线状态记为候选在线。
- 文档：交付客户端支持表、回滚材料，A12通过；说明最终实际部署版本与能力状态。

### CT12 最终文档与交付

- 复核CT00—CT11证据、命令退出码、失败保留；任何blocked则不宣称整项完成。
- 更新03/04/06/08及本页状态，validation记录证据；Idea仅保留入口，不另维护任务状态。
- 验证：文档链接、git diff --check、秘密扫描、提交范围；纯文档补记不重跑未变代码测试。
- 结论只允许“软件通过 / lab指定能力通过 / blocked”；production-ready和device_backend_ready仍由06完整门禁决定。

## 5. 命令、路径与交付约束

**下列chat_probe/chat_compat命令为计划新增，必须先完成对应任务，禁止现在当成现有命令执行。**
CLI使用argparse；未知参数退出2；输出目录必须不存在或为空，已有材料禁止覆盖；常规日志不输出原prompt或凭据。

```bash
# 在仓库根运行；SMS_PY必须指向已安装依赖的Python3.12，不使用系统别名猜测。
SMS_PY=/absolute/path/to/python3.12
"$SMS_PY" -c 'import sys; assert sys.version_info[:2] == (3, 12)'
"$SMS_PY" -m pytest tests -m 'not thor' -q
"$SMS_PY" -m ruff check .
"$SMS_PY" run.py --check-config
git diff --check

# CT01交付后：SITE_JSON为外部证据目录中已核实输入，OUT为每次新目录。
"$SMS_PY" -m model_scheduler.acceptance.chat_probe inspect --site "$SITE_JSON" --output "$OUT"
"$SMS_PY" -m model_scheduler.acceptance.chat_probe probe --site "$SITE_JSON" --phase baseline --output "$OUT"
# CT10的独立新OUT；candidate相位检验必需探测全部通过，不接受needs_candidate。
"$SMS_PY" -m model_scheduler.acceptance.chat_probe probe --site "$SITE_JSON" --phase candidate --output "$OUT"

# CT10：candidate suite；全部必需case从注册和固定fixture派生，不提供跳过case选项。
"$SMS_PY" -m model_scheduler.acceptance.chat_compat run --suite candidate --site "$SITE_JSON" --output "$CANDIDATE_OUT"
# CT11：仍为候选部署，测试真实网关及已声明客户端。
"$SMS_PY" -m model_scheduler.acceptance.chat_compat run --suite gateway --candidate "$CANDIDATE_OUT" --site "$GATEWAY_SITE_JSON" --output "$GATEWAY_OUT"
# 先在开发机完成审查过的回滚提交/push、目标ff-only，再填入回滚SHA并执行：
"$SMS_PY" -m model_scheduler.acceptance.chat_compat run --suite rollback --candidate "$CANDIDATE_OUT" --site "$ROLLBACK_SITE_JSON" --output "$ROLLBACK_OUT"
"$SMS_PY" -m model_scheduler.acceptance.chat_compat verify --candidate "$CANDIDATE_OUT" --gateway "$GATEWAY_OUT" --rollback "$ROLLBACK_OUT"
```

每次inspect/probe分配新OUT；三次run分别使用新CANDIDATE_OUT/GATEWAY_OUT/ROLLBACK_OUT，verify只读复用三目录。
site路径与输出目录变量均由CT00/CT09核实后赋值，不能原样使用占位路径。变量名不能复用HOME/CODEX_HOME。
CLI退出码：0=命令定义的检查通过，2=输入/结构/缺文件错误，3=完整材料显示语义失败。
baseline probe的0仅表示采集完整，report中needs_candidate不代表能力通过；candidate probe存在该状态退出3。
run失败仍写失败材料；verify离线、不启动模型，不因summary写passed返回0。

源码/配置任务使用根AGENTS指定的开发分支→本地原子提交→push→目标ff-only→同SHA测试流程。
本扩展按完整行为及其消费者划分原子提交，不沿用08历史P任务的约5文件划分；尤其能力闭集与控制协议consumer
必须同时交付，不能为文件数拆成无法导入的状态。确需细分时先在本页增加同ID后缀及依赖，保留本表父项验收全集。
规划文档自身只需本地审查与提交，不要求push或目标同步；本次生成计划不执行CT任务。
