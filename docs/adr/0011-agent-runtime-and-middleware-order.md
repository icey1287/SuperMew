# ADR-0011：Agent Runtime 与中间件顺序

- 状态：已接受
- 日期：2026-07-14

## 背景

旧 Chat Module 同时承担 HTTP 生命周期、Agent 构造、动态上下文、工具策略、预算、记忆、HITL、流式输出和消息持久化。调用者必须了解大量隐含顺序，导致 Runtime Interface 与 Implementation 同样复杂，缺少 Locality。

Run、Event、Checkpoint 已经成为持久化事实来源后，需要一个深 Module 隐藏 Agent 执行细节，并让旧 `/chat` 只保留为 compatibility adapter。

## 决策

建立两个相邻 seam：

1. `AgentRuntimeFactory.create()` 是 Agent 构造 Interface。它负责模型选择、工具装配、稳定基础提示词、预算和固定中间件链。
2. `RunAgentExecutor.spawn_once()` 是持久化 Run 执行 Interface。它负责领取 Run、加载执行快照、续租、驱动 Runtime、追加 Event、响应取消并原子终结消息与 Run。

固定中间件顺序如下：

1. `RequestContextMiddleware`
2. `RuntimeTracingMiddleware`
3. `DynamicContextMiddleware`
4. `ContextBudgetMiddleware`
5. `ToolPolicyMiddleware`
6. `ToolCallLimitMiddleware`
7. `ModelCallLimitMiddleware`
8. `LoopDetectionMiddleware`
9. `TerminalResponseMiddleware`
10. `ClarificationHITLMiddleware`

该顺序由测试锁定。修改顺序必须同时更新本 ADR，并说明新顺序如何保持下列不变量。

## 不变量

- 基础 System Prompt 保持稳定；日期、用户、Thread、Run 和长期记忆通过独立动态消息注入。
- 动态记忆是“不可信数据”，不能成为系统指令。
- 上下文按 token 预算裁剪，并保持 AI tool call 与对应 `ToolMessage` 成组。
- 工具权限先过滤暴露给模型的 schema，执行前再次 fail-closed 检查。
- deadline、模型调用、工具调用和循环预算都必须能稳定终止执行。
- 流式调用以最终 graph state 为权威结果；delta 只是可重放的中间投影。
- `message.completed` 与 Run terminal Event 只能由持久化事务产生，不能由 SSE producer 单独发布。
- `RunAgentExecutor` 必须使用数据库 owner、lease 和 fencing token；delta、progress 和 trace 也必须携带 owner/fence，terminal 后拒绝旧 writer。
- Run 版知识工具必须使用持久化 `CheckpointedRagRunner`；`waiting_input` 只有在 checkpoint、Run 状态和 `hitl.required` 已同事务落盘后才成立。
- HITL resume 必须使用 `Command(resume=...)` 恢复同一 checkpoint，不得重新执行原问题来模拟恢复。
- shutdown、用户取消和 ownership 丢失是三种不同终止原因；部署关闭不得记录成用户取消。
- 进程启动和周期 dispatcher 必须回收过期 owner，并唤醒持久化的 pending Run 与已接受的 checkpoint resume。
- Runtime 执行受进程级并发上限约束；配置的 worker 前缀必须附加 host、pid 和 boot UUID，不能作为跨实例身份本身。
- 旧 Chat Module 不得重新成为新 Run 路径的执行入口。

## 结果

调用者只需跨越一个小的执行 Interface，即可获得模型选择、上下文、工具策略、预算、追踪、取消、事件和原子终结的 Leverage。相关变更集中在 Runtime 与 Executor 两个 Module，提高 Locality，并为后续 Provider、Tool Registry、Skill、Guardrail 和 Worker adapter 保留稳定 seam。

代价是中间件顺序成为兼容性约束；新增中间件不能仅在列表中随意插入，必须验证它对上下文、工具执行和终态语义的影响。
