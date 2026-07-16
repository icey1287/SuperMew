import { defineStore } from 'pinia';
import api from '@/utils/api';
import type { CurrentUser } from '@/types/user';

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('accessToken') || '',
    currentUser: null as CurrentUser | null,
    authMode: 'login' as 'login' | 'register',
    authForm: {
      username: '',
      password: '',
      role: 'user' as 'user' | 'admin',
      admin_code: '',
    },
    authLoading: false,
  }),

  getters: {
    isAuthenticated(): boolean {
      return !!this.token && !!this.currentUser;
    },
    isAdmin(): boolean {
      return this.currentUser?.role === 'admin';
    },
  },

  actions: {
    async fetchMe() {
      if (!this.token) return;
      try {
        const response = await api.get('/auth/me');
        this.currentUser = response.data;
      } catch (error) {
        this.handleLogout();
        throw error;
      }
    },

    async handleAuthSubmit() {
      if (this.authLoading) return;
      const username = this.authForm.username.trim();
      const password = this.authForm.password.trim();
      if (!username || !password) {
        throw new Error('用户名和密码不能为空');
      }

      this.authLoading = true;
      try {
        const endpoint = this.authMode === 'login' ? '/auth/login' : '/auth/register';
        const payload: any = { username, password };
        if (this.authMode === 'register') {
          payload.role = this.authForm.role;
          payload.admin_code = this.authForm.admin_code || null;
        }

        const response = await api.post(endpoint, payload);
        const data = response.data;

        this.token = data.access_token;
        this.currentUser = { username: data.username, role: data.role };
        localStorage.setItem('accessToken', this.token);

        // Reset password fields
        this.authForm.password = '';
        this.authForm.admin_code = '';
      } catch (error: any) {
        const errMsg = error.response?.data?.detail || error.message || '认证失败';
        throw new Error(errMsg);
      } finally {
        this.authLoading = false;
      }
    },

    handleLogout() {
      this.token = '';
      this.currentUser = null;
      localStorage.removeItem('accessToken');
    },
  },
});
