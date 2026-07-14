# Schema migrations

空数据库直接执行：

```bash
uv run alembic upgrade head
```

从旧版 `create_all()` 数据库升级时，先确认存在 `users`、`chat_sessions`、
`chat_messages` 和 `parent_chunks` 四张旧表，再执行：

```bash
uv run python -m backend.db.migrate adopt-legacy
```

该命令只会把旧结构标记为 `0001_legacy`，随后执行增量迁移。应用启动不再
静默修改 schema；数据库版本落后时会拒绝进入 ready 状态。
