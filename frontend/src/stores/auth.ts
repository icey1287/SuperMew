import { computed, onScopeDispose, reactive, ref } from 'vue';
import { defineStore } from 'pinia';
import {
  clearAuthSession,
  getAuthSession,
  installAuthSession,
  restoreAuthSession,
  revokeRefreshSession,
  subscribeAuthSession,
  type AuthSession,
} from '@/auth/session';
import type { CurrentUser } from '@/types/user';
import api, { getPublicError } from '@/utils/api';

type AuthMode = 'login' | 'register';

interface AuthRequestPayload {
  username: string;
  password: string;
  role?: CurrentUser['role'];
  admin_code?: string | null;
}

function authenticationMessage(error: unknown): string {
  if (error && typeof error === 'object') {
    const response = (error as { response?: unknown }).response;
    if (response && typeof response === 'object') {
      const data = (response as { data?: unknown }).data;
      if (data && typeof data === 'object') {
        const detail = (data as { detail?: unknown }).detail;
        if (typeof detail === 'string' && detail.trim()) return detail.trim().slice(0, 240);
      }
    }
  }
  return getPublicError(error).message;
}

export const useAuthStore = defineStore('auth', () => {
  const session = ref<AuthSession | null>(getAuthSession());
  const authResolved = ref(false);
  const authMode = ref<AuthMode>('login');
  const authForm = reactive({
    username: '',
    password: '',
    role: 'user' as CurrentUser['role'],
    admin_code: '',
  });
  const authLoading = ref(false);
  let restorePromise: Promise<void> | null = null;

  const stopSessionSubscription = subscribeAuthSession((nextSession) => {
    session.value = nextSession;
  });
  onScopeDispose(stopSessionSubscription);

  const token = computed(() => session.value?.access_token || '');
  const currentUser = computed<CurrentUser | null>(() => {
    if (!session.value) return null;
    return {
      username: session.value.username,
      role: session.value.role,
    };
  });
  const isAuthenticated = computed(() => Boolean(token.value && currentUser.value));
  const isAdmin = computed(() => currentUser.value?.role === 'admin');

  function restoreSession(): Promise<void> {
    if (authResolved.value) return Promise.resolve();
    if (restorePromise) return restorePromise;

    restorePromise = (async () => {
      try {
        await restoreAuthSession();
      } catch {
        clearAuthSession();
      } finally {
        authResolved.value = true;
        restorePromise = null;
      }
    })();
    return restorePromise;
  }

  async function handleAuthSubmit(): Promise<void> {
    if (authLoading.value) return;
    const username = authForm.username.trim();
    const password = authForm.password;
    if (!username || !password.trim()) {
      throw new Error('用户名和密码不能为空');
    }

    authLoading.value = true;
    try {
      const endpoint = authMode.value === 'login' ? '/auth/login' : '/auth/register';
      const payload: AuthRequestPayload = { username, password };
      if (authMode.value === 'register') {
        payload.role = authForm.role;
        payload.admin_code = authForm.admin_code || null;
      }

      const response = await api.post<AuthSession>(endpoint, payload);
      installAuthSession(response.data);
      authForm.password = '';
      authForm.admin_code = '';
    } catch (error) {
      throw new Error(authenticationMessage(error));
    } finally {
      authLoading.value = false;
    }
  }

  function clearSession(): void {
    clearAuthSession();
  }

  async function handleLogout(): Promise<void> {
    if (authLoading.value) return;
    authLoading.value = true;
    try {
      await revokeRefreshSession();
    } catch {
      // Local sign-out is authoritative even when the revocation request cannot reach the server.
    } finally {
      clearSession();
      authLoading.value = false;
    }
  }

  return {
    token,
    currentUser,
    authResolved,
    authMode,
    authForm,
    authLoading,
    isAuthenticated,
    isAdmin,
    restoreSession,
    handleAuthSubmit,
    clearSession,
    handleLogout,
  };
});
