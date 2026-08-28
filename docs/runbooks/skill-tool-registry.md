# Skill / Tool Registry 运维手册

## 两种来源与生效方式

四个仓库内 Skill（`knowledge-base`、`sql-assistant`、`web-research`、`sandbox`）在控制面首次
初始化时写入数据库，之后由数据库记录作为目标配置。管理员可在前端侧栏 **Skill / Tool**：

- 编辑或停用内建 Skill；内建 Skill 不可删除；
- 创建、编辑、停用和删除自定义 Skill；
- 创建声明式自定义 HTTPS JSON Tool；
- 配置 SQL Assistant 与 Tavily Keyless 开关。

每次保存后，服务端会立即重建当前 Registry 和 Runtime，替换当前配置并关闭之前的 Runtime。
普通 Skill、Tool、SQL 或 Tavily 开关修改不需要重启 API/worker。

首次部署先执行：

```bash
uv run alembic upgrade head
```

## 启动前验证

```bash
uv run --frozen python -m backend.tools.registry_cli validate
uv run --frozen python -m backend.tools.registry_cli list-tools --role user
uv run --frozen python -m backend.tools.registry_cli list-skills --role user
uv run --frozen python -m backend.tools.registry_cli describe-skill knowledge-base --role user
```

命令只接受 Secret 名称，不打印 Secret 值。若要验证某个可选 Secret，先在进程环境中配置
它，再传 `--secret-name NAME`；未配置的名称不会被当成可用。

## 新增或升级仓库内默认 Skill

1. 在 `skills/<kebab-name>/` 创建 `skill.yaml` 和 UTF-8 entrypoint。
2. `allowed_tools` 只能引用已注册 Tool；role 与 Secret 使用符号名。
3. 版本使用 SemVer。修改正文时必须升级版本，避免相同版本产生新的 content hash。
4. 运行 registry validate、Skill/Tool 测试和 contract generator check。
5. 部署代码。新环境启动时只会写入数据库中尚不存在的默认 Skill。

仓库内文件只负责首次种子和可审查的默认值。数据库中同名 Skill 已存在时，启动不会用文件
覆盖管理员配置。若确需把新的仓库默认值同步到既有环境，应在前端显式编辑，或通过受审计的
数据库迁移更新；不要删除控制面记录来触发隐式重置。

## 自定义 HTTPS Tool

自定义 Tool 是声明式 JSON 集成，不是任意代码插件。允许的范围固定为：

- 公共 HTTPS FQDN 或全局 IP，端口固定为 443；禁止 userinfo、fragment、localhost、特殊用途域名和非全局地址；
- `GET` 查询参数或 `POST` JSON body；输入使用 Draft 2020-12 JSON Schema，根类型必须为 object；
- 静态 Header 只保存非敏感单行文本；Authorization/API key 等必须映射到环境 Secret 名称；
- DNS pinning、SSRF、redirect、absolute deadline、响应 Content-Type 和 byte budget 继续由安全 HTTP Runtime 强制执行；
- 不接受上传 Python、Shell、动态 import、任意命令或私网 Endpoint。

Tool 名称不能覆盖内建 Tool。已启用 Skill 引用某 Tool 时不能停用该 Tool；任意 Skill 仍引用时
不能删除。先编辑引用方 Skill，再执行停用或删除。

## 禁用与恢复配置

- 禁用：在前端关闭 Skill 或收紧 `required_roles`/`required_secrets`，保存后立即生效。
- 完全移除前，先解除它对自定义 Tool 的引用。
- 恢复旧配置时，重新提交已审查的指令和策略即可。

## 安全检查

- catalog、prompt、checkpoint、Run Event 和日志中不得出现 Secret 值。
- 未授权 Tool 不得出现在 Skill catalog、模型 schema、`tool_search` 结果或执行结果中。
- `requires_approval=true` 的 Tool 在 Guardrail/approval 未提供前默认不可用。
- `network_policy` 只表达 Registry 授权；网络目的地址的强制校验由对应 Adapter 和
  Guardrail/Sandbox 共同负责。
