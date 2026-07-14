# SuperMew contracts

`run_event_v1.json` 是运行事件的唯一真源。修改 schema 后执行：

```bash
uv run python scripts/generate_contract_types.py
```

CI 使用 `--check` 验证 Python 与 TypeScript 生成文件没有漂移。兼容性破坏必须
新增 schema version，不能原地改变 v1 的既有字段或事件语义。
