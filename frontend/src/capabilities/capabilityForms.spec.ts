import { describe, expect, it } from 'vitest';
import {
  buildManagedHttpToolPayload,
  buildManagedSkillPayload,
  buildSqlAssistantPayload,
  splitList,
} from './capabilityForms';

describe('capability form payloads', () => {
  it('normalizes comma/newline lists and removes duplicates', () => {
    expect(splitList('admin, analyst\nadmin\n')).toEqual(['admin', 'analyst']);
  });

  it('builds a trimmed Skill payload without exposing secret values', () => {
    expect(
      buildManagedSkillPayload({
        description: '  Research releases. ',
        instructions: '  # Workflow\nUse release_lookup. ',
        allowedTools: ['release_lookup', 'release_lookup', ''],
        requiredRoles: 'admin, analyst',
        requiredSecrets: 'RELEASE_API_TOKEN',
        enabled: true,
      })
    ).toEqual({
      description: 'Research releases.',
      instructions: '# Workflow\nUse release_lookup.',
      allowed_tools: ['release_lookup'],
      required_roles: ['admin', 'analyst'],
      required_secrets: ['RELEASE_API_TOKEN'],
      enabled: true,
    });
  });

  it('parses declarative HTTPS Tool JSON fields into the exact API payload', () => {
    expect(
      buildManagedHttpToolPayload({
        description: 'Release lookup',
        group: 'custom-http',
        endpoint: 'https://api.vendor.dev/v1/releases',
        method: 'POST',
        inputSchema:
          '{"type":"object","properties":{"query":{"type":"string"}},"additionalProperties":false}',
        staticHeaders: '{"X-Client":"SuperMew"}',
        secretHeaders: '{"Authorization":"RELEASE_API_TOKEN"}',
        requiredRoles: 'admin',
        requiresApproval: true,
        idempotent: true,
        timeoutSeconds: 20,
        maxResponseBytes: 65536,
        enabled: true,
      })
    ).toMatchObject({
      endpoint: 'https://api.vendor.dev/v1/releases',
      method: 'POST',
      static_headers: { 'X-Client': 'SuperMew' },
      secret_headers: { Authorization: 'RELEASE_API_TOKEN' },
      required_roles: ['admin'],
      requires_approval: true,
      timeout_seconds: 20,
      max_response_bytes: 65536,
    });
  });

  it('rejects insecure endpoints and non-string Header values before sending', () => {
    const base = {
      description: 'Release lookup',
      group: 'custom-http',
      endpoint: 'http://api.vendor.dev/releases',
      method: 'GET' as const,
      inputSchema: '{"type":"object"}',
      staticHeaders: '{}',
      secretHeaders: '{}',
      requiredRoles: '',
      requiresApproval: false,
      idempotent: true,
      timeoutSeconds: 20,
      maxResponseBytes: 65536,
      enabled: true,
    };

    expect(() => buildManagedHttpToolPayload(base)).toThrow('Endpoint 必须使用 HTTPS');
    expect(() =>
      buildManagedHttpToolPayload({
        ...base,
        endpoint: 'https://api.vendor.dev/releases',
        staticHeaders: '{"X-Retry":3}',
      })
    ).toThrow('静态 Headers的键和值都必须是字符串');
  });

  it('builds SQL Assistant allowlists and validates the Secret reference', () => {
    const payload = buildSqlAssistantPayload({
      enabled: true,
      dsnSecretName: 'ANALYTICS_READER_DSN',
      expectedRole: ' analytics_reader ',
      allowedSchemas: 'analytics, reporting',
      allowedTables: 'analytics.orders\nanalytics.customers',
      sensitiveColumns: 'analytics.customers.email',
      statementTimeoutSeconds: 10,
      maxRows: 200,
      maxResultBytes: 262144,
      maxEstimatedCost: 100000,
      maxEstimatedRows: 100000,
      maxEstimatedBytes: 8388608,
      catalogCacheTtlSeconds: 300,
    });

    expect(payload).toMatchObject({
      dsn_secret_name: 'ANALYTICS_READER_DSN',
      expected_role: 'analytics_reader',
      allowed_schemas: ['analytics', 'reporting'],
      allowed_tables: ['analytics.orders', 'analytics.customers'],
      sensitive_columns: ['analytics.customers.email'],
    });

    expect(() =>
      buildSqlAssistantPayload({ ...payloadToDraft(payload), dsnSecretName: 'dsn-value' })
    ).toThrow('DSN Secret 名称必须使用大写字母、数字和下划线');
  });
});

function payloadToDraft(payload: ReturnType<typeof buildSqlAssistantPayload>) {
  return {
    enabled: payload.enabled,
    dsnSecretName: payload.dsn_secret_name,
    expectedRole: payload.expected_role,
    allowedSchemas: payload.allowed_schemas.join('\n'),
    allowedTables: payload.allowed_tables.join('\n'),
    sensitiveColumns: payload.sensitive_columns.join('\n'),
    statementTimeoutSeconds: payload.statement_timeout_seconds,
    maxRows: payload.max_rows,
    maxResultBytes: payload.max_result_bytes,
    maxEstimatedCost: payload.max_estimated_cost,
    maxEstimatedRows: payload.max_estimated_rows,
    maxEstimatedBytes: payload.max_estimated_bytes,
    catalogCacheTtlSeconds: payload.catalog_cache_ttl_seconds,
  };
}
