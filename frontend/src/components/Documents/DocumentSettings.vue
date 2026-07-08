<template>
  <div class="settings-panel">
    <div class="settings-header">
      <h2><i class="fas fa-cog"></i> 文档管理</h2>
      <p>上传文档进行向量化处理，支持 PDF 和 Word、Excel 格式</p>
    </div>

    <!-- Upload Section -->
    <UploadSection />

    <!-- Documents List Section -->
    <div class="documents-section">
      <h3><i class="fas fa-list"></i> 已上传文档</h3>
      <button @click="onRefresh" class="btn-secondary" :disabled="documentStore.documentsLoading">
        <i class="fas fa-sync" :class="{ 'fa-spin': documentStore.documentsLoading }"></i> 刷新列表
      </button>
      
      <div v-if="documentStore.documentsLoading" class="loading-indicator">
        加载中...
      </div>
      
      <div v-else-if="documentStore.documents.length === 0" class="empty-documents">
        <i class="fas fa-inbox"></i>
        <p>暂无文档</p>
      </div>
      
      <div v-else class="documents-list">
        <DocumentItem 
          v-for="doc in documentStore.documents" 
          :key="doc.filename" 
          :doc="doc" 
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue';
import UploadSection from './UploadSection.vue';
import DocumentItem from './DocumentItem.vue';
import { useDocumentStore } from '@/stores/documents';

const documentStore = useDocumentStore();

const onRefresh = async () => {
  try {
    await documentStore.loadDocuments();
  } catch (error: any) {
    alert(error.message);
  }
};

onMounted(() => {
  documentStore.loadDocuments();
});

onUnmounted(() => {
  // Upload polling belongs to the active upload job, not this settings view.
  documentStore.stopAllDeleteJobPolling();
});
</script>
