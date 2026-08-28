# 浏览器认证与 Refresh Ledger 运维

本 Runbook 负责 ADR-0022 的部署与运行知识：浏览器 Access/Refresh Token 生命周期、auth HTTP
入口保护、PostgreSQL refresh ledger 保留/清理，以及生产 Secret 与可信代理前提。

## 浏览器不变量

- Access Token 只存在于当前页面 JavaScript 内存，通过 `Authorization: Bearer` 发送；不得写入
  `localStorage`、`sessionStorage`、IndexedDB、URL 或 Cookie。
- Opaque Refresh Token 只由 HttpOnly、`Path=/auth` Cookie 承载；响应 JSON 不返回 raw token，
  PostgreSQL 只保存 SHA-256 hash。
- 同一标签页 refresh 使用共享 promise。支持 Web Locks 的浏览器还用
  `supermew-auth-refresh-v1` 串行化跨标签页轮换；获得锁后必须重检本标签页 generation 与
  revocation tombstone。
- 已登录 refresh 的响应 username 必须与 attempt 主体一致。Axios 只对仍属于同一 Access Token
  与 username 的 401 刷新并重试一次；旧账号的 401 不能在切换账号后携带新 credential 重试。
- logout 先建立持久到 `sessionStorage` 的 revocation tombstone、清空内存状态并等待在途 refresh
  response 落定，再在同一 Web Lock 下撤销最新 Cookie。服务端撤销失败时保留 tombstone，页面
  下次恢复先重试 logout，不能静默恢复旧身份。

Web Locks 是跨标签页协调层；不支持该 API 的浏览器仍有单标签 promise 与服务端 rotation/replay
保护，但不能获得客户端跨标签串行保证。

## Auth HTTP 入口

所有 `/auth` unsafe POST 先经过外层 metadata guard：

1. request origin 与应用 origin 相同的 same-origin 请求始终可信。
2. 跨 origin 只有在 `CORS_ALLOW_CREDENTIALS=true` 且 normalized origin 命中显式
   `CORS_ORIGINS` allowlist 时可信；空 allowlist 表示 same-origin-only，禁止 `*`。
3. 优先检查 `Origin`，缺失时使用 Referer。畸形来源、`Origin: null`、没有可信来源的
   `Sec-Fetch-Site: same-site|cross-site` 返回 403；完全没有浏览器来源 metadata 的 CLI/服务间
   客户端可以继续使用。
4. login/register 必须使用 `application/json` 或 `application/*+json`；校验 Content-Length
   语法与声明的 16 KiB 上限。上述 metadata 拒绝发生在 Rate Limit 前，不消耗受害者 quota。

Rate Limit 随后先消费稳定 host/subject quota，内层 body guard 才流式累计实际 body。无
Content-Length 或声明值小于实际值的 chunked body 超过 16 KiB 时返回 413；任意 auth POST 只要
body 非空，就必须使用 JSON media type，refresh/logout/logout-all 允许空 body。这个顺序避免
慢速/分块 body 在未计费时占用连接和内存；最终 body 仍会在 route、PBKDF2 与 refresh mutation
前拒绝或回放。

所有 `/auth` 响应，包括成功、401 与 429，都必须返回：

```text
Cache-Control: no-store
Pragma: no-cache
```

Vite 开发服务器固定监听 3000，本地默认 allowlist 为：

```text
http://localhost:3000
http://127.0.0.1:3000
```

生产 same-origin-only 部署可将 `CORS_ORIGINS` 留空；跨 origin 前端必须替换为真实 HTTPS
origin，不能把开发 origin 或通配符带入部署。Web Locks 只在同一浏览器 Origin 内共享，生产
credentialed CORS 最多允许一个 canonical 前端 Origin；多个前端必须使用独立 Cookie/API host，
或先实现服务端 refresh family 并发协议。

## Refresh ledger 事务

登录、注册和 refresh 签发新 token；refresh 原子撤销旧 token 并插入 replacement。仍在自然
有效期内且已经撤销的 token 再次出现才视为 replay，服务端在同一用户范围撤销所有活跃 refresh
token。服务端先判断 `expires_at` 再判断 `revoked_at`；过期 token 只返回 expired，不能借很久
以前泄漏的 token 触发用户级撤销。
`/auth/logout` 撤销当前 Cookie，`/auth/logout-all` 撤销当前用户全部活跃 token。

所有在线生命周期写路径统一按以下顺序获取数据库锁：

```text
User row FOR UPDATE
  → RefreshToken row FOR UPDATE / insert / user-scoped revoke
```

rotate/logout 可先无锁读取 token hash 对应的 user_id，只用于定位 User；随后仍必须先锁 User、
再锁 RefreshToken。issue、rotate、logout 与 logout-all 都遵守该顺序，禁止新增相反锁序。
PostgreSQL 集成测试负责证明 rotate/replay/logout-all 多实例并发不会留下活跃 token 或形成死锁。

## 旧密码哈希迁移

新注册只写 PBKDF2-SHA256。既有 bcrypt / bcrypt-sha256 账号在密码验证成功后，会在签发 Access
和 Refresh credential 的同一数据库事务中改写为 PBKDF2；错误密码、签发失败或事务回滚都必须
保留原哈希且不产生 Refresh Token。该路径只迁移密码数据，不提供旧 Token、Cookie 或 Session
协议。

部署方可以只统计哈希格式，不读取或导出具体值：

```sql
SELECT count(*)
FROM users
WHERE password_hash NOT LIKE 'pbkdf2_sha256$%';
```

所有环境返回 0 或遗留账号已完成受控密码重置后，才可在后续变更中删除历史验证器和 bcrypt
依赖。

## Ledger 保留与清理

`AUTH_REFRESH_LEDGER_RETENTION_DAYS` 默认 30。清理 cutoff 是：

```text
RefreshToken.expires_at <= 当前时间 - retention days
```

因此任何记录都不得在 `expires_at` 前删除；自然过期后仍完整保留 retention window，只用于
forensic/audit evidence 与运维诊断，不再参与用户级 replay 撤销。API refresh/login/logout 热
路径不执行清理，部署方必须使用独立 scheduler 调用：

```bash
uv run --no-sync python -m backend.auth.cleanup
```

安装项目后也可使用 console entry point：

```bash
supermew-auth-cleanup
```

命令参数：

```text
--batch-size    单批上限，默认 1000
--max-batches   单次运行批次数上限，默认 100
```

命令没有 dry-run，只删除超过自然过期时间与保留窗口的记录；每批独立提交，最终输出
`purged_refresh_tokens=<count>`。调度频率、失败重试、运行日志与告警属于部署 supervisor 的独立
责任，不能假设 API 或 Index Job worker 会代为执行。

## 生产检查清单

- `AUTH_REFRESH_COOKIE_SECURE=true`；`SameSite=None` 只能与 Secure 同时使用。
- `JWT_SECRET_KEY` 至少 32 字符且非 placeholder。
- `ADMIN_INVITE_CODE` 留空表示禁用公开 admin 注册；启用时与 JWT/Rate Limit Secret 分离。
  邀请码使用 constant-time compare，但部署仍不得记录或回显它。
- `RATE_LIMIT_ENABLED=true`、`RATE_LIMIT_BACKEND=redis`，稳定的
  `RATE_LIMIT_HMAC_KEY` 至少 32 字符且与 JWT Secret 分离。
- 反向代理只允许受控 hop 写 forwarded headers，并由可信 ProxyHeaders/forwarded allowlist 在
  Rate Limit 前修正 `scope.client`。应用不信任任意 `X-Forwarded-For`；未修正时公网用户会共享
  代理 IP bucket，直接信任客户端头则可伪造身份绕过。
- refresh 与当前设备 logout 使用 client host 粗限额 120/min；logout-all 使用已验证 Access
  subject。raw access/refresh token 不作为 quota identity；未知动态 path 默认进入 general policy。
- 全局响应头包含 `X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`nosniff` 与受限
  `Permissions-Policy`；CSP 只用于正式前端 HTML，FastAPI Docs 不附加 CSP。

## 验证

```bash
uv run --no-sync pytest -q \
  tests/test_auth_routes.py \
  tests/test_auth_http.py \
  tests/test_auth_token_lifecycle.py \
  tests/test_auth_cleanup.py \
  tests/test_auth_rate_limit.py \
  tests/test_rate_limit_http.py \
  tests/test_rate_limit_app_integration.py \
  tests/test_security_headers.py \
  tests/test_settings_security.py
```

真实 PostgreSQL 并发锁序测试需要专用临时数据库：

```bash
AUTH_POSTGRES_TEST_URL=<专用 PostgreSQL DSN> \
  uv run --no-sync pytest -q tests/test_auth_postgres_integration.py
```

前端在 `frontend/` 中运行 `npm run test:unit`，并确认 Web Locks、generation/tombstone、主体一致、
旧 401 不跨账号重试和失败 logout 跨刷新恢复场景均通过。
