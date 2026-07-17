import axios, {
  AxiosError,
  AxiosHeaders,
  type AxiosAdapter,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios';
import { createPinia, disposePinia, setActivePinia, type Pinia } from 'pinia';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

type AdapterHandler = (config: InternalAxiosRequestConfig) => Promise<AxiosResponse>;

const originalAdapter = axios.defaults.adapter;
let handleRequest: AdapterHandler;
let pinia: Pinia;
let authSession: typeof import('@/auth/session');
let useAuthStore: typeof import('./auth').useAuthStore;

function response(config: InternalAxiosRequestConfig, data: unknown, status = 200): AxiosResponse {
  return {
    data,
    status,
    statusText: String(status),
    headers: new AxiosHeaders(),
    config,
  };
}

function rejectResponse(
  config: InternalAxiosRequestConfig,
  status: number,
  data: unknown
): Promise<never> {
  return Promise.reject(
    new AxiosError(
      `Request failed with status code ${status}`,
      AxiosError.ERR_BAD_REQUEST,
      config,
      undefined,
      response(config, data, status)
    )
  );
}

function jsonBody(config: InternalAxiosRequestConfig): Record<string, unknown> {
  if (typeof config.data === 'string') return JSON.parse(config.data) as Record<string, unknown>;
  return (config.data || {}) as Record<string, unknown>;
}

beforeAll(async () => {
  axios.defaults.adapter = ((config) => handleRequest(config)) as AxiosAdapter;
  authSession = await import('@/auth/session');
  ({ useAuthStore } = await import('./auth'));
});

afterAll(() => {
  axios.defaults.adapter = originalAdapter;
});

beforeEach(() => {
  authSession.installAuthSession({
    access_token: 'reset-access',
    username: 'reset-user',
    role: 'user',
  });
  authSession.clearAuthSession();
  pinia = createPinia();
  setActivePinia(pinia);
  handleRequest = (config) => rejectResponse(config, 500, { detail: 'unexpected request' });
  vi.stubGlobal('localStorage', {
    getItem: vi.fn(() => {
      throw new Error('authentication must not read localStorage');
    }),
    setItem: vi.fn(() => {
      throw new Error('authentication must not write localStorage');
    }),
    removeItem: vi.fn(() => {
      throw new Error('authentication must not remove localStorage keys');
    }),
  });
});

afterEach(() => {
  disposePinia(pinia);
  vi.unstubAllGlobals();
});

describe('authentication store lifecycle', () => {
  it('silently restores a credentialed session before resolving authentication', async () => {
    handleRequest = async (config) => {
      expect(config.url).toBe('/auth/refresh');
      expect(config.withCredentials).toBe(true);
      return response(config, {
        access_token: 'restored-access',
        username: 'restored-user',
        role: 'admin',
      });
    };
    const store = useAuthStore();

    expect(store.authResolved).toBe(false);
    expect(store.isAuthenticated).toBe(false);
    await store.restoreSession();

    expect(store.authResolved).toBe(true);
    expect(store.token).toBe('restored-access');
    expect(store.currentUser).toEqual({ username: 'restored-user', role: 'admin' });
    expect(store.isAdmin).toBe(true);
  });

  it('resolves to an unauthenticated state when silent restore fails', async () => {
    handleRequest = (config) => rejectResponse(config, 401, { detail: 'expired refresh token' });
    const store = useAuthStore();

    await expect(store.restoreSession()).resolves.toBeUndefined();

    expect(store.authResolved).toBe(true);
    expect(store.token).toBe('');
    expect(store.currentUser).toBeNull();
    expect(store.isAuthenticated).toBe(false);
  });

  it('installs login responses into the shared in-memory session', async () => {
    handleRequest = async (config) => {
      expect(config.url).toBe('/auth/login');
      expect(config.withCredentials).toBe(true);
      expect(jsonBody(config)).toEqual({ username: 'alice', password: ' safe-password ' });
      return response(config, {
        access_token: 'login-access',
        username: 'alice',
        role: 'user',
      });
    };
    const store = useAuthStore();
    store.authForm.username = ' alice ';
    store.authForm.password = ' safe-password ';

    await store.handleAuthSubmit();

    expect(store.token).toBe('login-access');
    expect(store.currentUser).toEqual({ username: 'alice', role: 'user' });
    expect(store.authForm.password).toBe('');
    expect(authSession.getAuthSession()?.access_token).toBe('login-access');
  });

  it('installs registration responses and forwards role metadata', async () => {
    handleRequest = async (config) => {
      expect(config.url).toBe('/auth/register');
      expect(jsonBody(config)).toEqual({
        username: 'admin-user',
        password: 'safe-password',
        role: 'admin',
        admin_code: 'invite-code',
      });
      return response(config, {
        access_token: 'register-access',
        username: 'admin-user',
        role: 'admin',
      });
    };
    const store = useAuthStore();
    store.authMode = 'register';
    store.authForm.username = 'admin-user';
    store.authForm.password = 'safe-password';
    store.authForm.role = 'admin';
    store.authForm.admin_code = 'invite-code';

    await store.handleAuthSubmit();

    expect(store.token).toBe('register-access');
    expect(store.isAdmin).toBe(true);
    expect(store.authForm.admin_code).toBe('');
  });

  it('always clears local state when server-side logout fails', async () => {
    authSession.installAuthSession({
      access_token: 'active-access',
      username: 'alice',
      role: 'user',
    });
    handleRequest = (config) => rejectResponse(config, 503, { detail: 'unavailable' });
    const store = useAuthStore();
    expect(store.isAuthenticated).toBe(true);

    await expect(store.handleLogout()).resolves.toBeUndefined();

    expect(store.isAuthenticated).toBe(false);
    expect(store.token).toBe('');
    expect(authSession.getAuthSession()).toBeNull();
  });
});
