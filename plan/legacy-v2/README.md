# 多后端模型调度与离线视频分析：修改和验收方案 v2

日期：2026-09-16。源码基线：`c32dd1a8ecdfa9b7f8f9a964886b794a943e16ca`。
状态：**设计交付，尚未实施生产代码、尚未进行设备或模型验收**。
本次只修改 `plan/`；旧计划完整保存在 [legacy-v1](../legacy-v1/README.md)，仅供历史追溯，不参与新规范解释。

## 文档与规范优先级

| 文件 | 唯一负责的内容 |
|---|---|
| [01-design.md](01-design.md) | 目标、范围、现状差距、模块归属 |
| [02-scheduler.md](02-scheduler.md) | 模型状态、阶段许可、请求租约、预算、恢复 |
| [03-api.md](03-api.md) | HTTP、内部端口、错误码、幂等和任务语义 |
| [04-deployment.md](04-deployment.md) | 配置、资产、运行时、设备绑定、迁移部署 |
| [05-tasks-and-acceptance.md](05-tasks-and-acceptance.md) | 实施任务、依赖、逐项交付和验证命令 |
| [06-acceptance.md](06-acceptance.md) | 验收分层、执行器、证据、判定与失效规则 |
| [07-pipeline.md](07-pipeline.md) | 阶段产物、检查点、时间轴和质量指标 |
| [reference/contracts.py](reference/contracts.py) | 严格字段、枚举和静态校验 |
| [reference/core.py](reference/core.py) | 预算、许可清理、状态边界的可执行语义 |
| [reference/acceptance.py](reference/acceptance.py) | 必测场景推导和证据门禁参考实现 |
| [adr/decisions.md](adr/decisions.md) | 重要取舍及其代价 |

字段以 contracts.py 为准；核心函数行为以 core.py/acceptance.py 为准；异步编排、真实 IO、指标计算以对应文档为准。
三者共同生效，冲突必须同时修正文档和测试，不能任选其一。reference 不被应用导入，不是完整生产实现。
参考测试不替代应用、Linux、真实 GPU 或结果质量验收。

## 固定的设计选择

1. SelfModelSwitch 管资源和生命周期；独立 `video_runner` 管业务和持久化。
2. 不写死 Thor。Orin/Thor 都采集实际设备、软件栈、运行模式，并在该设备验收。
3. 首版只实现 `balanced-v1`：一个电影任务、阶段顺序执行、每阶段至多一个模型。
   `light`、`heavy`、多电影并行、多模型并行均为配置错误。
4. 保留旧聊天/向量/重排 API；模型可配置；视频阶段与交互请求通过同一资源门禁互斥。
5. 仅本机：scheduler `127.0.0.1:8090`，runner `127.0.0.1:8091`，阶段控制使用 Unix socket。
6. 完整视频交付包括外部关系报告；禁用外部服务只可获得 `local_pipeline_ready`。
7. 量化文件大小、附件估算、第三方速度、health=200 均不是设备可用证据。

## 输入、假设和边界

固定采用 Linux、systemd、Docker、Python 3.12；scheduler 和 runner 各单进程单 worker。
真实模型 revision、镜像 digest、设备、SSD UUID、测试影片/标注、性能预算是部署输入。
缺值时可完成软件测试，不能生成设备或产品通过结论。质量门槛在 07 固定；改变门槛形成新 policy digest 并重跑。

用户已要求完整方案，因此本轮连续完成设计、任务及验收定义，不等待逐阶段批准。
不修改生产源码、不安装模型、不调用外部服务。Grill Review：跳过交互访谈，采用独立架构审查和参考反例测试。
设计作者承担待验证风险：MOSS ARM64/JetPack 兼容、模型质量、长片时间和存储消耗；由 B/Q/P 门禁阻止未验证发布。
不将这些风险描述为已被用户验收。

## 本轮检查

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s plan/reference -p 'test_*.py' -v
.venv/bin/python -m ruff check plan/reference
# 输出必须不存在；可在审阅/实施时生成规范JSON Schema。
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python plan/reference/export_schema.py --output /tmp/sms-plan-contracts.schema.json
```

实施后的 CLI 在 05/06，属于待实现接口。实际自检记录见 [validation.md](validation.md)。
