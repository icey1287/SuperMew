# ADR-0018：只读 SQL Assistant

- 状态：已接受
- 日期：2026-07-16

## 背景

SQL Assistant 能为运营分析提供很高的 Leverage，但模型生成的 SQL、数据库 catalog、
查询结果和错误文本都不可信。若 Tool Adapter 直接持有普通业务连接并拼接校验规则，权限、
SQL 语义、成本控制、敏感列处理和审计会散落在 Registry、Agent prompt 与数据库调用中，
形成一个接口宽、实现浅且无法证明只读的 Module。

PR-17 已建立 Tool/Skill Registry Seam。SQL 能力必须在这条 Seam 后继续 fail-closed，且不能
复用应用写库账号、把 DSN 放入 Run state，或仅凭 prompt 约束模型不执行写操作。

## 决策

建立 `backend.sql_assistant` 深 Module。它向 Tool Adapter 暴露两个小 Interface：

```text
describe_schema(tables) -> allowlist 内的 catalog snapshot
query(sql, deadline_at, cancellation_probe) -> 有界、脱敏的查询结果
```

Tool Adapter 是 request-owned Adapter，只负责把 Run deadline/cancellation 传入共享的 lazy
runtime，并返回内部 `ToolResultV1`。连接池、catalog cache、SQL policy、PostgreSQL 执行、
结果编码和审计都保留在 SQL Module 内，以获得 Locality。

### 能力门禁

`sql_schema` 与 `sql_query` 以 deferred Tool 注册，固定要求：

```text
数据库角色：admin
Secret 名称：SQL_ASSISTANT_DSN
network policy：private-data
active Skill：sql-assistant
```

SQL Assistant 默认关闭。只有 `SQL_ASSISTANT_ENABLED=true` 且独立 DSN 已配置时，默认
Secret provider 才声明 `SQL_ASSISTANT_DSN` 可用；缺角色、Secret、network policy 或 Skill
scope 时，schema 不进入模型上下文，伪造调用也在执行 Seam 被拒绝。Skill 只允许
`sql_schema` 与 `sql_query`，不能扩权到知识库、天气或未来外部工具。

### 数据库与策略

只支持 PostgreSQL。启动和 readiness 必须核验连接身份与预期角色；拒绝 superuser、
`BYPASSRLS`、`CREATEDB`、`CREATEROLE`、可登录高权限角色、允许创建临时对象的数据库权限，
以及由查询角色自身拥有的 allowlist relation。查询账号应只获得必要 schema 的 `USAGE` 与
relation 的 `SELECT`，且不是 relation owner。

每次查询依次通过：

1. 单 statement 与 SQL 长度门禁；
2. PostgreSQL AST 解析，只接受只读 `SELECT` / `WITH ... SELECT`；
3. schema/table allowlist、列解析与敏感投影策略；
4. 强制 row limit；
5. `EXPLAIN (FORMAT JSON)` 的 estimated cost/rows/bytes 门禁；
6. read-only transaction、statement timeout 与 lock timeout；
7. 增量 fetch、单 cell、总结果 bytes 和 Run cancellation 门禁；
8. 返回前敏感列掩码与 typed JSON 编码。

禁止 DDL、DML、`SELECT ... FOR UPDATE/SHARE`、多 statement、事务/会话控制、临时对象、
文件/程序访问、危险函数、自定义 operator 与通过未授权 relation 的间接绕行。allowlist
table/partitioned table 必须启用 RLS；普通 view 必须同时为 `security_invoker` 与
`security_barrier`，materialized/foreign view 和 sequence 不进入 catalog。列类型当前只接受
`pg_catalog` 的内建 base type，避免普通运算符解析到自定义 type/operator 代码。运行时和
数据库权限是两个独立防线，任一检查不确定即拒绝。

### Catalog、结果与审计

`sql_schema` 只投影 allowlist 内且当前角色可读的 schema/table/column。catalog 使用短 TTL
cache，但权限收紧、启动重建或 cache miss 都重新核验；模型不能请求任意 `pg_catalog`
内容。`schema.*` 不作为运行时通配符保留：Adapter 在启动/catalog refresh 时将它展开成
不可变的显式 relation snapshot，并逐表完成 owner/privilege 校验。strict privilege check 还会
枚举查询角色在目标数据库中可读、但不属于展开后 allowlist 的业务 relation；发现额外读取
权限即拒绝 readiness。新建表不会自动进入既有 snapshot，必须显式刷新并重新通过校验。

结果只包含有界 columns/rows 与安全统计。Tool observability metadata 仅允许查询指纹、
行列数、结果 bytes、估算成本、掩码列数、limit 状态和 catalog 计数等聚合字段。Registry
再次丢弃 descriptor allowlist 外的 metadata。原始 DSN、SQL 字符串、参数值、返回值和内部
异常不得进入日志、prompt、checkpoint 或 Run Event。

每次 schema/query 调用都写审计；query 使用服务端生成的规范化指纹关联，不以原始 SQL
作为审计键。审计中的 `query_fingerprint` 使用已抹除 literal 的 query shape；包含 literal
的 statement fingerprint 只留在 SQL Module 内部，不能跨越审计 Seam。超时、取消、policy
denied、成本拒绝与基础设施失败使用稳定错误码，不能伪装成“无数据”。

## 结果

调用方只需理解两个 Tool Interface；删除 SQL Module 后，AST 校验、数据库身份核验、成本
预算、掩码、结果编码和审计会重新散落到多个调用方，说明该 Module 提供了真实 Depth。

代价是当前只支持 PostgreSQL 和显式静态 allowlist；schema 变更需要同步配置并刷新 runtime。
写操作不会复用本 Skill。未来若新增写入能力，必须建立独立 Skill、独立数据库角色、人工审批
与更强 Sandbox，不得放宽本 ADR。
