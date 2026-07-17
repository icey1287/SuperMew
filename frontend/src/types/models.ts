export type ModelRole = 'answer' | 'fast' | 'grader' | 'evaluator';

export interface ModelProfile {
  id: string;
  display_name: string;
  provider: 'openai';
  model_name: string;
  base_url: string;
  timeout_seconds: number;
  supports_stream: boolean;
  supports_structured_output: boolean;
  enabled: boolean;
  source: 'environment' | 'user';
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ModelRoleRequirement {
  supports_stream: boolean;
  supports_structured_output: boolean;
  temperature: number;
}

export interface ModelControlPlane {
  schema_version: 1;
  catalog_hash: string;
  api_key_configured: boolean;
  profiles: ModelProfile[];
  assignments: Record<ModelRole, ModelProfile | null>;
  requirements: Record<ModelRole, ModelRoleRequirement>;
}

export interface ModelProfilePayload {
  display_name: string;
  provider: 'openai';
  model_name: string;
  base_url: string;
  timeout_seconds: number;
  supports_stream: boolean;
  supports_structured_output: boolean;
  enabled: boolean;
}

export interface ModelAssignmentPayload {
  profile_id: string;
}
