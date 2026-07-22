# SQL Assistant 运维手册

## 启用前准备

SQL Assistant 默认关闭，只支持独立 PostgreSQL 只读账号。不要把应用的 `DATABASE_URL`
复制为 SQL DSN，也不要让查询角色成为 relation owner。

下面示例中的 owner 与 reader 必须是不同角色，授权范围按实际业务表收窄：

```sql
CREATE ROLE analytics_owner NOLOGIN;
CREATE ROLE supermew_sql_reader
  LOGIN
  PASSWORD '<由 Secret 管理系统生成>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

GRANT CONNECT ON DATABASE analytics TO supermew_sql_reader;
GRANT USAGE ON SCHEMA analytics TO supermew_sql_reader;
GRANT SELECT ON TABLE analytics.orders, analytics.customers
  TO supermew_sql_reader;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE TEMPORARY ON DATABASE analytics FROM PUBLIC;
ALTER ROLE supermew_sql_reader SET default_transaction_read_only = on;

ALTER TABLE analytics.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytics.customers ENABLE ROW LEVEL SECURITY;
-- 按租户/团队作用域建立最小 SELECT policy；不要用宽泛 USING (true) 代替业务隔离。
CREATE POLICY orders_reader_scope ON analytics.orders
  FOR SELECT TO supermew_sql_reader
  USING (<服务端维护的只读作用域谓词>);
CREATE POLICY customers_reader_scope ON analytics.customers
  FOR SELECT TO supermew_sql_reader
  USING (<服务端维护的只读作用域谓词>);
```

relation owner 负责建表和迁移；reader 只获得 `CONNECT`、指定 schema 的 `USAGE` 与指定表的
`SELECT`。所有 allowlist table/partitioned table 都必须启用 RLS，并用 reader 账号验证策略；
禁止授予 `BYPASSRLS`。若开放普通 view，必须同时设置 `security_invoker=true` 与
`security_barrier=true`，且其底层 relation 也必须通过 allowlist、权限与 RLS 检查。
materialized/foreign view 与 sequence 不受支持。

## 配置

推荐在管理员侧栏 **Skill / Tool → SQL Assistant 配置** 中维护开关、DSN Secret 名称、
expected role、allowlist、敏感列和查询/结果预算。前端只保存类似
`ANALYTICS_READER_DSN` 的环境变量名称；真实 DSN 必须由 Secret 管理系统注入 API 与 worker
进程环境，控制面不会存储或返回 DSN。

`.env` 中的开关、allowlist 与预算只在控制面记录不存在时作为首次种子。数据库已经初始化后，
修改这些环境变量不会覆盖前端保存的目标配置；连接池、lock timeout、AST 等未开放字段仍由
服务端环境配置。前端保存后会立即重建并应用 SQL Assistant Runtime。

最小配置：

```dotenv
SQL_ASSISTANT_ENABLED=true
SQL_ASSISTANT_DSN=postgresql://supermew_sql_reader:<secret>@db.example/analytics?sslmode=require
SQL_ASSISTANT_EXPECTED_ROLE=supermew_sql_reader
SQL_ASSISTANT_ALLOWED_SCHEMAS=analytics
SQL_ASSISTANT_ALLOWED_TABLES=analytics.orders,analytics.customers
SQL_ASSISTANT_SENSITIVE_COLUMNS=analytics.customers.email
```

`SQL_ASSISTANT_ALLOWED_TABLES` 必须使用 `schema.table`；确需整个 schema 时可显式使用
`schema.*`，但上线评审应优先列出单表。通配符会在 catalog refresh 时展开为显式 relation
snapshot；新表不会静默获得访问权。strict privilege check 还会拒绝 reader 对 snapshot 外
业务表的额外 `SELECT` 权限。敏感列使用 `schema.table.column`，查询返回前统一掩码。标识符
仅支持普通 PostgreSQL identifier，不接受 quoted identifier。catalog 当前只接受
`pg_catalog` 内建 base type；自定义 domain、composite、enum、range、extension type 与
schema-qualified custom operator 都会令 readiness/query fail-closed。

预算默认值和完整变量见 `.env.example`。重要关系：

- lock timeout 必须小于 statement timeout；
- fetch size 不得大于最大返回行数；
- 单 cell 上限不得大于总结果上限；
- 返回上限不得大于 EXPLAIN estimated bytes 上限；
- pool min 不得大于 pool max。

配置验证失败时不要通过放大预算绕过高成本查询。前端显示“Secret 未配置”时，先在目标进程
环境中注入所选名称；如果 Secret 是由部署系统新增或修改的，需要重启目标进程让它重新读取
环境。这是环境变量加载要求，不是保存配置本身的发布步骤。不要把 DSN 粘贴到浏览器表单。

## 验证

```bash
uv run --frozen python -m backend.tools.registry_cli validate
uv run --frozen python -m backend.tools.registry_cli list-skills \
  --role admin --secret-name SQL_ASSISTANT_DSN
uv run --frozen pytest -q tests/test_sql_policy.py tests/test_sql_assistant.py \
  tests/test_sql_postgres_integration.py tests/test_sql_tools.py
```

启动后检查 readiness，再用 `/sql-assistant` 执行两类烟雾测试：

1. `sql_schema` 只能看到 allowlist 内表；
2. 小型聚合 `SELECT` 成功，DDL/DML、多 statement、锁查询、越权表和高成本查询全部返回稳定
   policy error。

同时确认 audit 中有 literal-redacted query shape fingerprint、结果规模和 outcome，但没有
DSN、密码、原始 SQL、literal、表中值或内部异常文本。

## 常见故障

### Skill 不可见

依次确认：SQL Assistant 已启用、DSN 非空、当前用户数据库角色为 `admin`、Run 允许
`private-data` network policy。任一条件缺失都应保持隐藏。

### readiness 拒绝角色

使用 DSN 登录后检查：

```sql
SELECT current_user, session_user, rolsuper, rolcreatedb, rolcreaterole,
       rolreplication, rolbypassrls
FROM pg_roles
WHERE rolname = current_user;
```

再检查 allowlist relation 的 owner；reader 不能拥有它们。不要通过改成 `postgres` 或授予
高权限解决 readiness。

### 查询超时或成本拒绝

先收窄日期、投影和聚合，检查业务索引与统计信息。只有在审计证明确为合理工作负载后，才按
最小幅度调整 statement/cost/rows/bytes 上限。不要关闭 `EXPLAIN` 门禁。

### schema 变更未出现

等待 catalog cache TTL，或在前端再次保存 SQL 配置以刷新 Runtime；确认新 relation 已加入 allowlist 且 reader 已获
`USAGE`/`SELECT`。不要直接开放 `pg_catalog` 或 `schema.*` 作为临时修复。

## 禁用与轮换

紧急禁用时在前端关闭 SQL Assistant 并保存；若控制面尚未初始化，也可用
`SQL_ASSISTANT_ENABLED=false` 作为首次种子。随后可在数据库撤销 reader 登录权限。轮换 DSN
时先更新当前配置引用的环境 Secret，滚动重启并验证 readiness；旧密码应立即失效。
Secret 值不得出现在工单、聊天、提交或命令输出中。
