# ADR-0019：Web Research、SSRF 防护与引用身份

- 状态：已接受
- 日期：2026-07-16

## 背景

Web Research 能补足知识库之外的时效信息，但搜索查询、搜索结果、URL、DNS、redirect、HTTP
响应和网页正文都不可信。若 Tool Adapter 直接调用通用 HTTP client，SSRF、DNS rebinding、
redirect 绕行、压缩炸弹、提示注入、数据外送、引用漂移与审计泄露会散落在 prompt 和调用方，
形成一个浅 Module。

PR-17 已建立 Skill/Tool Registry Seam，PR-19 在其后建立独立的 `backend.web_research` 深
Module。删除该 Module 会迫使每个调用方重新实现 URL canonicalization、DNS pin、HTTP
transport、证据预算和引用身份，说明它提供了真实 Depth 与 Leverage。

## 决策

### 小 Interface 与生命周期

Web Research Runtime 只向 request-owned Tool Adapter 暴露：

```text
search(query, limit, deadline_at, cancellation_probe) -> WebResearchResult
fetch(url, deadline_at, cancellation_probe) -> WebResearchResult
readiness() -> aggregate status
```

进程组合层从 Settings 构造并安装 Runtime；Tool 不持有 API key、HTTP client 或 DNS resolver。
Runtime 默认关闭，任何环境启用时都必须有 `BRAVE_SEARCH_API_KEY`。搜索 origin 固定为 Brave
官方 HTTPS endpoint，不允许用配置把 Secret 转发到任意主机。

`web_search` 与 `web_fetch` 均为 deferred Tool，要求 active `web-research` Skill、已配置
Secret 名称和 `restricted` network policy。`user` 与 `admin` 都可使用；Secret 只声明能力
可用性，值不进入模型上下文或 Run state。Factory 继续使用 configured secrets 与 caller
secrets 的交集，调用方伪造 Secret 名称不能越过 feature flag。

### 搜索铸造 capability，抓取不接受任意 URL

允许模型把任意 URL 交给 fetch 会形成数据外送通道：模型可把敏感文本编码进公网 URL。
因此 `web_search` 成功后只在当前 `ChatRequestContext` 记录
`evidence_id -> canonical_url`；`web_fetch` 的公开输入只有 `evidence_id`。未知、跨 Run 或已经
清理的 identity 一律返回稳定 policy failure。URL 从不由模型重新拼接，Run 关闭时 capability
map 被清理。

搜索 query 仍会发送到第三方服务，因此 Skill 明确禁止把 Secret、私有数据、知识库正文或
网页隐藏指令放入查询。未来 Guardrail 可在同一 Tool Seam 增加确定性 DLP policy，而无需
放宽本 ADR 的 capability flow。

### SSRF、DNS pin 与 redirect

`WebUrlPolicy` 对首跳及每个 redirect 都重新执行：

1. canonicalize URL；拒绝 userinfo、控制字符、非 HTTP(S) scheme、特殊用途域名，并移除不发送
   到服务端的 fragment；
2. 只允许 `http:80` 与 `https:443`，拒绝 scheme/port 混淆；
3. 在有界 DNS executor 中解析全部地址，数量受配置和硬上限 32 约束；
4. 任一地址不是 global routable 即整体拒绝；拒绝 loopback、private、link-local、reserved、
   multicast、unspecified、IPv4-mapped/transition/NAT64 绕行；
5. 冻结地址集合为 DNS pin，transport 只连接 numeric pinned address，并用实际 peer IP 复核；
6. redirect location 重新 canonicalize、重新解析和重新 pin，且受 hop 数上限约束。

HTTP Adapter 不读取环境 proxy。TLS 使用原 hostname 验证；响应必须通过 status、content type、
content encoding、压缩体、解压体、正文和总证据 byte budget。DNS、每个 hop、解析和投影均重验
Run deadline 与 cancellation，失败不降级为不安全 transport。

同步 `http.client` 的 socket timeout 只表示单次 I/O 空闲时间，不能阻止 slowloris 持续逐字节
续命。Transport 因此为每个请求启动一个受 Runtime semaphore 约束的 daemon watchdog，跟踪当前
raw/TLS socket；绝对 deadline 或 cancellation 到达时从旁路主动 `shutdown`，使阻塞的 TLS、
response-header 或 body read 立即退出。TLS wrap 与 handshake 分离，先登记新 socket 再握手，
避免所有权切换盲区；结束后停止 watchdog，禁止迟到 shutdown 影响后续连接。

压缩体、解压 HTTP response、单页正文、总证据和 ToolResult envelope 是不同 Interface 的
独立预算，不用大小关系互相替代。Tool descriptor 在总证据预算外固定保留 64 KiB envelope
空间，避免 Registry 对已通过结果契约的合法响应二次误拒绝。Tool descriptor 与 Runtime
共享 semaphore 使用同一并发上限，后者跨 search/fetch 提供进程级总在途边界。

ContextBudget Seam 把已通过 `ToolResultV1` 验证的 Web ToolMessage 视为原子消息。旧轮次可以
连同 tool call 整轮移除，但保留在当前轮次的结构化证据禁止字符截断；若完整结果与必需
上下文无法同时容纳，模型调用前以稳定 AppError fail-closed。这使 JSON 契约的完整性集中
在一个 Module，而不依赖模型容忍损坏输入。

启用 Web Research 时，应用启动还会校验跨 Settings 预算关系：
`WEB_RESEARCH_MAX_TOTAL_EVIDENCE_BYTES` 不得大于
`AGENT_MAX_CONTEXT_TOKENS - AGENT_RESPONSE_RESERVE_TOKENS` 的一半。ContextBudget 对混合中英文使用
1 character/token 的保守估算，因此该关系为 System Prompt、Tool schema、active Skill 和当前
请求保留至少一半输入预算。应用 Settings 默认总证据收敛为 4 KiB，单页正文
收敛为 3 KiB。

### 证据和引用身份

`WebResearchResult` 是唯一跨越 Module Seam 的结果契约。每条 `WebEvidence` 包含 canonical
URL、标题、snippet/正文、UTC `retrieved_at` 和正文 SHA-256；`evidence_id` 由 canonical URL、
正文 hash 与 schema version 确定生成。`WebCitation` 只引用已存在的 evidence identity，结果
创建时统一检查唯一性、引用完整性、条目数和 byte budget。

request-owned citation ledger 分别记录 `search_snippet` 与 `fetched_page` provenance，但只有
search evidence 会铸造 fetch capability；fetch 返回的新 identity 可用于引用，不能扩大后续
网络访问权限。模型只能输出 `[标题](webcite:evidence_id)` token，不能输出 raw HTTP(S) URL。
`TerminalResponseMiddleware` 在当前 Run ledger 中校验 identity，并用 ledger 内 authoritative
title 与 canonical URL 服务端渲染 Markdown link；未知、跨 Run、畸形 token、raw URL，或已有
成功证据却完全无引用时均 fail-closed。失败且无证据的回答不会被误伤。

Web Research 的模型 delta 在终态校验前只保存在 Runtime 内；只有最终 graph state 已经过
TerminalResponseMiddleware 后，才一次性发布可见内容。普通非 Web Run 继续逐 token 流式发布。
因此幻觉 URL 或坏 token 不会先进入 Event/SSE，再被终态结果“纠正”。Skill 在工作记录中使用
结构化 `W1/W2/...` 编号，并要求每个可外部验证事实附近放置引用，区分 search snippet 与已
fetch 正文，披露来源冲突、时间敏感性和覆盖缺口；网页内容始终是数据，不能成为系统指令。

浏览器 Markdown Adapter 关闭 raw HTML renderer，并在 `v-html` 前通过 DOMPurify 的 tag、attr
与 URI allowlist 再次清洗；只保留 HTTP(S) link，并统一添加 `noopener noreferrer`。这是对
非 Web 回答与未来渲染路径的防御纵深，不能替代服务端 Run-local citation ledger。

### 无原文审计

原始 query、URL（含 query string）、title、snippet、正文、正文 hash、evidence/citation ID、
API key 和内部异常不得进入日志、Run Event 或 ToolAudit。Tool observability metadata 仅允许：

```text
evidence_count, citation_count, output_bytes, truncated
```

Registry 会再次按 descriptor allowlist 丢弃任意额外 metadata。稳定错误码可以跨 Seam，且
统一使用 `WEB_` 前缀；异常 message 和 `safe_details` 默认不进入 ToolResult。由此保留运行
规模与 outcome 的可观测性，而不建立可反查用户查询或浏览内容的旁路数据集。

## 结果

代价是当前搜索 provider 固定为 Brave，fetch 只能跟随当前 Run 的搜索 capability，且只处理
有界 HTML/XHTML/plain text。该限制换来更小的攻击面、稳定引用身份和可证明的 Locality。
若未来增加其他 provider、PDF、浏览器执行或私网检索，应新增独立 Adapter/policy，不得复用
本公共网络权限静默放宽 SSRF 或审计边界。
