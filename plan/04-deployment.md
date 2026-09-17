# 04 模型配置、资产与部署

## 1. 配置边界

拟议 scheduler schema v2；部署候选/报告 v3；新增控制协议 v1，版本号彼此独立。
当前旧生产 schema 保持原状，由 M01 建立新严格契约，M02 实施显式迁移。
新模型表按动态 ID 注册 runtime、capability、asset 清单、端口、envelope、超时与实测峰值。
envelope 含每请求图像上限 `max_images`（vision 至少1，其他模型为0），并登记 `measurement_ref` 与
`physical_resident_peak_bytes`：未测两者为 null，生产开放必须为正整数且绑定测量材料。
配置不含 pipeline、阶段映射、影片输入、runner、人工标注、外部 provider、质量数据集或 full/local-pipeline scope。
唯一产品范围为模型服务，验收集合由实际登记模型和 capability 推导。

runtime 固定 image digest、adapter 实现 hash、依赖 lock hash、受限启动参数；不接受任意 entrypoint/extra_args。
每个 runtime 必填 `profile_id`，取自代码注册的有限 profile 集合，绑定允许参数、资产角色基数与 ready/terminal 协议；
未知 profile 拒绝，runtime_id 只作部署内身份。当前可执行 capability 闭集为 chat/vision/embeddings/rerank，
audio、Torch、ORT 属后续扩展，不得因为接受 runtime_id 就宣称任意 runtime 可用。
逐文件登记 role/path/size/hash，路径是唯一键（同一 role 可分片多文件）；目录展开为 regular files。
首个 GGUF profile 恰一 model、vision 恰一 projector；HF 分片 profile 只允许登记，补齐自己的 fixture 与实测前不得启动。
asset.path 必须相对已登记模型根目录，禁绝 symlink、绝对路径和 `..`；容器只读挂载模型，不挂 Docker socket 或完整宿主根目录。
模型端口10001..19999且唯一；restart=no，关闭上游自动换模，scheduler 唯一控制生命周期。
模型种类和性能目标是部署输入，不能将旧视频模型列表当成默认必选组合。

## 2. 实机验证先行

大规模实施前先在目标设备对计划接入的 runtime/模型做最小推理、最大输入峰值和耗时探测（M00）。
视频项目另外前置 MOSS、视觉模型和声线一致性方案验证（V00）；结果可提供模型登记依据，不反向成为模型服务发布前提。
探测只在独占维护环境和受控测试容器进行，记录停止证据；失败先调整模型/runtime/envelope，不继续堆业务实现。
探测不能替代最终 candidate 的正式验收。没有设备可完成文档和小范围软件准备，不能标记 M00 通过。

## 3. 身份、hash 与候选

现场采集实际架构、设备树、MemTotal、OS/kernel、JetPack/驱动、容器运行时、电源/时钟模式、模型盘和暂存盘 UUID。
设备身份含 `/etc/machine-id` 去尾换行字节的 SHA256；同型号换机重测，可信采集环境防止克隆身份混用。
锁定源码发布归档、配置、模型资产、镜像、adapter、collector/evaluator、性能 policy 和推理 fixture 的 hash。
候选不包含视频代码/参数/标注或业务数据；视频项目单独记录所使用模型服务 candidate hash。

规范 JSON 使用 sort_keys、紧凑分隔、UTF-8、非ASCII保留、allow_nan=false，并显式展开默认值。
配置摘要只排除顶层派生 candidate_sha256；先配置摘要→candidate摘要→回填绑定，避免循环。
纯代码归档不含生成配置/报告/evidence；最终 bundle 文件清单另外记录，不回填 candidate。
参数、模型、设备、runtime、预算或 evaluator 改变均产生新候选；报告、路径和生成时间不进入候选。

## 4. 存储与发布

模型盘默认 `/mnt/model-ssd/models`，通用传输暂存根独立配置；启动/恢复/发布前全量 hash，运行每1s核对挂载和metadata。
读取有界超时，失联/变更关准入；只读权限是前提，根盘同名空目录不能冒充模型盘。
输出暂存有配额与至少2GiB磁盘余量；不能清理客户端持久数据腾空间。

migrate 不覆盖原文件，保留旧四ID和行为，缺 runtime/asset/测量返回缺项报告，不猜值。
拟议流程：collect 现场事实 → 独占 calibration → candidate → acceptance run → verify → render production → preflight。
确切 CLI 参数在 M01/M06 定义并测试；上述名称不是当前可运行命令清单。
未验收 candidate 只用于隔离测试，production render 仅打包同候选的完整有效模型证据。
preflight 重算代码/配置/资产/镜像/设备/栈/报告，错配在加载前阻断；无 force 绕过。

部署仅安装模型服务：停准入→排空并确认旧实例停止→独立release/venv→切current→preflight→启动→冒烟。
回滚恢复已验收旧配置/代码/通用暂存元数据，校验后开放；无已验收旧版则停机修复。
视频项目有独立安装、工作盘、SQLite迁移与回滚流程；不生成 video-runner systemd unit。
