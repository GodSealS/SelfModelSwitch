# 本轮设计自检记录

日期：2026-09-16；源码基线 `c32dd1a8ecdfa9b7f8f9a964886b794a943e16ca`。
修改范围只有plan/，生产源码与配置未变；旧计划归档在legacy-v1，未删除。

## 已执行

| 检查 | 结果 |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s plan/reference -p 'test_*.py' -v` | 29项通过，exit0 |
| `.venv/bin/python -m ruff check plan/reference` | 通过，exit0 |
| 全部plan Python文件AST解析 | 通过 |
| 当前Markdown本地链接、全部围栏闭合、尾随空白 | 通过 |
| export_schema.py在临时目录导出 | 27个严格模型，全部additionalProperties=false |
| `git diff --exit-code -- . ':!plan'` | exit0，未改生产跟踪文件 |
| 独立架构交叉审查 | 已完成，两轮修正纳入 |

测试环境：本机现有venv，Python 3.13.5、Pydantic 2.13.5；并非方案规定的生产Python3.12或ARM64。
未为本轮安装新依赖。JSON Schema导出在临时目录完成，仓库以contracts.py为唯一字段定义源，不提交可能过期的派生schema。

## 已覆盖的主要反例

- 动态模型/三种scope、重复端口、未知字段、bool冒充整数、错误role、pinned组合、vision缺mmproj、路径逃逸。
- 内存恰好满足/差1byte、样本过期/未来时间、重复资源计账约束。
- permit TTL不清预算、PREPARING过期不能复活、旧boot无法提交/关闭、stop响应不足以释放。
- execution成功只释放执行租约、重复完成不能影响新执行、job取消必须确认资源停止。
- 必测集合缺失/重复、skip/not_run/unknown、坏文件/缺文件、伪passed、错误物理设备/代码。
- 过期/未来/逆序报告、重新包装旧证据无法刷新时间；摘要hash正确但原始语义失败仍拒绝。
- 配置摘要不依赖回填candidate hash，运行参数变化必改变摘要；30分钟空等不能冒充持续负载。

## 独立审查修正

1. 将FFmpeg等媒体进程也纳入许可和停止观察。
2. 用boot UUID和attempt fence拒绝跨重启的旧回写。
3. 明确配置、candidate、代码归档和最终bundle的摘要边界，消除自引用。
4. 明确原片InputRef与已提交ArtifactRef的解析和归属。
5. 规定audio.wav在probe已对齐零点，ASR不重复加audio_offset。
6. VLM文本输入包含本镜头人物ID/框对照，避免凭图猜匿名ID。
7. 原始case时间、case封套和report时间相互校验，防止重包刷新有效期。
8. 固定Q02时间匹配、Q06动作覆盖的分母、平局和人工裁决规则。

## 未执行且不能宣称通过

生产实现、既有应用全量回归、Python3.12/ARM64依赖安装、真实模型加载、Orin/Thor GPU、最大envelope峰值、
完整长片、人工标注质量评估、30分钟混合负载、SSD故障、外部LLM真实调用、systemd安装和生产发布。
reference中的synthetic_evaluator只用于门禁反例测试，不是生产评估器。
上述缺测是本轮“方案交付”的边界；未来生产验收按06执行，不能从这份记录推导设备或产品就绪。
