import axios, {
  AxiosError,
  AxiosHeaders,
  type AxiosAdapter,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

type AdapterHandler = (config: InternalAxiosRequestConfig) => Promise<AxiosResponse>;

const originalAdapter = axios.defaults.adapter;
let handleRequest: AdapterHandler;
let authSession: typeof import('@/auth/session');
let api: typeof import('./api').default;
let dispatchEvent: ReturnType<typeof vi.fn>;

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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function authorization(config: InternalAxiosRequestConfig): string {
  const value = AxiosHeaders.from(config.headers).get('Authorization');
  return typeof value === 'string' ? value : '';
}

beforeAll(async () => {
  axios.defaults.adapter = ((config) => handleRequest(config)) as AxiosAdapter;
  authSession = await import('@/auth/session');
  ({ default: api } = await import('./api'));
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
  handleRequest = (config) => rejectResponse(config, 500, { detail: 'unexpected request' });
  dispatchEvent = vi.fn();
  vi.stubGlobal('window', { dispatchEvent });
  vi.stubGlobal(
    'CustomEvent',
    class TestCustomEvent {
      constructor(readonly type: string) {}
    }
  );
  vi.stubGlobal('localStorage', {
    getItem: vi.fn(() => {
      throw new Error('api must not read localStorage');
    }),
    removeItem: vi.fn(() => {
      throw new Error('api must not mutate localStorage');
    }),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('authenticated Axios lifecycle', () => {
  it('uses credentialed requests and the in-memory Bearer token', async () => {
    authSession.installAuthSession({
      access_token: 'memory-access',
      username: 'alice',
      role: 'user',
    });
    handleRequest = async (config) => {
      expect(config.withCredentials).toBe(true);
      expect(authorization(config)).toBe('Bearer memory-access');
      return response(config, { ok: true });
    };

    await expect(api.get('/protected')).resolves.toMatchObject({ data: { ok: true } });
    expect(api.defaults.withCredentials).toBe(true);
  });

  it('single-flights concurrent refreshes and retries each request once with the new token', async () => {
    authSession.installAuthSession({
      access_token: 'expired-access',
      username: 'alice',
      role: 'user',
    });
    const releaseRefresh = deferred<void>();
    let refreshCalls = 0;
    const protectedCalls = new Map<string, number>();
    handleRequest = async (config) => {
      if (config.url === '/auth/refresh') {
        refreshCalls += 1;
        expect(config.withCredentials).toBe(true);
        await releaseRefresh.promise;
        return response(config, {
          access_token: 'fresh-access',
          username: 'alice',
          role: 'user',
        });
      }
      const url = config.url || '';
      protectedCalls.set(url, (protectedCalls.get(url) || 0) + 1);
      if (authorization(config) === 'Bearer fresh-access') {
        return response(config, { url });
      }
      return rejectResponse(config, 401, { detail: 'expired access token' });
    };

    const first = api.get('/protected/one');
    const second = api.get('/protected/two');
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    releaseRefresh.resolve();

    await expect(Promise.all([first, second])).resolves.toHaveLength(2);
    expect(refreshCalls).toBe(1);
    expect(protectedCalls).toEqual(
      new Map([
        ['/protected/one', 2],
        ['/protected/two', 2],
      ])
    );
    expect(authSession.getAuthSession()?.access_token).toBe('fresh-access');
    expect(dispatchEvent).not.toHaveBeenCalled();
  });

  it('never retries the same request more than once', async () => {
    authSession.installAuthSession({
      access_token: 'expired-access',
      username: 'alice',
      role: 'user',
    });
    let protectedCalls = 0;
    let refreshCalls = 0;
    handleRequest = async (config) => {
      if (config.url === '/auth/refresh') {
        refreshCalls += 1;
        return response(config, {
          access_token: 'still-rejected-access',
          username: 'alice',
          role: 'user',
        });
      }
      protectedCalls += 1;
      return rejectResponse(config, 401, { detail: 'unauthorized' });
    };

    await expect(api.get('/protected')).rejects.toMatchObject({
      code: 'AUTHENTICATION_REQUIRED',
    });

    expect(refreshCalls).toBe(1);
    expect(protectedCalls).toBe(2);
    expect(authSession.getAuthSession()).toBeNull();
    expect(dispatchEvent).toHaveBeenCalledOnce();
  });

  it('broadcasts unauthorized once when a shared refresh fails', async () => {
    authSession.installAuthSession({
      access_token: 'expired-access',
      username: 'alice',
      role: 'user',
    });
    const releaseRefresh = deferred<void>();
    let refreshCalls = 0;
    handleRequest = async (config) => {
      if (config.url === '/auth/refresh') {
        refreshCalls += 1;
        await releaseRefresh.promise;
        return rejectResponse(config, 401, { detail: 'refresh revoked' });
      }
      return rejectResponse(config, 401, { detail: 'access expired' });
    };

    const requests = [api.get('/protected/one'), api.get('/protected/two')];
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    releaseRefresh.resolve();
    const results = await Promise.allSettled(requests);

    expect(results.every((result) => result.status === 'rejected')).toBe(true);
    expect(refreshCalls).toBe(1);
    expect(authSession.getAuthSession()).toBeNull();
    expect(dispatchEvent).toHaveBeenCalledOnce();
  });

  it('does not clear a newer login when an older refresh finishes late', async () => {
    authSession.installAuthSession({
      access_token: 'old-access',
      username: 'old-user',
      role: 'user',
    });
    const releaseRefresh = deferred<void>();
    let refreshCalls = 0;
    handleRequest = async (config) => {
      if (config.url === '/auth/refresh') {
        refreshCalls += 1;
        await releaseRefresh.promise;
        return response(config, {
          access_token: 'stale-refreshed-access',
          username: 'old-user',
          role: 'user',
        });
      }
      return rejectResponse(config, 401, { detail: 'old access expired' });
    };

    const request = api.get('/protected');
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    authSession.installAuthSession({
      access_token: 'new-login-access',
      username: 'new-user',
      role: 'admin',
    });
    releaseRefresh.resolve();

    await expect(request).rejects.toMatchObject({ code: 'AUTHENTICATION_REQUIRED' });
    expect(authSession.getAuthSession()).toEqual({
      access_token: 'new-login-access',
      username: 'new-user',
      role: 'admin',
    });
    expect(dispatchEvent).not.toHaveBeenCalled();
  });

  it('does not clear a newer login when an anonymous refresh is superseded', async () => {
    const releaseRefresh = deferred<void>();
    let refreshCalls = 0;
    handleRequest = async (config) => {
      if (config.url === '/auth/refresh') {
        refreshCalls += 1;
        await releaseRefresh.promise;
        return response(config, {
          access_token: 'anonymous-cookie-access',
          username: 'alice',
          role: 'user',
        });
      }
      return rejectResponse(config, 401, { detail: 'authentication required' });
    };

    const request = api.get('/protected');
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    authSession.installAuthSession({
      access_token: 'bob-access',
      username: 'bob',
      role: 'admin',
    });
    releaseRefresh.resolve();

    await expect(request).rejects.toMatchObject({ code: 'AUTHENTICATION_REQUIRED' });
    expect(authSession.getAuthSession()).toEqual({
      access_token: 'bob-access',
      username: 'bob',
      role: 'admin',
    });
    expect(dispatchEvent).not.toHaveBeenCalled();
  });

  it('does not refresh an old Alice request after Bob has already logged in', async () => {
    authSession.installAuthSession({
      access_token: 'alice-access',
      username: 'alice',
      role: 'user',
    });
    const releaseResponse = deferred<void>();
    let protectedCalls = 0;
    let refreshCalls = 0;
    handleRequest = async (config) => {
      if (config.url === '/auth/refresh') {
        refreshCalls += 1;
        return response(config, {
          access_token: 'unexpected-refresh',
          username: 'bob',
          role: 'admin',
        });
      }
      protectedCalls += 1;
      await releaseResponse.promise;
      return rejectResponse(config, 401, { detail: 'Alice access expired' });
    };

    const request = api.get('/protected');
    await vi.waitFor(() => expect(protectedCalls).toBe(1));
    authSession.installAuthSession({
      access_token: 'bob-access',
      username: 'bob',
      role: 'admin',
    });
    releaseResponse.resolve();

    await expect(request).rejects.toMatchObject({ code: 'AUTHENTICATION_REQUIRED' });
    expect(refreshCalls).toBe(0);
    expect(protectedCalls).toBe(1);
    expect(authSession.getAuthSession()).toEqual({
      access_token: 'bob-access',
      username: 'bob',
      role: 'admin',
    });
    expect(dispatchEvent).not.toHaveBeenCalled();
  });

  it('rejects a cross-subject refresh response without retrying as the new username', async () => {
    authSession.installAuthSession({
      access_token: 'alice-access',
      username: 'alice',
      role: 'user',
    });
    let protectedCalls = 0;
    let refreshCalls = 0;
    handleRequest = async (config) => {
      if (config.url === '/auth/refresh') {
        refreshCalls += 1;
        return response(config, {
          access_token: 'bob-access',
          username: 'bob',
          role: 'admin',
        });
      }
      protectedCalls += 1;
      if (protectedCalls > 1) return response(config, { leaked: authorization(config) });
      return rejectResponse(config, 401, { detail: 'Alice access expired' });
    };

    await expect(api.get('/protected')).rejects.toMatchObject({
      code: 'AUTHENTICATION_REQUIRED',
    });

    expect(refreshCalls).toBe(1);
    expect(protectedCalls).toBe(1);
    expect(authSession.getAuthSession()?.username).not.toBe('bob');
  });

  it('rechecks the refreshed token and subject immediately before retrying', async () => {
    authSession.installAuthSession({
      access_token: 'alice-access',
      username: 'alice',
      role: 'user',
    });
    let protectedCalls = 0;
    let refreshCalls = 0;
    const stop = authSession.subscribeAuthSession((session) => {
      if (session?.access_token !== 'alice-refreshed') return;
      authSession.installAuthSession({
        access_token: 'bob-access',
        username: 'bob',
        role: 'admin',
      });
    });
    handleRequest = async (config) => {
      if (config.url === '/auth/refresh') {
        refreshCalls += 1;
        return response(config, {
          access_token: 'alice-refreshed',
          username: 'alice',
          role: 'user',
        });
      }
      protectedCalls += 1;
      if (protectedCalls > 1) return response(config, { leaked: authorization(config) });
      return rejectResponse(config, 401, { detail: 'Alice access expired' });
    };

    try {
      await expect(api.get('/protected')).rejects.toMatchObject({
        code: 'AUTHENTICATION_REQUIRED',
      });
    } finally {
      stop();
    }

    expect(refreshCalls).toBe(1);
    expect(protectedCalls).toBe(1);
    expect(authSession.getAuthSession()).toEqual({
      access_token: 'bob-access',
      username: 'bob',
      role: 'admin',
    });
    expect(dispatchEvent).not.toHaveBeenCalled();
  });

  it('does not refresh or clear the active session for login failures', async () => {
    authSession.installAuthSession({
      access_token: 'existing-access',
      username: 'alice',
      role: 'user',
    });
    let refreshCalls = 0;
    handleRequest = (config) => {
      if (config.url === '/auth/refresh') refreshCalls += 1;
      return rejectResponse(config, 401, { detail: 'bad credentials' });
    };

    await expect(api.post('/auth/login', {})).rejects.toMatchObject({
      code: 'AUTHENTICATION_REQUIRED',
    });

    expect(refreshCalls).toBe(0);
    expect(authSession.getAuthSession()?.access_token).toBe('existing-access');
    expect(dispatchEvent).not.toHaveBeenCalled();
  });
});
