import type {
  ManagedHttpToolPayload,
  ManagedSkillPayload,
  SqlAssistantConfigPayload,
} from '@/types/capabilities';

export interface SkillFormDraft {
  description: string;
  instructions: string;
  allowedTools: string[];
  requiredRoles: string;
  requiredSecrets: string;
  enabled: boolean;
}

export interface HttpToolFormDraft {
  description: string;
  group: string;
  endpoint: string;
  method: 'GET' | 'POST';
  inputSchema: string;
  staticHeaders: string;
  secretHeaders: string;
  requiredRoles: string;
  requiresApproval: boolean;
  idempotent: boolean;
  timeoutSeconds: number;
  maxResponseBytes: number;
  enabled: boolean;
}

export interface SqlAssistantFormDraft {
  enabled: boolean;
  dsnSecretName: string;
  expectedRole: string;
  allowedSchemas: string;
  allowedTables: string;
  sensitiveColumns: string;
  statementTimeoutSeconds: number;
  maxRows: number;
  maxResultBytes: number;
  maxEstimatedCost: number;
  maxEstimatedRows: number;
  maxEstimatedBytes: number;
  catalogCacheTtlSeconds: number;
}

export function splitList(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[\n,]/)
        .map((item) => item.trim())
        .filter(Boolean)
    ),
  ];
}

function requiredText(value: string, label: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${label}不能为空`);
  return normalized;
}

function boundedNumber(value: number, label: string, minimum: number, maximum: number): number {
  if (!Number.isFinite(value) || value < minimum || value > maximum) {
    throw new Error(`${label}必须在 ${minimum} 到 ${maximum} 之间`);
  }
  return value;
}

function parseJsonRecord(value: string, label: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`${label}必须是合法 JSON`);
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${label}必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

function parseStringRecord(value: string, label: string): Record<string, string> {
  const parsed = parseJsonRecord(value, label);
  for (const [name, item] of Object.entries(parsed)) {
    if (!name.trim() || typeof item !== 'string') {
      throw new Error(`${label}的键和值都必须是字符串`);
    }
  }
  return parsed as Record<string, string>;
}

export function buildManagedSkillPayload(draft: SkillFormDraft): ManagedSkillPayload {
  return {
    description: requiredText(draft.description, 'Skill 描述'),
    instructions: requiredText(draft.instructions, 'Skill 指令'),
    allowed_tools: [...new Set(draft.allowedTools.map((item) => item.trim()).filter(Boolean))],
    required_roles: splitList(draft.requiredRoles),
    required_secrets: splitList(draft.requiredSecrets),
    enabled: draft.enabled,
  };
}

export function buildManagedHttpToolPayload(draft: HttpToolFormDraft): ManagedHttpToolPayload {
  const endpoint = requiredText(draft.endpoint, 'Endpoint');
  if (!endpoint.toLocaleLowerCase().startsWith('https://')) {
    throw new Error('Endpoint 必须使用 HTTPS');
  }
  return {
    description: requiredText(draft.description, 'Tool 描述'),
    group: requiredText(draft.group, 'Tool 分组'),
    endpoint,
    method: draft.method,
    input_schema: parseJsonRecord(draft.inputSchema, 'Input Schema'),
    static_headers: parseStringRecord(draft.staticHeaders, '静态 Headers'),
    secret_headers: parseStringRecord(draft.secretHeaders, 'Secret Headers'),
    required_roles: splitList(draft.requiredRoles),
    requires_approval: draft.requiresApproval,
    idempotent: draft.idempotent,
    timeout_seconds: boundedNumber(draft.timeoutSeconds, '超时时间', 0.001, 120),
    max_response_bytes: Math.trunc(
      boundedNumber(draft.maxResponseBytes, '响应上限', 1024, 8_388_608)
    ),
    enabled: draft.enabled,
  };
}

export function buildSqlAssistantPayload(draft: SqlAssistantFormDraft): SqlAssistantConfigPayload {
  const dsnSecretName = requiredText(draft.dsnSecretName, 'DSN Secret 名称');
  if (!/^[A-Z][A-Z0-9_]{0,127}$/.test(dsnSecretName)) {
    throw new Error('DSN Secret 名称必须使用大写字母、数字和下划线');
  }
  return {
    enabled: draft.enabled,
    dsn_secret_name: dsnSecretName,
    expected_role: draft.expectedRole.trim(),
    allowed_schemas: splitList(draft.allowedSchemas),
    allowed_tables: splitList(draft.allowedTables),
    sensitive_columns: splitList(draft.sensitiveColumns),
    statement_timeout_seconds: boundedNumber(
      draft.statementTimeoutSeconds,
      'SQL 超时时间',
      0.001,
      120
    ),
    max_rows: Math.trunc(boundedNumber(draft.maxRows, '最大行数', 1, 10_000)),
    max_result_bytes: Math.trunc(boundedNumber(draft.maxResultBytes, '结果大小', 1024, 16_777_216)),
    max_estimated_cost: boundedNumber(draft.maxEstimatedCost, '最大估算成本', 0.001, 1_000_000_000),
    max_estimated_rows: Math.trunc(
      boundedNumber(draft.maxEstimatedRows, '最大估算行数', 1, 1_000_000_000)
    ),
    max_estimated_bytes: Math.trunc(
      boundedNumber(draft.maxEstimatedBytes, '最大估算字节', 1024, 1_073_741_824)
    ),
    catalog_cache_ttl_seconds: boundedNumber(
      draft.catalogCacheTtlSeconds,
      'Catalog 缓存时间',
      1,
      3600
    ),
  };
}
