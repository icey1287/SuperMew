# 数据库迁移

应用启动不会修改 schema。部署前显式执行：

```bash
uv run alembic upgrade head
```

随后校验当前 revision：

```bash
uv run python -c "from backend.infra.database import assert_schema_current; assert_schema_current()"
```

Document Version identity 收敛迁移是单向迁移：发现仍为 current/pending、尚未完成物理清理或缺少安全删除前提的数据时会 fail-closed。迁移完成后不得恢复被删除的字段、Interface、Adapter、Implementation 或运行时双读。

生产环境的备份、前端构建、三进程启动和发布顺序见
[`docs/runbooks/deployment.md`](../docs/runbooks/deployment.md)。
