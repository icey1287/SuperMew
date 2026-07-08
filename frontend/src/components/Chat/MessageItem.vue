<template>
  <div
    v-if="!msg.isHitlRequest && !msg.isHitlAnswer"
    :class="['message', msg.isUser ? 'user-message' : 'bot-message']"
  >
    <!-- User or finished AI answer -->
    <template v-if="msg.isUser">
      <MessageContent 
        :text="msg.text" 
        :is-user="true" 
        :msg-index="msgIndex" 
      />
    </template>
    
    <template v-else>
      <div v-if="msg.hitlResumeText" class="hitl-resume-note">
        <i class="fas fa-rotate-right"></i>
        <span>已补充：{{ msg.hitlResumeText }}，继续原流程</span>
      </div>

      <!-- RAG Thinking/Trace view -->
      <ThinkingTrace 
        v-if="msg.isThinking && !msg.text" 
        :msg="msg" 
        :msg-index="msgIndex" 
      />
      
      <!-- Actual response text -->
      <template v-else>
        <MessageContent
          :text="msg.text" 
          :is-user="false" 
          :msg-index="msgIndex" 
          @cite-click="onCiteClick"
        />
        
        <!-- RAG Source documents -->
        <References 
          ref="referencesRef"
          :msg="msg" 
          :msg-index="msgIndex" 
          @cite-click="onCiteClick"
        />
        
        <!-- Deep retrieval traces logs -->
        <RetrievalTraceDetails :msg="msg" />
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import MessageContent from './MessageContent.vue';
import ThinkingTrace from './ThinkingTrace.vue';
import References from './References.vue';
import RetrievalTraceDetails from './RetrievalTraceDetails.vue';
import type { Message } from '@/types/chat';

const props = defineProps<{
  msg: Message;
  msgIndex: number;
}>();

const emit = defineEmits<{
  (e: 'cite-click', msgIndex: number, chunkIndex: number): void;
}>();

const referencesRef = ref<InstanceType<typeof References> | null>(null);

const openReferences = () => {
  referencesRef.value?.openDetails();
};

defineExpose({
  openReferences
});

const onCiteClick = (msgIndex: number, chunkIndex: number) => {
  emit('cite-click', msgIndex, chunkIndex);
};
</script>
