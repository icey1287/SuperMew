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
