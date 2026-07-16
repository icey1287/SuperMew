// Generated from contracts/tool_result_v1.json. Do not edit by hand.
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface ToolArtifactV1 {
  artifact_id: string;
  name: string;
  media_type: string;
  uri?: string | null;
  size_bytes?: number | null;
  sha256?: string | null;
  metadata?: Record<string, JsonValue>;
}

interface ToolResultBaseV1 {
  schema_version: 1;
  data: JsonValue;
  duration_ms: number;
  artifacts: ToolArtifactV1[];
  observability_metadata: Record<string, JsonValue>;
}

export interface ToolSuccessResultV1 extends ToolResultBaseV1 {
  success: true;
  error_code: null;
  retryable: false;
}

export interface ToolFailureResultV1 extends ToolResultBaseV1 {
  success: false;
  error_code: string;
  retryable: boolean;
}

export type ToolResultV1 = ToolSuccessResultV1 | ToolFailureResultV1;
