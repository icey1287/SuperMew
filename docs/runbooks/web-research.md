# Web Research 运维手册

## 启用前准备

Web Research 默认关闭。创建最小权限 Brave Search API key，并通过 Secret 管理系统注入；不要
把 key 写入 `.env` 模板之外的仓库文件、工单、聊天、日志或命令输出。搜索 endpoint 固定为
Brave 官方 HTTPS origin，不提供自定义 endpoint 配置。

最小配置：

```dotenv
WEB_RESEARCH_ENABLED=true
BRAVE_SEARCH_API_KEY=<secret-manager-reference>
```

完整预算见 `.env.example`。上线前重点确认：

- DNS timeout 小于等于 request timeout；DNS 并发和每次地址数量保持最小；
- default search results 不大于 max results，max results 不大于 max citations；
- title/snippet 不大于单页 content，content 不大于 total evidence；
- total evidence 不大于 Agent 输入 token 预算的一半；默认为 4 KiB，不要直接恢复
  早期 512 KiB 高值；
- compressed body、解压 response、单页 content 与 total evidence 使用各自独立上限；
- redirect 上限、正文上限和总结果上限不因“抓取失败”被临时放大。

`WEB_RESEARCH_MAX_CONCURRENCY` 同时用于每个 Tool descriptor 和共享 Web Runtime semaphore，
因此 search/fetch 的总在途调用不会越过该值；每个 Run 还受 Agent tool-call、loop 和 deadline
budget 约束。request timeout 是整次 search/fetch stage（包括 DNS 与所有 redirect）的总边界，
不按 hop 倍增。

HTTP transport 还使用 request-owned watchdog 强制 absolute deadline/cancellation。验收不能只测
完全静默的 socket timeout；必须包含每次在 timeout 前发送一个字节的慢响应头、慢正文和慢 TLS
样例，并确认旁路 socket shutdown 会在总 deadline 附近终止，且请求结束后不会迟到关闭新连接。

Web ToolResult 跨越 ContextBudget Seam 时是原子 JSON 消息。历史旧轮次可以整轮裁掉；
当前轮次若在裁掉可选上下文后仍放不下完整结果，Run 应在模型调用前返回
`POLICY_DENIED` / `web_research` / `context_budget`，trace error code 为
`WEB_TOOL_RESULT_CONTEXT_BUDGET_EXCEEDED`。不得把 `…[truncated by context budget]` 写入
ToolResult JSON。

所有环境的启动验证都会在 feature enabled 时拒绝空 key 或模板占位 key。feature 关闭或 key
缺失时，Registry 不会声明 `BRAVE_SEARCH_API_KEY` capability。

## 发布验证

```bash
uv run --frozen python -m backend.tools.registry_cli validate
uv run --frozen python -m backend.tools.registry_cli list-skills --role user
uv run --frozen python -m backend.tools.registry_cli list-tools --role user
uv run --frozen pytest -q tests/test_web_research_contracts.py \
  tests/test_web_url_policy.py tests/test_web_research_http.py \
  tests/test_web_research_runtime.py tests/test_web_tools.py \
  tests/test_agent_runtime.py tests/test_settings_security.py
```

Secret 未配置时，`web-research` Skill 与两个 deferred Tool 必须隐藏。配置并重启后，目录可看到
Skill；激活 `/web-research` 后，`tool_search` 才能披露 `web_search` / `web_fetch` schema。
readiness 只应输出 enabled/ready/search-ready 与聚合预算，不得输出 key、query、URL 或正文。

执行烟雾测试：

1. 搜索一个公开、无敏感信息的主题，确认结果包含稳定 evidence/citation identity 和 UTC 时间；
2. 用返回的 `evidence_id` fetch，确认模型不能提交任意 URL；
3. 未知或跨 Run evidence ID 返回 `WEB_EVIDENCE_NOT_AUTHORIZED`；
4. 模型输出 `[标题](webcite:evidence_id)`，服务端只把当前 Run identity 渲染为 Markdown
   link；raw URL、未知/跨 Run identity 和成功取证后无引用均被终态拒绝；
5. ToolAudit 只有 evidence/citation count、output bytes、truncated 和 outcome。

Web Research 为保证终态引用校验，不逐 token 发布模型草稿；Event/SSE 只会在校验通过后收到
一次完整的安全回答。若看到 `WEB_CITATION_*` trace/error，先检查模型是否按 Skill 输出
`webcite:` token，不要关闭终态校验或允许 raw URL。

前端必须保留 DOMPurify allowlist 与 raw HTML renderer 禁用。任何修改 `marked`、`v-html`、
允许 tag/attr 或 URI scheme 的变更，都要重跑 `frontend/src/utils/markdown.spec.ts` 中的 script、
event handler、javascript/data/scheme-relative link 与恶意来源标题攻击用例。

## SSRF 与内容边界验证

在隔离测试环境覆盖下列拒绝样例，不要在生产手工探测内网：loopback、RFC1918、link-local、
特殊用途域名、带 userinfo URL、非 HTTP(S)、非标准或 scheme 混淆端口、DNS 结果混有私网地址、
redirect 转向私网、peer IP 不在 DNS pin、过多 redirect、未知 content type、压缩/解压超限。

确认 transport 不使用环境 proxy，TLS hostname 验证仍针对 canonical host，且每个 redirect 都
重新解析与 pin。DNS timeout、HTTP timeout、Run deadline 或 cancellation 发生时应返回稳定
错误码；不得改用普通 hostname client 重试。

## 常见故障

### Skill 或 Tool 不可见

依次检查 feature flag、Secret 是否由当前进程读到、调用方是否声明同名 capability、Run 是否
允许 `restricted` network policy、Skill 是否已经激活、Registry 是否在配置变更后重启。
不要把 key 放入 prompt 来“证明已配置”。

### 搜索可用但 fetch 被拒绝

`web_fetch` 只接受当前 Run 中 `web_search` 铸造的 `evidence_id`。重新搜索目标页面，不要把 URL
改写成 identity，也不要扩大为任意 URL fetch。若搜索 provider 没有返回该页面，回答中披露
覆盖缺口。

### DNS、redirect 或内容 policy 拒绝

这通常意味着目标不是安全公网页面、DNS 集合含非 global 地址、redirect 改变了安全边界，或
响应超出类型/字节预算。记录稳定错误码和聚合 outcome；不要记录原 URL/响应，也不要放宽
private/loopback policy。若目标确属内部资源，应设计独立的私网 Tool 和权限，而不是复用本
Skill。

## 禁用、轮换与事件响应

紧急禁用时设置 `WEB_RESEARCH_ENABLED=false` 并滚动重启 API/worker；必要时立即在 Brave
控制台撤销 key。轮换时先部署新 Secret、滚动重启并验证 readiness，再撤销旧 key。

若怀疑 key 泄露，除轮换外还应审查 provider 用量和本地审计是否违反“无原文”规则。审计、
Event、checkpoint 或日志中一旦出现 query、URL query string、正文或 key，应按数据泄露事件
处理并停止发布；不要仅靠日志脱敏规则掩盖错误的数据流。
