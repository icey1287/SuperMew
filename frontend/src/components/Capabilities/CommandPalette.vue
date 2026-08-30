<template>
  <Teleport to="body">
    <div
      v-if="store.paletteOpen"
      class="command-palette-backdrop"
      role="presentation"
      @click.self="closePalette"
    >
      <section
        ref="dialogRef"
        class="command-palette"
        role="dialog"
        aria-modal="true"
        aria-labelledby="command-palette-title"
        @keydown="handleDialogKeydown"
      >
        <header class="command-palette-header">
          <span class="command-palette-icon" aria-hidden="true">
            <i class="fa-solid fa-wand-magic-sparkles"></i>
          </span>
          <label class="command-palette-search">
            <span id="command-palette-title" class="sr-only">能力命令面板</span>
            <i class="fa-solid fa-magnifying-glass" aria-hidden="true"></i>
            <input
              ref="inputRef"
              v-model="query"
              type="search"
              role="combobox"
              autocomplete="off"
              aria-autocomplete="list"
              aria-expanded="true"
              aria-controls="capability-command-results"
              :aria-activedescendant="activeOptionId"
              placeholder="搜索模式或 Skill…"
              @input="resetActiveIndex"
            />
          </label>
          <kbd>ESC</kbd>
        </header>

        <p class="command-palette-status" role="status" aria-live="polite">
          {{ resultStatus }}
        </p>

        <div
          id="capability-command-results"
          ref="resultsRef"
          class="command-palette-results"
          role="listbox"
          aria-label="能力命令"
        >
          <button
            v-for="(item, index) in resultItems"
            :id="optionId(item.key)"
            :key="item.key"
            type="button"
            role="option"
            :aria-selected="index === activeIndex"
            :aria-disabled="item.disabled"
            :disabled="item.disabled"
            :class="[
              'command-palette-option',
              `is-${item.kind}`,
              { active: index === activeIndex, selected: item.selected },
            ]"
            @mousemove="activateIndex(index)"
            @click="selectItem(item)"
          >
            <span class="command-option-icon" aria-hidden="true">
              <i :class="item.icon"></i>
            </span>
            <span class="command-option-copy">
              <strong>{{ item.label }}</strong>
              <small>{{ item.description }}</small>
            </span>
            <span v-if="item.disabled" class="command-option-state">
              {{ item.unavailableLabel }}
            </span>
            <span v-else-if="item.selected" class="command-option-state is-selected">
              当前模式
            </span>
            <span v-else class="command-option-shortcut" aria-hidden="true">↵</span>
          </button>

          <div v-if="!resultItems.length" class="command-palette-empty" role="status">
            <i class="fa-solid fa-magnifying-glass" aria-hidden="true"></i>
            <strong>没有匹配的能力</strong>
            <span>换一个 Skill 名称或 Tool 关键词试试。</span>
          </div>
        </div>

        <footer class="command-palette-footer">
          <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
          <span><kbd>Enter</kbd> 确认</span>
          <span><kbd>Esc</kbd> 关闭</span>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { useCapabilityStore } from '@/stores/capabilities';
import type { CapabilityAvailabilityReason } from '@/types/capabilities';
import { getPublicError } from '@/utils/api';

interface CommandItem {
  key: string;
  kind: 'mode' | 'skill' | 'command';
  label: string;
  description: string;
  icon: string;
  skillName: string | null;
  disabled: boolean;
  selected: boolean;
  unavailableLabel: string;
}

const emit = defineEmits<{
  (event: 'capability-selected'): void;
}>();

const store = useCapabilityStore();
const dialogRef = ref<HTMLElement | null>(null);
const inputRef = ref<HTMLInputElement | null>(null);
const resultsRef = ref<HTMLElement | null>(null);
const query = ref('');
const activeIndex = ref(-1);
let returnFocus: HTMLElement | null = null;
let restoreFocusOnClose = true;

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

const unavailableLabel = (reason: CapabilityAvailabilityReason) =>
  reason === 'permission_required' ? '权限不足' : '尚未配置';

const searchableText = (item: CommandItem) =>
  `${item.label} ${item.description} ${item.skillName || ''}`.toLocaleLowerCase();

const resultItems = computed<CommandItem[]>(() => {
  const normalizedQuery = query.value.trim().toLocaleLowerCase();
  const general: CommandItem = {
    key: 'mode:general',
    kind: 'mode',
    label: '智能对话',
    description: '通用模式 · Agent 自动选择常驻 Tool',
    icon: 'fa-regular fa-message',
    skillName: null,
    disabled: false,
    selected: store.selectedSkillName === null,
    unavailableLabel: '',
  };
  const skills = store.skills.map<CommandItem>((skill) => ({
    key: `skill:${skill.name}`,
    kind: 'skill',
    label: skillLabel(skill.name),
    description: `${skill.activation} · ${skill.description}`,
    icon: skillIcon(skill.name),
    skillName: skill.name,
    disabled: !skill.available,
    selected: store.selectedSkillName === skill.name,
    unavailableLabel: unavailableLabel(skill.availability_reason),
  }));
  const center: CommandItem = {
    key: 'command:center',
    kind: 'command',
    label: '打开能力中心',
    description: '查看完整 Skill、Tool、网络策略与资源范围',
    icon: 'fa-solid fa-table-cells-large',
    skillName: null,
    disabled: false,
    selected: false,
    unavailableLabel: '',
  };
  const capabilityItems = [general, ...skills].filter(
    (item) => !normalizedQuery || searchableText(item).includes(normalizedQuery)
  );
  return [...capabilityItems, center];
});

const selectableIndexes = computed(() =>
  resultItems.value.map((item, index) => (item.disabled ? -1 : index)).filter((index) => index >= 0)
);

const activeOptionId = computed(() => {
  const item = resultItems.value[activeIndex.value];
  return item ? optionId(item.key) : undefined;
});

const resultStatus = computed(() => {
  const capabilityCount = resultItems.value.filter((item) => item.kind !== 'command').length;
  if (store.loading) return '正在同步能力目录';
  if (store.error) return `能力目录加载失败：${store.error}`;
  return `找到 ${capabilityCount} 个模式；不可用项不会被执行`;
});

const optionId = (key: string) => `capability-command-${key.replace(/[^a-zA-Z0-9_-]/g, '-')}`;

const firstSelectableIndex = () => selectableIndexes.value[0] ?? -1;

const resetActiveIndex = () => {
  activeIndex.value = firstSelectableIndex();
};

const activateIndex = (index: number) => {
  if (!resultItems.value[index]?.disabled) activeIndex.value = index;
};

const moveActive = (direction: 1 | -1) => {
  const indexes = selectableIndexes.value;
  if (!indexes.length) {
    activeIndex.value = -1;
    return;
  }
  const position = indexes.indexOf(activeIndex.value);
  const nextPosition =
    position < 0
      ? direction > 0
        ? 0
        : indexes.length - 1
      : (position + direction + indexes.length) % indexes.length;
  activeIndex.value = indexes[nextPosition];
  void nextTick(() => {
    document.getElementById(activeOptionId.value || '')?.scrollIntoView({ block: 'nearest' });
  });
};

const announceSelection = () => {
  emit('capability-selected');
  window.dispatchEvent(new CustomEvent('capability-selected'));
};

const selectItem = (item: CommandItem) => {
  if (item.disabled) return;
  if (item.kind === 'command') {
    restoreFocusOnClose = false;
    store.openCenter();
    return;
  }
  try {
    store.selectSkill(item.skillName);
    restoreFocusOnClose = false;
    store.closePalette();
    announceSelection();
  } catch (error) {
    store.error = getPublicError(error).message;
  }
};

const closePalette = () => store.closePalette();

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
    closePalette();
    return;
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    moveActive(event.key === 'ArrowDown' ? 1 : -1);
    return;
  }
  if (event.key === 'Enter') {
    const item = resultItems.value[activeIndex.value];
    if (item && !item.disabled) {
      event.preventDefault();
      selectItem(item);
    }
    return;
  }
  if (event.key === 'Tab') trapFocus(event);
};

watch(
  () => store.paletteOpen,
  async (open) => {
    if (open) {
      restoreFocusOnClose = true;
      returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      query.value = '';
      if (!store.catalog && !store.loading) void store.fetchCatalog().catch(() => undefined);
      await nextTick();
      resetActiveIndex();
      inputRef.value?.focus();
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

watch(resultItems, resetActiveIndex);
</script>

<style scoped>
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  clip-path: inset(50%);
}

.command-palette-backdrop {
  position: fixed;
  z-index: 1100;
  inset: 0;
  display: grid;
  place-items: start center;
  padding: min(14vh, 120px) 20px 20px;
  background: rgba(5, 7, 14, 0.68);
  backdrop-filter: blur(14px);
}

.command-palette {
  width: min(680px, 100%);
  overflow: hidden;
  border: 1px solid var(--line-strong);
  border-radius: 20px;
  color: var(--text);
  background: var(--surface-strong);
  box-shadow: var(--shadow);
}

.command-palette-header {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 13px 14px;
  border-bottom: 1px solid var(--line);
}

.command-palette-icon {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border-radius: 10px;
  color: var(--lilac);
  background: rgba(200, 185, 255, 0.09);
}

.command-palette-search {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
  color: var(--muted);
}

.command-palette-search input {
  width: 100%;
  border: 0;
  outline: 0;
  color: var(--text);
  background: transparent;
  font: inherit;
  font-size: 16px;
}

.command-palette kbd,
.command-palette-footer kbd {
  display: inline-grid;
  min-width: 22px;
  height: 22px;
  place-items: center;
  padding: 0 5px;
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  color: var(--muted);
  background: var(--surface-soft);
  font-size: 11px;
  font-family: inherit;
}

.command-palette-status {
  padding: 8px 15px;
  border-bottom: 1px solid var(--line);
  color: var(--muted);
  font-size: 13px;
}

.command-palette-results {
  max-height: min(480px, 58vh);
  overflow-y: auto;
  padding: 7px;
}

.command-palette-option {
  display: grid;
  grid-template-columns: 38px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px;
  border: 1px solid transparent;
  border-radius: 11px;
  color: var(--text-soft);
  background: transparent;
  text-align: left;
  cursor: pointer;
}

.command-palette-option.active,
.command-palette-option:hover:not(:disabled) {
  border-color: var(--line-strong);
  background: var(--surface-soft);
}

.command-palette-option.selected {
  border-color: rgba(200, 185, 255, 0.24);
}

.command-palette-option:disabled {
  cursor: not-allowed;
  opacity: 0.48;
}

.command-palette-option.is-command {
  margin-top: 6px;
  border-top-color: var(--line);
  border-radius: 0 0 11px 11px;
}

.command-option-icon {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border-radius: 11px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.07);
}

.command-option-copy {
  min-width: 0;
}

.command-option-copy strong,
.command-option-copy small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.command-option-copy strong {
  font-size: 15px;
  line-height: 1.35;
}

.command-option-copy small {
  margin-top: 3px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.4;
}

.command-option-state,
.command-option-shortcut {
  color: var(--muted);
  font-size: 12px;
}

.command-option-state.is-selected {
  color: var(--lilac);
}

.command-palette-empty {
  display: grid;
  justify-items: center;
  gap: 6px;
  padding: 36px 16px;
  color: var(--muted);
  text-align: center;
}

.command-palette-empty strong {
  color: var(--text-soft);
  font-size: 15px;
}

.command-palette-empty span {
  font-size: 13px;
}

.command-palette-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 9px 14px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 12px;
}

.command-palette-footer span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

@media (max-width: 640px) {
  .command-palette-backdrop {
    align-items: end;
    padding: 12px;
  }

  .command-palette {
    border-radius: 18px;
  }

  .command-palette-results {
    max-height: 58vh;
  }
}
</style>
