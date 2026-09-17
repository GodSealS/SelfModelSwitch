# 04 配置、运行时、资产与迁移

## 1. 版本与唯一输入

应用配置schema从1升级2；部署候选和验收报告从现有schema2升级3；worker protocol=1，artifact schema=1。
三者不是同一个版本号。部署候选精确schema为 `reference.contracts.Candidate`，导出的JSON Schema用于CLI/OpenAPI校验。
不再额外维护一份宽松字典schema。candidate中每个配置/源码hash都必须有本地文件供render和preflight重算。
缺字段、未知字段、重复ID/端口、无模型、非法capability、模型资产为空，全部失败，不自动填假值。

`candidate_sha256=SHA256(UTF8(json.dumps(candidate,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)))`。
candidate来自Pydantic model_dump(mode=json)，默认值已显式展开。数组保留定义顺序；生成器将runtimes/models按ID、
assets按path、capabilities按字典序排序；device compatible和stages顺序不可排序。校验输入不规范顺序时报错或先规范化生成新candidate，
不得在签发报告后隐式改顺序。报告路径、报告digest、生成时间不进入candidate，避免“先有报告才能生成测试配置”的循环。

配置摘要的精确算法是reference/acceptance.py的configuration_digest：解析并严格验证配置，展开默认值，
仅移除顶层派生candidate_sha256字段，再按上述规范JSON计算摘要。不得排除任何其它配置值。
先生成配置payload→算配置摘要→构造candidate→算candidate摘要→回填配置candidate_sha256；preflight分别验证payload摘要与回填绑定。
release_archive_sha256指纯应用源码/锁/模板/参数的发布归档，不含部署生成配置、candidate、报告或evidence；
最终部署bundle另有文件hash清单，不回填candidate。由此避免配置和归档自引用。
acceptance_policy_sha256绑定AcceptancePolicy严格JSON；模型SLO键集合必须恰等于candidate.models，fixture逐个验证hash。
llama_swap_sha256在存在llama_cpp runtime时必填，否则必须null；实际二进制也纳入现场preflight。

## 2. 模型和运行时

每个runtime有固定image digest、adapter名及实现hash、依赖lock hash。只允许：

| kind | adapter | 入口 |
|---|---|---|
| llama_cpp | llama | 固定llama-server二进制，由现有llama-swap控制 |
| python_worker | moss_transformers/yunet/scrfd/insightface/depth_v2 | `python -m workers.server --runtime-id ...` |
| media_worker | media | 同一worker入口，白名单媒体操作 |

不接受任意entrypoint/extra_args字符串。adapter内部根据类型字段构造argv；subprocess统一argv、shell=False。
模型GGUF、mmproj、HF tokenizer/config/shards、ONNX、engine全部以Asset逐文件列出path/role/size/hash。
目录不是单个“可信资产”；必须展开为regular files清单。禁绝symlink、`..`、绝对asset路径和未登记动态下载。
TRT engine的构建runtime、设备和构建参数必须在资产support清单，设备/栈变更后重新生成并测量。
模型目录容器只读；input只读；attempt输出目录可写；禁止映射完整宿主根目录和Docker socket给worker。

每模型端口10001..19999显式配置且唯一，media_port也不能重用。容器label由deployment/model/runtime/candidate计算。
容器restart=no；上游自动TTL/exclusive swap/preload关闭。只有scheduler决定启动和停止。
llama vision必须有projector角色，主权重和mmproj分别hash；context/output/image envelope固定，不直接沿用文本模型预算。

## 3. 实际设备身份

不根据用户文字“Thor/Orin”填表。collector采集DeviceIdentity的每个字段：实际架构、device-tree model/compatible、
MemTotal、kernel、OS、JetPack、驱动、container runtime、power/clocks模式、两类SSD UUID。
machine_id_sha256由该机`/etc/machine-id`原始去尾换行字节SHA256得到，不把原值写公开报告；
相同型号另一设备也必须重测。克隆machine-id不能建立强硬件认证，可信采集环境负责避免该问题。
任何字段为空或无法采集，设备层阻断；测试fake允许仅用于S层，不能伪造DeviceIdentity通过B层。
GPU加速必须来自实际执行后端证据与GPU活动/设备分配日志；不能凭.cuda()源码、镜像名称或torch可导入判定。
CPU-only人脸runtime允许显式CPU运行，但同样提供执行设备事实，不能标注GPU verified。

## 4. 配置改动的精确规则

scheduler schema2沿用现有server/llama_swap/scheduler/resources/storage/gateway严格字段，增删如下：

- models从固定四键改动态映射，值改ModelSpec；upstream_url/container_name由端口/ID生成，不能另填互相矛盾的值。
- 新增runtimes（RuntimeSpec列表）、deployment_id、candidate_sha256、scope、media_runtime_id/media_port（可null）。
- 新增control_socket（固定绝对路径）、runner_uid（非负整数）、model_budget_bytes（正整数）。
- memory.reserved_bytes由measured_peak*1.15计算，不再接受手填measured=true；旧scheduling/lifecycle子字典映射到ModelSpec字段。
- 默认chat body上限16MiB；音频64MiB独立硬上限。gateway原有参数只控制交互HTTP；阶段timeout由StagePolicy控制。
- balanced禁止pinned/preload且所有模型concurrency=1；scheduler-only保持原有组合校验。
- 原resources provider只能psutil；min_free_memory_bytes与Candidate.free_floor_bytes必须相等；
  resource_safety_margin固定0.15，不允许candidate外再乘一次；system_reserve必须与candidate一致。

runner配置严格字段：schema_version=1、candidate_sha256、host=127.0.0.1、port=8091、workers=1、
database_path、input_root、work_root、scheduler_socket、pipeline（PipelinePolicy）、external_secret_env（可null）。
路径都必须绝对；input_root只读、work_root必须真实挂载，database_path在系统盘；secret是环境变量名而非secret值。
full-pipeline必须配置该环境变量且非空，内容不入candidate/report；endpoint/provider固定在adapter配置hash中。
生产模型/阶段参数文件均纳入scheduler/runner config digest和纯代码归档内参数文件，不能通过未哈希环境变量改模型行为。

## 5. 存储与产物

模型盘默认`/mnt/model-ssd/models`，工作盘默认`/mnt/work-ssd/jobs`；允许同UUID，但必须独立目录和访问权限。
input默认`/mnt/work-ssd/inputs`；DB默认系统盘`/var/lib/self-model-switch/jobs.sqlite3`。
启动、恢复、发布前全量hash；运行中每1s检查挂载、inode/size/mtime，变更立即关准入。
读取hash必须限定超时、使用有界线程/子进程，不能把卡死存储当健康。不能仅靠metadata证明内容永久未变；只读权限是前提。
每job work上限来自policy；写之前预留最坏增量，剩余空间至少2GiB，低于门槛暂停，不删除用户文件自动腾空间。
临时产物只由该job清理，数据库记录和已提交产物默认保留；删除需要显式job purge命令，首版不实现自动保留期删除。

## 6. 迁移、候选、生产与回滚

以下CLI为待实现契约：

```bash
python -m model_scheduler.deploy migrate --input config.yaml --output build/config-v2.yaml
python -m acceptance collect --output build/facts.json
python -m model_scheduler.deploy candidate --input deployment-input.json --output build/candidate
python -m acceptance run --candidate build/candidate/candidate.json --scope full-pipeline --output build/evidence --maintenance
python -m acceptance verify --candidate build/candidate/candidate.json --evidence build/evidence
python -m model_scheduler.deploy render --candidate build/candidate/candidate.json --evidence build/evidence --mode production --output build/production
```

migrate只处理已识别schema1：保留四ID和行为、生成缺失asset/runtime/测量清单，exit2表示仍需环境输入；
不猜镜像/资产/预算、不覆盖源文件。迁移到balanced时明确要求取消pinned/preload，不能暗中修改服务语义。
candidate模式可以生成未验收配置用于独占测试；明确标记candidate，不能被生产systemd启动。
测量预算先形成新candidate；如果实测超过预算，更新candidate并重跑，不把旧hash证据拼进新候选。
正式render只打包已验证的同一candidate和原始证据；只生成文件，不安装、不部署。
preflight现场重算配置、源码归档、镜像、模型资产、设备栈和证据hash；任一错配退出非零并禁止启动。
production模式必须使用完整06门禁，不能用手写报告或`--force`绕过。

部署顺序：停止runner准入并暂停job → 排空/清理scheduler → 确认所有受管实例停止 → 安装独立release/venv →
备份SQLite（停写后复制）→执行一次有版本记录的向前DB迁移→切current→preflight→启动scheduler→runner→冒烟。
回滚停服务、恢复同版配置和已备份DB、还原current；不删除新产物，孤儿文件隔离。没有已验收旧版则停机修复，不自动回退未知基线。

## 7. 上游核验依据

2026-09-16核验：[MOSS官方说明](https://github.com/OpenMOSS/MOSS-Transcribe-Diarize/blob/main/README_zh.md)、
[llama.cpp多模态](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)、
[Jetson FFmpeg](https://docs.nvidia.com/jetson/archives/r36.4.4/DeveloperGuide/SD/Multimedia/AcceleratedDecodeWithFfmpg.html)。
main/master链接用于设计依据，不是版本锁。MOSS官方推荐后端不代表已在本设备验证；CUDA/JetPack/ARM64组合需实测。
不直接复制附件`-hwaccel cuda`或量化文件名；媒体adapter通过设备probe选择manifest锁定的加速路径，并在P/O层证明实际生效。
