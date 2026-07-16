# SuperMew Knowledge Agent Platform

SuperMew 是知识库优先的智能任务平台。它以持久化 **Thread**、可恢复 **Run**、版本化 **Event** 和可审计 **Tool** 执行为核心，让 RAG、HITL 与专业 Skill 共享同一运行生命周期。

## Language

### Conversation and execution

**Thread**:
一个用户拥有的连续对话容器；它按 sequence 保存 **Message**，并以 version 保护并发写入。
_Avoid_: Session、conversation session

**Message**:
**Thread** 中不可变排序位置上的用户或 assistant 记录；assistant Message 可由 streaming 过渡到 completed、failed、cancelled 或 incomplete。
_Avoid_: Chat item、bubble

**Run**:
一次绑定用户、Tenant、Thread 与幂等键的持久化 Agent 执行；一个 Run 恰好拥有一个用户 Message 和一个 assistant Message。
_Avoid_: Request、task、chat request

**Event**:
属于单个 **Run** 的版本化、单调 sequence 事实；SSE 是 Event Journal 的可恢复投影，不是事实来源。
_Avoid_: SSE chunk、stream message

**Checkpoint**:
**Run** 在可恢复图节点上的持久状态；它与 Run、Thread、用户和一次性 HITL token 绑定。
_Avoid_: Pending state、resume cache

**HITL**:
Human-in-the-loop 暂停状态；Run 进入 waiting_input 后，用户回答恢复同一 Checkpoint、同一 Run 和同一 assistant Message。
_Avoid_: Follow-up chat、重新提问

### Knowledge and evidence

**Knowledge Base**:
可按 Tenant 与权限范围检索的一组 **Document**。
_Avoid_: Collection、folder

**Document**:
知识目录中的稳定逻辑身份；它指向当前已发布的 **Document Version**。
_Avoid_: Filename、uploaded file

**Document Version**:
一个 Document 的不可变内容版本；解析、切分、嵌入与索引完成后才可原子发布为当前版本。
_Avoid_: Replacement file、latest upload

**Index Job**:
负责 Document Version 解析、索引、发布或清理的持久化 worker 工作项。
_Avoid_: Run、BackgroundTask

**Evidence**:
带 Document Version、chunk、来源和内容哈希身份的可引用检索材料。
_Avoid_: Context text、raw chunk

**RAG Trace**:
Run 对检索路线、候选、评分、降级、Evidence 与耗时的可审计投影；它不包含模型私有推理。
_Avoid_: Chain of thought、debug dump

### Extensibility and safety

**Skill**:
按需激活、版本固定且声明允许 Tool 的领域能力包；一个 Run 使用同一个 Registry snapshot。
_Avoid_: Prompt preset、plugin

**Tool**:
由 Registry descriptor 定义输入、输出、角色、网络、审批和预算约束的可执行能力。
_Avoid_: Function、command

**Guardrail Decision**:
针对一次具体 Tool 调用的确定性 `ALLOW`、`DENY` 或 `REQUIRE_APPROVAL` 结果，并携带稳定 policy identity。
_Avoid_: Permission boolean、model judgement

**Approval Grant**:
控制面在 Run 创建前签发的 names-only 预授权快照，绑定用户、Tenant、Thread 与 Run。
_Avoid_: Approval token、runtime override

**Destination Capability**:
由当前 Run 的已验证 Web search Evidence 派生、绑定具体公网目标的 request-owned HMAC 权限。
_Avoid_: Raw URL allowlist、fetch token

**Sandbox Execution**:
已通过 Guardrail 的隔离代码执行；它使用固定 digest image、无网络、无宿主挂载和有界资源。
_Avoid_: Shell Tool、host command

**Tenant**:
数据、Tool policy 与运行身份的最高隔离范围；即使当前部署使用默认 Tenant，所有 durable Run 与敏感能力仍显式绑定它。
_Avoid_: Workspace、organization（除非产品未来明确引入独立概念）

## Relationships

- 一个 **Thread** 有多个有序 **Message** 和多个串行或排队的 **Run**。
- 一个 **Run** 产生多个 **Event**，最多有一个当前 **Checkpoint**，并投影到一个 assistant Message。
- 一个 **Document** 有多个 **Document Version**，但任一时刻最多发布一个当前版本。
- 一个 **Skill** 允许零到多个 **Tool**；每次 Tool 调用都必须先产生 **Guardrail Decision**。
- **Sandbox Execution** 是 Tool 的一种隔离实现，不替代 Guardrail Decision。
- **Evidence** 来自已发布 Document Version 或受控 Web Research，并由 RAG Trace 记录其公开身份。

## Flagged ambiguities

- **Session**：旧代码与旧路由曾用 session 表示对话。领域统一称 **Thread**；`/sessions` 仅是待迁移的历史读取路由名称。
- **Task**：只用于面向用户描述工作，不用于持久化模型。Agent 执行称 **Run**，文档后台工作称 **Index Job**。
- **Chat**：只描述产品交互体验。公开执行 Interface 是 Run/Event；`backend.chat.service` 是退役的内部兼容 Implementation。

## Example dialogue

> 开发：用户刷新页面后，应该重新发送 Chat Request 吗？
>
> 领域专家：不应该。先加载 Thread 的 Message，再按 run_id 重放 Event；如果 Run 是 waiting_input，就展示同一 Checkpoint 的 HITL，如果仍在 running，就从最后 sequence 继续订阅。
>
> 开发：用户点停止后可以立即把 assistant Message 标成 cancelled 吗？
>
> 领域专家：不可以。先向同一 Run 请求取消，继续监听 Event，直到 `message.completed` 和权威 terminal Event 落定 Message 与 Run。
>
> 开发：模型想抓取搜索结果里的 URL，直接把 URL 交给 Web Tool 吗？
>
> 领域专家：不可以。Tool 只接受 Evidence identity；Run 用 Destination Capability 绑定已验证目标，再由 Guardrail Decision 决定是否执行。
