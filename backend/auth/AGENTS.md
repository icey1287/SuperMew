# Auth and Ingress Security AGENTS.md

本文件适用于 `backend/auth/`，也应由修改 `backend/rate_limits/`、`backend/security/`、认证 route 或前端认证生命周期的 Agent 主动读取。继承根目录和 `backend/AGENTS.md`。

修改前阅读：

- `../../docs/adr/0022-browser-auth-and-inbound-rate-limits.md`
- `../../docs/runbooks/auth-token-lifecycle.md`
- `../../docs/runbooks/repository-quality-gates.md`

## 浏览器 credential 模型

- **Access Token**：短期签名 Bearer，含 `iat`/`jti`；浏览器只存当前页面内存，不写 Cookie、`localStorage` 或持久缓存。
- **Refresh Token**：高熵 opaque credential，只通过 HttpOnly `Path=/auth` Cookie 传输；服务端只保存 SHA-256 hash。
- 每次成功 refresh 都 rotation；自然有效期内已撤销 token 的 replay 会撤销该用户全部活跃 refresh credential。
- 服务端先判 natural expiry，再判 replay。自然过期 token 不得触发用户级撤销；ledger 在过期后按独立 retention window 保留并由 cleanup 删除。
- Access/Refresh token、Cookie、密码、raw IP、username 和 Bearer 不进入日志、Event、Rate Limit key 或公开错误。

前端 `frontend/src/auth/session.ts` 的 generation、single-flight、Web Locks、subject check 和 revocation tombstone 是认证协议的一部分；后端变更必须同步验证前端竞态。

## 数据库锁序与事务

- Refresh rotate、当前设备 logout、logout-all 的写事务先锁 User，再锁定/写入该用户 RefreshToken。
- token 签发、旧 token 撤销和新 token 持久化必须处于同一事务；失败不能留下双活 credential。
- 旧 bcrypt/bcrypt-sha256 仅允许在成功登录事务中只读验证并单向升级 PBKDF2；它不是第二套密码写入 Interface。
- Cleanup 不在 API 热路径运行，只删除 `expires_at + retention` 已越过的 ledger 行。

## Unsafe auth POST 顺序

Starlette middleware 顺序是安全语义，不是风格：

1. 外层 metadata guard 校验 same-origin/显式跨源 allowlist、Fetch Metadata、JSON media type、Content-Length 语法和声明的 16 KiB 上限；拒绝不能消耗 quota。
2. Rate Limit 原子计费。
3. 内层 streaming body guard 在计费后累计真实 body，阻止无/伪 Content-Length 超过 16 KiB。
4. Route 才进入 username 规范化、密码校验与 credential mutation。

修改 `backend/app.py` 的 `add_middleware` 顺序时，必须理解 Starlette 后添加在外层的行为，并运行顺序锚点测试。

## Rate Limit

- Login/Register 在 PBKDF2/bcrypt 前分别消费直接 client IP 与 `IP + NFKC/trim/casefold username` 两个 bucket；复合 identity 一次消耗两个 quota unit。
- identity 在进入 Redis key 前使用独立 `RATE_LIMIT_HMAC_KEY`；该 key 与 `JWT_SECRET_KEY` 必须不同。
- 生产使用 Redis、多实例共享、Redis `TIME` 和原子 Lua；存储不可用返回 typed 503，不能 fail-open。
- 只读取已经过可信代理修正的 ASGI `scope.client`。Module 不直接相信任意 `X-Forwarded-For`。
- 除明确静态、health、docs、preflight skip 外，未知和未来动态 path 默认进入 general policy。

## Origin、CORS 与安全响应头

- same-origin 始终可信；跨 origin 只有 `CORS_ALLOW_CREDENTIALS=true` 且命中明确 HTTPS allowlist 时可信。`*` 不得与 credential 组合。
- Same-site 不是 auth 信任边界。
- `/auth` 成功、401、429、503 均 `no-store`。
- `Referrer-Policy: no-referrer`、`nosniff`、`X-Frame-Options: DENY` 和受限 Permissions-Policy 全局应用。
- CSP 只应用正式前端 HTML；不要让 FastAPI `/docs`、`/redoc` 或 JSON 响应继承前端 CSP。

## 同步/异步边界

登录和注册包含同步 SQLAlchemy 与 PBKDF2，因此 route 保持同步，由 FastAPI 在线程池执行整个 handler。不要把它们改成 `async def` 后继续调用同步数据库/密码逻辑，也不要把半条路径放入 `to_thread` 导致锁和事务跨线程漂移。

## 聚焦验证

```bash
uv run --no-sync pytest -q \
  tests/test_auth_routes.py \
  tests/test_auth_http.py \
  tests/test_auth_token_lifecycle.py \
  tests/test_auth_cleanup.py \
  tests/test_auth_rate_limit.py \
  tests/test_rate_limits.py \
  tests/test_rate_limit_http.py \
  tests/test_rate_limit_runtime.py \
  tests/test_rate_limit_app_integration.py \
  tests/test_security_headers.py \
  tests/test_settings_security.py
```

涉及真实锁序时额外运行：

```bash
AUTH_POSTGRES_TEST_URL=<专用临时 PostgreSQL DSN> \
  uv run --no-sync pytest -q tests/test_auth_postgres_integration.py
```

涉及 Redis Lua/TTL/多连接共享时运行 `uv run --no-sync python -m scripts.smoke_redis_compat`。禁止指向共享或生产数据库/Redis。

前端同时运行 `frontend/src/auth/session.spec.ts` 对应单测；验证页面恢复、同标签 single-flight、跨标签锁、等待锁后重检、subject 一致、旧 401 不跨账号重试、原请求最多重试一次和退出竞态。

## Code Review Rules

### Credential 持久化或泄露

- 阻止 Access Token 写入持久存储、Refresh Token 进入 JSON、raw credential 写日志/状态。
  安全路径：内存 Bearer + HttpOnly opaque Cookie + 服务端 hash 与脱敏公开错误。

### Replay/rotation 竞态

- 阻止 rotate/logout 分事务、锁序不一致、等待中的 refresh 覆盖新登录或退出。
  安全路径：User→RefreshToken 锁序、generation/tombstone、同一事务和 PostgreSQL 并发测试。

### 昂贵路径前置保护被绕过

- 阻止在 Origin、body 和两层 Rate Limit 之前执行 PBKDF2、数据库 credential mutation 或读取无界 body。
  安全路径：保持既定 middleware/route 顺序并运行锚点回归。

### 信任代理头或 fail-open

- 阻止直接解析客户端 `X-Forwarded-For`、Redis 故障放行、未知 path 绕过 policy。
  安全路径：可信 ProxyHeaders 先修正 `scope.client`，Limiter 异常 typed 503，matcher 默认保护。
