# SuperMew contracts

`run_event_v1.json` 是运行事件的唯一真源，`tool_result_v1.json` 是工具返回值的
唯一真源。修改任一 schema 后执行：

```bash
uv run python scripts/generate_contract_types.py
```

CI 使用 `--check` 验证清单中的全部 Python 与 TypeScript 生成文件没有漂移。
兼容性破坏必须新增 schema version，不能原地改变 v1 的既有字段或语义。

Tool Result v1 使用成功/失败判别联合：成功结果的 `error_code` 必须为 `null` 且
不可重试，失败结果必须提供稳定的 `error_code`。`data`、artifact metadata 和
observability metadata 只接受 JSON 值。artifact 只能引用 opaque artifact URI 或
应用内下载地址，不能携带宿主机绝对路径。JSON Schema 只能校验 URI 形状；
Artifact 解析 Module 必须在鉴权后验证 URI 引用的资源与 `artifact_id` 一致。
