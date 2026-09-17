# SelfModelSwitch：模型挂载与切换方案 v3

日期：2026-09-16。源码基线：`c32dd1a8ecdfa9b7f8f9a964886b794a943e16ca`。
状态：设计修订，未实施生产代码，未完成真机验收。

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
| [05-tasks-and-acceptance.md](05-tasks-and-acceptance.md) | 仅模型服务的实施任务 |
| [06-acceptance.md](06-acceptance.md) | 模型服务独立验收与发布门禁 |
| [adr/decisions.md](adr/decisions.md) | 本次 Grill Review 与边界决策 |
| [m00-envelope.md](m00-envelope.md) | M00 首轮最大输入与并发探测规格 |
| [validation.md](validation.md) | 本次文档验证记录 |

[07-pipeline.md](07-pipeline.md) 仅保留迁移导航，不再定义本项目功能。
[legacy-v1](legacy-v1/README.md) 和 [legacy-v2](legacy-v2/ARCHIVE.md) 均为历史资料，不参与当前规范解释。
旧 reference Python 契约随 v2 归档，不能作为新方案的字段真源；新 DTO/schema 在实施任务 M01 中建立。
本方案与生产代码有明确版本差异；新增接口和命令均为待实现设计，当前可用功能以根 README 为准。

## 已确认决策

- 两个项目独立；当前项目验收不依赖长片、人工标注或外部 LLM。
- 在大规模实施前进行目标设备的最小真实推理与最大输入内存、耗时验证。
- 视频项目允许身份未知及后补标注，保留分块声线编号，按已确认公式平均切片。
- 视频末尾进行跨块声线一致性分析；证据不足保留独立编号，待人工确认。

## 后续边界

本次只修订文档并归档旧设计，不安装模型或运行设备测试。
模型服务任务见 M00—M07；视频项目任务见其独立任务表，两者不再沿用混合方案 T01—T24 的串行依赖链。
