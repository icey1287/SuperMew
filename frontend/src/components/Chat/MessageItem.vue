<template>
  <div
    v-if="!msg.isHitlRequest && !msg.isHitlAnswer"
    :class="['message', msg.isUser ? 'user-message' : 'bot-message']"
  >
    <div v-if="!msg.isUser" class="message-avatar" aria-hidden="true">
      <i class="fa-solid fa-cat"></i>
    </div>

    <div class="message-column">
      <div v-if="!msg.isUser" class="message-author">
        <span>喵喵助手</span>
        <small v-if="msg.skillName" class="message-skill-badge">
          <i class="fa-solid fa-wand-magic-sparkles"></i>
          {{ skillLabel(msg.skillName) }}
        </small>
        <small v-if="msg.ragTrace?.retrieved_chunks?.length">
          已引用 {{ msg.ragTrace.retrieved_chunks.length }} 个来源
        </small>
      </div>

      <template v-if="msg.isUser">
        <MessageContent :text="msg.text" :is-user="true" :msg-index="msgIndex" />
      </template>

      <template v-else>
        <div v-if="msg.hitlResumeText" class="hitl-resume-note">
          <i class="fa-solid fa-rotate-right"></i>
          <span>已补充：{{ msg.hitlResumeText }}，正在继续原流程</span>
        </div>

        <ThinkingTrace v-if="msg.isThinking && !msg.text" :msg="msg" :msg-index="msgIndex" />

        <template v-else>
          <MessageContent
            :text="msg.text"
            :is-user="false"
            :msg-index="msgIndex"
            @cite-click="onCiteClick"
          />
          <References
            ref="referencesRef"
            :msg="msg"
            :msg-index="msgIndex"
            @cite-click="onCiteClick"
          />
          <RetrievalTraceDetails :msg="msg" />
        </template>

        <div
          v-if="msg.runId"
          :class="['message-run-inspector', { 'is-pre-delta': msg.isThinking && !msg.text }]"
        >
          <details :open="msg.isThinking" @toggle="loadInspector">
            <summary>
              <span><i class="fa-solid fa-route"></i> Run 执行记录</span>
              <small>
                {{
                  msg.runTimeline?.length
                    ? `${msg.runTimeline.length} 个公开 Event`
                    : msg.isThinking
                      ? '等待公开 Event'
                      : '展开后按需加载'
                }}
              </small>
            </summary>
            <p v-if="inspectorLoading" class="message-inspector-state" role="status">
              <i class="fa-solid fa-spinner fa-spin"></i> 正在重放 Event Journal…
            </p>
            <p v-else-if="inspectorError" class="message-inspector-state is-error" role="status">
              {{ inspectorError }}
            </p>
            <ExecutionTimeline v-else :items="msg.runTimeline || []" />
          </details>
          <ArtifactShelf v-if="msg.artifacts?.length" :artifacts="msg.artifacts" compact />
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import MessageContent from './MessageContent.vue';
import ThinkingTrace from './ThinkingTrace.vue';
import References from './References.vue';
import RetrievalTraceDetails from './RetrievalTraceDetails.vue';
import ArtifactShelf from '@/components/Artifacts/ArtifactShelf.vue';
import ExecutionTimeline from '@/components/Run/ExecutionTimeline.vue';
import type { Message } from '@/types/chat';
import { useChatStore } from '@/stores/chat';
import { getPublicError } from '@/utils/api';

const props = defineProps<{
  msg: Message;
  msgIndex: number;
}>();

const emit = defineEmits<{
  (e: 'cite-click', msgIndex: number, chunkIndex: number): void;
}>();

const referencesRef = ref<InstanceType<typeof References> | null>(null);
const inspectorLoading = ref(false);
const inspectorError = ref('');
const chatStore = useChatStore();

const openReferences = () => {
  referencesRef.value?.openDetails();
};

defineExpose({ openReferences });

const onCiteClick = (msgIndex: number, chunkIndex: number) => {
  emit('cite-click', msgIndex, chunkIndex);
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

const loadInspector = async (event: Event) => {
  const details = event.currentTarget as HTMLDetailsElement | null;
  if (
    !details?.open ||
    !props.msg.runId ||
    props.msg.isThinking ||
    props.msg.runTimeline?.length ||
    inspectorLoading.value
  ) {
    return;
  }
  inspectorLoading.value = true;
  inspectorError.value = '';
  try {
    await chatStore.restoreRunProjection(props.msg.runId);
  } catch (error) {
    inspectorError.value = `Run 记录加载失败：${getPublicError(error).message}`;
  } finally {
    inspectorLoading.value = false;
  }
};
</script>

<style scoped>
.message-skill-badge {
  display: inline-flex !important;
  align-items: center;
  gap: 4px;
  padding: 3px 6px;
  border: 1px solid rgba(200, 185, 255, 0.18);
  border-radius: 999px;
  color: var(--lilac) !important;
  background: rgba(200, 185, 255, 0.06);
}

.message-run-inspector {
  display: grid;
  gap: 9px;
  margin-top: 12px;
}

.message-run-inspector.is-pre-delta {
  display: none;
}

.message-run-inspector details {
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 13px;
  background: var(--surface-soft);
}

.message-run-inspector summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 9px;
  color: var(--text-soft);
  cursor: pointer;
  font-size: 8px;
  list-style: none;
}

.message-run-inspector summary::-webkit-details-marker {
  display: none;
}

.message-run-inspector summary small {
  color: var(--muted);
  font-size: 7px;
}

.message-run-inspector details[open] summary {
  margin-bottom: 11px;
}

.message-inspector-state {
  padding: 10px;
  border: 1px dashed var(--line);
  border-radius: 10px;
  color: var(--muted);
  font-size: 8px;
}

.message-inspector-state.is-error {
  color: var(--danger);
}

@media (max-width: 1180px) {
  .message-run-inspector.is-pre-delta {
    display: grid;
  }
}
</style>
