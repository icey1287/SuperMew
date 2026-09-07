export interface PublicErrorInfo {
  code: string;
  message: string;
  retryable: boolean;
  category?: string;
  stage?: string;
  provider?: string;
  retryAfterSeconds?: number;
  requestId?: string;
}

type UnknownRecord = Record<string, unknown>;

const CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/;

const WEB_ERROR_MESSAGES: Record<string, string> = {
  WEB_SEARCH_UNAVAILABLE: '网络搜索请求失败，未能获取本次搜索结果',
  WEB_FETCH_UNAVAILABLE: '网页内容提取失败，未能获取本次来源内容',
  WEB_INVALID_SEARCH_RESPONSE: '网络搜索服务返回了无法解析的结果',
  WEB_INVALID_EXTRACT_RESPONSE: '网页内容提取服务返回了无法解析的结果',
  WEB_INVALID_CONTENT: '网页内容无效，无法作为回答依据',
  WEB_DEADLINE_EXCEEDED: '本次网络检索已达到运行时限',
  WEB_RESEARCH_DISABLED: '网络检索能力尚未启用',
  WEB_RESEARCH_CLOSED: '网络检索服务已关闭',
  WEB_RESEARCH_NOT_STARTED: '网络检索服务尚未启动',
  WEB_RESEARCH_RUNTIME_NOT_CONFIGURED: '网络检索服务尚未配置',
  WEB_INVALID_INPUT: '网络检索参数无效，请调整查询',
  WEB_INPUT_TOO_LARGE: '网络检索输入过长，请缩小查询范围',
  WEB_OUTPUT_TOO_LARGE: '网络检索结果超过大小限制',
  WEB_INVALID_EVIDENCE: '网络检索证据无效，无法作为回答依据',
  WEB_INVALID_SOURCE_ID: '网络来源编号格式无效',
  WEB_SOURCE_NOT_FOUND: '该网络来源不属于本次运行或已失效',
  WEB_SOURCE_UNKNOWN: '回答引用了本次运行中未知的网络来源',
  WEB_SOURCE_CONTEXT_CLOSED: '本次运行的网络来源上下文已关闭',
  WEB_SOURCE_LIMIT: '本次运行的网络来源数量已达到上限',
  WEB_SOURCE_INVALID_CONTENT: '网络来源内容无效，无法作为回答依据',
  WEB_FETCH_BUDGET_EXHAUSTED: '本次运行的网页提取额度已用完，请基于已有结果回答',
};

const DEFAULT_MESSAGES: Record<string, string> = {
  ...WEB_ERROR_MESSAGES,
  INVALID_REQUEST: '请求参数不正确，请检查后重试',
  AUTHENTICATION_REQUIRED: '登录已过期，请重新登录',
  PERMISSION_DENIED: '当前账号无权执行此操作',
  NOT_FOUND: '请求的资源不存在',
  CONFLICT: '当前状态已发生变化，请刷新后重试',
  RATE_LIMITED: '请求过于频繁，请稍后重试',
  MODEL_RATE_LIMITED: '上游模型服务当前繁忙，请稍后重试',
  MODEL_TIMEOUT: '上游模型响应超时，请稍后重试',
  MODEL_UNAVAILABLE: '模型服务暂时不可用，请稍后重试',
  MODEL_CALL_LIMIT_EXCEEDED: '模型调用次数达到本次运行上限，请缩小问题范围后重试',
  EMBEDDING_UNAVAILABLE: '向量化服务暂时不可用，请稍后重试',
  VECTOR_STORE_UNAVAILABLE: '知识检索服务暂时不可用，请稍后重试',
  RERANK_TIMEOUT: '相关性排序服务响应超时',
  RERANK_RATE_LIMITED: '相关性排序服务当前繁忙，请稍后重试',
  RERANK_UNAVAILABLE: '相关性排序服务暂时不可用，已使用原始排序',
  TOOL_TIMEOUT: '工具执行超时，请稍后重试',
  TOOL_UNAVAILABLE: '工具服务暂时不可用，请稍后重试',
  PROVIDER_TIMEOUT: '运行截止时间已到，已停止等待上游服务',
  PROVIDER_AUTHENTICATION_FAILED: '上游服务配置不可用，请联系管理员',
  PROVIDER_REQUEST_INVALID: '上游服务拒绝了当前请求，请联系管理员',
  WEB_TOOL_RESULT_CONTEXT_BUDGET_EXCEEDED: '搜索结果超过上下文预算，请缩小搜索范围后重试',
  CONTEXT_TRIMMED: '已自动整理较早上下文以继续运行',
  POLICY_DENIED: '当前操作被安全策略拒绝',
  TOOL_POLICY_DENIED: '当前工具不在本次运行的可用范围内',
  TOOL_GUARDRAIL_DENIED: '当前工具调用未通过安全策略',
  TOOL_APPROVAL_REQUIRED: '当前工具需要人工批准后才能执行',
  RUN_CANCELLED: '运行已取消',
  REQUEST_CANCELLED: '请求已取消',
  REQUEST_TIMEOUT: '请求超时，请稍后重试',
  NETWORK_UNAVAILABLE: '无法连接服务，请检查网络后重试',
  STREAM_PROTOCOL_ERROR: '服务返回了无法识别的事件流，请刷新后重试',
  RUN_EXECUTION_FAILED: '运行失败，请稍后重试',
  TOOL_EXECUTION_FAILED: '工具执行失败',
  INTERNAL_ERROR: '服务暂时不可用，请稍后重试',
};

const PROVIDER_CODES = new Set([
  'MODEL_RATE_LIMITED',
  'MODEL_TIMEOUT',
  'MODEL_UNAVAILABLE',
  'EMBEDDING_UNAVAILABLE',
  'VECTOR_STORE_UNAVAILABLE',
  'RERANK_TIMEOUT',
  'RERANK_RATE_LIMITED',
  'RERANK_UNAVAILABLE',
  'TOOL_TIMEOUT',
  'TOOL_UNAVAILABLE',
  'PROVIDER_TIMEOUT',
  'PROVIDER_AUTHENTICATION_FAILED',
  'PROVIDER_REQUEST_INVALID',
]);

const FIXED_CLIENT_MESSAGE_CODES = new Set([
  ...Object.keys(WEB_ERROR_MESSAGES),
  'WEB_TOOL_RESULT_CONTEXT_BUDGET_EXCEEDED',
  'CONTEXT_TRIMMED',
  'TOOL_POLICY_DENIED',
  'TOOL_GUARDRAIL_DENIED',
  'TOOL_APPROVAL_REQUIRED',
]);

const RETRYABLE_CODES = new Set([
  'RATE_LIMITED',
  'MODEL_RATE_LIMITED',
  'MODEL_TIMEOUT',
  'MODEL_UNAVAILABLE',
  'EMBEDDING_UNAVAILABLE',
  'VECTOR_STORE_UNAVAILABLE',
  'RERANK_TIMEOUT',
  'RERANK_RATE_LIMITED',
  'RERANK_UNAVAILABLE',
  'TOOL_TIMEOUT',
  'TOOL_UNAVAILABLE',
  'REQUEST_TIMEOUT',
  'NETWORK_UNAVAILABLE',
  'RUN_EXECUTION_FAILED',
  'INTERNAL_ERROR',
]);

function asRecord(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function safeString(value: unknown, maxLength = 240): string | undefined {
  if (typeof value !== 'string') return undefined;
  const compact = value.trim();
  return compact ? compact.slice(0, maxLength) : undefined;
}

function optionalNumber(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return undefined;
  return value;
}

export function publicErrorMessage(code: string): string {
  return DEFAULT_MESSAGES[code] || DEFAULT_MESSAGES.INTERNAL_ERROR;
}

export function normalizePublicErrorInfo(
  value: unknown,
  defaults: Partial<PublicErrorInfo> = {}
): PublicErrorInfo {
  const outer = asRecord(value) || {};
  const nested = asRecord(outer.error);
  const source = nested || outer;
  const rawCode = safeString(source.code ?? outer.error_code ?? defaults.code, 64);
  const code = rawCode && CODE_PATTERN.test(rawCode) ? rawCode : 'INTERNAL_ERROR';
  const category = safeString(source.category ?? defaults.category, 64);
  const serverMessage = safeString(source.message ?? defaults.message, 500);
  const message =
    category === 'provider' || PROVIDER_CODES.has(code) || FIXED_CLIENT_MESSAGE_CODES.has(code)
      ? publicErrorMessage(code)
      : serverMessage || publicErrorMessage(code);
  const retryable =
    typeof source.retryable === 'boolean'
      ? source.retryable
      : typeof defaults.retryable === 'boolean'
        ? defaults.retryable
        : RETRYABLE_CODES.has(code);
  const stage = safeString(source.stage ?? defaults.stage, 64);
  const provider = safeString(source.provider ?? defaults.provider, 80);
  const sourceRetryAfterSeconds = optionalNumber(
    source.retry_after ?? source.retry_after_seconds ?? source.retryAfterSeconds
  );
  const defaultRetryAfterSeconds = optionalNumber(defaults.retryAfterSeconds);
  const retryAfterSeconds =
    sourceRetryAfterSeconds === undefined
      ? defaultRetryAfterSeconds
      : defaultRetryAfterSeconds === undefined
        ? sourceRetryAfterSeconds
        : Math.max(sourceRetryAfterSeconds, defaultRetryAfterSeconds);
  const requestId = safeString(source.request_id ?? source.requestId ?? defaults.requestId, 120);

  return {
    code,
    message,
    retryable,
    ...(category ? { category } : {}),
    ...(stage ? { stage } : {}),
    ...(provider ? { provider } : {}),
    ...(retryAfterSeconds !== undefined ? { retryAfterSeconds } : {}),
    ...(requestId ? { requestId } : {}),
  };
}
