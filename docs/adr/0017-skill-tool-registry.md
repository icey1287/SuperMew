# ADR-0017：Skill 与 Tool Registry

- 状态：已接受
- 日期：2026-07-16

## 背景

Agent Runtime 原先在 Factory 内硬编码天气和知识库 Adapter，正常 Run 还以
`allowed_tools=None` 表示全部放行。角色在 durable Run 的执行快照中被丢弃，工具
schema 过滤与执行拒绝只认识一个可空名称集合。随着 SQL、Web 和 Sandbox 工具加入，
权限、Secret、超时、并发、结果契约和 Skill 内容若继续散落在 Factory、middleware
与各 Adapter 中，会形成浅 Module，失去 Locality。

## 决策

建立两个相邻的深 Module：

1. `ToolRegistry` 是工具声明、授权、request-owned Adapter 构造和 progressive
   disclosure 的 Interface。每个 Run 获得独立 `ToolSession`；模型前 schema 过滤与
   执行前 fail-closed 检查共同调用 `ToolSession.is_allowed()`。
2. `SkillRegistry` 是 manifest 校验、路径安全、内容冻结、hash pin 和权限过滤的
   Interface。`SkillActivationSession` 每 Run 最多激活一个 Skill，并通过
   `ToolSession.apply_skill()` 只收窄权限，不能扩权。

工具 descriptor 固定声明：name、description、group、version、input/output schema、
timeout、max concurrency、idempotency、required roles/secrets、approval、network
policy 与 result size limit。Registry 在授权时取以下交集：

```text
调用方工具上限
∩ 当前数据库角色
∩ 当前可用 Secret 名称
∩ 审批与网络策略
∩ active Skill allowed_tools
∩ resident/deferred 暴露状态
```

空集合就是空权限；`None` 不再进入 Runtime Context。control 工具也不能越过调用方的
空 allowlist。

所有经 Registry 绑定的工具统一向模型返回 `ToolResultV1`。领域 Adapter 的原始输出先
按 descriptor output schema 校验，再封装为成功结果；timeout、非法输出和超限结果
返回稳定失败结果。每个 descriptor 使用共享的有界 executor 实施进程级并发上限和
调用超时。线程内不可协作取消的工作可能在超时后短暂收尾，硬隔离留给 ADR-0020 的
Sandbox。

deferred Adapter 会预先注册进 compiled graph，但初始不向模型暴露 schema，也不可执行。
`tool_search` 只搜索已授权且处于当前 Skill scope 的目录；命中后才让后续模型调用看到
完整 schema。伪造隐藏工具调用仍在执行 Seam 被拒绝。

Skill 使用 `skills/<name>/skill.yaml` 和相对 entrypoint。启动时拒绝未知字段、重复名称、
未知工具、绝对路径、`..`、反斜线、symlink、根外路径、坏 UTF-8 和超限内容。Registry
读取并冻结正文，以 canonical manifest JSON、NUL 和原始正文 bytes 计算 SHA-256。
基础动态上下文只披露过滤后的摘要；完整正文只在显式 slash、可信 router 或
`describe_skill` 激活后注入。

durable Run 将 Skill name、version、content hash 和 activation source 与 owner fencing
一起持久化。worker reclaim、HITL 后续回答和进程重启必须重新校验数据库当前角色、
Secret availability 与 hash；内容漂移或撤权时 fail-closed。只持久 Secret 名称的可用性
判断，不持久也不输出 Secret 值。

Skill 激活发生在 `AgentRuntime` 进入 graph 之前，或由 control tool 在一次 tool round
中完成。因此 ADR-0011 的 middleware 顺序保持不变；`DynamicContextMiddleware` 每次模型
调用重新投影当前激活状态，随后 `ContextBudgetMiddleware` 统一计入预算，最后
`ToolPolicyMiddleware` 读取同一个 `ToolSession`。动态预算可以先省略不可信记忆和
summary catalog，但 active Skill instructions 必须作为不可分割块完整保留；放不下时
在模型调用前 fail-closed。

## 结果

调用者只需提交数据库角色、显式工具上限和可用 Secret 名称，即可获得绑定完成、权限
收窄、schema 渐进披露、预算受控且结果有契约的工具集合。删除 Registry 后，这些规则会
重新散落到 Factory、middleware、worker 和每个 Adapter，说明两个 Module 提供了真实
Depth、Leverage 与 Locality。

代价是 catalog 与 Skill 内容成为启动时不可变快照；升级 Skill 需要重启进程，新旧
durable Run 通过 hash pin 明确区分。未来热重载必须生成新 Registry snapshot 并原子替换，
不得原地修改现有 Run 的 Session。
