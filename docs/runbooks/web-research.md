# Web Research Runbook

Web Research 使用 Tavily Keyless 的固定 `/search` 与 `/extract` 端点。模型先从 `web_search`
获得当前 Run 的 `S1`、`S2` 等 Source ID；需要更具体内容时再调用
`web_fetch(source_id, query?)`。应用不直接抓取目标网页，也不返回整页正文。

架构依据见 [ADR-0026](../adr/0026-run-local-source-id-and-tavily-extract.md)。

## 配置

`.env` 的最小启用配置：

```dotenv
WEB_RESEARCH_ENABLED=true
WEB_RESEARCH_REQUEST_TIMEOUT_SECONDS=10
WEB_RESEARCH_DEFAULT_SEARCH_RESULTS=5
WEB_RESEARCH_MAX_SEARCH_RESULTS=12
WEB_RESEARCH_MAX_QUERY_BYTES=4096
WEB_RESEARCH_MAX_URL_BYTES=4096
WEB_RESEARCH_MAX_RESPONSE_BYTES=2097152
WEB_RESEARCH_MAX_TITLE_BYTES=512
WEB_RESEARCH_MAX_CONTENT_BYTES=3072
WEB_RESEARCH_MAX_TOTAL_SOURCE_BYTES=3072
WEB_RESEARCH_MAX_CONCURRENCY=4
WEB_RESEARCH_USER_AGENT=SuperMew-WebResearch/2.0
```

`WEB_RESEARCH_ENABLED` 只作为数据库控制面的首次默认值；之后由管理员在 **Skill / Tool**
控制面切换。Tavily Keyless 不需要 API Key，Registry 仍用内部 `WEB_RESEARCH_RUNTIME` capability
表示当前进程已安装可用 Runtime。

`WEB_RESEARCH_MAX_TOTAL_SOURCE_BYTES` 是单个 Run 内所有 Web ToolResult 的累计模型可见预算。
Tool Adapter 在完整封装 `ToolResultV1` 后按实际剩余字节裁剪 `content`；Runtime 不按结果数或
四分之一预算预截断每条摘要。

## 正式接口

`web_search`：

```json
{
  "query": "Python 3.15 free-threading changes",
  "max_results": 5,
  "allowed_domains": ["python.org"]
}
```

模型可见成功结果只包含：

```json
{
  "sources": [
    {
      "source_id": "S1",
      "title": "What’s New In Python 3.15",
      "content": "Python 3.15 improves ..."
    }
  ],
  "truncated": false
}
```

`web_fetch`：

```json
{
  "source_id": "S1",
  "query": "Python 3.15 free-threading performance limitations"
}
```

`query` 可省略；服务端会使用产生 `S1` 的原始 search query。Runtime 向 Tavily Extract 固定发送
`chunks_per_source=5` 与 `extract_depth=basic`。最多消费五个 chunk，每个 chunk 在进入模型
上下文前限制为约 500 字符。

模型引用格式为 `[S1]`。终态会把当前 Run 已知 Source ID 渲染为 `[S1](<url>)`。Source ID
不能跨 Run 使用，Run 关闭后映射被清理。

## 离线验证

从仓库根目录运行：

```bash
uv run --no-sync pytest -q \
  tests/test_web_research_contracts.py \
  tests/test_web_research_runtime.py \
  tests/test_web_citations.py \
  tests/test_web_tools.py \
  tests/test_agent_runtime.py

uv run --no-sync python -m backend.tools.registry_cli validate
```

重点断言：

- `web_fetch` schema 只有 `source_id` 与可选 `query`，没有 URL 或旧 Evidence identity；
- 搜索投影没有 hash、time、domain、snippet、citations 或 Web Research schema version；
- `web_fetch` 只发送 Tavily `/extract` POST，请求参数固定；
- 五个以上或超过 500 字符的 chunks 被有界处理，不会退回整页；
- 两个 Run 都可拥有自己的 `S1`，彼此不能解析；
- 最终预算裁剪发生在 `ToolResultV1` 封装后，并且只裁剪 `content`。

## 在线冒烟测试

在线检查需要显式启用 Web Research，并允许访问 Tavily。普通 pytest 不联网。

1. 在控制面启用 Web Research，确认 readiness 为 ready。
2. 激活 `/web-research`，搜索一个公开主题，确认结果含 `S1`、`title`、`content`，不含 URL 和旧
   identity 字段。
3. 调用 `web_fetch(source_id="S1")`，确认返回的是少量相关 chunks，而不是整页正文。
4. 再用更具体的 query 调用另一个 Source ID，确认 Extract 内容随 query 聚焦。
5. 最终回答使用 `[S1]`，确认发布内容渲染为对应链接。
6. 新建另一个 Run 直接 fetch `S1`，应返回 `WEB_SOURCE_NOT_FOUND`。

在线 smoke 只能证明当前 Tavily 协议与网络可用，不能替代离线契约、预算和 Run 隔离测试。

## 稳定错误

常见错误：

- `WEB_SOURCE_NOT_FOUND`：Source ID 不属于当前 Run，或 Run 已关闭；
- `WEB_SOURCE_BUDGET_EXHAUSTED`：当前 Run 剩余 ToolResult 字节不足；
- `WEB_SEARCH_UNAVAILABLE`：Tavily Search 临时不可用；
- `WEB_FETCH_UNAVAILABLE`：Tavily Extract 临时不可用；
- `WEB_INVALID_SEARCH_RESPONSE` / `WEB_INVALID_EXTRACT_RESPONSE`：Provider 返回结构不符合协议；
- `WEB_DEADLINE_EXCEEDED`：Run deadline 已到。

Provider failure 不应被解释为无搜索结果，也不要在模型侧重复调用同一 Tool 规避失败。

## 禁用与恢复

紧急禁用时在 **Skill / Tool** 控制面关闭 Web Research。新 Run 将不再获得
`WEB_RESEARCH_RUNTIME`，`web_search` 与 `web_fetch` 不会披露；已创建 Run 的冻结能力语义按现有
Run 生命周期处理。

恢复前先运行离线验证，再完成一次真实 Tavily `/search` + `/extract` smoke。不要恢复旧
`evidence_id`、Destination Capability、direct page fetch 或双接口兼容路径。
