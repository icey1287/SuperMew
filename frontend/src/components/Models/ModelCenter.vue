<template>
  <div class="model-center-page">
    <header class="workspace-header">
      <div>
        <span class="panel-eyebrow">Model control plane</span>
        <h1>模型中心</h1>
        <p>
          集中创建 OpenAI-compatible Model Profile，并为新 Run 分配 Answer、Fast、Grader 与
          Evaluator。
        </p>
      </div>
      <div class="workspace-actions">
        <button type="button" class="secondary-button" :disabled="store.loading" @click="refresh">
          <i class="fa-solid fa-rotate" :class="{ 'fa-spin': store.loading }"></i>
          刷新目录
        </button>
        <button type="button" class="primary-button" @click="openCreateProfile">
          <i class="fa-solid fa-plus"></i>
          新建 Model Profile
        </button>
      </div>
    </header>

    <div v-if="store.error" class="workspace-alert is-error" role="alert">
      <i class="fa-solid fa-triangle-exclamation"></i>
      <span>{{ store.error }}</span>
      <button type="button" @click="refresh">重试</button>
    </div>
    <div v-else-if="store.notice" class="workspace-alert is-success" role="status">
      <i class="fa-solid fa-circle-check"></i>
      <span>{{ store.notice }}</span>
      <button type="button" aria-label="关闭提示" @click="store.clearNotice">
        <i class="fa-solid fa-xmark"></i>
      </button>
    </div>

    <section class="control-summary" aria-label="模型控制面状态">
      <article :class="{ 'is-warning': !store.apiKeyConfigured }">
        <span class="summary-icon"><i class="fa-solid fa-key"></i></span>
        <div>
          <small>API Key</small>
          <strong>{{ store.apiKeyConfigured ? '服务端已配置' : '服务端未配置' }}</strong>
          <p>仅从 ARK_API_KEY 读取，前端不可录入或查看。</p>
        </div>
      </article>
      <article>
        <span class="summary-icon"><i class="fa-solid fa-layer-group"></i></span>
        <div>
          <small>Model Profiles</small>
          <strong>{{ store.profiles.length }} 个</strong>
          <p>{{ enabledProfiles.length }} 个已启用，可参与角色分配。</p>
        </div>
      </article>
      <article :class="{ 'is-warning': assignedRoleCount < roleDefinitions.length }">
        <span class="summary-icon"><i class="fa-solid fa-diagram-project"></i></span>
        <div>
          <small>Assignments</small>
          <strong>{{ assignedRoleCount }} / {{ roleDefinitions.length }}</strong>
          <p>每个 Run 创建时冻结完整模型快照。</p>
        </div>
      </article>
      <article>
        <span class="summary-icon"><i class="fa-solid fa-fingerprint"></i></span>
        <div>
          <small>Catalog Hash</small>
          <strong class="hash-value">{{ shortHash }}</strong>
          <p>目录发生变化后仅影响后续新 Run。</p>
        </div>
      </article>
    </section>

    <section class="assignment-section">
      <div class="section-heading">
        <div>
          <span class="section-kicker">Runtime assignments</span>
          <h2>模型角色</h2>
          <p>选择会立即更新控制面；正在运行或等待 HITL 的 Run 继续使用其已冻结快照。</p>
        </div>
        <span class="snapshot-note">
          <i class="fa-solid fa-camera"></i>
          新 Run 生效
        </span>
      </div>

      <div class="assignment-grid">
        <article v-for="role in roleDefinitions" :key="role.key" class="assignment-card">
          <div class="assignment-card-head">
            <span :class="['role-icon', `is-${role.key}`]"><i :class="role.icon"></i></span>
            <div>
              <small>{{ role.eyebrow }}</small>
              <h3>{{ role.label }}</h3>
            </div>
            <span
              :class="['assignment-state', assignmentFor(role.key) ? 'is-ready' : 'is-missing']"
            >
              {{ assignmentFor(role.key) ? '已分配' : '待配置' }}
            </span>
          </div>

          <p>{{ role.description }}</p>

          <label class="assignment-select">
            <span>当前 Model Profile</span>
            <select
              :value="assignmentFor(role.key)?.id || ''"
              :disabled="store.saving || !compatibleProfiles(role.key).length"
              :aria-label="`${role.label} 模型`"
              @change="handleAssignmentChange(role.key, $event)"
            >
              <option value="" disabled>选择兼容的 Model Profile</option>
              <option
                v-for="profile in compatibleProfiles(role.key)"
                :key="profile.id"
                :value="profile.id"
              >
                {{ profile.display_name }} · {{ profile.model_name }}
              </option>
            </select>
            <i class="fa-solid fa-chevron-down"></i>
          </label>

          <div class="requirement-list">
            <span v-if="roleRequirement(role.key)?.supports_stream">
              <i class="fa-solid fa-wave-square"></i> 必须支持 Stream
            </span>
            <span v-if="roleRequirement(role.key)?.supports_structured_output">
              <i class="fa-solid fa-brackets-curly"></i> 必须支持 Structured Output
            </span>
            <span>
              <i class="fa-solid fa-temperature-half"></i>
              Temperature {{ roleRequirement(role.key)?.temperature ?? 0 }}
            </span>
          </div>
        </article>
      </div>
    </section>

    <section class="profiles-section">
      <div class="section-heading profiles-heading">
        <div>
          <span class="section-kicker">Reusable profiles</span>
          <h2>Model Profile 目录</h2>
          <p>Provider 地址与能力声明持久化保存；Secret 始终留在服务端环境。</p>
        </div>
        <label class="profile-search">
          <i class="fa-solid fa-magnifying-glass"></i>
          <input v-model="searchQuery" type="search" placeholder="搜索名称、模型或 Base URL" />
        </label>
      </div>

      <div v-if="store.loading && !store.controlPlane" class="empty-state" role="status">
        <i class="fa-solid fa-spinner fa-spin"></i>
        <strong>正在同步模型目录</strong>
        <p>读取 Model Profile、Assignment 与服务端凭据状态…</p>
      </div>

      <div v-else-if="!filteredProfiles.length" class="empty-state">
        <i class="fa-solid fa-cubes-stacked"></i>
        <strong>{{ searchQuery ? '没有匹配的 Model Profile' : '还没有 Model Profile' }}</strong>
        <p>{{ searchQuery ? '尝试其他关键词。' : '创建一个 Profile 后即可分配给运行角色。' }}</p>
        <button v-if="!searchQuery" type="button" @click="openCreateProfile">
          创建第一个 Profile
        </button>
      </div>

      <div v-else class="profile-table-wrap">
        <table class="profile-table">
          <thead>
            <tr>
              <th>Profile</th>
              <th>Provider / Model</th>
              <th>能力</th>
              <th>当前角色</th>
              <th>状态</th>
              <th><span class="sr-only">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="profile in filteredProfiles" :key="profile.id">
              <td>
                <div class="profile-identity">
                  <span>{{ profile.display_name.slice(0, 1).toUpperCase() }}</span>
                  <div>
                    <strong>{{ profile.display_name }}</strong>
                    <small>
                      {{ profile.source === 'environment' ? '环境初始化' : '前端创建' }} · v{{
                        profile.version
                      }}
                    </small>
                  </div>
                </div>
              </td>
              <td>
                <strong class="model-name">{{ profile.model_name }}</strong>
                <small class="endpoint-label">{{
                  profile.base_url || '默认 Provider Endpoint'
                }}</small>
              </td>
              <td>
                <div class="capability-tags">
                  <span :class="{ disabled: !profile.supports_stream }">Stream</span>
                  <span :class="{ disabled: !profile.supports_structured_output }">Structured</span>
                  <span>{{ profile.timeout_seconds }}s</span>
                </div>
              </td>
              <td>
                <div v-if="rolesForProfile(profile.id).length" class="role-tags">
                  <span v-for="role in rolesForProfile(profile.id)" :key="role">
                    {{ roleLabel(role) }}
                  </span>
                </div>
                <small v-else class="muted-cell">未分配</small>
              </td>
              <td>
                <span :class="['profile-status', profile.enabled ? 'is-enabled' : 'is-disabled']">
                  <i class="fa-solid fa-circle"></i>
                  {{ profile.enabled ? '已启用' : '已停用' }}
                </span>
              </td>
              <td>
                <div class="row-actions">
                  <button
                    type="button"
                    :aria-label="`编辑 ${profile.display_name}`"
                    title="编辑 Model Profile"
                    @click="openEditProfile(profile)"
                  >
                    <i class="fa-regular fa-pen-to-square"></i>
                  </button>
                  <button
                    type="button"
                    class="danger-action"
                    :disabled="isAssigned(profile.id)"
                    :aria-label="`删除 ${profile.display_name}`"
                    :title="
                      isAssigned(profile.id) ? '请先将角色分配到其他 Profile' : '删除 Model Profile'
                    "
                    @click="requestDelete(profile)"
                  >
                    <i class="fa-regular fa-trash-can"></i>
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <Teleport to="body">
      <div v-if="formOpen" class="modal-backdrop" @click.self="closeProfileForm">
        <section
          class="model-modal"
          role="dialog"
          aria-modal="true"
          :aria-labelledby="formTitleId"
          @keydown.esc="closeProfileForm"
        >
          <header>
            <div>
              <span class="section-kicker">{{
                editingProfile ? 'Edit profile' : 'Create profile'
              }}</span>
              <h2 :id="formTitleId">
                {{ editingProfile ? '编辑 Model Profile' : '新建 Model Profile' }}
              </h2>
              <p>只保存无 Secret 的连接配置；API Key 不会通过此表单传输。</p>
            </div>
            <button type="button" aria-label="关闭 Model Profile 表单" @click="closeProfileForm">
              <i class="fa-solid fa-xmark"></i>
            </button>
          </header>

          <form @submit.prevent="submitProfile">
            <div class="form-grid">
              <label class="form-field wide-field">
                <span>显示名称</span>
                <input
                  ref="firstInput"
                  v-model.trim="profileForm.display_name"
                  type="text"
                  maxlength="120"
                  required
                  placeholder="例如：生产回答模型"
                />
              </label>
              <label class="form-field">
                <span>Provider</span>
                <select v-model="profileForm.provider" disabled>
                  <option value="openai">OpenAI-compatible</option>
                </select>
              </label>
              <label class="form-field">
                <span>模型标识</span>
                <input
                  v-model.trim="profileForm.model_name"
                  type="text"
                  maxlength="160"
                  required
                  placeholder="例如：doubao-seed-1-6"
                />
              </label>
              <label class="form-field wide-field">
                <span>Base URL</span>
                <input
                  v-model.trim="profileForm.base_url"
                  type="url"
                  maxlength="512"
                  placeholder="留空使用服务端默认 Endpoint"
                />
                <small>只接受无用户名、密码、query 与 fragment 的 HTTP/HTTPS 地址。</small>
              </label>
              <label class="form-field">
                <span>超时（秒）</span>
                <input
                  v-model.number="profileForm.timeout_seconds"
                  type="number"
                  min="1"
                  max="600"
                  step="1"
                  required
                />
              </label>
              <div class="secret-boundary">
                <i class="fa-solid fa-shield-halved"></i>
                <div>
                  <strong>Secret Seam</strong>
                  <span>API Key 只能由部署环境提供；数据库、响应与浏览器均不保存。</span>
                </div>
              </div>
            </div>

            <fieldset class="capability-fieldset">
              <legend>能力声明</legend>
              <label>
                <input v-model="profileForm.supports_stream" type="checkbox" />
                <span><strong>Stream</strong><small>可用于 Answer 流式输出</small></span>
              </label>
              <label>
                <input v-model="profileForm.supports_structured_output" type="checkbox" />
                <span
                  ><strong>Structured Output</strong
                  ><small>可用于路由、评分与 Evaluator</small></span
                >
              </label>
              <label>
                <input v-model="profileForm.enabled" type="checkbox" />
                <span
                  ><strong>启用 Profile</strong><small>停用后不可创建新的 Assignment</small></span
                >
              </label>
            </fieldset>

            <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>

            <footer>
              <button type="button" class="secondary-button" @click="closeProfileForm">取消</button>
              <button type="submit" class="primary-button" :disabled="store.saving || !formValid">
                <i v-if="store.saving" class="fa-solid fa-spinner fa-spin"></i>
                {{ editingProfile ? '保存修改' : '创建 Profile' }}
              </button>
            </footer>
          </form>
        </section>
      </div>

      <div v-if="deleteCandidate" class="modal-backdrop" @click.self="deleteCandidate = null">
        <section
          class="confirm-modal"
          role="alertdialog"
          aria-modal="true"
          aria-labelledby="delete-model-title"
          @keydown.esc="deleteCandidate = null"
        >
          <span class="confirm-icon"><i class="fa-regular fa-trash-can"></i></span>
          <h2 id="delete-model-title">删除 Model Profile？</h2>
          <p>
            将永久删除「{{ deleteCandidate.display_name }}」。历史 Run 与 Evaluation Job
            的模型快照不受影响。
          </p>
          <div>
            <button type="button" class="secondary-button" @click="deleteCandidate = null">
              取消
            </button>
            <button
              type="button"
              class="danger-button"
              :disabled="store.saving"
              @click="confirmDelete"
            >
              <i v-if="store.saving" class="fa-solid fa-spinner fa-spin"></i>
              确认删除
            </button>
          </div>
        </section>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue';
import { useModelStore } from '@/stores/models';
import type {
  ModelProfile,
  ModelProfilePayload,
  ModelRole,
  ModelRoleRequirement,
} from '@/types/models';
import { getPublicError } from '@/utils/api';

interface RoleDefinition {
  key: ModelRole;
  label: string;
  eyebrow: string;
  description: string;
  icon: string;
}

const roleDefinitions: RoleDefinition[] = [
  {
    key: 'answer',
    label: 'Answer',
    eyebrow: 'Primary response',
    description: '生成面向用户的最终回答，并支持持续流式输出。',
    icon: 'fa-regular fa-message',
  },
  {
    key: 'fast',
    label: 'Fast',
    eyebrow: 'Routing & extraction',
    description: '承担意图路由、问题分类与轻量结构化提取。',
    icon: 'fa-solid fa-bolt',
  },
  {
    key: 'grader',
    label: 'Grader',
    eyebrow: 'Runtime grading',
    description: '对检索结果与运行中间状态执行快速结构化判断。',
    icon: 'fa-solid fa-scale-balanced',
  },
  {
    key: 'evaluator',
    label: 'Evaluator',
    eyebrow: 'Offline evaluation',
    description: '自动评估 RAG 答案正确性、忠实度、相关性与完整性。',
    icon: 'fa-solid fa-flask-vial',
  },
];

const emptyProfileForm = (): ModelProfilePayload => ({
  display_name: '',
  provider: 'openai',
  model_name: '',
  base_url: '',
  timeout_seconds: 30,
  supports_stream: true,
  supports_structured_output: true,
  enabled: true,
});

const store = useModelStore();
const searchQuery = ref('');
const formOpen = ref(false);
const editingProfile = ref<ModelProfile | null>(null);
const deleteCandidate = ref<ModelProfile | null>(null);
const firstInput = ref<HTMLInputElement | null>(null);
const formError = ref('');
const profileForm = reactive<ModelProfilePayload>(emptyProfileForm());

const enabledProfiles = computed(() => store.profiles.filter((profile) => profile.enabled));
const assignedRoleCount = computed(
  () => roleDefinitions.filter((role) => Boolean(assignmentFor(role.key))).length
);
const shortHash = computed(() => store.controlPlane?.catalog_hash.slice(0, 12) || '尚未同步');
const formTitleId = computed(() =>
  editingProfile.value ? 'edit-model-profile-title' : 'create-model-profile-title'
);
const formValid = computed(
  () =>
    Boolean(profileForm.display_name.trim() && profileForm.model_name.trim()) &&
    Number(profileForm.timeout_seconds) > 0 &&
    Number(profileForm.timeout_seconds) <= 600
);
const filteredProfiles = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase();
  if (!query) return store.profiles;
  return store.profiles.filter((profile) =>
    [profile.display_name, profile.model_name, profile.base_url, profile.provider].some((value) =>
      value.toLocaleLowerCase().includes(query)
    )
  );
});

const assignmentFor = (role: ModelRole): ModelProfile | null =>
  store.controlPlane?.assignments[role] || null;

const roleRequirement = (role: ModelRole): ModelRoleRequirement | null =>
  store.controlPlane?.requirements[role] || null;

const compatibleProfiles = (role: ModelRole): ModelProfile[] => {
  const requirement = roleRequirement(role);
  return store.profiles.filter(
    (profile) =>
      profile.enabled &&
      (!requirement?.supports_stream || profile.supports_stream) &&
      (!requirement?.supports_structured_output || profile.supports_structured_output)
  );
};

const rolesForProfile = (profileId: string): ModelRole[] =>
  roleDefinitions
    .filter((role) => assignmentFor(role.key)?.id === profileId)
    .map((role) => role.key);

const isAssigned = (profileId: string) => rolesForProfile(profileId).length > 0;

const roleLabel = (role: ModelRole) =>
  roleDefinitions.find((definition) => definition.key === role)?.label || role;

const refresh = async () => {
  try {
    await store.fetchControlPlane();
  } catch {
    // Store exposes a durable user-facing error state.
  }
};

const handleAssignmentChange = async (role: ModelRole, event: Event) => {
  const profileId = (event.target as HTMLSelectElement).value;
  if (!profileId || profileId === assignmentFor(role)?.id) return;
  try {
    await store.assignRole(role, profileId);
  } catch {
    // The previous Assignment remains selected from the authoritative projection.
  }
};

const assignProfileForm = (payload: ModelProfilePayload) => {
  Object.assign(profileForm, payload);
};

const openCreateProfile = async () => {
  editingProfile.value = null;
  formError.value = '';
  assignProfileForm(emptyProfileForm());
  formOpen.value = true;
  await nextTick();
  firstInput.value?.focus();
};

const openEditProfile = async (profile: ModelProfile) => {
  editingProfile.value = profile;
  formError.value = '';
  assignProfileForm({
    display_name: profile.display_name,
    provider: profile.provider,
    model_name: profile.model_name,
    base_url: profile.base_url,
    timeout_seconds: profile.timeout_seconds,
    supports_stream: profile.supports_stream,
    supports_structured_output: profile.supports_structured_output,
    enabled: profile.enabled,
  });
  formOpen.value = true;
  await nextTick();
  firstInput.value?.focus();
};

const closeProfileForm = () => {
  if (store.saving) return;
  formOpen.value = false;
  editingProfile.value = null;
  formError.value = '';
};

const submitProfile = async () => {
  if (!formValid.value || store.saving) return;
  formError.value = '';
  const payload: ModelProfilePayload = {
    ...profileForm,
    display_name: profileForm.display_name.trim(),
    model_name: profileForm.model_name.trim(),
    base_url: profileForm.base_url.trim(),
    timeout_seconds: Number(profileForm.timeout_seconds),
  };
  try {
    if (editingProfile.value) await store.updateProfile(editingProfile.value.id, payload);
    else await store.createProfile(payload);
    closeProfileForm();
  } catch (error) {
    formError.value = getPublicError(error).message;
  }
};

const requestDelete = (profile: ModelProfile) => {
  if (isAssigned(profile.id)) return;
  deleteCandidate.value = profile;
};

const confirmDelete = async () => {
  const profile = deleteCandidate.value;
  if (!profile || store.saving) return;
  try {
    await store.deleteProfile(profile.id);
    deleteCandidate.value = null;
  } catch {
    deleteCandidate.value = null;
  }
};

onMounted(refresh);
</script>

<style scoped>
.model-center-page {
  width: 100%;
  height: 100%;
  padding: 25px;
  overflow-y: auto;
}

.workspace-header,
.section-heading,
.workspace-actions,
.assignment-card-head,
.profile-identity,
.row-actions,
.model-modal header,
.model-modal footer,
.confirm-modal > div,
.workspace-alert {
  display: flex;
  align-items: center;
}

.workspace-header {
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding-bottom: 21px;
  border-bottom: 1px solid var(--line);
}

.workspace-header h1 {
  margin-top: 5px;
  font-size: 26px;
  letter-spacing: -0.045em;
}

.workspace-header p,
.section-heading p {
  margin-top: 7px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.55;
}

.workspace-actions {
  gap: 8px;
}

.primary-button,
.secondary-button,
.danger-button {
  display: inline-flex;
  min-height: 39px;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 0 14px;
  border-radius: 11px;
  cursor: pointer;
  font-size: 11px;
  font-weight: 720;
}

.primary-button {
  color: #111820;
  background: linear-gradient(135deg, var(--mint), var(--lilac));
  box-shadow: 0 10px 24px rgba(116, 225, 183, 0.12);
}

html[data-theme='light'] .primary-button {
  color: white;
  background: linear-gradient(135deg, var(--mint-strong), var(--lilac-strong));
}

.secondary-button {
  border: 1px solid var(--line);
  color: var(--text-soft);
  background: var(--surface);
}

.danger-button {
  color: white;
  background: #d95064;
}

.workspace-alert {
  gap: 9px;
  margin-top: 14px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 11px;
  font-size: 11px;
}

.workspace-alert span {
  flex: 1;
}

.workspace-alert button {
  padding: 4px 7px;
  border-radius: 7px;
  color: inherit;
  background: transparent;
  cursor: pointer;
}

.workspace-alert.is-error {
  border-color: rgba(255, 105, 122, 0.34);
  color: var(--danger);
  background: var(--danger-soft);
}

.workspace-alert.is-success {
  border-color: rgba(112, 228, 183, 0.28);
  color: var(--success);
  background: rgba(112, 228, 183, 0.07);
}

.control-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin: 18px 0;
}

.control-summary article {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 11px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 15px;
  background: var(--surface);
}

.control-summary article.is-warning {
  border-color: rgba(244, 199, 109, 0.28);
  background: var(--warning-soft);
}

.summary-icon {
  display: grid;
  width: 32px;
  height: 32px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 10px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.08);
}

.control-summary article > div {
  min-width: 0;
}

.control-summary small,
.control-summary strong,
.control-summary p {
  display: block;
}

.control-summary small {
  color: var(--muted);
  font-size: 9px;
}

.control-summary strong {
  margin-top: 5px;
  overflow: hidden;
  font-size: 14px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.control-summary p {
  margin-top: 4px;
  color: var(--muted-strong);
  font-size: 8px;
  line-height: 1.45;
}

.hash-value {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.assignment-section,
.profiles-section {
  padding: 17px;
  border: 1px solid var(--line);
  border-radius: 18px;
  background: var(--surface);
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.025);
}

.profiles-section {
  margin-top: 12px;
}

.section-heading {
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

.section-kicker {
  color: var(--mint);
  font-size: 8px;
  font-weight: 780;
  letter-spacing: 0.13em;
  text-transform: uppercase;
}

.section-heading h2 {
  margin-top: 4px;
  font-size: 15px;
}

.snapshot-note {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 7px 9px;
  border: 1px solid rgba(200, 185, 255, 0.2);
  border-radius: 999px;
  color: var(--lilac);
  background: rgba(200, 185, 255, 0.07);
  font-size: 9px;
  font-weight: 680;
}

.assignment-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-top: 15px;
}

.assignment-card {
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 15px;
  background: var(--surface-soft);
}

.assignment-card-head {
  gap: 9px;
}

.role-icon {
  display: grid;
  width: 31px;
  height: 31px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 10px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.08);
}

.role-icon.is-fast {
  color: var(--warning);
  background: var(--warning-soft);
}

.role-icon.is-grader {
  color: var(--lilac);
  background: rgba(200, 185, 255, 0.08);
}

.role-icon.is-evaluator {
  color: #8ab8ff;
  background: rgba(89, 155, 255, 0.09);
}

.assignment-card-head > div {
  min-width: 0;
  flex: 1;
}

.assignment-card-head small {
  color: var(--muted-strong);
  font-size: 7px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.assignment-card-head h3 {
  margin-top: 2px;
  font-size: 12px;
}

.assignment-state {
  padding: 4px 6px;
  border-radius: 999px;
  font-size: 7px;
  font-weight: 720;
}

.assignment-state.is-ready {
  color: var(--success);
  background: rgba(112, 228, 183, 0.08);
}

.assignment-state.is-missing {
  color: var(--warning);
  background: var(--warning-soft);
}

.assignment-card > p {
  min-height: 38px;
  margin-top: 11px;
  color: var(--muted);
  font-size: 9px;
  line-height: 1.55;
}

.assignment-select {
  position: relative;
  display: grid;
  gap: 6px;
  margin-top: 12px;
}

.assignment-select > span {
  color: var(--text-soft);
  font-size: 8px;
  font-weight: 650;
}

.assignment-select select {
  width: 100%;
  min-height: 38px;
  padding: 0 29px 0 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  outline: 0;
  color: var(--text);
  background: var(--surface-strong);
  appearance: none;
  font-size: 9px;
}

.assignment-select > i {
  position: absolute;
  right: 11px;
  bottom: 14px;
  color: var(--muted);
  font-size: 8px;
  pointer-events: none;
}

.requirement-list {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 10px;
}

.requirement-list span,
.capability-tags span,
.role-tags span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  border: 1px solid var(--line);
  border-radius: 7px;
  color: var(--muted);
  background: var(--surface);
  font-size: 7px;
}

.profiles-heading {
  align-items: center;
}

.profile-search {
  display: flex;
  width: min(300px, 42vw);
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  color: var(--muted);
  background: var(--surface-soft);
}

.profile-search input {
  min-width: 0;
  flex: 1;
  border: 0;
  outline: 0;
  color: var(--text);
  background: transparent;
  font-size: 9px;
}

.profile-table-wrap {
  margin-top: 13px;
  overflow-x: auto;
}

.profile-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
}

.profile-table th {
  padding: 9px 10px;
  border-bottom: 1px solid var(--line);
  color: var(--muted-strong);
  font-size: 7px;
  font-weight: 760;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.profile-table td {
  padding: 11px 10px;
  border-bottom: 1px solid var(--line);
  vertical-align: middle;
  font-size: 9px;
}

.profile-table tbody tr:last-child td {
  border-bottom: 0;
}

.profile-identity {
  min-width: 180px;
  gap: 9px;
}

.profile-identity > span {
  display: grid;
  width: 30px;
  height: 30px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 9px;
  color: #171622;
  background: linear-gradient(135deg, var(--mint), var(--lilac));
  font-size: 10px;
  font-weight: 820;
}

.profile-identity strong,
.profile-identity small,
.model-name,
.endpoint-label {
  display: block;
}

.profile-identity strong,
.model-name {
  font-size: 9px;
}

.profile-identity small,
.endpoint-label,
.muted-cell {
  margin-top: 3px;
  color: var(--muted-strong);
  font-size: 7px;
}

.endpoint-label {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.capability-tags,
.role-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.capability-tags span.disabled {
  opacity: 0.38;
  text-decoration: line-through;
}

.role-tags span {
  border-color: rgba(200, 185, 255, 0.17);
  color: var(--lilac);
  background: rgba(200, 185, 255, 0.06);
}

.profile-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 8px;
}

.profile-status i {
  font-size: 5px;
}

.profile-status.is-enabled {
  color: var(--success);
}

.profile-status.is-disabled {
  color: var(--muted);
}

.row-actions {
  justify-content: flex-end;
  gap: 4px;
}

.row-actions button,
.model-modal header > button {
  display: grid;
  width: 31px;
  height: 31px;
  place-items: center;
  border: 1px solid transparent;
  border-radius: 9px;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
}

.row-actions button:hover:not(:disabled),
.model-modal header > button:hover {
  border-color: var(--line);
  color: var(--text);
  background: var(--surface);
}

.row-actions .danger-action:hover:not(:disabled) {
  color: var(--danger);
  background: var(--danger-soft);
}

.empty-state {
  display: grid;
  min-height: 190px;
  place-items: center;
  align-content: center;
  gap: 8px;
  color: var(--muted);
  text-align: center;
}

.empty-state > i {
  color: var(--mint);
  font-size: 20px;
}

.empty-state strong {
  color: var(--text);
  font-size: 12px;
}

.empty-state p {
  font-size: 9px;
}

.empty-state button {
  margin-top: 5px;
  padding: 8px 10px;
  border-radius: 9px;
  color: var(--mint-ink);
  background: var(--mint);
  cursor: pointer;
  font-size: 9px;
  font-weight: 720;
}

.modal-backdrop {
  position: fixed;
  z-index: 1200;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(3, 5, 12, 0.7);
  backdrop-filter: blur(12px);
}

.model-modal,
.confirm-modal {
  width: min(680px, 100%);
  max-height: min(820px, calc(100vh - 40px));
  overflow-y: auto;
  border: 1px solid var(--line-strong);
  border-radius: 22px;
  background: var(--surface-strong);
  box-shadow: 0 28px 90px rgba(0, 0, 0, 0.42);
}

.model-modal header {
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding: 21px 22px 17px;
  border-bottom: 1px solid var(--line);
}

.model-modal h2,
.confirm-modal h2 {
  margin-top: 4px;
  font-size: 18px;
}

.model-modal header p {
  margin-top: 6px;
  color: var(--muted);
  font-size: 9px;
}

.model-modal form {
  padding: 19px 22px 22px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 13px;
}

.form-field {
  display: grid;
  gap: 7px;
}

.form-field.wide-field {
  grid-column: 1 / -1;
}

.form-field > span,
.capability-fieldset legend {
  color: var(--text-soft);
  font-size: 9px;
  font-weight: 680;
}

.form-field input,
.form-field select {
  width: 100%;
  min-height: 41px;
  padding: 0 11px;
  border: 1px solid var(--line);
  border-radius: 10px;
  outline: 0;
  color: var(--text);
  background: var(--surface);
  font-size: 10px;
}

.form-field input:focus,
.form-field select:focus {
  border-color: rgba(168, 246, 209, 0.35);
}

.form-field small {
  color: var(--muted-strong);
  font-size: 7px;
  line-height: 1.45;
}

.secret-boundary {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 10px;
  border: 1px solid rgba(112, 228, 183, 0.17);
  border-radius: 10px;
  color: var(--success);
  background: rgba(112, 228, 183, 0.05);
}

.secret-boundary div {
  min-width: 0;
}

.secret-boundary strong,
.secret-boundary span {
  display: block;
}

.secret-boundary strong {
  font-size: 8px;
}

.secret-boundary span {
  margin-top: 2px;
  color: var(--muted);
  font-size: 7px;
  line-height: 1.35;
}

.capability-fieldset {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: 17px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
}

.capability-fieldset legend {
  padding: 0 5px;
}

.capability-fieldset label {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface-soft);
  cursor: pointer;
}

.capability-fieldset input {
  margin-top: 2px;
  accent-color: var(--mint-strong);
}

.capability-fieldset strong,
.capability-fieldset small {
  display: block;
}

.capability-fieldset strong {
  font-size: 8px;
}

.capability-fieldset small {
  margin-top: 3px;
  color: var(--muted);
  font-size: 7px;
  line-height: 1.35;
}

.form-error {
  margin-top: 13px;
  padding: 9px 10px;
  border: 1px solid rgba(255, 105, 122, 0.3);
  border-radius: 9px;
  color: var(--danger);
  background: var(--danger-soft);
  font-size: 9px;
}

.model-modal footer {
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
}

.confirm-modal {
  width: min(420px, 100%);
  padding: 25px;
  text-align: center;
}

.confirm-icon {
  display: grid;
  width: 48px;
  height: 48px;
  margin: 0 auto;
  place-items: center;
  border-radius: 15px;
  color: var(--danger);
  background: var(--danger-soft);
  font-size: 18px;
}

.confirm-modal p {
  margin-top: 9px;
  color: var(--muted);
  font-size: 10px;
  line-height: 1.65;
}

.confirm-modal > div {
  justify-content: center;
  gap: 8px;
  margin-top: 19px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}

@media (max-width: 1240px) {
  .control-summary,
  .assignment-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .model-center-page {
    padding: 17px 13px;
  }

  .workspace-header,
  .profiles-heading {
    align-items: stretch;
    flex-direction: column;
  }

  .workspace-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }

  .profile-search {
    width: 100%;
  }

  .control-summary,
  .assignment-grid {
    grid-template-columns: 1fr;
  }

  .form-grid,
  .capability-fieldset {
    grid-template-columns: 1fr;
  }

  .secret-boundary {
    min-height: 41px;
  }
}
</style>
