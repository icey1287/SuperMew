# Contracts AGENTS.md

本文件适用于 `contracts/` 及其生成消费者。继承根目录 `AGENTS.md`。

## 真源与生成物

- `run_event_v1.json`：Run Event envelope/type 的唯一真源。
- `tool_result_v1.json`：Tool Result/Artifact 的唯一真源。

修改后运行：

```bash
uv run python scripts/generate_contract_types.py
uv run python scripts/generate_contract_types.py --check
```

确定性输出：

```text
backend/events/generated/run_event_v1.py
frontend/src/types/generated/run-event-v1.ts
backend/tools/generated/tool_result_v1.py
frontend/src/types/generated/tool-result-v1.ts
```

不要直接编辑这些生成文件。生成器不支持的新字段/结构必须同时更新生成器测试，而不是在输出上打补丁。

## 兼容性规则

- 已发布 v1 的字段、required 集、type、枚举语义和 invariant 不做原地破坏；不兼容变化新增 schema version 和并行解码/迁移计划。
- 新 Event type 在 v1 enum 中增加时，后端 producer、前端 reducer 和测试同一变更集更新；旧前端可把未知 type 记录为前向兼容，但未知 schema version 必须拒绝。
- JSON object 优先 `additionalProperties: false`，开放 metadata/data 的位置必须明确是受控扩展面。
- ID、URI、时间、大小和 error code 使用有界 pattern/length/range；不要让 Contract 接受宿主绝对路径或无界字符串。
- Contract 描述公开可交换事实，不承载 Secret、原始异常、endpoint、私有推理或进程对象。

## Tool Result v1

- 成功结果：`success=true`、`error_code=null`、`retryable=false`。
- 失败结果：稳定非空 `error_code`，并明确 `retryable`。
- `data`、artifact metadata、observability metadata 只包含 JSON 值。
- Artifact `uri` 只能是 opaque artifact URI 或应用内下载地址；不能是宿主机绝对路径。
- JSON Schema 只验证 URI 形状；服务端解析时仍须鉴权并验证 URI 与 `artifact_id` 的资源绑定。

## Run Event v1

- `sequence` 在单个 Run 内单调、从 1 开始；`event_id` 不替代 sequence。
- `run_id/thread_id` 是前端隔离和重放边界；producer 不得省略或借用其他 Run 身份。
- `data` 是按 Event type 解释的公开 payload；敏感审计细节保留服务端。
- terminal Run Event、`message.completed`、HITL 和 Tool/Artifact Event 的语义由持久化后端产生，Contract 不授权前端推测状态。

## 变更清单

1. 说明兼容性：additive、behavioral 还是 breaking。
2. 修改 schema 真源。
3. 更新生成器（若需要）并重新生成全部 Python/TypeScript 输出。
4. 更新 producer、consumer、parser/reducer 和 schema/serialization 测试。
5. 运行后端完整生成检查与前端 typecheck/unit tests。
6. 用户/集成方可见时更新 README；架构语义变化更新 ADR/CONTEXT。

## Code Review Rules

### 手改生成物

- 阻止只改 `generated/` 或前端类型而不改 schema。
  安全路径：修改 JSON Schema/生成器并提交全部确定性输出。

### 原地破坏 v1

- 阻止删除 required 字段、改变既有含义、收窄合法值或让旧消费者误解释新数据。
  安全路径：additive 字段/type，或新增版本与显式迁移。

### Contract 泄露实现细节

- 阻止绝对路径、原始异常、Secret、数据库主键细节或私有推理进入公开 payload。
  安全路径：opaque identity、稳定 error taxonomy 和脱敏 metadata。
