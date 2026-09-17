# AGX Thor 调度器完整修复计划

状态：设计交付；尚未执行生产代码修复。日期：2026-09-15。
审查基线：`71f857f174f68af8f468b05d2a8c95065d7d1ae1`。

## 阅读顺序与约束

1. [01 目标、架构与问题清单](01-design.md)
2. [02 状态机、内存、队列与生命周期算法](02-scheduler.md)
3. [03 HTTP 与内部接口契约](03-api.md)
4. [04 配置、Thor 部署与外接 SSD](04-deployment.md)
5. [05 实施任务、验收与发布](05-tasks-and-acceptance.md)
6. [Python 类型契约](reference/contracts.py)、[核心状态与预算实现](reference/core.py)、[参考实现测试](reference/test_core.py)

本文中的“必须”是实现验收条件。“默认”也是确定值，除非通过配置显式覆盖。
`reference/` 是计划的一部分，不被现有应用导入；其中核心算法可运行，协议方法由后续实现提供。
HTTP 契约以 03 为准；状态原子操作以 reference/core.py 为准；异步任务编排以 02 为准。
发现三者冲突时必须修订计划及测试，不允许实现者任选一种。

## 已确定的设计

- 用户设备：AGX Thor，Type-C 外接 SSD 存模型；操作系统采用该设备受支持的 Jetson Linux。
- 部署：宿主机 Python 虚拟环境 + systemd 调度器；宿主机 llama-swap；Docker llama.cpp 模型服务。
- 控制路径：scheduler → llama-swap → Docker；推理路径：scheduler → 模型容器固定 loopback 端口。
- 单进程、单 worker、单调度状态所有者；不支持多个 uvicorn worker 或多副本。
- 统一内存以 Linux MemAvailable 为实时准入依据，另设模型预算上限；不把 GPU 指标与系统内存相加。
- 四类模型 ID 保留；chat、embeddings、rerank 必须各自有可测 API。
- 模型不进入镜像；SSD 固定 UUID 挂载，只读 bind mount；磁盘不满足条件时拒绝推理。
- 首版仅本机调用；非 loopback 绑定直接配置报错。远程客户端可另行通过 SSH 隧道访问。

## 环境输入不是未决设计

真实模型、SSD UUID、JetPack 版本和镜像摘要尚未取得。04 定义了这些输入的字段、校验、采集和失败行为。
任何缺值、样例占位值或未通过 GPU 实测的组合均不得生成“生产就绪”部署产物。
不虚构镜像 digest、磁盘 UUID、模型性能或已经通过的 Thor 验收结果。

## 本计划自检

仅验证计划附带的核心代码：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s plan/reference -p 'test_*.py' -v
```

完整应用的未来验收命令见 05；它们尚不是本次已通过的验证。

本次验证记录：16项参考核心测试通过；全部Python文件和文档内Python片段语法通过；
Markdown本地链接、代码围栏及空白检查通过；Pydantic请求示例实测拒绝top_n的显式null、bool和字符串。
参考测试运行于现有Anaconda Python3.13.5；生产Python3.12、Linux、Docker和Thor验证仍按05执行。

## Grill Review

本次跳过交互式 grill 访谈，依据用户“输出完整计划”的要求直接固定设计选择。
计划作者承担的设计风险：单生命周期操作可能增加冷启动排队时间；保守双重内存门槛可能降低利用率；
实际模型/硬件兼容性只能在 Thor 验收阶段消除。对此分别用有界 deadline、状态可观测性和实机发布门禁控制。
这不表示用户已接受某个未测试的模型/镜像，也不表示生产发布已获授权。
模块边界和 API 边界经过独立架构/API 审查；审查结论已纳入 01—05。
