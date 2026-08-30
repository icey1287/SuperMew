<template>
  <div class="input-area-wrapper">
    <div v-if="chatStore.currentPendingHitl" class="hitl-panel">
      <div class="hitl-panel-header">
        <span class="hitl-icon"><i class="fa-solid fa-circle-question"></i></span>
        <span>
          <strong>需要你补充一下</strong>
          <small>喵喵会沿着你的选择继续原来的检索流程</small>
        </span>
      </div>
      <div class="hitl-panel-prompt">{{ chatStore.currentPendingHitl.prompt }}</div>
      <div
        v-if="chatStore.currentPendingHitl.options && chatStore.currentPendingHitl.options.length"
        class="hitl-options"
      >
        <button
          v-for="option in chatStore.currentPendingHitl.options"
          :key="option"
          type="button"
          class="hitl-option"
          @click="selectHitlOption(option)"
        >
          {{ option }}
        </button>
      </div>
    </div>

    <template v-if="!chatStore.currentPendingHitl">
      <div class="mode-toolbar">
        <button
          type="button"
          class="mode-trigger"
          :class="{ 'is-selected': capabilityStore.selectedSkill }"
          :disabled="chatStore.isInputLocked"
          aria-label="选择运行模式"
          @click="capabilityStore.openPalette"
        >
          <span class="mode-trigger-icon"><i :class="modeIcon"></i></span>
          <span class="mode-trigger-copy">
            <strong>{{ modeLabel }}</strong>
            <small>{{ modeSummary }}</small>
          </span>
          <span
            v-if="capabilityStore.selectedSkill?.approval_tools.length"
            class="mode-approval-dot"
          >
            <i class="fa-solid fa-shield-halved"></i>
            需审批
          </span>
          <kbd>⌘K</kbd>
          <i class="fa-solid fa-chevron-down" aria-hidden="true"></i>
        </button>

        <button
          type="button"
          class="mode-center-link"
          :disabled="chatStore.isInputLocked"
          @click="capabilityStore.openCenter"
        >
          查看能力
        </button>
      </div>

      <div
        v-if="capabilityStore.loading || capabilityStore.error || selectedModeUnavailable"
        :class="['mode-notice', { 'is-error': capabilityStore.error || selectedModeUnavailable }]"
        role="status"
      >
        <i
          :class="
            capabilityStore.loading
              ? 'fa-solid fa-spinner fa-spin'
              : 'fa-solid fa-triangle-exclamation'
          "
        ></i>
        <span v-if="capabilityStore.loading">正在同步能力目录…</span>
        <span v-else-if="selectedModeUnavailable">{{ unavailableModeMessage }}</span>
        <span v-else>{{ capabilityStore.error }}</span>
        <button v-if="capabilityStore.error" type="button" @click="retryCapabilities">重试</button>
        <button v-if="selectedModeUnavailable" type="button" @click="useGeneralMode">
          切回通用
        </button>
      </div>

      <section v-if="isSandbox || quickPrompts.length" class="mode-composer-panel">
        <div v-if="isSandbox" class="sandbox-language" aria-label="代码沙盒执行语言">
          <span>执行语言</span>
          <button
            v-for="language in sandboxLanguages"
            :key="language.value"
            type="button"
            :class="{ active: capabilityStore.sandboxLanguage === language.value }"
            :disabled="chatStore.isInputLocked"
            @click="capabilityStore.setSandboxLanguage(language.value)"
          >
            {{ language.label }}
          </button>
          <small><i class="fa-solid fa-wifi"></i> 无网络 · 无宿主挂载</small>
        </div>

        <div v-else-if="quickPrompts.length" class="mode-quick-prompts">
          <span>快速开始</span>
          <button
            v-for="prompt in quickPrompts"
            :key="prompt"
            type="button"
            :disabled="chatStore.isInputLocked"
            @click="applyPrompt(prompt)"
          >
            {{ prompt }}
          </button>
        </div>
      </section>
    </template>

    <div :class="['input-area', { 'hitl-active': chatStore.currentPendingHitl }]">
      <button
        class="attach-btn"
        type="button"
        title="当前版本暂不支持聊天附件"
        aria-label="聊天附件暂不可用"
        disabled
      >
        <i class="fa-solid fa-paperclip"></i>
      </button>

      <textarea
        ref="textareaRef"
        v-model="chatStore.userInput"
        class="chat-input-textarea"
        :class="{ 'is-code-input': isSandbox }"
        :placeholder="chatStore.inputPlaceholder"
        :disabled="chatStore.isInputLocked"
        :spellcheck="!isSandbox"
        rows="1"
        @keydown="handleKeyDown"
        @compositionstart="handleCompositionStart"
        @compositionend="handleCompositionEnd"
        @input="autoResize"
      ></textarea>

      <button
        v-if="chatStore.isViewingStreamingThread"
        type="button"
        class="send-btn stop-btn"
        title="终止回答"
        aria-label="终止回答"
        :disabled="chatStore.currentRunStatus === 'cancelling'"
        @click="chatStore.handleStop"
      >
        <i class="fa-solid fa-stop"></i>
      </button>

      <button
        v-else
        type="button"
        class="send-btn"
        :disabled="chatStore.isInputLocked"
        :title="chatStore.isInputLocked ? '当前对话正在处理操作' : '发送'"
        aria-label="发送消息"
        @click="onSend"
      >
        <i class="fa-regular fa-paper-plane"></i>
      </button>
    </div>

    <div class="input-footer">
      <span>{{ footerNotice }}</span>
      <span><kbd>Enter</kbd> 发送 · <kbd>Shift</kbd> + <kbd>Enter</kbd> 换行</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue';
import {
  skillDisplayIcon,
  skillDisplayName,
  skillDisplaySummary,
} from '@/capabilities/skillPresentation';
import { useChatStore } from '@/stores/chat';
import { useCapabilityStore } from '@/stores/capabilities';
import type { SandboxLanguage } from '@/types/capabilities';

const chatStore = useChatStore();
const capabilityStore = useCapabilityStore();
const textareaRef = ref<HTMLTextAreaElement | null>(null);
const isComposing = ref(false);

const sandboxLanguages: Array<{ value: SandboxLanguage; label: string }> = [
  { value: 'python', label: 'Python' },
  { value: 'sh', label: 'Shell' },
];

const isSandbox = computed(() => capabilityStore.selectedSkillName === 'sandbox');
const selectedModeUnavailable = computed(() => capabilityStore.selectedModeUnavailableReason);

const modeLabel = computed(() => {
  const name = capabilityStore.selectedSkillName;
  if (!name) return '智能对话';
  return skillDisplayName(name, capabilityStore.selectedSkill?.description);
});

const modeIcon = computed(() => {
  const name = capabilityStore.selectedSkillName;
  return name ? skillDisplayIcon(name) : 'fa-regular fa-message';
});

const modeSummary = computed(() => {
  if (capabilityStore.loading) return '能力目录同步中';
  if (selectedModeUnavailable.value === 'permission_required') return '当前账号权限不足';
  if (selectedModeUnavailable.value === 'not_configured') return '运行配置尚未就绪';
  if (!capabilityStore.selectedSkill) return '自动选择合适的工具回答';
  return skillDisplaySummary(capabilityStore.selectedSkill);
});

const quickPrompts = computed(() => {
  const name = capabilityStore.selectedSkillName;
  if (name === 'web-research') {
    return ['调研最近 7 天的行业变化', '比较三个公开来源的说法', '核验一条新闻是否准确'];
  }
  if (name === 'sql-assistant') {
    return ['分析最近 30 天核心指标趋势', '按维度比较业务表现', '检查异常波动并说明筛选条件'];
  }
  if (name === 'knowledge-base') {
    return ['总结已上传文档的关键结论', '对比两份文档的差异', '查找原文证据并引用'];
  }
  return [];
});

const unavailableModeMessage = computed(() =>
  selectedModeUnavailable.value === 'permission_required'
    ? '当前账号没有使用此模式所需的角色，无法发起任务。'
    : '此模式所需的运行配置尚未就绪，无法发起任务。'
);

const footerNotice = computed(() => {
  if (isSandbox.value) return '代码将在隔离环境中执行；请勿提交密钥或私有文档正文。';
  if (capabilityStore.selectedSkillName === 'sql-assistant') {
    return '数据分析只允许受限的只读查询，结果仍需结合业务口径复核。';
  }
  if (capabilityStore.selectedSkillName === 'web-research') {
    return '网页信息具有时效性，请结合来源时间与覆盖范围复核。';
  }
  return 'AI 生成内容可能有误，重要结论请结合引用复核。';
});

const handleCompositionStart = () => {
  isComposing.value = true;
};

const handleCompositionEnd = () => {
  isComposing.value = false;
};

const handleKeyDown = (event: KeyboardEvent) => {
  if (event.key === 'Enter' && !event.shiftKey && !isComposing.value) {
    event.preventDefault();
    onSend();
  }
};

const autoResize = () => {
  if (!textareaRef.value) return;
  textareaRef.value.style.height = 'auto';
  textareaRef.value.style.height = Math.min(textareaRef.value.scrollHeight, 140) + 'px';
};

const resetTextareaHeight = () => {
  if (textareaRef.value) textareaRef.value.style.height = 'auto';
};

const focusTextarea = async () => {
  await nextTick();
  textareaRef.value?.focus();
  autoResize();
};

const applyPrompt = async (prompt: string) => {
  chatStore.userInput = prompt;
  await focusTextarea();
};

const retryCapabilities = () => void capabilityStore.retryCatalog().catch(() => undefined);

const useGeneralMode = async () => {
  capabilityStore.selectSkill(null);
  await focusTextarea();
};

const selectHitlOption = async (option: string) => {
  chatStore.selectHitlOption(option);
  await focusTextarea();
};

const onSend = async () => {
  const text = chatStore.userInput.trim();
  if (!text || chatStore.isInputLocked || isComposing.value) return;
  await chatStore.handleSend();
  await nextTick();
  resetTextareaHeight();
};

onMounted(() => window.addEventListener('capability-selected', focusTextarea));
onUnmounted(() => window.removeEventListener('capability-selected', focusTextarea));
</script>

<style scoped>
.mode-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 7px;
}

.mode-trigger {
  display: flex;
  min-width: 0;
  flex: 1;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  border: 1px solid var(--line);
  border-radius: 12px;
  color: var(--muted);
  background: var(--surface-soft);
  cursor: pointer;
  text-align: left;
}

.mode-trigger:hover:not(:disabled),
.mode-trigger.is-selected {
  border-color: var(--line-strong);
  color: var(--text-soft);
  background: var(--surface);
}

.mode-trigger-icon {
  display: grid;
  width: 28px;
  height: 28px;
  flex: none;
  place-items: center;
  border-radius: 9px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.07);
  font-size: var(--font-small);
}

.mode-trigger-copy {
  min-width: 0;
  flex: 1;
}

.mode-trigger-copy strong,
.mode-trigger-copy small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mode-trigger-copy strong {
  color: var(--text-soft);
  font-size: var(--font-small);
}

.mode-trigger-copy small {
  margin-top: 2px;
  color: var(--muted);
  font-size: var(--font-micro);
}

.mode-trigger kbd {
  padding: 3px 5px;
  border: 1px solid var(--line);
  border-radius: 5px;
  color: var(--muted);
  font-size: var(--font-micro);
}

.mode-trigger > .fa-chevron-down {
  font-size: var(--font-micro);
}

.mode-approval-dot {
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 3px;
  padding: 3px 6px;
  border-radius: 999px;
  color: var(--warning);
  background: var(--warning-soft);
  font-size: var(--font-micro);
}

.mode-center-link {
  flex: none;
  padding: 7px 9px;
  border-radius: 9px;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
  font-size: var(--font-caption);
}

.mode-center-link:hover:not(:disabled) {
  color: var(--mint);
  background: var(--surface);
}

.mode-notice {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 7px;
  padding: 8px 10px;
  border: 1px solid rgba(168, 246, 209, 0.16);
  border-radius: 10px;
  color: var(--muted);
  background: rgba(168, 246, 209, 0.04);
  font-size: var(--font-caption);
}

.mode-notice.is-error {
  border-color: rgba(255, 137, 151, 0.18);
  color: var(--danger);
  background: var(--danger-soft);
}

.mode-notice button {
  margin-left: auto;
  padding: 4px 7px;
  border-radius: 7px;
  color: inherit;
  background: var(--surface);
  cursor: pointer;
}

.mode-composer-panel {
  margin-bottom: 7px;
  padding: 9px 10px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-soft);
}

.mode-quick-prompts,
.sandbox-language {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}

.mode-quick-prompts > span,
.sandbox-language > span {
  margin-right: 3px;
  color: var(--muted);
  font-size: var(--font-micro);
}

.mode-quick-prompts button,
.sandbox-language button {
  padding: 4px 7px;
  border: 1px solid var(--line);
  border-radius: 7px;
  color: var(--text-soft);
  background: var(--surface);
  cursor: pointer;
  font-size: var(--font-micro);
}

.sandbox-language button.active {
  border-color: rgba(168, 246, 209, 0.3);
  color: var(--mint);
  background: rgba(168, 246, 209, 0.06);
}

.sandbox-language small {
  margin-left: auto;
  color: var(--muted);
  font-size: var(--font-micro);
}

.chat-input-textarea.is-code-input {
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', monospace;
  tab-size: 2;
}

@media (max-width: 620px) {
  .mode-trigger kbd,
  .mode-center-link,
  .sandbox-language small {
    display: none;
  }

  .mode-trigger-copy small {
    max-width: 190px;
  }
}
</style>
