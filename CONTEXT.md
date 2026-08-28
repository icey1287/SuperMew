# SuperMew Knowledge Agent Platform

SuperMew 是知识库优先的智能任务平台。它以持久化 **Thread**、可恢复 **Run**、版本化 **Event** 和可审计 **Tool** 执行为核心，让 RAG、HITL 与专业 Skill 共享同一运行生命周期。

## Language

### Conversation and execution

**Thread**:
一个用户拥有的连续对话容器；正式 Interface 由服务端生成安全 ID。它按 sequence 保存
**Message**，以只随 append 递增的 version 保护并发写入；自身 lifecycle status 与活跃
**Run** status 是两个独立事实。
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

### Models and evaluation

**Model Profile**:
一个持久化、无 Secret 的 OpenAI-compatible 模型配置；包含模型标识、Base URL、超时与能力声明，API Key 不属于该身份。
_Avoid_: Model env、API Key 配置、模型单例

**Model Assignment**:
控制面中从 Answer、Fast、Grader 或 Evaluator 角色到一个已启用 Model Profile 的当前映射；修改只影响后续创建的 Run 与 RAG Evaluation Job。
_Avoid_: 当前 Run 模型、全局 MODEL 值

**Model Snapshot**:
Run 或 RAG Evaluation Job 创建时冻结的完整无 Secret 模型目录；它保存角色、Model Profile 版本与 catalog hash，并在恢复、重试和 HITL 期间保持不变。
_Avoid_: 动态模型查找、环境变量快照、Secret snapshot

**RAG Evaluation Dataset**:
带 schema version、唯一 Case identity、人工期望与内容 fingerprint 的评估事实集合。
_Avoid_: Prompt list、临时问题文件

**RAG Evaluation Job**:
绑定 RAG Evaluation Dataset fingerprint、GatePolicy、可选 baseline 与 Model Snapshot 的持久化自动评估工作项；独立 worker 逐 Case 执行 RAG、生成回答、结构化评分并发布 Report。
_Avoid_: Run、前端任务、LangSmith session

### Identity and ingress

**Access Token**:
短期、带 `iat`/`jti` 的签名 Bearer credential；正式浏览器只在内存中持有，并在页面恢复时
通过 Refresh Token 重新签发。
_Avoid_: Browser session、localStorage token

**Refresh Token**:
仅由 `Path=/auth`、HttpOnly Cookie 承载的高熵 opaque credential；服务端只持久化其 SHA-256
hash，并通过 rotation、replay detection 与撤销 ledger 管理生命周期。ledger 在自然过期后仍
保留独立 forensic/audit retention window；过期 token 不再触发用户级 replay 撤销。
_Avoid_: Refresh JWT、remember-me value

**Auth Origin Decision**:
在 Rate Limit 与 credential mutation 前对 auth unsafe POST 作出的来源/JSON/大小判断；same-origin
始终可信，跨 origin 只有 credentials CORS 与显式 allowlist 同时允许时可信。
_Avoid_: Same-site trust、CORS wildcard

**Rate Limit Decision**:
对一个稳定 policy 与 HMAC identity 原子消费后的 allowed/remaining/reset 结果；它不携带原始
IP、username、Bearer 或 Cookie。
_Avoid_: Route counter、raw identity key

### Extensibility and safety

**Skill**:
按需激活、带版本且声明允许 Tool 的领域能力包。
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

- 一个 **Thread** 有多个有序 **Message** 和多个串行或排队的 **Run**；更新已有 assistant
  Message 的正文或终态不会推进 Thread append version。
- 一个 **Run** 产生多个 **Event**，最多有一个当前 **Checkpoint**，并投影到一个 assistant Message。
- 一个 **Document** 有多个 **Document Version**，但任一时刻最多发布一个当前版本。
- 一个 **Model Assignment** 恰好把一个模型角色映射到零或一个 **Model Profile**；新 Run 创建时把所有当前 Assignment 冻结成同一个 **Model Snapshot**。
- 一个 **RAG Evaluation Job** 只消费一个 **RAG Evaluation Dataset** fingerprint，并冻结自己的 baseline、GatePolicy 与 Model Snapshot；每个 Case 有独立持久状态。
- 一个 **Skill** 允许零到多个 **Tool**；每次 Tool 调用都必须先产生 **Guardrail Decision**。
- **Sandbox Execution** 是 Tool 的一种隔离实现，不替代 Guardrail Decision。
- **Evidence** 来自已发布 Document Version 或受控 Web Research，并由 RAG Trace 记录其公开身份。
- 浏览器用内存 **Access Token** 调用受保护 Interface；页面刷新通过 Cookie 中的
  **Refresh Token** 恢复身份，每次成功刷新都会轮换该 credential；支持 Web Locks 时跨标签页
  串行 refresh，并在等待锁后重检 generation/tombstone 与主体。
- **Refresh Token** 的所有在线生命周期写事务先锁 User，再锁定或写入该用户的 token；独立 purge 只能删除
  自然过期且已越过 retention window 的 ledger 行。
- 每个 auth unsafe POST 必须先得到允许的 **Auth Origin Decision**；login/register JSON media
  type 与声明的 16 KiB 上限在 Rate Limit 前校验，实际 stream 在计费后、route 前受同一上限约束。
- 一个入口请求可先后产生 IP 与 IP+规范化 username 两个 **Rate Limit Decision**；登录与注册
  只有在两者都允许后才进入密码校验，复合 identity 每次消耗两个 quota unit。

## Flagged ambiguities

- **Task**：只用于面向用户描述工作，不用于持久化模型。Agent 执行称 **Run**，文档后台工作称 **Index Job**。
- **Evaluation Job**：不是 Run。Run 产生面向用户的 Thread Message；RAG Evaluation Job 产生 Case Observation 与脱敏 Report。
- **Model**：运行时谈具体角色时使用 Model Profile、Model Assignment 或 Model Snapshot；不要用 `MODEL`、`FAST_MODEL` 等环境变量名代替领域身份。
- **Bearer**：描述受保护 HTTP Interface 的 access credential 传输协议，不表示浏览器可把
  Access Token 持久化；正式前端只从内存认证状态读取它。
- **Same-site**：不是认证来源信任边界。Auth 使用严格 same-origin；跨 origin 即使 same-site，
  也必须同时满足 credentials CORS 与显式 allowlist。

## Example dialogue

> 开发：用户刷新页面后，应该重新创建 Run 吗？
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
>
> 开发：页面刷新后从 localStorage 恢复 Bearer 可以吗？
>
> 领域专家：不可以。Access Token 只在内存中；页面先用 HttpOnly Refresh Token 调用
> `/auth/refresh`，轮换成功后再恢复受保护请求。
>
> 开发：Redis 限流不可用时，先放行登录避免影响可用性吗？
>
> 领域专家：生产环境不可以。Rate Limit 是 PBKDF2 前的入口保护，存储异常必须 fail-closed，
> 返回 typed 503，不能把昂贵认证路径暴露为降级模式。
>
> 开发：Nginx 已经加了 X-Forwarded-For，Rate Limit 直接读它可以吗？
>
> 领域专家：不可以。只有可信 ProxyHeaders/forwarded allowlist 可以先修正 `scope.client`；
> Module 直接信任任意转发头会允许客户端伪造 quota identity。
>
> 开发：管理员把 Evaluator Assignment 切到新 Model Profile 后，正在执行的评估要跟着切吗？
>
> 领域专家：不可以。当前 RAG Evaluation Job 继续使用创建时冻结的 Model Snapshot；新 Assignment 只进入之后创建的 Job 与 Run。
