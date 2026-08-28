# 生产部署 Runbook

本文描述当前 canonical 部署流程。`docker-compose.prod.yml` 只管理基础依赖，不包含应用镜像或
应用进程；API、索引 worker 与 RAG 评估 worker 由部署平台分别托管。

## 运行拓扑

| 进程 | 命令 | 必要共享资源 |
| --- | --- | --- |
| API | `uvicorn backend.app:app` | PostgreSQL、Redis、Milvus、`frontend/dist`、`UPLOAD_DIR` |
| 索引 worker | `python -m backend.workers.indexing` | PostgreSQL、Milvus、与 API 相同的 `UPLOAD_DIR` |
| RAG 评估 worker | `python -m backend.workers.evaluation` | PostgreSQL、Milvus、模型与 RAG 配置 |
| Auth ledger cleanup | `python -m backend.auth.cleanup` | PostgreSQL；由 scheduler 周期执行 |

三个常驻进程必须使用同一 release、同一 `.env` 或 Secret 集合。不要把 `scripts/start.sh` 当作
多主机生产 supervisor；它适合本地和单机验收。

## 生产配置

至少确认：

- `APP_ENV=production`。
- `DATABASE_URL` 使用 PostgreSQL 独立应用账号，不使用 `postgres/postgres`。
- `REDIS_URL` 携带与生产 Compose 相同的 Redis Secret。
- `JWT_SECRET_KEY` 与 `RATE_LIMIT_HMAC_KEY` 均为至少 32 字符的独立随机 Secret。
- `AUTH_REFRESH_COOKIE_SECURE=true`、`RATE_LIMIT_ENABLED=true`、
  `RATE_LIMIT_BACKEND=redis`、`INDEX_WORKER_REQUIRED=true`。
- `ARK_API_KEY`、模型控制面首次种子、Embedding revision、Milvus 地址和文档 build version
  已按目标环境设置。
- API 与索引 worker 的 `UPLOAD_DIR` 指向同一个持久卷；多主机部署不能使用各自的本地目录。
- 生产前端优先与 API 同源部署。跨源时只配置一个明确的 HTTPS `CORS_ORIGINS`。

`docker-compose.prod.yml` 还要求部署环境注入：

```text
POSTGRES_DB
POSTGRES_USER
POSTGRES_PASSWORD
REDIS_PASSWORD
MINIO_ROOT_USER
MINIO_ROOT_PASSWORD
```

DSN 中包含 `@:/#%` 等保留字符时必须 percent-encode。不要把真实 Secret 提交到仓库。

## 首次部署

以下命令均在 release 根目录执行。

### 1. 校验并启动基础依赖

```bash
docker compose -f docker-compose.prod.yml config
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
```

如果 PostgreSQL、Redis 或 Milvus 由外部平台提供，可跳过对应 Compose 服务，但应用环境必须指向
同一套实际 Adapter。

### 2. 安装依赖并构建前端

```bash
uv sync --frozen --no-dev

cd frontend
npm ci --include=dev
npm run build
cd ..
```

API 只会在 `frontend/dist` 存在时挂载正式前端，因此构建失败时不要继续发布。

### 3. 迁移与启动前校验

执行迁移前先备份生产 PostgreSQL。迁移是前向流程，不依赖运行时双读或旧 schema fallback。

```bash
uv run --frozen alembic upgrade head
uv run --frozen python -c \
  "from backend.infra.database import assert_schema_current; assert_schema_current()"
uv run --frozen python -m backend.tools.registry_cli validate
```

### 4. 启动常驻进程

由 systemd、Kubernetes 或等价 supervisor 分别运行：

```bash
uv run --frozen python -m backend.workers.indexing
uv run --frozen python -m backend.workers.evaluation
uv run --frozen uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

三个进程都应配置自动重启、SIGTERM 优雅退出和足够的 termination grace。反向代理负责 TLS，
并只允许受控代理写 forwarded headers。容器或 Pod 内需要接受外部 Service 流量时，将 API host
改为 `0.0.0.0`；单机反向代理部署保持 `127.0.0.1`。

### 5. 健康检查与烟雾测试

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/health/live
curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
```

`/health/ready` 必须确认数据库、Redis、Milvus、模型/Embedding、Sandbox（若启用）以及当前 build
fingerprint 的索引 worker。随后至少验证：管理员登录、创建 Thread/Run、一次知识库问答、一次
文档索引任务；启用了评估功能时再提交一个最小 RAG Evaluation Job。

## 后续发布顺序

单集群最小发布流程：

1. 暂停新文档上传和新的 RAG Evaluation Job。
2. 正常停止旧 API、索引 worker 与评估 worker。
3. 备份数据库，准备新 release，安装锁定依赖并构建前端。
4. 执行 `alembic upgrade head` 和 schema / Registry 校验。
5. 先启动索引 worker 与评估 worker，再启动 API。
6. 通过 live、ready、登录、Thread/Run 和 RAG 烟雾测试后恢复入口。

不要同时运行 build fingerprint 不一致的旧索引 worker。数据库迁移不可逆时，不要通过降级应用
代码来回退 schema；应修复当前 release 并向前发布。

## 周期清理

Refresh ledger 不在 API 热路径自动清理。至少每天由 scheduler 执行一次，并对非零退出告警：

```bash
uv run --frozen python -m backend.auth.cleanup
```

命令只删除自然过期且已超过 `AUTH_REFRESH_LEDGER_RETENTION_DAYS` 的记录。

## 旧密码哈希迁移检查

新注册和新密码始终写入 PBKDF2-SHA256。为保留既有账号，登录边界目前仍只读验证历史 bcrypt /
bcrypt-sha256 哈希，并在成功签发凭据的同一事务中改写为 PBKDF2。这是一次性数据迁移，不是第二
套认证或会话 Interface。

发布后可只统计格式，不读取或导出具体哈希：

```sql
SELECT
  count(*) FILTER (WHERE password_hash LIKE 'pbkdf2_sha256$%') AS current_count,
  count(*) FILTER (WHERE password_hash NOT LIKE 'pbkdf2_sha256$%') AS legacy_count
FROM users;
```

在所有环境 `legacy_count=0` 或遗留账号已完成受控密码重置之前，不要删除该迁移读取器。
