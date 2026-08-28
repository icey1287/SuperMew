# ADR-0022：浏览器认证与入口限流

- 状态：已接受
- 日期：2026-07-17

## 背景

持久化 Thread/Run/Event 已把执行事实收进服务端，但浏览器认证仍曾把 access token 持久化到
`localStorage`。这种做法让长期存在的脚本可读凭据成为浏览器身份事实，并且无法表达刷新轮换、
当前设备退出、全部设备撤销或旧凭据重放。把长生命周期 JWT 交给每个前端调用点，也会让刷新、
401 重试和 SSE 身份恢复散落在 Store、Axios 与 Run/Event Adapter 中。

入口流量同样需要独立治理。登录与注册在同步 SQLAlchemy handler 中执行 PBKDF2
校验，Run、HITL、检索和上传又具有显著不同的成本。若每条 route 自行计数、拼 Redis key 和选择
身份，原始 IP、用户名、Bearer 或 Cookie 容易泄漏到存储与日志；多实例计数、Redis 故障语义和
响应头也会形成重复的浅 Implementation。

因此建立相邻但职责不同的认证生命周期与 Rate Limit 深 Module：认证 Module 管理短期 access
credential 与可撤销 refresh credential；Rate Limit Module 在进入昂贵 handler 前统一选择策略、
隐藏身份并原子消费配额。浏览器响应头作为独立的 HTTP 边界防线，不替代认证、授权或限流。

## 决策

### 浏览器 access/refresh 生命周期

Access Token 是短期签名 JWT，携带 `iat` 与唯一 `jti`。登录、注册和刷新响应只把 access token
返回给前端认证状态；前端仅在 JavaScript 内存中保存它，并继续通过标准
`Authorization: Bearer <access-token>` 调用受保护 Interface。浏览器不得把 access token 写入
`localStorage`、`sessionStorage`、IndexedDB、URL、持久 Cookie 或其他可跨页面恢复的客户端
存储。Bearer 是 HTTP 协议，不代表浏览器持久化策略。

页面启动时先以 credentials 调用 `POST /auth/refresh`，完成前保持认证状态 unresolved，避免先
渲染错误页面。同一标签页内的并发启动或多个 401 共享一个 refresh promise；支持 Web Locks 的
浏览器还使用命名锁串行化跨标签页 refresh，避免共享 Cookie 被并发轮换后把正常请求误判为
replay。refresh attempt 在等待锁前捕获本标签页 generation、Access Token 与 username，获得锁后
必须重新检查 generation 与 revocation tombstone，再决定是否发送请求。

refresh 响应的 username 必须与 attempt 预期主体一致。Axios 对原始 401 记录 Access Token 与
username，在 refresh 前、refresh 后以及实际重试的 request interceptor 中都重新确认当前认证
主体未变化；来自旧账号的 401 不得携带新账号 credential 重试。成功后只重试原请求一次，失败
则清空仍匹配该 attempt 的内存 token 并广播未认证状态，禁止刷新循环。Run HTTP、SSE 与其他
前端 Adapter 只向统一认证状态取当前内存 token，不得回退读取旧持久键。

退出时先建立本地 revocation tombstone 并清空内存状态；若已有 refresh 在途，必须等待其响应
（包括可能轮换的 Set-Cookie）落定后，再调用 `POST /auth/logout` 撤销浏览器中最新 credential。
否则 logout 响应先到、旧 refresh 响应后到会重新留下活跃 Cookie。只有服务端撤销成功后才清除
tombstone；失败时页面刷新继续优先重试 logout，不能静默恢复旧会话。

Refresh Token 是高熵 opaque credential，不是 JWT。它只存在于固定 `Path=/auth` 的 HttpOnly
Cookie 与当前 HTTP request 中，永不进入 JSON 响应、前端状态、日志或公开错误；数据库只保存
SHA-256 hash、用户、过期时间和撤销时间，不保存原始 token。登录和注册签发 access/refresh
pair；每次 `POST /auth/refresh` 在同一事务中撤销旧 refresh token、创建替代 token 并签发新的
access token。

Refresh ledger 的在线生命周期写路径统一使用 `User → RefreshToken` 锁序：issue、rotate、当前设备
logout 与 logout-all 都先锁 `users` 行，再锁定或写入 `refresh_tokens`。rotate/logout 允许先做
一次不加锁的 token→user_id 定位，但随后必须取得 User `FOR UPDATE`，再取得对应 RefreshToken
`FOR UPDATE`；这次定位不授予任何 mutation 权限。统一锁序使 rotate、replay revocation 与
logout-all 在 PostgreSQL 多实例下串行化，避免 logout-all 返回后又留下并发签发的活跃 token。

仍在自然有效期内、但已经撤销的 refresh token 再次出现才视为 replay：服务端撤销该用户所有
仍活跃 refresh token，使可能被窃取的替代 token 同时失效。服务端先判断 `expires_at`，再判断
`revoked_at`；过期 token 只返回 expired，不触发用户级撤销，避免攻击者用很久以前泄漏的 token
制造账号 DoS。`POST /auth/logout` 撤销当前 Cookie 对应的 credential；Bearer 鉴权的
`POST /auth/logout-all` 撤销该用户全部活跃 refresh token。缺失、畸形、过期和 replay 都返回
稳定的 401，并清除浏览器 refresh Cookie。

Refresh Cookie 始终设置 HttpOnly 与 `Path=/auth`。生产环境必须启用 Secure；配置
`SameSite=None` 时，无论环境都必须同时启用 Secure。默认 `SameSite=Lax` 适合前后端同站部署。

所有 `/auth` unsafe POST（login、register、refresh、logout、logout-all）先经过外层
`AuthRequestGuardMiddleware`。request origin 与应用 origin 相同的 same-origin 请求始终可信，
不受 CORS credentials 开关影响；跨 origin 只有在 `CORS_ALLOW_CREDENTIALS=true` 且命中显式
`CORS_ORIGINS` allowlist 时才可信，allowlist 可留空表示 same-origin-only，禁止 `*`。Guard 优先
校验 `Origin`，缺失时使用 Referer；畸形来源、`Origin: null`、没有可信来源的
`Sec-Fetch-Site: same-site|cross-site` 均返回 403，完全没有浏览器来源 metadata 的非浏览器
客户端仍可使用。

外层 Guard 还在 Rate Limit 前校验 Content-Length 语法与声明的 16 KiB 上限，并要求 login/
register 使用 `application/json` 或 `application/*+json`。这些 metadata 拒绝不消耗受害者 quota。
随后 Rate Limit 先消费稳定 host/subject 配额，内层 `AuthBodyLimitMiddleware` 再流式累计实际
body；无 Content-Length 或伪造较小长度的 chunked body 超过 16 KiB 时返回 413，任何非空 auth
body 都必须使用 JSON media type。refresh/logout/logout-all 可以发送空 body。刻意先计费再读取
完整 stream，避免慢速/分块请求在未消费 quota 时占用连接与内存；但 body guard 仍位于 route、
token mutation 与 PBKDF2 之前。

所有 `/auth` 响应均带 `Cache-Control: no-store` 与 `Pragma: no-cache`，包括成功、401 与 429，
避免 access token、Cookie lifecycle 响应或认证错误被浏览器/中间缓存保存。HttpOnly 降低脚本
读取 credential 的能力，但不能阻止已执行脚本发起同源操作，因此仍需 CSP、输出编码和依赖治理。

新注册和迁移后的密码哈希固定使用 PBKDF2-SHA256。登录边界临时保留对历史 bcrypt 与
bcrypt-sha256 哈希的只读验证；验证成功后在凭据签发的同一数据库事务中改写为 PBKDF2，失败或
事务回滚都不得改写。这是有终点的一次性数据迁移，不是第二套认证、Token 或 Session Interface；
所有环境不再存在历史哈希后应删除该读取器。

Refresh ledger 不能在 refresh 热路径中 opportunistic delete。每条记录必须在自然 `expires_at`
之前完整保留；自然过期后再继续保留 `AUTH_REFRESH_LEDGER_RETENTION_DAYS`（默认 30 天），仅作为
forensic/audit evidence 与运维诊断。过期后的保留行不再参与用户级 replay 撤销。部署方必须独立调度
`uv run --no-sync python -m backend.auth.cleanup`（安装后也可运行 `supermew-auth-cleanup`）；命令
使用 `--batch-size`（默认 1000）与 `--max-batches`（默认 100）执行有界批量删除，没有 dry-run。

### Rate Limit 深 Module 与 Adapter Seam

建立 `backend.rate_limits` 深 Module。HTTP Adapter 只提交 method、path 与一次请求身份，
`RateLimiter.check(...)` 在 Module 内完成 route policy 选择、identity HMAC、原子消费、remaining、
reset 与 Retry-After 推导。调用方不拼存储 key，也不理解 fixed-window 实现。

Module 提供两个 Adapter：

- `InMemoryRateLimitAdapter` 用于单进程开发和测试；进程重启或多实例不会共享计数。
- `RedisRateLimitAdapter` 用于生产；单次 Lua evaluation 使用 Redis `TIME`，原子执行
  `HSET`/`PEXPIREAT` fixed window，使多个 API 实例共享同一配额事实。

生产启动校验强制 `RATE_LIMIT_ENABLED=true`、`RATE_LIMIT_BACKEND=redis`，并要求稳定、至少
32 字符的 `RATE_LIMIT_HMAC_KEY`。HMAC key 必须独立于 `JWT_SECRET_KEY`，不能复用签名 Secret；
Redis Adapter 使用现有认证 `REDIS_URL`。Redis 超时、断连、畸形响应或已关闭 Runtime 都返回
typed `RATE_LIMIT_UNAVAILABLE` 与 503，生产路径 fail-closed，不能因限流基础设施异常放行昂贵
操作。应用拥有 limiter 生命周期，并幂等关闭 Adapter。

原始 IP、username 与已验证 access subject 只短暂跨越 HTTP Adapter 到 `RateLimiter`；存储 key
只包含稳定 policy id 与 HMAC-SHA256 digest，不包含可恢复的原始身份。原始 Bearer/refresh token
绝不跨越该 Interface：HTTP Adapter 通过注入的 Auth resolver 验证 access JWT 并只提交稳定
subject，因此 access token 轮换不会获得新 bucket；opaque refresh 与当前设备 logout 使用直接
client IP bucket，因此 refresh rotation 同样不会重置配额。无效/过期 Bearer 回退 client IP。
中间件不读取请求 body。

除健康检查、OpenAPI/Docs、正式前端静态资源与 CORS preflight 外，所有动态 HTTP path 默认进入
general policy；未知或未来新增 path 不能因不在硬编码前缀中 fail-open。已知高成本 route 再由
matcher 选择更严格的专用 policy。

HTTP Adapter 不直接信任客户端提供的 `X-Forwarded-For`。若 API 位于 Nginx、Ingress 或云负载
均衡器之后，部署层必须只允许受控代理写入 forwarded headers，并由配置了可信 proxy allowlist
的 ProxyHeaders 层先把 `scope.client` 修正为真实来源；否则所有公网请求可能共享代理 IP bucket。
禁止为了“获得真实 IP”在 Rate Limit Module 内解析任意转发头，这会允许客户端伪造身份绕过配额。

登录与注册额外执行第二层身份限流。FastAPI dependency 在同步 route 与密码校验前读取框架已
缓存的 JSON，将 username 按 NFKC、trim 与 casefold 规范化，并以
`direct-client-IP + normalized-username` 消费同一 route policy，但每次计为两个 quota unit。
第一层 IP bucket 约束单一来源的总认证流量；第二层复合 bucket 进一步约束该来源集中尝试同一
username，login 实际最多 5 次/60 秒，register 实际最多 2 次/小时。这里刻意不使用
username-only 全局 bucket，避免攻击者仅凭已知用户名耗尽他人的全局配额；因此该策略不宣称
阻止多来源分布式账号攻击。密码和 request body 不进入 identity 或日志。任一层拒绝或 Redis
故障都会在 PBKDF2 前结束请求。

默认 fixed-window policy 为：

```text
login host                         10 / 60s
login host+username (cost=2)       10 / 60s，实际 5 次
register host                       5 / 3600s
register host+username (cost=2)     5 / 3600s，实际 2 次
refresh / current logout host     120 / 60s
logout-all access subject         120 / 60s
Thread Run creation                30 / 60s
HITL resume                        30 / 60s
Document upload                    10 / 3600s
general API                       120 / 60s
```

Thread Run policy 覆盖 canonical Run 创建；HITL resume、Document upload 和其余动态请求分别
使用专用或 general policy。refresh 与当前设备 logout 使用 120/min
host 粗限额，避免大型 NAT 把正常轮换变成可用性事故；logout-all 有已验证 Access Token，因此
使用稳定 subject bucket。原始 token 永远不作为 quota identity。
拒绝响应返回 typed `RATE_LIMITED`、429、`Retry-After` 与标准 RateLimit headers；响应不回显
identity、credential、Redis key 或内部异常。

### 浏览器安全响应头

所有 HTTP 响应统一附加：

```text
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Permissions-Policy: camera=(), geolocation=(), microphone=(), payment=(), usb=()
```

Content Security Policy 只附加到正式前端 HTML，不附加到 JSON、FastAPI `/docs` 或 `/redoc`，
避免破坏文档 UI 的 CDN 资源：

```text
default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none';
form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'
```

CSP 是浏览器减损边界，不改变 access/refresh token 生命周期，也不放宽任何后端授权或 Rate
Limit policy。未来若引入新的外部脚本、字体、图片、WebSocket 或跨 origin API，必须先修改并
测试明确 directive，不能退回通配符。

### 配置与质量门禁

入口限流的部署 Interface 为：

```text
RATE_LIMIT_ENABLED
RATE_LIMIT_BACKEND=memory|redis
RATE_LIMIT_HMAC_KEY
RATE_LIMIT_KEY_PREFIX
```

认证配置继续使用 `JWT_REFRESH_EXPIRE_DAYS`、`AUTH_REFRESH_LEDGER_RETENTION_DAYS`、
`AUTH_REFRESH_COOKIE_NAME`、`AUTH_REFRESH_COOKIE_SECURE`、`AUTH_REFRESH_COOKIE_SAMESITE`、
显式 `CORS_ORIGINS` 与 `CORS_ALLOW_CREDENTIALS`。Vite 开发服务器固定使用 3000，因此默认
allowlist 是 `http://localhost:3000,http://127.0.0.1:3000`。生产 same-origin-only 部署可把
`CORS_ORIGINS` 留空；只有跨 origin 前端才配置真实 HTTPS origin。Cookie name 可配置，Path 与
HttpOnly 不可放宽。Web Locks 以浏览器 Origin 为作用域，因此生产 credentialed CORS 最多允许
一个 canonical 前端 Origin；多个前端必须使用独立 Cookie/API host，或先引入服务端 refresh
family 并发协议。

`ADMIN_INVITE_CODE` 留空表示禁用公开 admin 注册；启用时不得与 JWT 或 Rate Limit Secret 相同。
邀请码比较使用 constant-time
`hmac.compare_digest`，不能把示例值或部署 Secret 写入仓库。

仓库门禁覆盖 access token 不持久化、Cookie 属性、refresh rotation、replay 后全量撤销、统一
User→RefreshToken 锁序、expired-before-revoked 判定、ledger forensic retention/purge、当前与全部
设备退出、标签页内 single-flight、
Web Locks 跨标签串行、等待锁后的 generation/tombstone 重检、refresh 主体一致、旧 401 不跨账号
重试、logout 等待在途 refresh、unsafe auth POST metadata 在 Rate Limit 前拒绝、streamed 16 KiB
cap 在计费后但 route 前拒绝、JSON/no-store 边界、token 轮换不重置稳定 subject/host bucket、
未知/未来动态 path 默认限流、二级 username 限流早于
密码 handler、typed 429/503、全局 `X-Frame-Options: DENY` 和 CSP/docs 分流。
`storage-compatibility` job 还必须连接真实认证 Redis，验证同 identity 达限、不同 identity 隔离、
window reset、跨 Adapter 共享计数、key 不含原始 identity/token，以及 Redis 不可用时
fail-closed；fake Adapter 单测不能替代该发布证据。

## 结果

浏览器只持有可随刷新丢弃的内存 Access Token，长期 credential 被收进 HttpOnly Cookie 与服务端
可撤销 ledger；rotation、replay、当前设备退出和全部设备退出成为一个认证 Module 的事务不变量。
Rate Limit Module 把 route 成本、身份隐藏、Redis 原子性和故障语义集中到一个小 Interface，
PBKDF2、Run、HITL 与上传在昂贵工作前获得一致保护。安全响应头则为正式前端 HTML 提供独立的
浏览器边界，同时保持 FastAPI Docs 可用。

代价是页面刷新必须先完成一次 refresh、生产 API 依赖 Redis 才能接收受限入口流量，并且
`SameSite=None` 或跨站前后端需要额外的 HTTPS/CORS/CSRF 评审。未来增加多地域配额、滑动窗口、
设备管理或 refresh token family 时，应扩展现有 Module 的 durable contract，不得重新把 token
写入浏览器持久存储或把 raw identity 暴露给 Adapter。
