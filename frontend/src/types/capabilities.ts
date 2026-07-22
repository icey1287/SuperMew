export type CapabilityAvailabilityReason = 'permission_required' | 'not_configured' | null;

export type CapabilityToolExposure = 'resident' | 'control' | 'deferred';

export interface CapabilitySkill {
  name: string;
  version: string;
  description: string;
  activation: string;
  available: boolean;
  availability_reason: CapabilityAvailabilityReason;
  required_roles: string[];
  tool_names: string[];
  approval_tools: string[];
  network_policies: string[];
  resource_scopes: string[];
}

export interface CapabilityTool {
  name: string;
  description: string;
  group: string;
  version: string;
  exposure: CapabilityToolExposure;
  available: boolean;
  availability_reason: CapabilityAvailabilityReason;
  required_roles: string[];
  requires_approval: boolean;
  network_policy: string;
  resource_scope: string;
  idempotent: boolean;
}

export interface CapabilityCatalogResponse {
  schema_version: 1;
  catalog_hash: string;
  skills: CapabilitySkill[];
  tools: CapabilityTool[];
}

export type CapabilityAvailabilityFilter = 'all' | 'available' | 'unavailable';

export type SandboxLanguage = 'python' | 'sh';

export interface CapabilityApprovalDraft {
  skillName: string;
  toolNames: string[];
  confirmed: boolean;
}

export interface CapabilityExecutionMessage {
  message: string;
  approvedTools: string[];
}

export interface ManagedSkill {
  name: string;
  version: string;
  description: string;
  instructions: string;
  allowed_tools: string[];
  required_roles: string[];
  required_secrets: string[];
  enabled: boolean;
  source: 'builtin' | 'custom';
  created_at: string;
  updated_at: string;
}

export interface ManagedSkillPayload {
  name?: string;
  description: string;
  instructions: string;
  allowed_tools: string[];
  required_roles: string[];
  required_secrets: string[];
  enabled: boolean;
}

export interface ManagedHttpTool {
  name: string;
  version: string;
  description: string;
  group: string;
  endpoint: string;
  method: 'GET' | 'POST';
  input_schema: Record<string, unknown>;
  static_headers: Record<string, string>;
  secret_headers: Record<string, string>;
  required_roles: string[];
  requires_approval: boolean;
  idempotent: boolean;
  timeout_seconds: number;
  max_response_bytes: number;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface ManagedHttpToolPayload {
  name?: string;
  description: string;
  group: string;
  endpoint: string;
  method: 'GET' | 'POST';
  input_schema: Record<string, unknown>;
  static_headers: Record<string, string>;
  secret_headers: Record<string, string>;
  required_roles: string[];
  requires_approval: boolean;
  idempotent: boolean;
  timeout_seconds: number;
  max_response_bytes: number;
  enabled: boolean;
}

export interface SqlAssistantConfig {
  enabled: boolean;
  dsn_secret_name: string;
  dsn_configured: boolean;
  expected_role: string;
  allowed_schemas: string[];
  allowed_tables: string[];
  sensitive_columns: string[];
  statement_timeout_seconds: number;
  max_rows: number;
  max_result_bytes: number;
  max_estimated_cost: number;
  max_estimated_rows: number;
  max_estimated_bytes: number;
  catalog_cache_ttl_seconds: number;
  updated_at: string;
}

export type SqlAssistantConfigPayload = Omit<SqlAssistantConfig, 'dsn_configured' | 'updated_at'>;

export interface BuiltinToolAdmin {
  name: string;
  description: string;
  group: string;
  version: string;
  required_roles: string[];
  requires_approval: boolean;
  network_policy: string;
  resource_scope: string;
}

export interface CapabilityControlPlane {
  schema_version: 1;
  web_research: {
    enabled: boolean;
    provider: 'tavily-keyless';
    api_key_required: false;
  };
  sql_assistant: SqlAssistantConfig;
  skills: ManagedSkill[];
  custom_tools: ManagedHttpTool[];
  builtin_tools: BuiltinToolAdmin[];
}

export interface CapabilityDeleteResponse {
  name: string;
  deleted: true;
}
