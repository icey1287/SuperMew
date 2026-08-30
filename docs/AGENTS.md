# Documentation AGENTS.md

本文件适用于 `docs/`，继承根目录 `AGENTS.md`。SuperMew 已有明确的文档分工；不要把同一事实复制到多个长文档并让它们独立演化。

## 文档类型与所有权

### `../CONTEXT.md`

正式领域语言、对象关系和禁止混用的名称。新增核心对象或改变身份关系时先更新这里，再同步 schema、代码、测试、README 和 ADR。

### `adr/`

记录“为什么选择这一架构”、正式 Interface、关键不变量和结果。已接受 ADR 是决策历史：

- 实施细节在同一决策内演进，可以更新并保持准确。
- 实质性反转、替代正式 Interface 或改变安全/一致性模型，应新增 superseding ADR，并在旧 ADR 标记被取代；不要把历史改写成仿佛旧决策从未存在。
- ADR 不写临时任务清单、机器路径、Secret 或无法长期维持的函数级复制。

### `runbooks/`

记录当前可执行的部署、门禁、清理、上线和故障处理步骤。Runbook 必须能被操作者直接执行：前置条件、命令、期望结果、失败方向和恢复方式清楚；旧命令/路径在实现删除时同步移除。

### `../README.md`

面向用户/贡献者的当前能力、架构概览、配置和使用流程。不要在 README 放内部竞态证明或完整安全推导；链接到 ADR/Runbook。

## 写作规则

- 使用 `CONTEXT.md` 的正式名词和大小写：Thread、Run、Event、Checkpoint、Document Version、Index Job、Evidence、Model Snapshot、Skill、Tool、Guardrail Decision、Tenant。
- 清楚区分事实来源与投影：Event Journal vs SSE、Run vs Evaluation Job、Document Version vs upload、Access Token vs Refresh Token。
- 描述公开 RAG Trace，不把模型私有推理或 chain of thought 写成可观察能力。
- 命令从仓库根还是子目录运行必须说明；变量用占位符，绝不粘贴真实 credential。
- 链接使用仓库相对路径；代码/配置名使用反引号；长段落优先拆成标题、表格和短列表。
- 避免“永远”“完全安全”等无法证明的表述；写清边界、已知限制和未运行的验证。
- 日期、版本和默认值必须来自当前代码/配置；变更后搜索并移除旧说法。

## 代码与文档同步矩阵

| 代码变化 | 文档动作 |
| --- | --- |
| 用户能力、入口、配置 | README |
| 核心领域名/身份 | CONTEXT + 相关 ADR/Contract |
| 新正式 Interface/一致性模型 | ADR |
| 部署、门禁、cleanup、故障流程 | Runbook |
| 内部目录、命令、Agent 反复错误 | 最近的 AGENTS |
| 删除旧实现 | 删除所有 active docs 中旧 route/config/步骤 |

文档-only 改动也要核对引用的代码和命令，不能因为没有代码 diff 就跳过事实验证。

## ADR 模板建议

```md
# ADR-NNNN：标题

- 状态：提议 / 已接受 / 已取代
- 日期：YYYY-MM-DD
- 取代：ADR-NNNN（如适用）

## 背景
## 决策
## 不变量
## 结果
```

不要求每篇机械一致，但必须能区分问题、决定、不可破坏条件和代价。

## Runbook 校验

- 开发/质量命令与 `.github/workflows/`、`pyproject.toml`、`frontend/package.json` 对齐。
- 生产命令与 `docker-compose.prod.yml`、worker 入口、settings startup validation 对齐。
- destructive 命令说明备份、目标环境和 fail-closed 条件；示例不得指向生产资源。
- 在线模型、Milvus、Redis、PostgreSQL 检查与离线 fake/SQLite 检查明确区分。

## Code Review Rules

### 文档授权已删除实现

- 阻止 active docs 继续推荐旧 route、环境变量热切换、双读/双写或已删除 fallback。
  安全路径：只描述 canonical Interface；迁移历史留在 ADR/迁移文件而非运行步骤。

### 不同文档定义不同术语

- 阻止 Session/Task/Collection 等近义词重新成为领域身份。
  安全路径：引用 CONTEXT，并在首次出现时链接正式对象。

### 命令不可复现或危险

- 阻止缺 cwd、Secret 前置、目标环境或 destructive 警告的命令。
  安全路径：给出最小可复制命令、占位变量、期望输出和失败处理。

### 夸大验证结论

- 阻止以 offline smoke、SQLite、fake Adapter 或单元测试声称生产端到端已验证。
  安全路径：精确写明验证层级、缺口和所需 live/integration 证据。
