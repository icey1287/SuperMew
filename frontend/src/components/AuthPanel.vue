<template>
  <section class="auth-page">
    <div class="auth-showcase">
      <div class="auth-showcase-badge">
        <i class="fa-solid fa-sparkles"></i>
        <span>SuperMew Knowledge Copilot</span>
      </div>
      <h2>让每一份知识<br />都有清晰的回声。</h2>
      <p>
        混合检索、并行 Agent、证据精排与可追溯引用，
        现在都汇聚在同一个工作台里。
      </p>
      <div class="auth-feature-list">
        <div>
          <i class="fa-solid fa-magnifying-glass-chart"></i>
          <span><strong>Hybrid RAG</strong><small>Dense + BM25 + Rerank</small></span>
        </div>
        <div>
          <i class="fa-solid fa-diagram-project"></i>
          <span><strong>并行 Agent</strong><small>复杂问题自动拆解与合成</small></span>
        </div>
        <div>
          <i class="fa-regular fa-file-lines"></i>
          <span><strong>可信引用</strong><small>答案与原始证据一一对齐</small></span>
        </div>
      </div>
    </div>

    <div class="auth-panel">
      <div class="auth-panel-heading">
        <span class="auth-mini-logo"><i class="fa-solid fa-cat"></i></span>
        <div>
          <span class="auth-eyebrow">{{ authStore.authMode === 'login' ? 'Welcome back' : 'Create account' }}</span>
          <h1>{{ authStore.authMode === 'login' ? '登录喵喵助手' : '注册喵喵助手' }}</h1>
        </div>
      </div>
      <p class="auth-description">
        {{ authStore.authMode === 'login'
          ? '进入你的私有知识空间，继续上一次对话。'
          : '创建账号后即可开始对话和保存历史记录。' }}
      </p>

      <form class="auth-form" @submit.prevent="onSubmit">
        <label class="form-field">
          <span>用户名</span>
          <span class="field-input">
            <i class="fa-regular fa-user"></i>
            <input v-model="authStore.authForm.username" type="text" autocomplete="username" placeholder="请输入用户名" />
          </span>
        </label>

        <label class="form-field">
          <span>密码</span>
          <span class="field-input">
            <i class="fa-solid fa-lock"></i>
            <input
              v-model="authStore.authForm.password"
              type="password"
              :autocomplete="authStore.authMode === 'login' ? 'current-password' : 'new-password'"
              placeholder="请输入密码"
            />
          </span>
        </label>

        <label v-if="authStore.authMode === 'register'" class="form-field">
          <span>账号角色</span>
          <span class="field-input">
            <i class="fa-regular fa-id-badge"></i>
            <select v-model="authStore.authForm.role">
              <option value="user">普通用户</option>
              <option value="admin">管理员</option>
            </select>
          </span>
        </label>

        <label
          v-if="authStore.authMode === 'register' && authStore.authForm.role === 'admin'"
          class="form-field"
        >
          <span>管理员邀请码</span>
          <span class="field-input">
            <i class="fa-solid fa-key"></i>
            <input
              v-model="authStore.authForm.admin_code"
              type="password"
              autocomplete="off"
              placeholder="请输入管理员邀请码"
            />
          </span>
        </label>

        <button class="auth-submit" type="submit" :disabled="authStore.authLoading">
          <span>{{ authStore.authLoading ? '正在连接...' : (authStore.authMode === 'login' ? '进入工作台' : '创建账号') }}</span>
          <i :class="authStore.authLoading ? 'fa-solid fa-spinner fa-spin' : 'fa-solid fa-arrow-right'"></i>
        </button>
      </form>

      <div class="auth-divider"><span>或者</span></div>
      <button class="auth-switch" type="button" @click="toggleAuthMode">
        {{ authStore.authMode === 'login' ? '还没有账号？创建一个' : '已有账号？返回登录' }}
      </button>
      <p class="auth-footnote">登录即表示你理解 AI 输出需要经过必要的人工复核。</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { useAuthStore } from '@/stores/auth';

const authStore = useAuthStore();

const toggleAuthMode = () => {
  authStore.authMode = authStore.authMode === 'login' ? 'register' : 'login';
};

const onSubmit = async () => {
  try {
    await authStore.handleAuthSubmit();
  } catch (error: any) {
    alert(error.message);
  }
};
</script>
