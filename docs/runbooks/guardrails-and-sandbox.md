# Guardrail 与隔离 Sandbox 运维手册

## 适用范围与不变量

Guardrail 随 Agent Runtime 始终启用，没有“临时关闭策略”的环境变量。它在每次 Tool handler
前返回 `ALLOW`、`DENY` 或 `REQUIRE_APPROVAL`；上下文缺失、策略异常和未知 capability 均
fail-closed。`shell`、`code`、`process`、`network-private`、`high-risk` 是 hard-deny group，
任何审批都不能放行。

Sandbox 是独立的 approval-only 执行能力，默认关闭。它只接受 `language` 与 `source`，固定
无网络、无 bind mount、无宿主文件、无持久 workspace、无 artifact export。不要把 Sandbox
当作 SQL/Web/private-data policy 的绕行路径。完整架构决策见
`docs/adr/0020-guardrails-and-isolated-sandbox.md`。

## 数据库迁移

发布前先应用迁移，使 durable Run 持久化 tenant、channel 与 approval names，并让 ToolAudit
记录 Guardrail policy identity：

```bash
uv run --frozen alembic upgrade head
uv run --frozen alembic current
```

旧 ToolAudit 的 `reason_code`、`policy_version`、`policy_hash` 可以为空；新调用必须写入稳定值。
不要人工把历史记录回填成当前 policy，它们没有经过当前决策矩阵。

## 准备 Sandbox image 与 daemon

### Image 要求

Sandbox 只接受本地已有的 immutable image digest，拒绝 tag，也不会 pull。image 必须使用仓库
内固定 runner，并满足：

```text
Entrypoint: /usr/local/bin/python -I -B /opt/supermew/runner.py
User:       65532:65532（或 image metadata 中的 65532）
```

构建时应把 base image 也固定到已评审 digest。下面只展示流程；digest 必须来自组织批准的
镜像仓库和扫描结果：

```bash
docker build \
  --build-arg PYTHON_BASE_IMAGE='python:3.12-slim@sha256:<approved-base-digest>' \
  --file docker/sandbox/Dockerfile \
  --tag supermew-sandbox:candidate \
  docker/sandbox

docker image inspect \
  --format '{{.Id}} {{json .Config.Entrypoint}} {{.Config.User}}' \
  supermew-sandbox:candidate
```

将输出的 `sha256:<64 hex>` image ID 配置为 `SANDBOX_DOCKER_IMAGE`。不要配置
`supermew-sandbox:latest`，也不要依赖部署时自动拉取。

### 专用 rootless daemon

生产环境必须使用 rootless Docker，并建议为 SuperMew Sandbox 使用专用 daemon/Unix socket，
不要与日常构建、运维或其他租户 workload 共享 daemon。宿主 API/worker 进程需要访问该可信
socket；不可信 Sandbox 容器绝不能挂载 Docker socket。

上线前使用与应用完全相同的 `DOCKER_HOST` 验证：

```bash
docker --host 'unix:///path/to/supermew-rootless.sock' \
  info --format '{{json .SecurityOptions}}'
docker --host 'unix:///path/to/supermew-rootless.sock' \
  image inspect 'sha256:<sandbox-image-id>'
```

第一条输出必须包含 rootless security option。`SANDBOX_DOCKER_HOST` 只接受本地
`unix://` endpoint；TCP daemon 会在 Settings 校验时被拒绝。

## 配置

开发环境最小启用配置：

```dotenv
SANDBOX_ENABLED=true
SANDBOX_ADAPTER=docker
SANDBOX_DOCKER_IMAGE=sha256:<sandbox-image-id>
SANDBOX_DOCKER_HOST=unix:///path/to/supermew-rootless.sock
SANDBOX_REQUIRE_ROOTLESS=true
```

生产环境还必须设置 `APP_ENV=production`；此时 `SANDBOX_REQUIRE_ROOTLESS=false` 会令应用拒绝
启动。完整 CPU、memory、PID、workspace、source、output、file/path 与 cleanup 预算见
`.env.example`。调整时保持：

- `SANDBOX_MAX_FILE_BYTES <= SANDBOX_MAX_TOTAL_FILE_BYTES`；
- `SANDBOX_MAX_TOTAL_FILE_BYTES <= SANDBOX_WORKSPACE_BYTES`；
- `SANDBOX_MAX_SOURCE_BYTES < SANDBOX_WORKSPACE_BYTES`；
- Tool timeout 已包含执行、cleanup 与小幅 envelope 余量，不要把多个层级的预算误当成一个值；
- 并发上限按进程生效，API/worker 副本数会放大 daemon 总并发，需在部署层另外限额。

关闭时保持 `SANDBOX_ENABLED=false` 即可；disabled Adapter 不检查 Docker binary、daemon 或
image，也不阻止 `/health/ready`。

## Approval 与 Skill 激活

当前前端有创建 Run 前的审批确认 UX，但没有运行中 approval interrupt 或 resume endpoint。
确认操作最终仍是在 durable Run 创建请求中预先声明 names-only `approved_tools`；只有数据库
角色为 `admin` 的可信调用方可以提交，普通用户会收到 403，未知工具或未声明
`requires_approval=true` 的工具会收到 400。

示例请求：

```bash
curl --fail-with-body \
  --header 'Authorization: Bearer <admin-access-token>' \
  --header 'Content-Type: application/json' \
  --request POST \
  --data '{
    "message": "/sandbox\nUse one Python execution to calculate 2 + 2.",
    "idempotency_key": "sandbox-smoke-001",
    "approved_tools": ["sandbox_execute"]
  }' \
  'http://127.0.0.1:8000/v1/threads/<thread-id>/runs'
```

approval names 被绑定到该 user、tenant、Thread 与 Run；不要复制到另一 Run，不要让模型生成
approval token，也不要把审批材料写进 prompt。`sandbox_execute` 仍是 deferred Tool，必须由
active `/sandbox` Skill 收窄后才可见。

前端确认框不是独立授权凭证：它只在真正创建 Run 时把已确认的 Tool 名称放入同一请求。取消
确认不会签发 grant；创建失败后也不能把本地确认状态用于另一 Run。当前 Runtime 不会在执行
过程中弹出审批框、进入 waiting_input 或通过 HITL resume 扩权。

未预授权时，Registry 不把 Sandbox schema 放进当前 `ToolSession`；伪造调用在执行 Seam 被
拒绝。若未来控制面改变已持久化的 approval 集合，必须重建该 Run 的 Runtime/ToolSession，
不能修改现有 Session 的内存集合。worker reclaim 会从 durable Run snapshot 重建 grant。

## 发布验证

先验证 Registry 与静态契约：

```bash
uv run --frozen python -m backend.tools.registry_cli validate
uv run --frozen python -m backend.tools.registry_cli list-skills \
  --role admin --secret-name SANDBOX_RUNTIME
uv run --frozen python -m backend.tools.registry_cli list-tools \
  --role admin --secret-name SANDBOX_RUNTIME

uv run --frozen pytest -q \
  tests/test_guardrail_policy.py \
  tests/test_destination_capability.py \
  tests/test_guardrail_approval.py \
  tests/test_sandbox_contracts.py \
  tests/test_sandbox_runtime.py \
  tests/test_sandbox_docker_adapter.py \
  tests/test_sandbox_security.py \
  tests/test_sandbox_tool.py
```

启用配置生效后，`list-skills` 应包含 `sandbox`；`list-tools` 在没有 durable Run-bound grant 的
CLI 检查上下文中应刻意不显示 `sandbox_execute`。这证明 Secret/角色只提供候选资格，不能替代
Run 预授权。descriptor 的 approval-only 静态契约由 `validate` 与上述测试锁定。

真实 Docker 集成默认明确 skip。只有已经准备好专用 daemon 与 digest-pinned image 时才启用；
一旦显式启用，daemon/image 缺失必须让测试失败，不能再次 silent skip：

```bash
TEST_SANDBOX_DOCKER=1 \
TEST_SANDBOX_IMAGE='sha256:<sandbox-image-id>' \
TEST_SANDBOX_DOCKER_HOST='unix:///path/to/supermew-rootless.sock' \
TEST_SANDBOX_REQUIRE_ROOTLESS=1 \
uv run --frozen pytest -q tests/test_sandbox_docker_integration.py
```

集成测试确认 workload 非 root、root filesystem read-only、host env 不透传、network blocked，
逃离原进程组的后台进程会在 workspace scan 前被清理，且两次 invocation 之间 workspace 已销毁。

## Readiness 与烟雾测试

启用后应用启动会验证 daemon、rootless 要求、image digest、固定 entrypoint 与非 root user。
检查 readiness：

```bash
curl --fail-with-body http://127.0.0.1:8000/health/ready
```

`sandbox` 投影只应包含：

```json
{
  "enabled": true,
  "ready": true,
  "adapter": "docker",
  "daemon_reachable": true,
  "image_available": true,
  "active_executions": 0
}
```

不得出现 Docker host、image digest、container ID、source、路径或内部诊断。启用但不 ready 时
整体 endpoint 必须返回 503；关闭时 Sandbox 不参与整体 readiness gate。

使用新的 admin Run 做一次最小烟雾测试，并确认：

1. 没有 `approved_tools` 时 `sandbox_execute` 不可见/不可执行；
2. 有 pre-grant 且激活 `/sandbox` 后，简单 Python 输出成功；
3. workload 访问公网、宿主路径或 root filesystem 写入均失败；
4. 创建 workspace 文件只增加 `files_created`，回答中没有宿主路径或下载链接；
5. 下一次 invocation 看不到上一次文件；
6. timeout、cancel、output/file/path 超限返回稳定 `SANDBOX_*` code，容器均被删除；
7. Run Event/SSE 不含 `guardrail_audit`、capability、approval 或 Docker metadata。

## ToolAudit 检查

ToolAudit 应记录 Tool/catalog identity、`ALLOW|DENY|REQUIRE_APPROVAL`、reason code、policy
version/hash、success、error code、duration/result size 与 allowlist 内聚合 metadata。禁止记录
args、Sandbox source、SQL、query、URL、evidence/capability、签名、Secret 或内部异常。

可使用最小字段检查最近记录：

```sql
SELECT run_id, tool_name, decision, reason_code, policy_version,
       policy_hash, success, error_code, duration_ms, result_size
FROM tool_audits
ORDER BY id DESC
LIMIT 20;
```

不要把 `metadata_json` 整体复制到工单或聊天。若发现敏感原文，应按数据泄露事件处理，而不是
只增加日志脱敏。

## 常见故障

### Sandbox Skill 或 Tool 不可见

依次确认：`SANDBOX_ENABLED=true`、digest 非空、进程已重启、当前角色为 `admin`、Run 创建时
包含 `approved_tools=["sandbox_execute"]`、Registry 声明 `SANDBOX_RUNTIME`、active Skill 为
`sandbox`。任何条件缺失都应保持隐藏。

### 启动时报 ADAPTER_UNAVAILABLE

检查 Docker binary、Unix socket 权限、daemon 是否可达，以及
`SANDBOX_REQUIRE_ROOTLESS=true` 时 security options 是否包含 rootless。不要改用开放的 TCP
daemon，也不要通过把调用方加入高权限共享 Docker group 作为快速修复。

### 启动时报 IMAGE_UNAVAILABLE

检查配置是否为本地 image ID/RepoDigest、image 是否存在于同一个 daemon、Entrypoint 和 User
是否与固定 runner 一致。不要改成 tag，不要开启 pull-on-start。

### 执行返回 BUSY、TIMEOUT 或 CANCELLED

先检查 Run deadline、当前进程 active execution 和调用是否被取消。收窄 source 与输出；只有
在负载基线和 daemon 容量证明确实需要时，才小幅增加并发或时间上限。不要循环重试不可信
代码。

### CLEANUP_FAILED 或残留容器

Runtime/Adapter 在 cleanup 失败后应立即变为 not-ready、阻止新执行，并保留目标供后续 close
重试；不得把失败实例当作已关闭。运维上仍应立即停止接收新的 Sandbox Run，设置
`SANDBOX_ENABLED=false` 并滚动重启。只检查带固定 label
`com.supermew.sandbox.managed=true` 的容器；不要执行会影响其他 workload 的无过滤全局清理。
修复 daemon/权限后确认 Runtime close 和下一次 start 的 managed-container cleanup 成功，再恢复
流量。

## 禁用、轮换与事件响应

紧急禁用：设置 `SANDBOX_ENABLED=false`，滚动重启 API/worker，并确认 readiness 中 Adapter 为
disabled。若怀疑 daemon 或 image 被攻破，同时撤销进程对专用 socket 的访问、隔离 daemon、
轮换 image digest，并检查 ToolAudit 与 daemon audit，但不要收集/传播用户 source。

Guardrail policy 变更必须升级 policy version，验证 canonical hash 与拒绝矩阵，再滚动重启所有
执行 worker。不同 worker 不应长期运行不同 policy snapshot。hard-deny group、destination
capability 绑定和公开 Event 脱敏不能作为应急恢复手段被关闭。
