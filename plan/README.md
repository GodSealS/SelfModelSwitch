# SelfModelSwitch：模型挂载与切换方案 v3

日期：2026-09-16。源码基线：`c32dd1a8ecdfa9b7f8f9a964886b794a943e16ca`（**历史快照**：09-16 设计阶段的核对）。
状态（**历史**）：设计修订，未实施生产代码，未完成真机验收——该行只描述 09-16 的设计阶段；
v3 实施与修复的当前状态见下方「文档角色」与「当前进度」，设备与生产结论文档仍未验收。

## 项目范围

SelfModelSwitch 只提供模型挂载、加载、卸载、切换及其必要的资源调度与推理访问能力。
模型挂载指已登记资产的校验和只读映射；服务不自动下载模型、格式化磁盘或安装驱动。
保留现有聊天、向量、重排接口兼容性；多模态和其他模型通过通用后端能力扩展，不引入视频业务。

视频分析是独立项目，设计暂存于 [video-analysis](video-analysis/README.md)，将来迁入独立仓库。
本仓库不实施视频任务、FFmpeg 管线、影片数据库、声脸关联、标注表格或外部关系报告。
独立项目通过版本化的模型服务接口集成，分别配置、测试和发布。

## 当前规范入口

| 文档 | 职责 |
|---|---|
| [01-design.md](01-design.md) | 模型服务范围、模块与项目边界 |
| [02-scheduler.md](02-scheduler.md) | 资源、租约、切换、停止证据与恢复 |
| [03-api.md](03-api.md) | 兼容接口与通用模型控制协议设计 |
| [04-deployment.md](04-deployment.md) | 模型配置、资产、设备、迁移与部署 |
| [05-tasks-and-acceptance.md](05-tasks-and-acceptance.md) | 仅模型服务的实施任务（范围定义；进度见 08） |
| [06-acceptance.md](06-acceptance.md) | 模型服务独立验收与发布门禁（**规范性真源**） |
| [08-execution-plan.md](08-execution-plan.md) | v3 执行计划：C01—C09 接口/行为约束、有序任务与验证模板 |
| [tool-calling-and-reasoning/README.md](tool-calling-and-reasoning/README.md) | 工具调用/思考扩展执行计划：CT00—CT12、TC01—TC09接口及A01—A12验收；仅计划，尚未实施 |
| [adr/decisions.md](adr/decisions.md) | 本次 Grill Review 与边界决策 |
| [m00-envelope.md](m00-envelope.md) | M00 首轮最大输入与并发探测规格 |
| [validation.md](validation.md) | 证据与验证记录（不是规范），含历史快照段落 |
| [../check/20260922-v3plan-execution-contracts.md](../check/20260922-v3plan-execution-contracts.md) | 本轮修复冻结的接口/状态契约 K1—K8（**规范性真源**，由 08 引用） |
| [../check/20260922-v3plan-execution-plan.md](../check/20260922-v3plan-execution-plan.md) | 本轮修复的 19 项任务（RP00—RP18）与逐项实际验证记录 |

[07-pipeline.md](07-pipeline.md) 仅保留迁移导航，不再定义本项目功能。
[legacy-v1](legacy-v1/README.md) 和 [legacy-v2](legacy-v2/ARCHIVE.md) 均为历史资料，不参与当前规范解释。
旧 reference Python 契约随 v2 归档，不能作为新方案的字段真源；新 DTO/schema 在 M01/`contracts_v2.py` 中建立。
[../check/20260922-v3plan-review-verification.md](../check/20260922-v3plan-review-verification.md) 与
[../check/20260922-v3plan-fix-plan.md](../check/20260922-v3plan-fix-plan.md) 分别是原团队审查 23 个 ID 的逐项
事实复核与修复思路来源——两者都是记录，不是规范。

## 文档角色：当前规范 / 基线快照 / 记录

- **当前规范**：`01`—`06` 与 `08` 的 C01—C09 约束；`06` 始终是必测集合与发布门槛的规范来源，
  K1—K8 契约在 `check/20260922-v3plan-execution-contracts.md`，08 的 Cxx 节引用它们。
- **待实施扩展**：[工具调用/思考计划](tool-calling-and-reasoning/README.md)是该扩展的任务入口，
  其contracts与acceptance定义未来实现的接口和用例；不表示当前已支持，也不放宽06或ADR-05的生产门禁。
- **基线快照（历史）**：`legacy-v1`、`legacy-v2`、`video-analysis`、`05` 的 M00—M07 任务表、
  `08` §1 的实施前核对（含 3.13.5 解释器、226 passed、当时“模块不存在”等判断）、`validation.md` 的旧段落。
  它们记录实施前的事实，不再代表当前状态。
- **记录（不是规范）**：`validation.md` 的证据段落与 `check/` 下的复核/方案/执行计划。记录只回答
  “做过什么、证据是什么”，不能覆盖 `06` 的门禁或 `08` 的约束，也不能把未执行的验收写成通过。

## 当前进度（2026-09-22）

v3 实施按 `08` 与 [执行Plan](../check/20260922-v3plan-execution-plan.md) 推进：**RP00—RP14 已交付并原子提交**
（含 C03 的 K1—K5、C08 的 K7 准备/开放分离与双入口共同就绪、C09 的 K6 第一阶段），**RP16**（本页导航与总检查）
同批完成；剩余 **RP15**（C02 产品决策，待用户选择）与依赖本页的 **RP17**（同 SHA 目标复验），以及**仅当**最终
生产模型数 > 1 时必需的 **RP11/RP12**。逐项实际验证记录写在 `check/` 执行计划内；**设备与生产结论仍未验收**，
本页不宣称任何模型可用。

## 已确认决策

- 两个项目独立；当前项目验收不依赖长片、人工标注或外部 LLM。
- 在大规模实施前进行目标设备的最小真实推理与最大输入内存、耗时验证。
- 视频项目允许身份未知及后补标注，保留分块声线编号，按已确认公式平均切片。
- 视频末尾进行跨块声线一致性分析；证据不足保留独立编号，待人工确认。

## 后续边界

本次只修订文档并归档旧设计，不安装模型或运行设备测试。
模型服务任务见 M00—M07；视频项目任务见其独立任务表，两者不再沿用混合方案 T01—T24 的串行依赖链。
