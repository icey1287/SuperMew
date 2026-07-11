<template>
  <div
    v-if="msg.ragTrace && msg.ragTrace.retrieved_chunks && msg.ragTrace.retrieved_chunks.length"
    class="references-section"
  >
    <details ref="detailsRef" class="references-details">
      <summary class="references-title">
        <span><i class="fa-regular fa-bookmark"></i> 引用来源</span>
        <small>{{ msg.ragTrace.retrieved_chunks.length }} 个证据片段</small>
      </summary>
      <ul class="sources-list references-list">
        <li
          v-for="(chunk, cIdx) in msg.ragTrace.retrieved_chunks"
          :id="'chunk-' + msgIndex + '-' + (cIdx + 1)"
          :key="cIdx"
          class="source-item"
        >
          <button
            type="button"
            class="ref-index cite-ref"
            :data-msg-index="msgIndex"
            :data-chunk-index="cIdx + 1"
            @click="onCiteClick(cIdx + 1)"
          >
            {{ cIdx + 1 }}
          </button>
          <div class="source-content">
            <div class="source-title-line">
              <span class="source-file"><i class="fa-regular fa-file-lines"></i>{{ chunk.filename }}</span>
              <span v-if="chunk.page_number" class="source-page">第 {{ chunk.page_number }} 页</span>
            </div>
            <div class="source-meta-line">
              <span>RRF #{{ chunk.rrf_rank || (cIdx + 1) }}</span>
              <span v-if="chunk.rerank_score !== null && chunk.rerank_score !== undefined">
                Rerank {{ Number(chunk.rerank_score).toFixed(4) }}
              </span>
            </div>
            <div v-if="chunk.text" class="source-excerpt">{{ chunk.text }}</div>
          </div>
        </li>
      </ul>
    </details>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import type { Message } from '@/types/chat';

const props = defineProps<{
  msg: Message;
  msgIndex: number;
}>();

const emit = defineEmits<{
  (e: 'cite-click', msgIndex: number, chunkIndex: number): void;
}>();

const detailsRef = ref<HTMLDetailsElement | null>(null);

const openDetails = () => {
  if (detailsRef.value) detailsRef.value.open = true;
};

defineExpose({ openDetails });

const onCiteClick = (chunkIndex: number) => {
  emit('cite-click', props.msgIndex, chunkIndex);
};
</script>
