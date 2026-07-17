<template>
  <Teleport to="body">
    <div
      v-if="store.centerOpen"
      class="capability-backdrop"
      role="presentation"
      @click.self="closeCenter"
    >
      <section
        ref="dialogRef"
        class="capability-center"
        role="dialog"
        aria-modal="true"
        aria-labelledby="capability-center-title"
        tabindex="-1"
        @keydown="handleDialogKeydown"
      >
        <header class="capability-center-header">
          <div>
            <span class="capability-eyebrow">Capability control plane</span>
            <h2 id="capability-center-title">能力中心</h2>
            <p>选择当前 Thread 的 Skill，查看 Tool 权限、网络策略与审批要求。</p>
          </div>
          <button type="button" aria-label="关闭能力中心" @click="closeCenter">
            <i class="fa-solid fa-xmark"></i>
          </button>
        </header>

        <div class="capability-toolbar">
          <label class="capability-search">
            <i class="fa-solid fa-magnifying-glass" aria-hidden="true"></i>
            <input
              v-model="store.searchQuery"
              type="search"
              placeholder="搜索 Skill、Tool 或能力说明"
              aria-label="搜索能力"
            />
            <kbd>⌘ K</kbd>
          </label>
          <div class="capability-tabs" role="tablist" aria-label="能力类型">
            <button
              id="capability-skills-tab"
              type="button"
              role="tab"
              :aria-selected="activeTab === 'skills'"
              aria-controls="capability-skills-panel"
              :class="{ active: activeTab === 'skills' }"
              @click="activeTab = 'skills'"
            >
              Skills
            </button>
            <button
              id="capability-tools-tab"
              type="button"
              role="tab"
              :aria-selected="activeTab === 'tools'"
              aria-controls="capability-tools-panel"
              :class="{ active: activeTab === 'tools' }"
              @click="activeTab = 'tools'"
            >
              Tools
            </button>
          </div>
        </div>

        <div class="capability-filter-row">
          <button
            v-for="filter in filters"
            :key="filter.value"
            type="button"
            :class="{ active: store.availabilityFilter === filter.value }"
            @click="store.setAvailabilityFilter(filter.value)"
          >
            {{ filter.label }}
          </button>
          <span v-if="store.catalog">目录 {{ store.catalog.catalog_hash.slice(0, 8) }}</span>
        </div>

        <div
          :id="activeTab === 'skills' ? 'capability-skills-panel' : 'capability-tools-panel'"
          class="capability-content"
          role="tabpanel"
          :aria-labelledby="
            activeTab === 'skills' ? 'capability-skills-tab' : 'capability-tools-tab'
          "
        >
          <div v-if="store.loading" class="capability-state" role="status">
            <i class="fa-solid fa-spinner fa-spin"></i>
            <strong>正在同步能力目录</strong>
            <p>读取当前账号可用的 Skill、Tool 与安全策略…</p>
          </div>

          <div v-else-if="store.error" class="capability-state is-error" role="status">
            <i class="fa-solid fa-triangle-exclamation"></i>
            <strong>能力目录加载失败</strong>
            <p>{{ store.error }}</p>
            <button type="button" @click="retry">重新加载</button>
          </div>

          <div v-else-if="store.isEmpty" class="capability-state">
            <i class="fa-regular fa-compass"></i>
            <strong>暂无已发布能力</strong>
            <p>Registry 中还没有可展示的 Skill 或 Tool。</p>
          </div>

          <template v-else-if="activeTab === 'skills'">
            <article
              v-if="matchesGeneralMode"
              :class="['skill-card', 'is-general', { selected: !store.selectedSkillName }]"
            >
              <span class="skill-icon"><i class="fa-regular fa-message"></i></span>
              <div class="skill-main">
                <div class="skill-title-row">
                  <div>
                    <span>默认模式</span>
                    <h3>智能对话</h3>
                  </div>
                  <span class="availability-badge is-available">可用</span>
                </div>
                <p>由 Agent 根据问题选择常驻 Tool，适合普通问答与知识库检索。</p>
                <div class="skill-tags">
                  <span>自动路由</span>
                  <span>无需审批</span>
                </div>
              </div>
              <button type="button" @click="selectSkill(null)">
                {{ !store.selectedSkillName ? '当前模式' : '使用' }}
              </button>
            </article>

            <div v-if="store.filteredSkills.length" class="skill-grid">
              <article
                v-for="skill in store.filteredSkills"
                :key="skill.name"
                :class="[
                  'skill-card',
                  {
                    selected: store.selectedSkillName === skill.name,
                    unavailable: !skill.available,
                  },
                ]"
              >
                <span class="skill-icon"><i :class="skillIcon(skill.name)"></i></span>
                <div class="skill-main">
                  <div class="skill-title-row">
                    <div>
                      <span>{{ skill.activation }} · v{{ skill.version }}</span>
                      <h3>{{ skillLabel(skill.name) }}</h3>
                    </div>
                    <span
                      :class="[
                        'availability-badge',
                        skill.available ? 'is-available' : 'is-unavailable',
                      ]"
                    >
                      {{ availabilityLabel(skill.available, skill.availability_reason) }}
                    </span>
                  </div>
                  <p>{{ skill.description }}</p>
                  <div class="skill-tags">
                    <span v-for="toolName in skill.tool_names" :key="toolName">
                      {{ toolName }}
                    </span>
                    <span v-if="skill.approval_tools.length" class="is-warning">
                      <i class="fa-solid fa-shield-halved"></i> 创建 Run 前审批
                    </span>
                  </div>
                  <div class="skill-policy">
                    <span>
                      <i class="fa-solid fa-user-shield"></i>
                      {{ rolesLabel(skill.required_roles) }}
                    </span>
                    <span>
                      <i class="fa-solid fa-network-wired"></i>
                      {{ skill.network_policies.map(networkLabel).join(' · ') || '无网络' }}
                    </span>
                    <span>
                      <i class="fa-solid fa-lock"></i>
                      {{ skill.resource_scopes.map(scopeLabel).join(' · ') || '基础范围' }}
                    </span>
                  </div>
                </div>
                <button
                  type="button"
                  :disabled="!skill.available"
                  :title="unavailableTitle(skill.available, skill.availability_reason)"
                  @click="selectSkill(skill.name)"
                >
                  {{ store.selectedSkillName === skill.name ? '当前模式' : '使用' }}
                </button>
              </article>
            </div>

            <div v-else-if="!matchesGeneralMode" class="capability-state is-compact">
              <i class="fa-solid fa-magnifying-glass"></i>
              <strong>没有匹配的 Skill</strong>
              <p>试试能力名称、Tool 名称或调整可用性筛选。</p>
            </div>
          </template>

          <div v-else class="tool-grid">
            <article
              v-for="tool in filteredTools"
              :key="tool.name"
              :class="['tool-card', { unavailable: !tool.available }]"
            >
              <div class="tool-card-heading">
                <span class="tool-icon"><i :class="toolIcon(tool.group)"></i></span>
                <div>
                  <code>{{ tool.name }}</code>
                  <small>{{ tool.group }} · v{{ tool.version }}</small>
                </div>
                <span
                  :class="['availability-dot', tool.available ? 'is-available' : 'is-unavailable']"
                  :title="availabilityLabel(tool.available, tool.availability_reason)"
                ></span>
              </div>
              <p>{{ tool.description }}</p>
              <div class="tool-policy-grid">
                <span><strong>暴露方式</strong>{{ exposureLabel(tool.exposure) }}</span>
                <span><strong>所需角色</strong>{{ rolesLabel(tool.required_roles) }}</span>
                <span><strong>网络</strong>{{ networkLabel(tool.network_policy) }}</span>
                <span><strong>资源</strong>{{ scopeLabel(tool.resource_scope) }}</span>
                <span><strong>执行</strong>{{ tool.idempotent ? '幂等' : '非幂等' }}</span>
              </div>
              <p v-if="tool.requires_approval" class="tool-approval-note">
                <i class="fa-solid fa-shield-halved"></i>
                需要绑定到当前 Run 的预审批
              </p>
            </article>

            <div v-if="!filteredTools.length" class="capability-state is-compact">
              <i class="fa-solid fa-magnifying-glass"></i>
              <strong>没有匹配的 Tool</strong>
              <p>调整搜索关键词或可用性筛选。</p>
            </div>
          </div>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { useCapabilityStore } from '@/stores/capabilities';
import type {
  CapabilityAvailabilityFilter,
  CapabilityAvailabilityReason,
} from '@/types/capabilities';
import { getPublicError } from '@/utils/api';

const store = useCapabilityStore();
const dialogRef = ref<HTMLElement | null>(null);
const activeTab = ref<'skills' | 'tools'>('skills');
let returnFocus: HTMLElement | null = null;
let restoreFocusOnClose = true;

const filters: Array<{ value: CapabilityAvailabilityFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'available', label: '可用' },
  { value: 'unavailable', label: '不可用' },
];

const matchesGeneralMode = computed(() => {
  if (store.availabilityFilter === 'unavailable') return false;
  const query = store.searchQuery.trim().toLocaleLowerCase();
  return (
    !query ||
    ['智能对话', '通用', 'general', 'chat', '自动路由'].some((text) => text.includes(query))
  );
});

const filteredTools = computed(() => {
  const query = store.searchQuery.trim().toLocaleLowerCase();
  return store.tools.filter((tool) => {
    if (store.availabilityFilter === 'available' && !tool.available) return false;
    if (store.availabilityFilter === 'unavailable' && tool.available) return false;
    if (!query) return true;
    return [tool.name, tool.description, tool.group, tool.network_policy, tool.resource_scope]
      .join(' ')
      .toLocaleLowerCase()
      .includes(query);
  });
});

watch(
  () => store.centerOpen,
  async (open) => {
    if (open) {
      restoreFocusOnClose = true;
      returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      if (!store.catalog && !store.loading) void store.fetchCatalog().catch(() => undefined);
      await nextTick();
      dialogRef.value?.focus();
      return;
    }
    const target = returnFocus;
    returnFocus = null;
    const shouldRestore = restoreFocusOnClose;
    restoreFocusOnClose = true;
    await nextTick();
    if (shouldRestore) target?.focus();
  }
);

const retry = () => void store.retryCatalog().catch(() => undefined);

const closeCenter = () => store.closeCenter();

const trapFocus = (event: KeyboardEvent) => {
  const dialog = dialogRef.value;
  if (!dialog) return;
  const focusable = Array.from(
    dialog.querySelectorAll<HTMLElement>(
      'input, button:not(:disabled), [tabindex]:not([tabindex="-1"])'
    )
  );
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
};

const handleDialogKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape') {
    event.preventDefault();
    closeCenter();
  } else if (event.key === 'Tab') {
    trapFocus(event);
  }
};

const selectSkill = (skillName: string | null) => {
  try {
    store.selectSkill(skillName);
    restoreFocusOnClose = false;
    store.closeCenter();
    window.dispatchEvent(new CustomEvent('capability-selected'));
  } catch (error) {
    store.error = getPublicError(error).message;
  }
};

const skillLabel = (name: string) => {
  const labels: Record<string, string> = {
    'knowledge-base': '知识库问答',
    'web-research': 'Web Research',
    'sql-assistant': 'SQL Assistant',
    sandbox: 'Sandbox',
  };
  return labels[name] || name;
};

const skillIcon = (name: string) => {
  if (name === 'knowledge-base') return 'fa-regular fa-bookmark';
  if (name === 'web-research') return 'fa-solid fa-globe';
  if (name === 'sql-assistant') return 'fa-solid fa-database';
  if (name === 'sandbox') return 'fa-solid fa-terminal';
  return 'fa-solid fa-wand-magic-sparkles';
};

const toolIcon = (group: string) => {
  if (group.includes('web')) return 'fa-solid fa-globe';
  if (group === 'sql') return 'fa-solid fa-database';
  if (group.includes('sandbox')) return 'fa-solid fa-terminal';
  if (group === 'knowledge') return 'fa-regular fa-bookmark';
  return 'fa-solid fa-screwdriver-wrench';
};

const availabilityLabel = (available: boolean, reason: CapabilityAvailabilityReason) => {
  if (available) return '可用';
  return reason === 'permission_required' ? '权限不足' : '尚未配置';
};

const unavailableTitle = (available: boolean, reason: CapabilityAvailabilityReason) => {
  if (available) return '用于当前对话';
  return reason === 'permission_required'
    ? '当前账号没有使用该能力所需的角色'
    : '该能力所需的运行配置尚未就绪';
};

const networkLabel = (policy: string) => {
  const labels: Record<string, string> = {
    none: '无网络',
    restricted: '受限公网',
    'private-data': '私有数据网络',
  };
  return labels[policy] || policy;
};

const scopeLabel = (scope: string) => {
  const labels: Record<string, string> = {
    none: '无额外资源',
    'public-web': '公开网页',
    'private-data-read': '私有数据只读',
    'code-execution': '隔离代码执行',
    'knowledge-read': '知识库只读',
  };
  return labels[scope] || scope;
};

const exposureLabel = (exposure: string) => {
  if (exposure === 'resident') return '常驻';
  if (exposure === 'control') return '控制面';
  return '按需发现';
};

const rolesLabel = (roles: string[]) =>
  roles.length ? `角色：${roles.join(' · ')}` : '全部已登录用户';
</script>

<style scoped>
.capability-backdrop {
  position: fixed;
  z-index: 1000;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(5, 7, 14, 0.72);
  backdrop-filter: blur(16px);
}

.capability-center {
  display: grid;
  grid-template-rows: auto auto auto minmax(0, 1fr);
  width: min(1080px, 100%);
  height: min(780px, calc(100vh - 48px));
  overflow: hidden;
  border: 1px solid var(--line-strong);
  border-radius: 24px;
  color: var(--text);
  background:
    radial-gradient(circle at 92% 3%, rgba(103, 217, 173, 0.12), transparent 30%),
    radial-gradient(circle at 5% 2%, rgba(142, 121, 255, 0.13), transparent 30%),
    var(--surface-strong);
  box-shadow: var(--shadow);
}

.capability-center-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 22px 24px 18px;
  border-bottom: 1px solid var(--line);
}

.capability-eyebrow {
  color: var(--mint);
  font-size: 8px;
  font-weight: 780;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.capability-center-header h2 {
  margin-top: 6px;
  font-size: 24px;
  letter-spacing: -0.04em;
}

.capability-center-header p {
  margin-top: 6px;
  color: var(--muted);
  font-size: 9px;
  line-height: 1.6;
}

.capability-center-header > button {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: 11px;
  color: var(--muted);
  background: var(--surface);
  cursor: pointer;
}

.capability-toolbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 14px;
  padding: 16px 24px 10px;
}

.capability-search {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  min-height: 42px;
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  color: var(--muted);
  background: var(--surface);
}

.capability-search input {
  min-width: 0;
  border: 0;
  outline: 0;
  color: var(--text);
  background: transparent;
  font-size: 10px;
}

.capability-search kbd {
  padding: 3px 6px;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--muted);
  background: var(--surface-soft);
  font-size: 7px;
}

.capability-tabs {
  display: flex;
  padding: 3px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
}

.capability-tabs button,
.capability-filter-row button {
  border-radius: 9px;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
  font-size: 8px;
}

.capability-tabs button {
  min-width: 76px;
  padding: 8px 11px;
}

.capability-tabs button.active,
.capability-filter-row button.active {
  color: var(--text);
  background: var(--surface-hover);
}

.capability-filter-row {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 0 24px 12px;
  border-bottom: 1px solid var(--line);
}

.capability-filter-row button {
  padding: 5px 9px;
}

.capability-filter-row > span {
  margin-left: auto;
  color: var(--muted-strong);
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 7px;
}

.capability-content {
  min-height: 0;
  overflow: auto;
  padding: 18px 24px 24px;
}

.skill-grid,
.tool-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.skill-card {
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr) auto;
  align-items: start;
  gap: 12px;
  padding: 15px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--surface);
  transition:
    border-color 180ms ease,
    background 180ms ease,
    transform 180ms ease;
}

.skill-grid .skill-card {
  grid-template-columns: 42px minmax(0, 1fr);
}

.skill-grid .skill-card > button {
  grid-column: 2;
  justify-self: start;
}

.skill-card.is-general {
  margin-bottom: 12px;
  background: linear-gradient(135deg, rgba(168, 246, 209, 0.06), var(--surface));
}

.skill-card.selected {
  border-color: rgba(168, 246, 209, 0.36);
  background: rgba(168, 246, 209, 0.055);
}

.skill-card.unavailable,
.tool-card.unavailable {
  opacity: 0.68;
}

.skill-card:hover:not(.unavailable) {
  border-color: var(--line-strong);
  transform: translateY(-1px);
}

.skill-icon,
.tool-icon {
  display: grid;
  place-items: center;
  border: 1px solid var(--line);
  color: var(--mint);
  background: var(--surface-soft);
}

.skill-icon {
  width: 42px;
  height: 42px;
  border-radius: 13px;
  font-size: 15px;
}

.skill-main {
  min-width: 0;
}

.skill-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 9px;
}

.skill-title-row > div > span {
  color: var(--muted);
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 7px;
}

.skill-title-row h3 {
  margin-top: 3px;
  font-size: 13px;
}

.skill-main > p,
.tool-card > p {
  margin-top: 8px;
  color: var(--muted);
  font-size: 8px;
  line-height: 1.65;
}

.availability-badge {
  flex: none;
  padding: 3px 6px;
  border-radius: 999px;
  font-size: 7px;
}

.availability-badge.is-available {
  color: var(--success);
  background: rgba(112, 228, 183, 0.08);
}

.availability-badge.is-unavailable {
  color: var(--warning);
  background: var(--warning-soft);
}

.skill-tags,
.skill-policy {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 9px;
}

.skill-tags span {
  padding: 3px 6px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--text-soft);
  background: var(--surface-soft);
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 7px;
}

.skill-tags .is-warning {
  color: var(--warning);
  font-family: inherit;
}

.skill-policy {
  padding-top: 8px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 7px;
}

.skill-card > button,
.capability-state > button {
  padding: 7px 10px;
  border: 1px solid var(--line-strong);
  border-radius: 9px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.06);
  cursor: pointer;
  font-size: 8px;
  font-weight: 680;
}

.tool-card {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 15px;
  background: var(--surface);
}

.tool-card-heading {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
}

.tool-icon {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  font-size: 11px;
}

.tool-card-heading code,
.tool-card-heading small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-card-heading code {
  color: var(--lilac);
  font-size: 9px;
}

.tool-card-heading small {
  margin-top: 3px;
  color: var(--muted);
  font-size: 7px;
}

.availability-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.availability-dot.is-available {
  background: var(--success);
  box-shadow: 0 0 0 4px rgba(112, 228, 183, 0.08);
}

.availability-dot.is-unavailable {
  background: var(--warning);
  box-shadow: 0 0 0 4px var(--warning-soft);
}

.tool-policy-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  margin-top: 11px;
}

.tool-policy-grid span {
  display: flex;
  justify-content: space-between;
  gap: 7px;
  padding: 6px 7px;
  border-radius: 8px;
  color: var(--text-soft);
  background: var(--surface-soft);
  font-size: 7px;
}

.tool-policy-grid strong {
  color: var(--muted);
  font-weight: 580;
}

.tool-approval-note {
  color: var(--warning) !important;
}

.capability-state {
  display: grid;
  min-height: 300px;
  place-items: center;
  align-content: center;
  gap: 8px;
  color: var(--muted);
  text-align: center;
}

.capability-state > i {
  color: var(--mint);
  font-size: 20px;
}

.capability-state strong {
  color: var(--text-soft);
  font-size: 12px;
}

.capability-state p {
  max-width: 380px;
  font-size: 8px;
  line-height: 1.6;
}

.capability-state.is-error > i {
  color: var(--danger);
}

.capability-state.is-compact {
  min-height: 220px;
}

@media (max-width: 760px) {
  .capability-backdrop {
    padding: 8px;
  }

  .capability-center {
    height: calc(100vh - 16px);
    border-radius: 18px;
  }

  .capability-center-header,
  .capability-content {
    padding-inline: 16px;
  }

  .capability-toolbar {
    grid-template-columns: 1fr;
    padding-inline: 16px;
  }

  .capability-filter-row {
    padding-inline: 16px;
  }

  .skill-grid,
  .tool-grid {
    grid-template-columns: 1fr;
  }

  .skill-card.is-general {
    grid-template-columns: 38px minmax(0, 1fr);
  }

  .skill-card.is-general > button {
    grid-column: 2;
    justify-self: start;
  }
}
</style>
