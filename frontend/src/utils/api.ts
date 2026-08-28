import axios, { AxiosHeaders, type InternalAxiosRequestConfig } from 'axios';
import { expireAuthSession, getAuthSession, refreshAuthSession } from '@/auth/session';
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
  withCredentials: true,
});

type AuthRetryConfig = InternalAxiosRequestConfig & {
  _authRetry?: boolean;
  _authRequestUsername?: string;
  _authExpectedAccessToken?: string;
  _authExpectedUsername?: string;
};

const AUTH_LIFECYCLE_PATHS = new Set([
  '/auth/login',
  '/auth/register',
  '/auth/refresh',
  '/auth/logout',
]);

function requestPath(url: string | undefined): string {
  if (!url) return '';
  try {
    return new URL(url, 'http://supermew.local').pathname.replace(/\/+$/, '') || '/';
  } catch {
    return url.split('?')[0].replace(/\/+$/, '') || '/';
  }
}

function isAuthLifecycleRequest(config: AuthRetryConfig | undefined): boolean {
  return AUTH_LIFECYCLE_PATHS.has(requestPath(config?.url));
}

function installAuthorization(config: AuthRetryConfig, token: string): void {
  config.headers = AxiosHeaders.from(config.headers);
  config.headers.set('Authorization', `Bearer ${token}`);
}

function removeAuthorization(config: AuthRetryConfig): void {
  config.headers = AxiosHeaders.from(config.headers);
  config.headers.delete('Authorization');
}

function requestAccessToken(config: AuthRetryConfig): string | undefined {
  const value = AxiosHeaders.from(config.headers).get('Authorization');
  if (typeof value !== 'string') return undefined;
  const match = /^Bearer\s+(.+)$/i.exec(value.trim());
  return match?.[1];
}

function authenticationSupersededError(): PublicRequestError {
  return new PublicRequestError(
    normalizePublicErrorInfo({ code: 'AUTHENTICATION_REQUIRED', retryable: false })
  );
}

function requestMatchesSession(
  config: AuthRetryConfig,
  requestToken: string | undefined,
  session: ReturnType<typeof getAuthSession>
): boolean {
  if (!requestToken) return session === null && config._authRequestUsername === undefined;
  return Boolean(
    session &&
    session.access_token === requestToken &&
    (!config._authRequestUsername || session.username === config._authRequestUsername)
  );
}

api.interceptors.request.use(
  (config) => {
    const authConfig = config as AuthRetryConfig;
    const session = getAuthSession();
    if (authConfig._authExpectedAccessToken) {
      if (
        !session ||
        session.access_token !== authConfig._authExpectedAccessToken ||
        session.username !== authConfig._authExpectedUsername
      ) {
        throw authenticationSupersededError();
      }
      installAuthorization(authConfig, authConfig._authExpectedAccessToken);
      authConfig._authRequestUsername = session.username;
      return authConfig;
    }
    if (session) {
      installAuthorization(authConfig, session.access_token);
      authConfig._authRequestUsername = session.username;
    } else {
      removeAuthorization(authConfig);
      delete authConfig._authRequestUsername;
    }
    return authConfig;
  },
  (error) => {
    return Promise.reject(getPublicError(error));
  }
);

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const publicError = getPublicError(error);
    const config = error?.config as AuthRetryConfig | undefined;
    if (
      publicError.code !== 'AUTHENTICATION_REQUIRED' ||
      !config ||
      isAuthLifecycleRequest(config)
    ) {
      return Promise.reject(publicError);
    }

    const requestToken = requestAccessToken(config);
    const sessionBeforeRefresh = getAuthSession();
    if (!requestMatchesSession(config, requestToken, sessionBeforeRefresh)) {
      return Promise.reject(publicError);
    }

    if (config._authRetry) {
      expireAuthSession(requestToken);
      return Promise.reject(publicError);
    }

    config._authRetry = true;
    const expectedUsername = config._authRequestUsername;
    try {
      const session = await refreshAuthSession();
      const activeSession = getAuthSession();
      if (
        !activeSession ||
        activeSession.access_token !== session.access_token ||
        activeSession.username !== session.username ||
        (expectedUsername !== undefined && session.username !== expectedUsername)
      ) {
        return Promise.reject(publicError);
      }
      config._authExpectedAccessToken = session.access_token;
      config._authExpectedUsername = session.username;
      installAuthorization(config, session.access_token);
      return api.request(config);
    } catch {
      const activeSession = getAuthSession();
      if (requestToken && activeSession?.access_token === requestToken) {
        expireAuthSession(requestToken);
      }
      return Promise.reject(publicError);
    }
  }
);

export default api;
