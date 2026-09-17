# 05 模型服务实施任务与验收

## 1. 任务范围

本表只实施 SelfModelSwitch 的模型挂载、切换和必要接口；全部为待办。
视频项目的 V00—V07 在 [独立任务表](video-analysis/05-tasks.md) 中，不能加入本项目完成定义。
旧混合任务 T01—T24 已归档，不再构成当前依赖关系。每项按垂直切片提交，改动过大先拆子任务。
行为修改使用能揭露违约的测试；保留既有状态机、取消、资源、SSD 和三类 API 回归。

## 2. 顺序与交付

| ID | 依赖 | 工作范围 | 验收结果 |
|---|---|---|---|
| M00 | 无 | 目标设备、拟接入模型/runtime的独占最小验证 | 最小推理、最大envelope输入、峰值/耗时、执行设备和停止证据；失败先改选型/规格 |
| M01 | M00 | contracts/config、通用会话与Blob协议、能力矩阵 | 动态ID/资产/runtime严格schema；无job/stage/pipeline字段；确定幂等、权限、限制和版本契约 |
| M02 | M01 | deploy、model_runner、process_observer、storage_monitor | 多资产只读映射、多runtime受限启动、独立实例/停止观察、显式旧配置迁移 |
| M03 | M02 | registry/scheduler/request_queue/resource_monitor | 请求lease与会话分离、统一准入、旧token fencing、两重内存门槛、取消/恢复不提前清账 |
| M04 | M03 | backend adapters、control API、传输暂存 | load/execute/cancel/stop闭环；owner隔离、幂等、过期blob、断线和晚到结果可验证 |
| M05 | M04 | gateway/app 与模型能力fixture | 旧chat/embedding/rerank/SSE兼容；新增能力只承诺已登记协议；无视频业务代码 |
| M06 | M05 | acceptance、deploy gate、运维文档、systemd/CI | S/B/O真实执行及重算，缺测/伪passed拒绝；仅模型服务部署、单worker、恢复/回滚可验证 |
| M07 | M06 | 目标设备最终候选与发布包 | 全部模型/能力、最大输入、切换压力、取消、SSD故障通过，production preflight有效 |

M00 是大规模实施启动条件；无设备时可做文档、接口草案与小型软件实验，不能把 M00 标通过或进入完整实施链。
M00 前置探测不要求先实现 M01—M07，使用受控探测 harness；不启动生产服务，不将探测当最终验收。
M01 后协议可供独立项目编写 fake 客户端测试，跨项目实机联调须模型服务能力可用。

## 3. 实施检查点

- CP1=M02：配置、资产、实例与存储闭环。
- CP2=M04：受管加载/执行/取消/停止、会话和数据传输闭环。
- CP3=M06：兼容 API、完整模型证据执行器及发布门禁。
- CP4=M07：目标设备模型服务独立通过，视频项目通过与否不影响这一结论。

当前代码的回归入口（沿用现有 marker；新增 marker 由 M06 显式迁移）：

```bash
python -m pytest tests -m 'not thor' -q
python -m ruff check .
python run.py --check-config
```

发布环境 Python3.12；每项记录真实命令、exit code、commit/代码hash、工具链和证据目录。
设备执行/verify/render 的新增 CLI 必须在 M06 交付后使用，不从旧文档复制未实现命令宣称通过。

## 4. 完成定义

- [ ] 动态模型与多资产只读挂载、身份核验及旧配置显式迁移完成。
- [ ] 切换、取消、重启、未知实例/存储故障均保持正确资源账本和独立停止证据。
- [ ] 兼容接口回归与通用协议测试通过；不依赖任何影片任务或分析数据结构。
- [ ] 每个候选模型及其能力在目标设备完成真实推理和最大输入测量。
- [ ] 30分钟真实切换混合负载、故障/恢复、独立verify与production preflight通过。
- [ ] 仅生成模型服务发布包；视频模型效果、长片、标注和报告由独立项目验收。
