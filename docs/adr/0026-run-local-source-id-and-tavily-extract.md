# ADR-0026：Run-local Source ID 与 Tavily Extract

- 状态：已接受
- 日期：2026-08-30
- 取代：ADR-0019；ADR-0020 的 Request-owned destination capability 部分

## 背景

旧 Web Research 把搜索结果建模为带 hash、`evidence_id`、`citation_id`、时间、域名和 schema
字段的完整引用协议，又让 `web_fetch` 经过 Run-owned HMAC Destination Capability、Guardrail
验证、DNS pin、redirect 复核和应用侧整页抓取。模型真正需要的是少量搜索摘要和一个可继续
追问的来源引用；旧协议占用大量上下文，也使正常 fetch 因 capability 串联失败而被普通
Guardrail 拒绝。

Tavily Extract 已支持用 `query` 对目标网页的 chunks 重新排序，并可限制每个来源返回的 chunk
数量。因此应用不再需要直接下载整页再交给模型自行筛选。

## 决策

### 单一正式流程

Web Research 只保留以下流程：

```text
web_search(query, max_results?, allowed_domains?)
  -> [{source_id: "S1", title, content}]
  -> web_fetch(source_id="S1", query?)
  -> Tavily Extract query-ranked chunks
```

`web_search` 返回的服务端结果仍包含 URL 与 UTC `retrieved_at`，但模型投影只包含
`source_id`、`title` 和 `content`。不再生成或传递 content hash、`evidence_id`、
`citation_id`、`citation_token`、snippet、domain 或 Web Research schema version。

`RunRequestContext` 为首次出现的来源 URL 依次分配 `S1`、`S2`……；同一 Run 再次出现相同 URL
时复用 Source ID。映射同时保存标题与产生该来源的原始 search query，Run 关闭时整体清理，
不进入数据库、Checkpoint 或 Event，也不能跨 Run 解析。

`web_fetch` 的唯一来源参数是 `source_id`，另有可选 `query`。未提供 query 时使用该 Source ID
记录的原始 search query。未知 Source ID 返回稳定 `WEB_SOURCE_NOT_FOUND`，不提供 URL 输入或
旧 identity 兼容分支。

### Tavily Search 与 Extract

Runtime 只向固定 Tavily 官方端点发送 HTTPS POST：

```text
https://api.tavily.com/search
https://api.tavily.com/extract
```

Extract 请求固定为：

```json
{
  "urls": "<Source ID 对应 URL>",
  "query": "<显式 query 或原始 search query>",
  "chunks_per_source": 5,
  "extract_depth": "basic"
}
```

应用最多消费五个 chunk，并在模型投影前把每个 chunk 限制为约 500 字符。若 Provider 返回单个
长字符串，只保留其前 500 字符，不把它重新解释成整页正文。`web_fetch` 不再执行目标页面
GET、redirect、HTML/plain-text 解析或正文清洗。

Web Research Runtime 不再依赖应用侧 URL policy、DNS resolver、pinned transport 或 SSRF
校验，因为应用只连接固定 Tavily origin，目标 URL 由 Tavily Extract 处理。声明式 Custom HTTP
Tool 是独立能力，仍保留其固定 HTTPS endpoint 与网络策略；本 ADR 不改变该 Tool。

### Guardrail 边界

删除 `DestinationCapability`、`DestinationCapabilityBinding`、Run HMAC authority、签名 verifier、
`ToolGuardrailRequest.destination_capability`、middleware 注入和 destination reason codes。
`web_search` 与 `web_fetch` 与其他 Registry Tool 一样继续经过普通 Tool Guardrail、Skill scope、
角色、feature capability、network policy 和 approval 检查，但 Guardrail 不再解析或验证 Web
Source ID。Source ID 的 Run-local 解析由 `RunRequestContext` 与 Web Tool Adapter 负责。

### 引用与预算

模型在事实附近输出短 token `[S1]`。终态 Source ledger 只把当前 Run 已知的 token 渲染为
`[S1](<url>)`；未知 Source ID 拒绝发布。它不再禁止普通 raw URL/Markdown，也不会在模型漏写
Source ID 时自动追加来源列表。

Runtime 不按总预算的四分之一预截断每条搜索摘要，也不按旧结构协议估算正文。Tool Adapter 先
构造完整 `ToolResultV1` 模型投影，再按当前 Run 实际剩余 byte budget 从低优先级来源开始简单
裁剪 `content`。标题与 Source ID 不参与正文裁剪；若仅固定结构就无法容纳，返回
`WEB_SOURCE_BUDGET_EXHAUSTED`。

Tool observability metadata 只保留：

```text
source_count, output_bytes, truncated
```

## 不变量

- Source ID 只在一个 Run 内有效，按来源 URL 稳定复用，Run 关闭后不可解析。
- 模型不能向 `web_fetch` 提交 URL；服务端 URL 只来自同一 Run 的 `web_search` 结果。
- Web Research 外部 HTTP 只连接固定 Tavily `/search` 与 `/extract`。
- `web_fetch` 不返回整篇网页，最多返回五个约 500 字符的 query-ranked chunks。
- 只有 Tool Adapter 负责最终模型可见 byte budget；Runtime 不进行 `/4` 预截断。
- 不保留旧 Evidence identity、Destination Capability、direct fetch 或兼容 Adapter。

## 结果

模型上下文主要用于搜索摘要和定向 chunks，协议开销显著下降；fetch 不再因 HMAC capability
链缺失而被 Guardrail 拒绝。代价是 Source ID 不是持久引用，且 Tavily Extract 的可用性直接
决定深度抓取是否成功。Provider failure 继续以 typed `WEB_SEARCH_UNAVAILABLE`、
`WEB_FETCH_UNAVAILABLE` 或 invalid-response code 公开，不能伪装成空知识。
