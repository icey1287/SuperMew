<template>
  <section class="welcome-screen">
    <div class="welcome-avatar" aria-hidden="true">
      <i class="fa-solid fa-cat"></i>
    </div>
    <span class="welcome-eyebrow"><i class="fa-solid fa-sparkles"></i> Mew is ready</span>
    <h2>你好，我是喵喵。</h2>
    <p>我会在回答时检索你的知识库、展示处理过程，并把每个关键结论链接回原始证据。</p>

    <div class="welcome-mode-grid" aria-label="快速选择能力">
      <button
        type="button"
        :class="{ active: !capabilityStore.selectedSkillName }"
        @click="selectMode(null)"
      >
        <span><i class="fa-regular fa-message"></i></span>
        <strong>智能对话</strong>
        <small>自动路由常驻 Tool</small>
      </button>
      <button
        v-for="skill in featuredSkills"
        :key="skill.name"
        type="button"
        :class="{ active: capabilityStore.selectedSkillName === skill.name }"
        :disabled="!skill.available"
        :title="skill.available ? `使用 ${skillLabel(skill.name)}` : unavailableLabel(skill)"
        @click="selectMode(skill.name)"
      >
        <span><i :class="skillIcon(skill.name)"></i></span>
        <strong>{{ skillLabel(skill.name) }}</strong>
        <small>{{ skill.available ? skillHint(skill.name) : unavailableLabel(skill) }}</small>
      </button>
    </div>

    <button type="button" class="welcome-center-link" @click="capabilityStore.openCenter">
      <i class="fa-solid fa-wand-magic-sparkles"></i>
      浏览全部 Skill 与 Tool
    </button>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useCapabilityStore } from '@/stores/capabilities';
import type { CapabilitySkill } from '@/types/capabilities';

const capabilityStore = useCapabilityStore();
const featuredNames = ['knowledge-base', 'web-research', 'sql-assistant', 'sandbox'];
const featuredSkills = computed(() =>
  featuredNames
    .map((name) => capabilityStore.skills.find((skill) => skill.name === name))
    .filter((skill): skill is CapabilitySkill => Boolean(skill))
);

const selectMode = (name: string | null) => {
  capabilityStore.selectSkill(name);
  window.dispatchEvent(new CustomEvent('capability-selected'));
};

const skillLabel = (name: string) => {
  const labels: Record<string, string> = {
    'knowledge-base': '知识库',
    'web-research': 'Web Research',
    'sql-assistant': 'SQL Assistant',
    sandbox: 'Sandbox',
  };
  return labels[name] || name;
};

const skillHint = (name: string) => {
  if (name === 'knowledge-base') return '基于文档证据';
  if (name === 'web-research') return '公开网络调研';
  if (name === 'sql-assistant') return '有界只读分析';
  if (name === 'sandbox') return '隔离代码执行';
  return 'Registry Skill';
};

const skillIcon = (name: string) => {
  if (name === 'knowledge-base') return 'fa-regular fa-bookmark';
  if (name === 'web-research') return 'fa-solid fa-globe';
  if (name === 'sql-assistant') return 'fa-solid fa-database';
  if (name === 'sandbox') return 'fa-solid fa-terminal';
  return 'fa-solid fa-wand-magic-sparkles';
};

const unavailableLabel = (skill: CapabilitySkill) =>
  skill.availability_reason === 'permission_required' ? '当前账号权限不足' : '运行配置尚未就绪';
</script>

<style scoped>
.welcome-mode-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  margin-top: 20px;
}

.welcome-mode-grid button {
  display: grid;
  min-width: 0;
  justify-items: center;
  gap: 5px;
  padding: 12px 8px;
  border: 1px solid var(--line);
  border-radius: 13px;
  color: var(--muted);
  background: var(--surface-soft);
  cursor: pointer;
  transition:
    transform 180ms ease,
    border-color 180ms ease,
    background 180ms ease;
}

.welcome-mode-grid button:hover:not(:disabled),
.welcome-mode-grid button.active {
  border-color: rgba(168, 246, 209, 0.3);
  color: var(--text-soft);
  background: rgba(168, 246, 209, 0.055);
  transform: translateY(-2px);
}

.welcome-mode-grid button > span {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  border-radius: 9px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.07);
  font-size: var(--font-small);
}

.welcome-mode-grid strong,
.welcome-mode-grid small {
  overflow: hidden;
  max-width: 100%;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.welcome-mode-grid strong {
  color: inherit;
  font-size: var(--font-small);
}

.welcome-mode-grid small {
  font-size: var(--font-micro);
}

.welcome-center-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 12px;
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--lilac);
  background: transparent;
  cursor: pointer;
  font-size: var(--font-caption);
}

@media (max-width: 720px) {
  .welcome-mode-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
