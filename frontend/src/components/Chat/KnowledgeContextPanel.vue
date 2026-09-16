<template>
  <aside class="knowledge-context">
    <div class="context-header">
      <div>
        <span class="panel-eyebrow">Live evidence</span>
        <h2>知识脉络</h2>
      </div>
      <span :class="['context-status', { running: isRunning }]">
        <i :class="isRunning ? 'fa-solid fa-spinner fa-spin' : 'fa-solid fa-circle-check'"></i>
        {{ statusLabel }}
      </span>
    </div>

    <div v-if="!latestMessage" class="context-empty">
      <img :src="emptyEvidence" class="context-empty-illustration" alt="" />
      <h3>等待一次提问</h3>
      <p>检索步骤、证据置信度和引用来源会实时出现在这里。</p>
    </div>

    <template v-else>
      <section class="context-card run-card">
        <div class="context-card-heading">
          <div>
            <strong>本次 Agent 运行</strong>
            <small>{{ shortThreadId }}</small>
          </div>
          <span v-if="totalDuration">{{ totalDuration }}</span>
        </div>

        <ExecutionTimeline :items="timelineItems" />
      </section>

      <ArtifactShelf
        v-if="latestMessage?.artifacts?.length"
        :artifacts="latestMessage.artifacts"
        compact
      />

      <section class="context-card confidence-card">
        <div class="confidence-ring" :style="{ '--confidence': (confidence ?? 0) + '%' }">
          <strong>{{ confidence === null ? '—' : confidence + '%' }}</strong>
        </div>
        <div>
          <strong>证据置信度</strong>
          <p>{{ confidenceDescription }}</p>
        </div>
      </section>

      <div class="context-section-heading">
        <strong>引用来源</strong>
        <span>{{ sources.length ? sources.length + ' 个片段' : '暂无引用' }}</span>
      </div>

      <div v-if="sources.length" class="context-sources">
        <button
          v-for="(source, index) in sources.slice(0, 5)"
          :key="source.filename + index"
          type="button"
          class="context-source-card"
          @click="onSourceClick(index)"
        >
          <span class="context-file-icon"><i class="fa-regular fa-file-lines"></i></span>
          <span class="context-source-copy">
            <strong>{{ source.filename }}</strong>
            <small>{{ sourceLocation(source, index) }}</small>
          </span>
          <span class="context-source-score">{{ formatScore(source.rerank_score) }}</span>
        </button>
      </div>

      <div v-else class="context-no-sources">
        <i class="fa-solid fa-route"></i>
        <span>{{
          trace?.tool_used === false ? '本次为直接回答，未调用知识库。' : '正在等待可引用证据。'
        }}</span>
      </div>
    </template>
  </aside>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useChatStore } from '@/stores/chat';
import ExecutionTimeline from '@/components/Run/ExecutionTimeline.vue';
import ArtifactShelf from '@/components/Artifacts/ArtifactShelf.vue';
import type { RetrievedChunk } from '@/types/chat';
import { runActiveDurationMs } from '@/utils/runDuration';
import emptyEvidence from '@/assets/images/empty-evidence.webp';

const emit = defineEmits<{
  (e: 'cite-click', msgIndex: number, chunkIndex: number): void;
}>();

const chatStore = useChatStore();
const currentTime = ref(Date.now());
let durationTimer: ReturnType<typeof setInterval> | null = null;

onMounted(() => {
  durationTimer = setInterval(() => {
    currentTime.value = Date.now();
  }, 250);
});

onUnmounted(() => {
  if (durationTimer !== null) clearInterval(durationTimer);
});

const latestMessageIndex = computed(() => {
  for (let index = chatStore.messages.length - 1; index >= 0; index -= 1) {
    const message = chatStore.messages[index];
    if (!message.isUser) return index;
  }
  return -1;
});

const latestMessage = computed(() => {
  const index = latestMessageIndex.value;
  return index >= 0 ? chatStore.messages[index] : null;
});

const trace = computed(() => latestMessage.value?.ragTrace || null);
const sources = computed(() => trace.value?.retrieved_chunks || []);
const isRunning = computed(() =>
  Boolean(latestMessage.value?.isThinking || chatStore.isViewingStreamingThread)
);
const shortThreadId = computed(() => chatStore.threadId.replace('thread_', '').slice(-8));

const statusLabel = computed(() => {
  if (isRunning.value) return '运行中';
  if (latestMessage.value?.isHitlRequest) return '等待补充';
  return latestMessage.value ? '已完成' : '待命';
});

const timelineItems = computed(() => latestMessage.value?.runTimeline || []);

const confidence = computed<number | null>(() => {
  const raw = trace.value?.evidence_confidence;
  if (raw === null || raw === undefined || Number.isNaN(Number(raw))) return null;
  const normalized = Number(raw) <= 1 ? Number(raw) * 100 : Number(raw);
  return Math.max(0, Math.min(100, Math.round(normalized)));
});

const confidenceDescription = computed(() => {
  if (confidence.value === null) {
    return sources.value.length ? '已找到引用来源，等待证据评分。' : '本次回答没有可用的证据评分。';
  }
  if (confidence.value >= 85) return '来源相关性较高，证据之间未发现明显冲突。';
  if (confidence.value >= 60) return '证据基本可用，建议同时查看原始引用。';
  return '证据支撑较弱，请谨慎使用并进一步核验。';
});

const totalDuration = computed(() => {
  const total = runActiveDurationMs(latestMessage.value, currentTime.value);
  return total > 0 ? formatMilliseconds(total) : '';
});

const formatMilliseconds = (milliseconds: number) => {
  if (milliseconds < 1000) return Math.round(milliseconds) + 'ms';
  return (milliseconds / 1000).toFixed(1) + 's';
};

const sourceLocation = (source: RetrievedChunk, index: number) => {
  const parts: string[] = [];
  if (source.page_number) parts.push('第 ' + source.page_number + ' 页');
  parts.push('RRF #' + (source.rrf_rank || index + 1));
  return parts.join(' · ');
};

const formatScore = (score?: number | null) => {
  if (score === null || score === undefined) return '—';
  return Number(score).toFixed(2);
};

const onSourceClick = (index: number) => {
  if (latestMessageIndex.value < 0) return;
  emit('cite-click', latestMessageIndex.value, index + 1);
};
</script>
