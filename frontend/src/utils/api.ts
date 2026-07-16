import axios from 'axios';
import { normalizePublicErrorInfo, type PublicErrorInfo } from '@/types/publicError';

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function headerValue(headers: unknown, name: string): unknown {
  const source = asRecord(headers);
  if (!source) return undefined;
  const getter = source.get;
  if (typeof getter === 'function') {
    const value = getter.call(headers, name);
    if (value !== null && value !== undefined) return value;
  }
  return source[name] ?? source[name.toLowerCase()];
}

function retryAfterSeconds(response: UnknownRecord | null): number | undefined {
  const value = headerValue(response?.headers, 'Retry-After');
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0) {
    return value;
  }
  if (typeof value !== 'string') return undefined;
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric >= 0) return numeric;
  const retryAt = Date.parse(value);
  if (!Number.isFinite(retryAt)) return undefined;
  return Math.max((retryAt - Date.now()) / 1000, 0);
}

export class PublicRequestError extends Error implements PublicErrorInfo {
  readonly code: string;
  readonly retryable: boolean;
  readonly category?: string;
  readonly stage?: string;
  readonly provider?: string;
  readonly retryAfterSeconds?: number;
  readonly requestId?: string;

  constructor(info: PublicErrorInfo) {
    super(info.message);
    this.name = 'PublicRequestError';
    this.code = info.code;
    this.retryable = info.retryable;
    this.category = info.category;
    this.stage = info.stage;
    this.provider = info.provider;
    this.retryAfterSeconds = info.retryAfterSeconds;
    this.requestId = info.requestId;
  }
}

export function getPublicError(error: unknown): PublicRequestError {
  if (error instanceof PublicRequestError) return error;

  const source = asRecord(error) || {};
  const response = asRecord(source.response);
  const responseData = asRecord(response?.data);
  const retryAfter = retryAfterSeconds(response);
  if (asRecord(responseData?.error)) {
    return new PublicRequestError(
      normalizePublicErrorInfo(responseData, { retryAfterSeconds: retryAfter })
    );
  }
  if (typeof responseData?.code === 'string') {
    return new PublicRequestError(
      normalizePublicErrorInfo(responseData, { retryAfterSeconds: retryAfter })
    );
  }

  const name = typeof source.name === 'string' ? source.name : '';
  const transportCode = typeof source.code === 'string' ? source.code : '';
  if (name === 'AbortError' || name === 'CanceledError' || transportCode === 'ERR_CANCELED') {
    return new PublicRequestError(
      normalizePublicErrorInfo({ code: 'REQUEST_CANCELLED', retryable: false })
    );
  }
  if (transportCode === 'ECONNABORTED' || transportCode === 'ETIMEDOUT') {
    return new PublicRequestError(
      normalizePublicErrorInfo({ code: 'REQUEST_TIMEOUT', retryable: true })
    );
  }
  if (
    typeof source.code === 'string' &&
    (typeof source.retryable === 'boolean' || typeof source.category === 'string')
  ) {
    return new PublicRequestError(normalizePublicErrorInfo(source));
  }

  const status = typeof response?.status === 'number' ? response.status : null;
  if (status === null) {
    return new PublicRequestError(
      normalizePublicErrorInfo({ code: 'NETWORK_UNAVAILABLE', retryable: true })
    );
  }

  const statusError =
    status === 401
      ? { code: 'AUTHENTICATION_REQUIRED', retryable: false }
      : status === 403
        ? { code: 'PERMISSION_DENIED', retryable: false }
        : status === 404
          ? { code: 'NOT_FOUND', retryable: false }
          : status === 409
            ? { code: 'CONFLICT', retryable: false }
            : status === 429
              ? { code: 'RATE_LIMITED', retryable: true }
              : status >= 500
                ? { code: 'INTERNAL_ERROR', retryable: true }
                : { code: 'INVALID_REQUEST', retryable: false };
  return new PublicRequestError(
    normalizePublicErrorInfo(statusError, { retryAfterSeconds: retryAfter })
  );
}

export async function getPublicErrorFromResponse(response: Response): Promise<PublicRequestError> {
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    data = undefined;
  }
  return getPublicError({
    response: {
      status: response.status,
      data,
      headers: response.headers,
    },
  });
}

const api = axios.create({
  timeout: 60000,
});

// Request interceptor to attach Bearer token
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('accessToken');
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => {
    return Promise.reject(getPublicError(error));
  }
);

// Response interceptor to handle session expiration (401)
api.interceptors.response.use(
  (response) => {
    return response;
  },
  (error) => {
    const publicError = getPublicError(error);
    if (publicError.code === 'AUTHENTICATION_REQUIRED') {
      localStorage.removeItem('accessToken');
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('unauthorized'));
      }
    }
    return Promise.reject(publicError);
  }
);

export default api;
