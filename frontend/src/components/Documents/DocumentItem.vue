<template>
  <article :class="['document-item', { deleting: deleteJob?.status === 'running' }]">
    <div class="document-row">
      <div class="document-info">
        <span :class="['document-icon', fileTone]">
          <i :class="fileIcon"></i>
        </span>
        <span class="document-details">
          <strong class="document-name">{{ doc.filename }}</strong>
          <small>{{ doc.file_type }} · 已建立混合索引</small>
        </span>
      </div>

      <span class="document-chunks">{{ doc.chunk_count.toLocaleString() }}</span>
      <span :class="['document-status', { failed: deleteJob?.status === 'failed' }]">
        <i :class="statusIcon"></i>
        {{ statusLabel }}
      </span>

      <button
        type="button"
        class="btn-danger"
        :title="deleteJob?.status === 'failed' ? '重试删除' : '删除文档'"
        :disabled="documentStore.isDeleteActionLocked(doc.filename)"
        @click="onDelete"
      >
        <i :class="documentStore.getDeleteButtonIcon(doc.filename)"></i>
      </button>
    </div>

    <div
      v-if="deleteJob"
      :class="['upload-progress', 'delete-progress', { collapsed: deleteJob.collapsed }]"
    >
      <button type="button" class="upload-progress-header" @click="onToggleCollapse">
        <span>
          <strong>{{ deleteJob.message || '删除进度' }}</strong>
          <small>{{ deleteJob.status === 'completed' ? '清理完成' : '正在同步各存储层' }}</small>
        </span>
        <span class="upload-toggle">
          {{ deleteJob.collapsed ? '展开' : '收起' }}
          <i :class="deleteJob.collapsed ? 'fa-solid fa-chevron-down' : 'fa-solid fa-chevron-up'"></i>
        </span>
      </button>

      <div v-show="!deleteJob.collapsed" class="upload-step-list">
        <div
          v-for="step in deleteJob.steps"
          :key="step.key"
          :class="['upload-step', 'upload-step-' + step.status]"
        >
          <div class="upload-step-header">
            <span class="upload-step-label">{{ step.label }}</span>
            <span class="upload-step-percent">{{ step.percent }}%</span>
          </div>
          <div class="upload-step-bar">
            <div class="upload-step-fill" :style="{ width: step.percent + '%' }"></div>
          </div>
          <div v-if="step.message" class="upload-step-message">{{ step.message }}</div>
        </div>
      </div>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useDocumentStore } from '@/stores/documents';
import type { DocumentItem } from '@/types/document';

const props = defineProps<{
  doc: DocumentItem;
}>();

const documentStore = useDocumentStore();
const deleteJob = computed(() => documentStore.deleteJobs[props.doc.filename]);

const fileIcon = computed(() => {
  if (props.doc.file_type === 'PDF') return 'fa-regular fa-file-pdf';
  if (props.doc.file_type === 'Word') return 'fa-regular fa-file-word';
  if (props.doc.file_type === 'Excel') return 'fa-regular fa-file-excel';
  return 'fa-regular fa-file-lines';
});

const fileTone = computed(() => {
  if (props.doc.file_type === 'PDF') return 'pdf';
  if (props.doc.file_type === 'Word') return 'word';
  if (props.doc.file_type === 'Excel') return 'excel';
  return 'generic';
});

const statusLabel = computed(() => {
  if (deleteJob.value?.status === 'running') return '删除中';
  if (deleteJob.value?.status === 'completed') return '已删除';
  if (deleteJob.value?.status === 'failed') return '删除失败';
  return '可检索';
});

const statusIcon = computed(() => {
  if (deleteJob.value?.status === 'running') return 'fa-solid fa-spinner fa-spin';
  if (deleteJob.value?.status === 'failed') return 'fa-solid fa-triangle-exclamation';
  return 'fa-solid fa-circle-check';
});

const onDelete = async () => {
  try {
    await documentStore.deleteDocument(props.doc.filename);
  } catch (error: any) {
    alert(error.message);
  }
};

const onToggleCollapse = () => {
  documentStore.toggleDeleteJobCollapsed(props.doc.filename);
};
</script>
