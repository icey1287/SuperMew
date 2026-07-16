# Skill / Tool Registry 运维手册

## 启动前验证

```bash
uv run --frozen python -m backend.tools.registry_cli validate
uv run --frozen python -m backend.tools.registry_cli list-tools --role user
uv run --frozen python -m backend.tools.registry_cli list-skills --role user
uv run --frozen python -m backend.tools.registry_cli describe-skill knowledge-base --role user
```

命令只接受 Secret 名称，不打印 Secret 值。若要验证某个可选 Secret，先在进程环境中配置
它，再传 `--secret-name NAME`；未配置的名称不会被当成可用。

## 新增或升级 Skill

1. 在 `skills/<kebab-name>/` 创建 `skill.yaml` 和 UTF-8 entrypoint。
2. `allowed_tools` 只能引用已注册 Tool；role 与 Secret 使用符号名。
3. 版本使用 SemVer。修改正文时必须升级版本，避免相同版本产生新的 content hash。
4. 运行 registry validate、Skill/Tool 测试和 contract generator check。
5. 重启 API 与 Run worker。Registry 是启动时不可变 snapshot，不支持原地热改。

正在运行或待恢复的 Run 已固定 name、version 与 SHA-256。若目录内容与 pin 不一致，Run
会 fail-closed；不要通过覆盖旧版本文件来“修复”运行中的任务，应恢复原内容或明确终止
旧 Run。

## 禁用与回滚

- 禁用新激活：从 manifest 的 `required_roles`/`required_secrets` 收紧权限并重启进程。
- 完全移除前，先确认没有非终态 Run 固定该 Skill。
- 回滚时恢复相同 version 的原始 manifest 与 entrypoint bytes，验证 hash 后再重启。

## 安全检查

- catalog、prompt、checkpoint、Run Event 和日志中不得出现 Secret 值。
- 未授权 Tool 不得出现在 Skill catalog、模型 schema、`tool_search` 结果或执行结果中。
- `requires_approval=true` 的 Tool 在 Guardrail/approval 未提供前默认不可用。
- `network_policy` 只表达 Registry 授权；网络目的地址的强制校验由对应 Adapter 和
  Guardrail/Sandbox 共同负责。
