<template>
  <div class="input-area-wrapper">
    <div v-if="chatStore.currentPendingHitl" class="hitl-panel">
      <div class="hitl-panel-header">
        <i class="fas fa-circle-question"></i>
        <span>需要你补充一下</span>
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

    <div :class="['input-area', { 'hitl-active': chatStore.currentPendingHitl }]">
      <button class="attach-btn" :disabled="chatStore.isInputLocked"><i class="fas fa-paperclip"></i></button>
      
      <textarea 
        v-model="chatStore.userInput" 
        @keydown="handleKeyDown"
        @compositionstart="handleCompositionStart"
        @compositionend="handleCompositionEnd"
        @input="autoResize"
        :placeholder="chatStore.inputPlaceholder" 
        rows="1"
        ref="textareaRef"
        :disabled="chatStore.isInputLocked"
      ></textarea>
      
      <button 
        v-if="chatStore.isViewingStreamingSession" 
        @click="chatStore.handleStop" 
        class="send-btn stop-btn" 
        title="终止回答"
      >
        <i class="fas fa-stop"></i>
      </button>
      
      <button 
        v-else 
        @click="onSend" 
        class="send-btn" 
        :disabled="chatStore.isLoading"
        :title="chatStore.isLoading ? '当前已有回答正在生成' : '发送'"
      >
        <i class="fas fa-paper-plane"></i>
      </button>
    </div>
    <div class="footer-text">AI 生成的内容可能包含错误，请仔细甄别。</div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue';
import { useChatStore } from '@/stores/chat';

const chatStore = useChatStore();
const textareaRef = ref<HTMLTextAreaElement | null>(null);
const isComposing = ref(false);

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
  if (textareaRef.value) {
    textareaRef.value.style.height = 'auto';
    textareaRef.value.style.height = textareaRef.value.scrollHeight + 'px';
  }
};

const resetTextareaHeight = () => {
  if (textareaRef.value) {
    textareaRef.value.style.height = 'auto';
  }
};

const focusTextarea = async () => {
  await nextTick();
  textareaRef.value?.focus();
  autoResize();
};

const selectHitlOption = async (option: string) => {
  chatStore.selectHitlOption(option);
  await focusTextarea();
};

const onSend = async () => {
  const text = chatStore.userInput.trim();
  if (!text || chatStore.isLoading || isComposing.value) return;

  await chatStore.handleSend();
  
  await nextTick();
  resetTextareaHeight();
};
</script>
