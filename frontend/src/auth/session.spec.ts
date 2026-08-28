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
let authSession: typeof import('./session');

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
  const result = response(config, data, status);
  return Promise.reject(
    new AxiosError(
      `Request failed with status code ${status}`,
      AxiosError.ERR_BAD_REQUEST,
      config,
      undefined,
      result
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

function createWebLockManager() {
  let tail = Promise.resolve();
  let active = 0;
  let maximumActive = 0;
  const names: string[] = [];
  const request = vi.fn(
    (name: string, callback: () => unknown | Promise<unknown>): Promise<unknown> => {
      names.push(name);
      const result = tail.then(async () => {
        active += 1;
        maximumActive = Math.max(maximumActive, active);
        try {
          return await callback();
        } finally {
          active -= 1;
        }
      });
      tail = result.then(
        () => undefined,
        () => undefined
      );
      return result;
    }
  );
  return {
    names,
    request,
    get maximumActive() {
      return maximumActive;
    },
  };
}

async function importFreshAuthSession() {
  vi.resetModules();
  const freshAxios = (await import('axios')).default;
  freshAxios.defaults.adapter = ((config) => handleRequest(config)) as AxiosAdapter;
  return import('./session');
}

beforeAll(async () => {
  axios.defaults.adapter = ((config) => handleRequest(config)) as AxiosAdapter;
  authSession = await import('./session');
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
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('in-memory authentication session', () => {
  it('publishes installed and cleared snapshots without persistent storage', () => {
    const snapshots: Array<string | null> = [];
    const stop = authSession.subscribeAuthSession((session) => {
      snapshots.push(session?.access_token || null);
    });

    const installed = authSession.installAuthSession({
      access_token: 'access-one',
      username: 'alice',
      role: 'admin',
    });
    expect(authSession.getAuthSession()).toEqual(installed);
    expect(authSession.clearAuthSession()).toBe(true);
    expect(authSession.clearAuthSession()).toBe(false);
    stop();

    expect(snapshots).toEqual([null, 'access-one', null]);
  });

  it('shares one refresh request and installs the rotated access token', async () => {
    const release = deferred<void>();
    let refreshCalls = 0;
    let usedCredentials = false;
    handleRequest = async (config) => {
      expect(config.url).toBe('/auth/refresh');
      refreshCalls += 1;
      usedCredentials = config.withCredentials === true;
      await release.promise;
      return response(config, {
        access_token: 'rotated-access',
        username: 'alice',
        role: 'user',
      });
    };

    const first = authSession.refreshAuthSession();
    const second = authSession.refreshAuthSession();
    expect(first).toBe(second);
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    release.resolve();

    await expect(Promise.all([first, second])).resolves.toEqual([
      expect.objectContaining({ access_token: 'rotated-access' }),
      expect.objectContaining({ access_token: 'rotated-access' }),
    ]);
    expect(usedCredentials).toBe(true);
    expect(authSession.getAuthSession()?.access_token).toBe('rotated-access');
  });

  it('serializes refresh cookie rotation across independent tab runtimes', async () => {
    const locks = createWebLockManager();
    vi.stubGlobal('navigator', { locks });
    const firstTab = authSession;
    const secondTab = await importFreshAuthSession();
    firstTab.installAuthSession({
      access_token: 'alice-tab-one',
      username: 'alice',
      role: 'user',
    });
    secondTab.installAuthSession({
      access_token: 'alice-tab-two',
      username: 'alice',
      role: 'user',
    });
    const releaseFirst = deferred<void>();
    let refreshCalls = 0;
    let activeRequests = 0;
    let maximumActiveRequests = 0;
    handleRequest = async (config) => {
      expect(config.url).toBe('/auth/refresh');
      refreshCalls += 1;
      activeRequests += 1;
      maximumActiveRequests = Math.max(maximumActiveRequests, activeRequests);
      try {
        if (refreshCalls === 1) await releaseFirst.promise;
        return response(config, {
          access_token: `alice-rotated-${refreshCalls}`,
          username: 'alice',
          role: 'user',
        });
      } finally {
        activeRequests -= 1;
      }
    };

    const first = firstTab.refreshAuthSession();
    const second = secondTab.refreshAuthSession();
    try {
      await vi.waitFor(() => expect(refreshCalls).toBe(1));
      expect(maximumActiveRequests).toBe(1);
    } finally {
      releaseFirst.resolve();
    }

    await expect(Promise.all([first, second])).resolves.toHaveLength(2);
    expect(refreshCalls).toBe(2);
    expect(maximumActiveRequests).toBe(1);
    expect(locks.maximumActive).toBe(1);
    expect(new Set(locks.names)).toEqual(new Set(['supermew-auth-refresh-v1']));
  });

  it('does not send a queued refresh after the local session changes while waiting for the lock', async () => {
    const locks = createWebLockManager();
    vi.stubGlobal('navigator', { locks });
    const lockAcquired = deferred<void>();
    const releaseLock = deferred<void>();
    const holder = locks.request('supermew-auth-refresh-v1', async () => {
      lockAcquired.resolve();
      await releaseLock.promise;
    });
    await lockAcquired.promise;
    authSession.installAuthSession({
      access_token: 'alice-access',
      username: 'alice',
      role: 'user',
    });
    let refreshCalls = 0;
    handleRequest = async (config) => {
      refreshCalls += 1;
      return response(config, {
        access_token: 'must-not-be-installed',
        username: 'alice',
        role: 'user',
      });
    };

    const refreshing = authSession.refreshAuthSession();
    authSession.installAuthSession({
      access_token: 'bob-access',
      username: 'bob',
      role: 'admin',
    });
    releaseLock.resolve();
    await holder;

    await expect(refreshing).rejects.toBeInstanceOf(authSession.AuthSessionSupersededError);
    expect(refreshCalls).toBe(0);
    expect(authSession.getAuthSession()?.username).toBe('bob');
  });

  it('rejects a refresh response for a different established username', async () => {
    authSession.installAuthSession({
      access_token: 'alice-access',
      username: 'alice',
      role: 'user',
    });
    handleRequest = async (config) =>
      response(config, {
        access_token: 'bob-access',
        username: 'bob',
        role: 'admin',
      });

    await expect(authSession.refreshAuthSession()).rejects.toMatchObject({
      name: 'AuthSessionSubjectMismatchError',
    });
    expect(authSession.getAuthSession()).toEqual({
      access_token: 'alice-access',
      username: 'alice',
      role: 'user',
    });
  });

  it('rejects malformed authentication responses without installing them', async () => {
    handleRequest = async (config) => response(config, { access_token: 'missing-user-and-role' });

    await expect(authSession.refreshAuthSession()).rejects.toBeInstanceOf(TypeError);
    expect(authSession.getAuthSession()).toBeNull();
  });

  it('does not let an older in-flight refresh overwrite a newer login', async () => {
    authSession.installAuthSession({
      access_token: 'old-access',
      username: 'old-user',
      role: 'user',
    });
    const release = deferred<void>();
    let refreshCalls = 0;
    handleRequest = async (config) => {
      refreshCalls += 1;
      await release.promise;
      return response(config, {
        access_token: 'stale-refreshed-access',
        username: 'old-user',
        role: 'user',
      });
    };

    const refreshing = authSession.refreshAuthSession();
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    authSession.installAuthSession({
      access_token: 'new-login-access',
      username: 'new-user',
      role: 'admin',
    });
    release.resolve();

    await expect(refreshing).rejects.toBeInstanceOf(authSession.AuthSessionSupersededError);
    expect(authSession.getAuthSession()).toEqual({
      access_token: 'new-login-access',
      username: 'new-user',
      role: 'admin',
    });
  });

  it('retries a failed logout revocation instead of restoring the stale cookie session', async () => {
    authSession.installAuthSession({
      access_token: 'active-access',
      username: 'alice',
      role: 'user',
    });
    let logoutCalls = 0;
    let refreshCalls = 0;
    handleRequest = async (config) => {
      if (config.url === '/auth/logout') {
        logoutCalls += 1;
        return rejectResponse(config, 503, { detail: 'logout unavailable' });
      }
      refreshCalls += 1;
      return response(config, {
        access_token: 'must-not-be-restored',
        username: 'alice',
        role: 'user',
      });
    };

    await expect(authSession.revokeRefreshSession()).rejects.toBeInstanceOf(AxiosError);
    await expect(authSession.restoreAuthSession()).resolves.toBeNull();

    expect(logoutCalls).toBe(2);
    expect(refreshCalls).toBe(0);
    expect(authSession.getAuthSession()).toBeNull();
  });

  it('waits for an in-flight refresh response before revoking the rotated cookie', async () => {
    authSession.installAuthSession({
      access_token: 'active-access',
      username: 'alice',
      role: 'user',
    });
    const releaseRefresh = deferred<void>();
    let refreshCalls = 0;
    let logoutCalls = 0;
    handleRequest = async (config) => {
      if (config.url === '/auth/refresh') {
        refreshCalls += 1;
        await releaseRefresh.promise;
        return response(config, {
          access_token: 'rotated-but-revoked-access',
          username: 'alice',
          role: 'user',
        });
      }
      expect(config.url).toBe('/auth/logout');
      logoutCalls += 1;
      return response(config, undefined, 204);
    };

    const refreshing = authSession.refreshAuthSession();
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    const revoking = authSession.revokeRefreshSession();
    await Promise.resolve();
    expect(logoutCalls).toBe(0);

    releaseRefresh.resolve();

    await expect(refreshing).rejects.toBeInstanceOf(authSession.AuthSessionSupersededError);
    await expect(revoking).resolves.toBeUndefined();
    expect(logoutCalls).toBe(1);
    expect(authSession.getAuthSession()).toBeNull();
  });

  it('revokes the refresh session with credentialed logout', async () => {
    handleRequest = async (config) => {
      expect(config.url).toBe('/auth/logout');
      expect(config.method).toBe('post');
      expect(config.withCredentials).toBe(true);
      return response(config, undefined, 204);
    };

    await expect(authSession.revokeRefreshSession()).resolves.toBeUndefined();
  });
});
