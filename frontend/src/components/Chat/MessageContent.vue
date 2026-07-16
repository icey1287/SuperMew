<template>
  <div class="message-content" v-html="parsedHtml" @click="onContentClick"></div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { parseMarkdown, escapeHtml } from '@/utils/markdown';

const props = defineProps<{
  text: string;
  isUser: boolean;
  msgIndex?: number | null;
}>();

const emit = defineEmits<{
  (e: 'cite-click', msgIndex: number, chunkIndex: number): void;
}>();

const parsedHtml = computed(() => {
  if (props.isUser) {
    return escapeHtml(props.text);
  }
  return parseMarkdown(props.text, props.msgIndex);
});

const onContentClick = (e: MouseEvent) => {
  const citeRef = (e.target as HTMLElement).closest('.cite-ref');
  if (!citeRef) return;
  const msgIndexStr = citeRef.getAttribute('data-msg-index');
  const chunkIndexStr = citeRef.getAttribute('data-chunk-index');
  if (msgIndexStr !== null && chunkIndexStr !== null) {
    emit('cite-click', Number(msgIndexStr), Number(chunkIndexStr));
  }
};
</script>
