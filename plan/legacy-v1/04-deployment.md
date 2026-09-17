# 04 配置、Thor 部署与外接 SSD

## 1. 固定部署拓扑

| 组件 | 路径/地址 | 运行方式 |
|---|---|---|
| scheduler | 127.0.0.1:8090 | /opt/self-model-switch/current/.venv/bin/python，单worker |
| llama-swap | 127.0.0.1:8080 | 固定版本宿主机ARM64二进制 |
| embedding | 127.0.0.1:10001 | Docker llama.cpp |
| reranker | 127.0.0.1:10002 | Docker llama.cpp |
| qwen-small | 127.0.0.1:10003 | Docker llama.cpp |
| qwen-large | 127.0.0.1:10004 | Docker llama.cpp |
| SSD | /mnt/model-ssd | 按真实UUID挂载；模型目录为models/ |
| 配置 | /etc/self-model-switch/ | root所有，服务只读 |
| 模型 | /mnt/model-ssd/models/*.gguf | 容器内/models，只读 |
| 发布目录 | /opt/self-model-switch/releases/<release-id>/ | 每个版本独立venv、依赖锁、源码 |
| current | /opt/self-model-switch/current | 指向已验证发布的原子符号链接 |
| 运行状态 | /run/model-scheduler/ | 锁和瞬态状态，重启清理 |
| 日志 | journald | journalctl -u model-scheduler -u llama-swap |

不提供首版scheduler Dockerfile；模型容器已经提供推理环境隔离。
不在Mac打包Linux二进制。发布物为源码归档、严格Python依赖锁、systemd模板和确定的模型manifest。
Python3.12支持区间 `>=3.12,<3.13`；后续扩大版本范围必须加CI矩阵。

## 2. 必填部署输入及校验

部署工具接受一个JSON文件；所有参数只能从此文件派生，不分别手改scheduler、llama-swap、Docker命令。

```json
{
  "deployment_id": "thor-local",
  "ssd_uuid": "REQUIRED_REAL_UUID",
  "ssd_filesystem": "ext4",
  "jetpack_version": "REQUIRED_FROM_DEVICE",
  "llama_swap_version": "REQUIRED_FIXED_RELEASE",
  "llama_swap_sha256": "REQUIRED_64_HEX",
  "image": "ghcr.io/ggml-org/llama.cpp@sha256:REQUIRED_64_HEX",
  "validation_report": null,
  "models": {
    "embedding": {"file":"embedding.gguf","sha256":"REQUIRED_64_HEX","context_size":2048,"parallel":1,"pooling":"mean","reserved_bytes":4294967296,"measured":false},
    "reranker": {"file":"reranker.gguf","sha256":"REQUIRED_64_HEX","context_size":2048,"parallel":1,"pooling":"rank","reserved_bytes":6442450944,"measured":false},
    "qwen-small": {"file":"qwen-small.gguf","sha256":"REQUIRED_64_HEX","context_size":16384,"parallel":1,"pooling":null,"reserved_bytes":10737418240,"measured":false},
    "qwen-large": {"file":"qwen-large.gguf","sha256":"REQUIRED_64_HEX","context_size":32768,"parallel":1,"pooling":null,"reserved_bytes":32212254720,"measured":false}
  }
}
```

这里的内存数沿用原项目估算，仅允许生成lab配置；production模式要求measured=true及05中关联测量报告。
production必须附 `validation_report` 路径，报告的model hash、context、parallel、image digest必须与输入完全一致。
lab模式允许没有validation_report；production要求该字段为可读JSON路径。
每模型parallel为1..16严格整数，生成scheduler.max_concurrency与llama.cpp --parallel使用同一个值。
context_size为正整数且须能被parallel整除；这里定义为后端总context预算，每slot上限=context_size/parallel，报告按每slot上限压测。
首版固定batch_size=512、ubatch_size=128、cache_type_k=f16、cache_type_v=f16、gpu_layers=99、fit=off；
这些值写入manifest和runner参数，不依赖镜像的可变默认值。改变它们属于后续配置schema变更和重新测量。
缺字段、未知字段、REQUIRED占位、浮动tag、非法digest、不可读文件、路径逃逸、未知文件系统均失败退出78。
file只能单个文件名，拒绝 `/`、`..`、符号链接；首版每模型只支持一个GGUF，不支持分片和mmproj。
文件系统首版限定ext4；现有SSD若为其他格式，部署工具明确报unsupported_filesystem并退出，**不会自动格式化**。
格式化/迁移属于另外的数据操作，不能隐含在安装流程中。
embedding模型必须支持所选mean池化；reranker必须是rank模型；普通chat模型不能因改ID就用作reranker。

模型max_concurrency、上下文、pooling或镜像改变时视为预算测量失效，production渲染必须重新验证。
CPU离线模拟可用于CI，但不能将其标记为Thor GPU验收通过。

## 3. Scheduler YAML v1

生成文件包含以下精确字段；模型只列qwen-small示例，其余按下面表格生成相同字段。

```yaml
schema_version: 1
server:
  host: 127.0.0.1
  port: 8090
  workers: 1
  max_request_body_bytes: 4194304
  body_timeout_seconds: 10
  shutdown_grace_seconds: 30
llama_swap:
  base_url: http://127.0.0.1:8080
  connect_timeout_seconds: 5
  control_timeout_seconds: 30
  load_timeout_seconds: 900
  unload_timeout_seconds: 45
scheduler:
  poll_interval_seconds: 2
  request_queue_timeout_seconds: 1800
  queue_capacity: 128
  priority_aging_seconds: 30
  switch_drain_timeout_seconds: 30
  switch_retry_seconds: 30
  resource_safety_margin: 0.15
  min_free_memory_bytes: 2147483648
  max_evictions_per_request: 8
  memory_reclaim_timeout_seconds: 10
  heat:
    half_life_seconds: 1800
    request_weight: 1.0
    token_weight: 0.0001
  thrash:
    switch_window_seconds: 10
    max_switches_in_window: 3
    cooldown_seconds: 15
resources:
  provider: psutil
  system_reserve_bytes: 8589934592
  sample_interval_seconds: 1
  sample_max_age_seconds: 2
storage:
  mount_path: /mnt/model-ssd
  model_directory: /mnt/model-ssd/models
  expected_uuid: REQUIRED_REAL_UUID
  filesystem: ext4
gateway:
  connect_timeout_seconds: 5
  pool_timeout_seconds: 5
  read_idle_timeout_seconds: 60
  write_idle_timeout_seconds: 60
  inference_timeout_seconds: 900
  close_timeout_seconds: 5
  max_response_body_bytes: 16777216
  max_sse_event_bytes: 1048576
models:
  qwen-small:
    upstream_url: http://127.0.0.1:10003
    capabilities: [chat]
    file: qwen-small.gguf
    sha256: REQUIRED_64_HEX
    container_name: sms-thor-local-qwen-small
    memory:
      reserved_bytes: 10737418240
    scheduling:
      priority: 50
      max_concurrency: 1
      evictable: true
      pinned: false
    lifecycle:
      preload: false
      ttl_seconds: 900
```

| model_id | 端口 | capability | priority | pinned/evictable/preload | TTL |
|---|---:|---|---:|---|---:|
| embedding | 10001 | embeddings | 100 | true/false/true | 0 |
| reranker | 10002 | rerank | 80 | false/true/false | 900 |
| qwen-small | 10003 | chat | 50 | false/true/false | 900 |
| qwen-large | 10004 | chat | 40 | false/true/false | 1800 |

严格配置规则：bool只接受YAML true/false；整数拒绝bool；bytes为正整数（floor可0）；不再接受单位字符串。
所有秒数必须有限且>0（TTL允许0）；half_life>0；margin范围[0,1]；priority范围[0,1000]；parallel范围[1,16]。
workers必须1；host仅127.0.0.1或::1；所有上游必须http、loopback字面IP、固定非重复端口、无userinfo/query/fragment/path。
超时必须满足control_timeout<=unload_timeout、sample_interval<=sample_max_age；模型预算不得超过B。
未知配置字段一律报错；YAML重复key报错；enabled语义首版为“存在于models即启用”，没有第二个enabled开关。

CLI优先级：`--config` > `MODEL_SCHEDULER_CONFIG` > 项目根目录config.yaml。
路径相对基准为config文件所在目录，不是当前工作目录；storage及upstream生产配置只接受绝对路径/URL。
`python run.py --config /etc/self-model-switch/config.yaml --check-config`只校验并输出摘要，无网络/模型启动；失败退出78。

旧配置不悄悄兼容：提供 `python -m model_scheduler.deploy migrate --input config.yaml --output /tmp/config.v1.yaml`。
迁移输出样例UUID/hash占位及报告；将resources.auto转psutil，删除active_bonus，补并发/preload字段；生产检查仍拒绝缺值。
所有预算估算默认measured=false；不能由迁移直接获得生产就绪标记。

## 4. llama-swap 配置生成规则

固定globalTTL=0、各模型ttl=0、healthCheckTimeout=900、unloadTimeout=45。
组内swap=false、exclusive=false；没有llama-swap preload。固定proxy端口，不依赖${PORT}。
llama-swap仅接受127.0.0.1:8080；命令行参数通过固定发行版的`--help`测试，不猜测不同版本flag。
生成配置采用该固定版本实际支持的routing schema，保存启动验证fixture。

单模型渲染结果示意（@字段由工具替换，运行前必须没有占位符）：

```yaml
models:
  qwen-small:
    proxy: http://127.0.0.1:10003
    cmd: >-
      /usr/local/libexec/sms-model-runner start qwen-small
    cmdStop: /usr/local/libexec/sms-model-runner stop qwen-small
    checkEndpoint: /health
    ttl: 0
routing:
  router:
    use: group
    settings:
      groups:
        all-managed:
          swap: false
          exclusive: false
          members: [embedding, reranker, qwen-small, qwen-large]
```

`sms-model-runner`从root所有manifest取argv，不拼shell；start调用Docker的核心argv：

```text
docker run --name sms-thor-local-qwen-small --init --rm --restart=no
  --gpus all
  --label io.self-model-switch.deployment=thor-local
  --label io.self-model-switch.model=qwen-small
  --label io.self-model-switch.config-sha256=<generated-config-hash>
  --publish 127.0.0.1:10003:8080
  --mount type=bind,src=/mnt/model-ssd/models,dst=/models,readonly
  ghcr.io/ggml-org/llama.cpp@sha256:<verified-digest>
  --model /models/qwen-small.gguf --alias qwen-small
  --host 0.0.0.0 --port 8080 --ctx-size 16384
  --parallel 1 --n-gpu-layers 99 --fit off --jinja
  --batch-size 512 --ubatch-size 128 --cache-type-k f16 --cache-type-v f16
```

start前再次检查UUID挂载和模型可读；已有同名容器则报错，不删除、不接管未知容器。
stop仅在标签和manifest匹配时执行 `docker stop --time 30 <exact-name>`；不存在幂等成功；Docker不可达失败。
stop成功退出不是最终停止证据，scheduler还要inspect/端口/控制操作终结确认。
runner必须等待docker run子进程并转发SIGTERM/SIGINT；退出时清理其自己创建的实例，不能遗留未等待子进程。

embedding参数：`--embedding --pooling mean --ctx-size <manifest>`；reranker：`--embedding --pooling rank`。
二者也固定alias、parallel、GPU层数、fit off、batch和cache参数；不加jinja。实际模型不支持这些设置则部署输入不合格，不能自动换语义。
所有参数必须被固定镜像`llama-server --help`和一次真实请求验证。

## 5. 镜像与依赖锁

Thor先检查JetPack、CUDA和ARM64。当前上游提供CUDA13 ARM64镜像，但不能仅凭tag宣称兼容。
候选为server-cuda13；拉取并完成GPU验收后锁定digest，真实报告记录镜像Id、架构、driver、llama.cpp构建号。
`requirements.in`保留直接依赖范围；生成Python3.12/Linux ARM64适用的`requirements.lock`，所有传递依赖固定并含hash。
开发测试依赖另存requirements-dev.lock，含pytest、pytest-asyncio、ruff；不将测试工具装入生产venv。
在Thor执行 `python3.12 -m pip install --require-hashes -r requirements.lock`；依赖不可解析或hash不匹配即停止发布。

## 6. SSD 和 systemd

预检使用 `findmnt --json --target /mnt/model-ssd` 与 `lsblk --json --output NAME,UUID,FSTYPE,MOUNTPOINTS`。
必须证明目标本身是mountpoint、UUID与expected_uuid完全一致、fstype=ext4，而不是父级根文件系统。
检查文件regular/readable、不为symlink，首个启动计算SHA256；后台只检查UUID和已记录inode/size/mtime及读取探测。
后台元数据变化立即拒绝准入；重新完整hash后才能恢复。运行中外部修改模型文件不受支持。
不允许开机时空目录被Docker -v自动创建后当作模型盘；使用--mount，但仍须独立检查UUID。

fstab生成模板：

```text
UUID=<actual-uuid> /mnt/model-ssd ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2
```

工具只生成建议fstab片段；不覆盖现有/etc/fstab。首次安装由运维审核合并，无格式化命令。
渲染mount unit名使用 `systemd-escape --path --suffix=mount /mnt/model-ssd`，写入下列@SSD_MOUNT_UNIT@。

`model-scheduler.service`：

```ini
[Unit]
Description=AGX Thor model scheduler
Wants=network-online.target llama-swap.service
After=network-online.target docker.service llama-swap.service @SSD_MOUNT_UNIT@
RequiresMountsFor=/mnt/model-ssd
BindsTo=@SSD_MOUNT_UNIT@
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=model-scheduler
Group=model-scheduler
SupplementaryGroups=docker
WorkingDirectory=/opt/self-model-switch/current
Environment=MODEL_SCHEDULER_CONFIG=/etc/self-model-switch/config.yaml
RuntimeDirectory=model-scheduler
ExecStart=/opt/self-model-switch/current/.venv/bin/python run.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

不能用Requires/BindsTo绑定llama-swap.service：控制面恢复需要停启llama-swap，而scheduler必须继续运行并展示恢复状态。

`llama-swap.service`：

```ini
[Unit]
Description=Model lifecycle controller
Requires=docker.service
After=docker.service @SSD_MOUNT_UNIT@
RequiresMountsFor=/mnt/model-ssd
BindsTo=@SSD_MOUNT_UNIT@

[Service]
Type=simple
User=model-scheduler
Group=model-scheduler
SupplementaryGroups=docker
ExecStart=/opt/self-model-switch/current/bin/llama-swap --config /etc/self-model-switch/llama-swap.yaml --listen 127.0.0.1:8080
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
```

以上llama-swap命令在固定版本验证后固化；若help不支持--listen，则该候选版本不通过模板兼容测试。
model-scheduler用户的Docker访问是部署所需权限；本机为单用户可信管理边界，不把这个权限当作容器隔离保证。

恢复helper：`/usr/local/libexec/sms-control-recover`，root所有不可由服务用户修改。
唯一调用为 `sudo -n /usr/local/libexec/sms-control-recover`，不接受参数；sudoers只允许这一条精确命令。
helper读取root所有manifest，执行02第7节的stop/cgroup检查/指定标签容器清理/start。
helper持独占锁，重复调用返回already_running，不并发恢复；返回JSON `{ok,phase,error_code,stopped_models}`，退出0成功/1失败。
stopped_models只有成功时为本部署全部model_id排序数组，失败时为空；scheduler据此调用finish_recovery，不能根据ok单独清账。
helper不访问网络下载，不执行任意参数，不提供shell；其stdout不包含密钥或模型文本。

拔盘会触发mount绑定服务停止；后台UUID探测作为补充。重新插盘不保证systemd自动拉起已停止服务。
确定恢复命令：`sudo mount /mnt/model-ssd` 后 `sudo systemctl start llama-swap.service model-scheduler.service`。
盘不在时系统仍可开机，推理服务不能就绪。

## 7. 部署工具命令与输出

后续实现固定CLI（目前尚未存在）：

```bash
python -m model_scheduler.deploy collect --output /tmp/thor-facts.json
python -m model_scheduler.deploy render --input deploy-input.json --mode lab --output build/deploy
python -m model_scheduler.deploy preflight --manifest build/deploy/manifest.json
python -m model_scheduler.deploy render --input deploy-input.json --mode production --output build/release
```

collect：只读采集uname、JetPack发行信息、python、Docker、GPU运行时、lsblk、llama-swap版本；不启动模型、不改系统。
render：只写指定输出目录；拒绝非空目录，避免混合旧新配置；输出config.yaml、llama-swap.yaml、manifest.json、units、fstab.fragment、安装说明。
preflight：检查文件、版本、digest、端口、设备、只读bind、配置一致性；允许临时启动指定镜像进行GPU和每模型一次请求验证，必须标注该命令会加载模型。
preflight不启动第二个scheduler；运行服务持有实例锁时退出73；临时容器使用独立随机名并finally清理。
校验退出码统一：0成功，1运行检查失败，64参数错误，73锁/输出目录冲突，78配置/部署输入不合法。

安装步骤：创建固定服务用户→安装root所有配置/helper→创建release专属venv→校验systemd单位→审核挂载片段→切换current→daemon-reload→启动服务→检查health及三种真实API。
安装文档给出逐条命令；不提供执行任意下载脚本的一键curl|sh。

## 8. 官方依据与验证约束

- [Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)：只读bind与源目录行为。
- [llama.cpp Docker镜像](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md)：架构/CUDA候选；上游构建成功不等于本机GPU已验证。
- [llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)：chat、embedding、rerank和启动参数。rerank需固定版本fixture。
- [llama-swap配置](https://github.com/mostlygeek/llama-swap/blob/main/docs/config.example.yaml)：生命周期和组配置。
- [JetPack 7.0](https://developer.nvidia.com/embedded/jetpack/downloads/archive-7.0)：Thor软件栈/CUDA背景；以collect取得的机器版本为准。

上述链接用于确定接口方向；发布必须记录实际固定commit/digest，不把main/master页面作为版本锁。
