import axios from 'axios';

export interface AuthSession {
  access_token: string;
  username: string;
  role: 'user' | 'admin';
}

type AuthSessionListener = (session: AuthSession | null) => void;

interface WebLockManagerLike {
  request<T>(name: string, callback: () => T | PromiseLike<T>): Promise<T>;
}

interface RefreshAttempt {
  generation: number;
  expectedAccessToken: string | null;
  expectedUsername: string | null;
}

const credentialClient = axios.create({
  timeout: 60000,
  withCredentials: true,
});

const PENDING_REVOCATION_KEY = 'supermew-auth-revocation-pending';
const REFRESH_LOCK_NAME = 'supermew-auth-refresh-v1';
const listeners = new Set<AuthSessionListener>();
let currentSession: AuthSession | null = null;
let refreshPromise: Promise<AuthSession> | null = null;
let sessionGeneration = 0;
let revocationPending = readPendingRevocation();

export class AuthSessionSupersededError extends Error {
  constructor() {
    super('authentication session changed while refresh was in flight');
    this.name = 'AuthSessionSupersededError';
  }
}

export class AuthSessionSubjectMismatchError extends Error {
  constructor() {
    super('authentication refresh returned a different user');
    this.name = 'AuthSessionSubjectMismatchError';
  }
}

function readPendingRevocation(): boolean {
  try {
    return (
      typeof sessionStorage !== 'undefined' &&
      sessionStorage.getItem(PENDING_REVOCATION_KEY) === '1'
    );
  } catch {
    return false;
  }
}

function setPendingRevocation(pending: boolean): void {
  revocationPending = pending;
  try {
    if (typeof sessionStorage === 'undefined') return;
    if (pending) sessionStorage.setItem(PENDING_REVOCATION_KEY, '1');
    else sessionStorage.removeItem(PENDING_REVOCATION_KEY);
  } catch {
    // The in-memory tombstone still protects this page when storage is unavailable.
  }
}

function parseAuthSession(value: unknown): AuthSession {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError('authentication response is not an object');
  }
  const record = value as Record<string, unknown>;
  if (
    typeof record.access_token !== 'string' ||
    !record.access_token ||
    typeof record.username !== 'string' ||
    !record.username ||
    (record.role !== 'user' && record.role !== 'admin')
  ) {
    throw new TypeError('authentication response is malformed');
  }
  return {
    access_token: record.access_token,
    username: record.username,
    role: record.role,
  };
}

function publish(session: AuthSession | null) {
  currentSession = session;
  listeners.forEach((listener) => listener(session));
}

function webLockManager(): WebLockManagerLike | null {
  if (typeof navigator === 'undefined') return null;
  const candidate = (navigator as Navigator & { locks?: Partial<WebLockManagerLike> }).locks;
  return candidate && typeof candidate.request === 'function'
    ? (candidate as WebLockManagerLike)
    : null;
}

function withRefreshLock<T>(callback: () => T | PromiseLike<T>): Promise<T> {
  const manager = webLockManager();
  if (!manager) return Promise.resolve().then(callback);
  return manager.request(REFRESH_LOCK_NAME, callback);
}

function captureRefreshAttempt(): RefreshAttempt {
  return {
    generation: sessionGeneration,
    expectedAccessToken: currentSession?.access_token || null,
    expectedUsername: currentSession?.username || null,
  };
}

function assertRefreshAttemptCurrent(attempt: RefreshAttempt): void {
  if (revocationPending || sessionGeneration !== attempt.generation) {
    throw new AuthSessionSupersededError();
  }
  if (attempt.expectedAccessToken === null) {
    if (currentSession !== null) throw new AuthSessionSupersededError();
    return;
  }
  if (
    currentSession?.access_token !== attempt.expectedAccessToken ||
    currentSession.username !== attempt.expectedUsername
  ) {
    throw new AuthSessionSupersededError();
  }
}

async function executeRefresh(attempt: RefreshAttempt): Promise<AuthSession> {
  assertRefreshAttemptCurrent(attempt);
  const response = await credentialClient.post<AuthSession>('/auth/refresh', undefined);
  const session = parseAuthSession(response.data);
  assertRefreshAttemptCurrent(attempt);
  if (attempt.expectedUsername !== null && session.username !== attempt.expectedUsername) {
    throw new AuthSessionSubjectMismatchError();
  }
  sessionGeneration += 1;
  publish(session);
  return session;
}

export function getAuthSession(): AuthSession | null {
  return currentSession;
}

export function installAuthSession(value: unknown): AuthSession {
  const session = parseAuthSession(value);
  sessionGeneration += 1;
  setPendingRevocation(false);
  publish(session);
  return session;
}

export function clearAuthSession(): boolean {
  sessionGeneration += 1;
  const changed = currentSession !== null;
  if (changed) publish(null);
  return changed;
}

export function expireAuthSession(expectedAccessToken?: string): boolean {
  if (!currentSession) return false;
  if (expectedAccessToken && currentSession.access_token !== expectedAccessToken) return false;
  const changed = clearAuthSession();
  if (changed && typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('unauthorized'));
  }
  return changed;
}

export function subscribeAuthSession(listener: AuthSessionListener): () => void {
  listeners.add(listener);
  listener(currentSession);
  return () => listeners.delete(listener);
}

export function refreshAuthSession(): Promise<AuthSession> {
  if (revocationPending) return Promise.reject(new AuthSessionSupersededError());
  if (refreshPromise) return refreshPromise;
  const attempt = captureRefreshAttempt();
  const operation = withRefreshLock(() => executeRefresh(attempt));
  const shared = operation.finally(() => {
    if (refreshPromise === shared) refreshPromise = null;
  });
  refreshPromise = shared;
  return shared;
}

export async function revokeRefreshSession(): Promise<void> {
  setPendingRevocation(true);
  clearAuthSession();
  const inFlightRefresh = refreshPromise;
  if (inFlightRefresh) {
    try {
      await inFlightRefresh;
    } catch {
      // Its response (including any rotated Set-Cookie) has settled. The
      // following logout now revokes the newest browser credential.
    }
  }
  await withRefreshLock(() => credentialClient.post('/auth/logout', undefined));
  setPendingRevocation(false);
}

export async function restoreAuthSession(): Promise<AuthSession | null> {
  if (revocationPending) {
    try {
      await revokeRefreshSession();
    } catch {
      // Stay locally signed out and retry revocation on the next page restore.
    }
    return null;
  }
  return refreshAuthSession();
}
