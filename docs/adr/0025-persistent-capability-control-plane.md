# ADR-0025：持久化 Capability Control Plane

- 状态：已接受
- 日期：2026-07-21

## 背景

默认 Skill 只能通过仓库文件修改，SQL Assistant 和 Web Research 主要依赖环境变量。运营侧
无法在前端审查、启停或组合能力，新增集成也容易退化为任意服务端代码上传。

## 决策

建立数据库持久化的 Capability Control Plane：

- 首次初始化把四个仓库内 Skill 写入 `capability_skill_profiles`；之后同名记录不会被文件覆盖；
- 内建 Skill 可编辑或停用但不可删除；自定义 Skill 可完整 CRUD；每次编辑自动递增 patch version；
- `capability_http_tool_profiles` 保存自定义声明式 HTTPS JSON Tool；
- `sql_assistant_profiles` 保存 SQL 开关、DSN 环境 Secret 名称、allowlist 和查询预算，不保存 DSN；
- `capability_state` 保存 Tavily Keyless 开关。

管理员保存配置后，服务端立即从数据库重建当前 ToolRegistry、SkillRegistry 和相关 Runtime，
替换当前引用并关闭之前的 Runtime。API 不维护额外的版本或发布状态，也不要求为普通配置修改
重启进程。进程启动时使用相同逻辑加载数据库配置。

自定义 Tool 只允许公共 HTTPS 443、GET/POST、Draft 2020-12 object JSON Schema、静态 Header
和环境 Secret Header 引用。Endpoint 继续经过 DNS pinning、SSRF、redirect、deadline、Content
Type 与 byte budget 门禁。控制面不接受 Python、Shell、动态模块、任意命令或私网访问。已启用
Skill 引用某 Tool 时不能停用该 Tool，任意 Skill 仍引用时不能删除。

Web Research provider 固定为 Tavily Keyless。控制面只保存启用状态，不保存或请求 API Key；
搜索请求固定发送到 Tavily 官方 HTTPS origin，并携带
`X-Tavily-Access-Mode: keyless`。

SQL Assistant 的 `dsn_secret_name` 只引用 API/worker 环境中的 Secret。前端只看到名称与
`dsn_configured` 布尔值，DSN、密码和 Header Secret 值不会进入数据库、响应、Run state、
checkpoint、Event 或审计。

## 结果

管理员可以从一个前端控制面管理默认与自定义能力，保存后立即生效。新增外部 JSON 集成不再
要求修改代码，但仍受固定安全边界约束。环境变量只负责首次种子和 Secret 值；若部署系统新增
或修改进程环境中的 Secret，仍需让目标进程重新读取环境，但这不是控制面配置发布步骤。
