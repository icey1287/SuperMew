# ADR-0020：Tool Guardrail 与隔离 Sandbox

- 状态：已接受
- 日期：2026-07-16

## 背景

ADR-0011 已把 durable Run 执行集中到 `AgentRuntimeFactory` 与 `RunAgentExecutor`，
ADR-0017 建立 Skill/Tool Registry Seam，ADR-0019 则把公网 URL policy 与引用身份收进
Web Research Module。但 Registry 的“工具是否可见”不能替代“本次具体调用是否允许”：
调用时的 user、tenant、Thread、Run、active Skill、channel、network policy、目标资源和审批
状态都可能使一个已注册 Tool 变得不安全。

同样，普通进程内 Tool timeout 不能隔离不可信代码。直接让 Agent 使用宿主 shell、共享目录或
通用 Docker 命令，会把命令构造、宿主文件、Docker daemon、网络、资源配额、清理和审计泄漏
到 Factory、middleware 与 Tool Adapter，形成 Interface 与 Implementation 同样复杂的浅
Module。Sandbox 也只解决执行隔离，不能决定某个语义操作是否有权发生。

因此 PR-20 需要两个相邻但职责不同的深 Module：Guardrail 负责每次 Tool 调用的确定性语义
授权，Sandbox 负责已经获准的代码执行隔离。二者都必须 fail-closed，并保持小 Interface、
高 Depth、可替换 Adapter 和集中的 Locality。

## 决策

### Guardrail Module 与执行 Seam

建立 `backend.guardrails` 深 Module。`ToolGuardrail.evaluate(request)` 是唯一决策 Interface，
调用方只提交完整、Run-local 且审计安全的 `ToolGuardrailRequest`，获得带 policy version/hash
的稳定结果：

```text
ALLOW
DENY
REQUIRE_APPROVAL
```

输入包含 user、roles、tenant、Thread、Run、Tool name/group、参数结构摘要、active Skill
状态、channel、network policy、`resource_scope`、descriptor approval 声明、Run-bound grant
状态和可选 destination capability。原始参数值、query、SQL、URL、Secret、request body 和
approval material 不跨越该 Seam。上下文缺失、值未知、策略 Adapter 异常或 capability 无法
验证时统一 `DENY`。

`ToolPolicyMiddleware` 保持 ADR-0011 中的位置：模型调用前先按同一个 Run-local
`ToolSession` 过滤 schema；每次 Tool handler 前再构造完整 Guardrail request 并重新判断。
Registry 拒绝的伪造调用也生成稳定 denial 与完整 policy identity。由此“未向模型披露”与
“即使伪造也不能执行”是两个独立防线。

默认确定性策略按以下顺序收窄权限：

1. 校验完整身份、channel、network policy、Tool group 与 `resource_scope`；
2. 永久拒绝 hard-deny group；
3. 校验 active Skill 已注册且允许该 Tool；
4. 执行 SQL/private-data 与 Web destination 专用规则；
5. 检查 descriptor approval 与 approval-only group；
6. 只有剩余调用才 `ALLOW`。

可选 policy provider Adapter 只能进一步收窄基础 `ALLOW`，不能覆盖基础 `DENY`。policy
version 与 canonical hash 固定决策身份，任何决策矩阵变化都必须升级 version 并通过契约测试。

### Hard deny 与 approval-only group

以下 group 永久 hard deny：

```text
shell
code
process
network-private
high-risk
```

grant、角色、Skill 或外部 policy provider 都不能把这些 group 改为 `ALLOW`。需要代码执行时
不得把普通 `code`/`shell` group 放开，而应走独立的 `sandbox-execution` group。

`file-write` 与 `sandbox-execution` 是 approval-only group。Sandbox descriptor 还必须同时
满足：`requires_approval=true`、`admin` 数据库角色、`SANDBOX_RUNTIME` capability、active
`sandbox` Skill、`network_policy=none` 与 `resource_scope=code-execution`。缺少 grant 时决策
为 `REQUIRE_APPROVAL`，不是静默降级为普通 Tool；但当前 Runtime 不会在执行中暂停等待审批，
而是向模型返回稳定的 approval-required ToolResult。

### Durable Run approval contract

`RunToolApprovalGrant` 是 trusted control-plane 提供的 names-only snapshot，绑定完整的
user、tenant、Thread 与 Run identity。它不包含可复制 token，`repr`、prompt、checkpoint、
Event 和 ToolAudit 都不得泄漏身份或审批材料。Factory 在构造 Runtime 时先验证绑定；
`ToolPolicyMiddleware` 在每次 handler 前再调用 `grant.allows(...)`，因此仅在 Registry 建图时
通过一次检查不足以执行 Tool。

当前前端控制面提供创建 Run 前的明确确认 UX，但没有运行中审批 endpoint 或可恢复 approval
interrupt。唯一支持的授权流程仍是 trusted admin 在创建 durable Run 时通过 `approved_tools`
预先授权，且名称只能指向 Registry 中明确 `requires_approval=true` 的 descriptor，最多 32 个。
tenant、channel 与 approval names 进入 Run request hash 并持久化；worker reclaim、进程重启或
lease 转移时从执行快照重建绑定后的 grant。

`ToolSession` 在 Runtime 构造时按 grant 过滤 Tool 可见性。因此未来即使新增审批状态变更，
也必须先以 durable transaction 更新 Run，再重建该 Run 的 Runtime/ToolSession；禁止向正在
运行的 Session 原地注入权限。PR-20 不实现这条互动流程，也不把 `REQUIRE_APPROVAL` 误写成
“用户已经批准”。

### Request-owned destination capability

公网搜索的 provider origin 固定，但 `web_fetch` 的具体目标来自不可信搜索结果。为避免模型
提交任意 URL 或把敏感数据编码进 URL，`ChatRequestContext` 为每个 Run 建立 request-owned
HMAC authority：

1. 只有成功的 `record_web_search_result(WebResearchResult)` 可为 search evidence 的
   canonical URL 签发 capability；
2. 模型公开 Interface 只提交 `evidence_id`，不能提交 URL、capability 或签名；
3. 内部 authorization record 把 `evidence_id`、canonical URL 与 capability 保存在同一
   request-owned 映射；
4. capability 绑定 user、tenant、Thread、Run、Tool、network policy 与 `resource_scope`；
5. `web_fetch` 执行前由 Guardrail 重新验证绑定与 HMAC，authority 缺失或异常时 `DENY`；
6. capability 是不保留 issuance registry 的 request-owned HMAC claim；Run context 关闭时清空
   ledger、authorization 并销毁签名 key，已有 claim 随即全部失效。

fetch 后得到的新 evidence 可用于引用，但不能继续铸造网络权限。该 capability Seam 是
ADR-0019 SSRF/DNS-pin URL policy 的前置授权，不能替代逐跳地址校验；URL policy 也不能替代
Run identity 绑定。

### 审计与公开投影

每次 Tool 完成、失败或拒绝都写 `ToolAudit`，新记录使用 `ALLOW`、`DENY` 或
`REQUIRE_APPROVAL`，并持久化稳定 `reason_code`、policy version/hash 与 descriptor/catalog
身份。审计只允许结构化规模、outcome 和 allowlist 内 safe metadata；不得保存 Tool args、
source、SQL、query、URL、evidence/capability identity、签名、approval token、Secret 或内部
异常文本。

内部 trace 的 `guardrail_audit` 只用于 durable ToolAudit。公开 Run Event/SSE projection 必须
剥离 `guardrail_audit` 与 `audit_metadata`，避免把安全决策上下文变成旁路数据集。

### Sandbox 深 Module 与 Adapter Seam

建立 `backend.sandbox` 深 Module。调用方只依赖以下概念 Interface：

```text
start() / close() / readiness()
execute(identity, language, source, deadline, cancellation) -> bounded result
```

`SandboxRuntime` 隐藏生命周期、Run identity binding、并发、deadline/cancellation、资源预算、
结果验证和 Adapter 选择。`DisabledSandboxAdapter` 与 `DockerSandboxAdapter` 是同一 Seam 上的
两个真实 Adapter；默认使用 disabled 路径，不探测 Docker，也不影响 readiness。

Tool Adapter 是 request-owned Adapter，只允许模型传 `language` 与 `source`。模型不能选择
image、mount、host path、environment、user、network、daemon、资源限制或容器参数，也不会
获得容器 identity、宿主路径或 artifact handle。

### Docker 隔离不变量

启用 Docker Adapter 时必须同时满足：

- image 使用本地已有的 immutable digest；tag 被拒绝，容器固定 `--pull=never`；
- 启动检查 daemon、rootless 要求、image digest、固定 entrypoint 与非 root user；
- Docker CLI 使用 argv、`shell=False` 和最小环境，不继承 host env、proxy 或 Docker config；
- source 只通过 stdin 的有界 Base64 JSON frame 进入可信 runner，不进入 argv、环境或文件挂载；
- root filesystem read-only，workload user 固定为 `65532:65532`，drop all capabilities，启用
  `no-new-privileges`，关闭 network 与 IPC，并使用 private PID namespace；
- CPU、memory/swap、PID、file descriptor、core、file size、并发、执行时间、workspace、输出、
  文件数量/大小和路径深度都有独立硬上限；
- `/workspace` 与 `/tmp` 只使用有大小上限的 tmpfs，禁止任何 bind mount 或宿主目录共享；
- runner 在返回前拒绝路径穿越、symlink、hardlink、多链接 regular file、FIFO、socket、device、
  非法路径与超限 workspace；
- runner 在读取完成后、扫描 workspace 前杀死并回收 PID namespace 内所有剩余进程；即使不可信
  子进程调用 `fork()`/`setsid()` 逃离原进程组，也不能与结果读取或文件扫描并发；
- 成功、执行失败、超时、取消、输出超限和协议失败均执行
  `docker rm --force --volumes`，启动时还清理同 label 的已停止容器；
- 只返回有界 stdout/stderr、exit code 与聚合文件数量，不返回文件内容或 durable artifact。

无 bind mount、无持久 workspace、无 artifact export 是本版本的刻意取舍。若未来业务确实需要
文件产物，应建立独立的鉴权 Artifact Module、object storage Adapter、内容扫描、保留策略与
下载审计；不得通过暴露宿主路径或给当前 Tool 增加 mount 参数实现。

### 配置、生命周期与 readiness

Sandbox 默认关闭。关闭时 `SandboxRuntime.start()` 不调用 Adapter，也不要求本机存在 Docker。
启用时必须配置 `SANDBOX_ADAPTER=docker` 与 digest-pinned image；生产环境还必须设置
`SANDBOX_REQUIRE_ROOTLESS=true`，并应连接专用、最小权限的 rootless Docker daemon。Docker
socket 只属于可信宿主进程，绝不能挂入不可信 Sandbox 容器。

启用后的启动必须验证 daemon reachable 与 image available；任何失败都先调用 Adapter close
再令应用启动失败。`GET /health/ready` 在 disabled 时不受 Sandbox 影响，在 enabled 时只有
Runtime 已启动且 daemon/image 均 ready 才返回整体 ready。readiness 只投影 enabled、ready、
Adapter、daemon/image 状态和 active execution count，不暴露 Docker host、image digest、
container ID 或内部诊断。

应用关闭顺序保持：先停止 Run executor，随后关闭 Sandbox，再关闭 Web、SQL 与 Provider
Runtime，避免新调用进入正在拆除的执行环境。Sandbox close 先阻止新执行并等待已领取执行
退出，再清理 Adapter；cleanup 失败时保持不可 ready 和可重试状态，不能标记为已安全关闭。
所有 close 都必须幂等；partial start 也必须执行清理。

## 结果

Guardrail Module 把完整上下文校验、Skill scope、hard deny、approval、SQL/Web 专用规则、
destination capability 和审计身份隐藏在一个小 Interface 后；Sandbox Module 则把 Docker
命令、隔离参数、runner 协议、预算、清理和 readiness 隐藏在另一个小 Interface 后。删除任一
Module 都会使复杂度重新散落到 Factory、middleware、Tool Adapter、worker 与运维脚本，说明
它们提供了真实 Depth、Leverage 与 Locality。

代价是当前 Sandbox 只支持固定 image 中的 Python 与 POSIX shell、无网络、无 package install、
无持久文件和无 artifact；审批也只能由 admin 在 Run 创建前预授权。若未来增加互动审批、
文件写入、私网访问、更多语言或 artifact，必须建立新的 durable 状态机和专用 Adapter，不能
放宽 hard-deny group、复用 raw URL，或向当前 Sandbox Interface 添加宿主能力。
