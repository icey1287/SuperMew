# Run Event Frontend AGENTS.md

本文件适用于 `frontend/src/events/`，继承根目录与 `frontend/AGENTS.md`。协议真源是 `../../../contracts/run_event_v1.json`，生成类型位于 `../types/generated/run-event-v1.ts`，禁止手改生成类型。

## 模块职责

- `runEventStream.ts`：解析 SSE frame、验证 Event v1 envelope、创建/恢复连接、维护连续 cursor、处理可重试断流。
- `runEventReducer.ts`：把一个已验证、连续、属于同一 Run/Thread 的 Event 确定性投影到 `RunEventState`。
- `*.spec.ts`：锁定 parser、重连、sequence、终态、HITL、Tool/Retrieval timeline 和公开错误语义。

Stream 负责 transport；Reducer 负责领域投影；Store 负责请求/连接生命周期。不要把三者合并成一个难以测试的可变对象。

## Envelope 与 SSE 解析

Event v1 必须包含：`schema_version=1`、正整数 `sequence`、非空 `event_id/run_id/type/timestamp`、合法 `thread_id` 和 object `data`。

- CRLF/CR 正规化后按空行分 frame；合并多个 `data:` 行再 JSON parse。
- 缺字段、非法类型、JSON 损坏、Run/Thread 不匹配或 sequence gap 是 `STREAM_PROTOCOL_ERROR`。
- 重复/旧 sequence 可以忽略；不得重复投影。
- 未知 Event **type** 在已知 schema version 内记录到 `unknownEventTypes`，不猜测状态变化。
- 未知 schema version 不前向猜测，必须 fail-closed 并等待 Contract 升级。

## Cursor 与重连

- `lastSequence` 只在 `onEvent` 成功返回后推进；callback/reducer 抛错时不能吞掉并推进 cursor。
- 请求用 `Last-Event-ID` 从最后成功 sequence 重放。
- 接收 `n+2` 而缺 `n+1` 时中止当前连接并从 `n` 重连；不能把 state 标记 gap 后继续消费后续事件。
- 退避有上限并尊重公开 `Retry-After`；Abort 立即停止等待。
- 服务端在 terminal/pause 前关闭 stream 属于可重试断流；非 retryable 公开错误直接传播。
- Reader cancel 是 best effort，只停止观察；注释和命名不得暗示它取消后端 Run。

## Reducer 不变量

- Event 必须属于 state 的 `runId/threadId`，且 `sequence == lastSequence + 1`。
- Reducer 对同一输入保持确定性，不读取时间、网络、全局 Store 或 DOM。
- terminal Event 设置 `terminalSequence` 后拒绝所有后续 Event，包括迟到 delta。
- `message.delta` 只追加流文本；`message.completed` 落定正文、Message 状态和公开 RAG Trace。
- Run terminal 与 Message terminal 分开处理；不要因其中一个缺失而在前端伪造另一个。
- `hitl.required` 保存 token/checkpoint/prompt/options 并进入 `waiting_input`；`hitl.resumed` 恢复同一 state。
- Tool timeline 以稳定 `tool_call_id` 优先关联；Artifact 以 `artifact_id` 去重。
- Rerank fallback 是 warning，不把整个 Run 标记 failed；Tool denied/failed 保留 typed error。
- `activeDurationMs` 仅累计权威 running 区间；无效/倒退 timestamp 不产生负数。

## Contract 变更流程

1. 修改 `../../../contracts/run_event_v1.json`，兼容性破坏新增版本而非原地改变 v1。
2. 在仓库根运行 `uv run python scripts/generate_contract_types.py`。
3. 更新 stream parser、reducer、Store/组件和后端 Event producer。
4. 运行生成检查、后端 contract 测试及：

```bash
cd frontend
npm run test:unit -- src/events/runEventStream.spec.ts
npm run test:unit -- src/events/runEventReducer.spec.ts
npm run typecheck
```

若 npm script 不接受文件透传，使用项目当前 Vitest 命令运行对应 spec；不要改 package script 只为迁就一次命令。

## 必测场景

- SSE frame 被任意 chunk 边界拆分；CRLF、多 data line、坏 JSON。
- 创建流响应头缺失/非法。
- 重复 Event、Run/Thread 串号、sequence gap、重连后重放。
- `onEvent` 抛错时 cursor 不前进。
- abort、retryable/non-retryable 关闭、Retry-After。
- message delta/completed、Run completed/failed/cancelled 的组合顺序。
- HITL required/resumed、cancelling warning。
- Rerank warning、Tool denied/failed、Artifact 去重、unknown type。
- terminal 后迟到 Event 被拒绝。

## Code Review Rules

### 先推进 cursor 后投影

- 阻止在 reducer/callback 成功前更新 cursor。
  安全路径：先调用 `onEvent`，成功后原子更新 sequence；失败让重连重放同一 Event。

### 缺口继续消费

- 阻止标记 `hasGap` 后仍接受更高 sequence。
  安全路径：立即关闭并从最后连续 cursor 补放。

### 断流即终态

- 阻止把 reader EOF、Abort 或组件卸载映射成 completed/cancelled。
  安全路径：只由 terminal Event 落定；transport 状态单独保存。

### Event data 未经验证直达 UI

- 阻止对 `data` 字段宽泛断言后渲染 raw object/HTML/错误正文。
  安全路径：安全 extractor、公开错误规范化和组件层清洗。
