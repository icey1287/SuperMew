<template>
  <div class="settings-panel">
    <header class="settings-header">
      <div>
        <span class="panel-eyebrow">Mew memory</span>
        <h1>知识库</h1>
        <p>管理喵喵可以检索的文档、索引与数据源。</p>
      </div>
      <button
        type="button"
        class="settings-refresh-btn"
        :disabled="documentStore.documentsLoading"
        @click="onRefresh"
      >
        <i class="fa-solid fa-rotate" :class="{ 'fa-spin': documentStore.documentsLoading }"></i>
        刷新数据
      </button>
    </header>

    <section class="settings-stats">
      <article>
        <span>文档总数</span>
        <strong>{{ documentStore.documents.length }}</strong>
        <small>当前知识空间</small>
      </article>
      <article>
        <span>可检索片段</span>
        <strong>{{ totalChunks.toLocaleString() }}</strong>
        <small>Milvus 叶子分块</small>
      </article>
      <article>
        <span>索引状态</span>
        <strong>{{ documentStore.documentsLoading ? '同步中' : '正常' }}</strong>
        <small>{{ documentStore.isUploading ? '正在处理新文档' : '服务已连接' }}</small>
      </article>
      <article>
        <span>支持格式</span>
        <strong>5</strong>
        <small>PDF · Word · Excel · HTML</small>
      </article>
    </section>

    <div class="settings-grid">
      <section class="documents-section">
        <div class="documents-section-head">
          <div>
            <h2>全部文档</h2>
            <p>{{ filteredDocuments.length }} 份资料可供喵喵检索</p>
          </div>
          <label class="document-search">
            <i class="fa-solid fa-magnifying-glass"></i>
            <input v-model="searchQuery" type="search" placeholder="搜索文档名称…" />
          </label>
        </div>

        <div class="document-table-head">
          <span>名称</span>
          <span>片段</span>
          <span>状态</span>
          <span></span>
        </div>

        <div v-if="documentStore.documentsLoading" class="loading-indicator">
          <span class="loading-orb"><i class="fa-solid fa-spinner fa-spin"></i></span>
          <strong>正在同步知识库</strong>
          <p>从 Milvus 读取文档与片段统计。</p>
        </div>

        <div v-else-if="filteredDocuments.length === 0" class="empty-documents">
          <span class="empty-icon"><i class="fa-regular fa-folder-open"></i></span>
          <h3>{{ searchQuery ? '没有匹配的文档' : '知识库还是空的' }}</h3>
          <p>{{ searchQuery ? '换一个关键词试试。' : '从右侧上传第一份资料，让喵喵开始学习。' }}</p>
        </div>

        <div v-else class="documents-list">
          <DocumentItem
            v-for="doc in filteredDocuments"
            :key="doc.filename"
            :doc="doc"
          />
        </div>
      </section>

      <UploadSection />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import UploadSection from './UploadSection.vue';
import DocumentItem from './DocumentItem.vue';
import { useDocumentStore } from '@/stores/documents';

const documentStore = useDocumentStore();
const searchQuery = ref('');

const totalChunks = computed(() => documentStore.documents.reduce(
  (total, document) => total + Number(document.chunk_count || 0),
  0
));

const filteredDocuments = computed(() => {
  const query = searchQuery.value.trim().toLowerCase();
  if (!query) return documentStore.documents;
  return documentStore.documents.filter((document) =>
    document.filename.toLowerCase().includes(query)
    || document.file_type.toLowerCase().includes(query)
  );
});

const onRefresh = async () => {
  try {
    await documentStore.loadDocuments();
  } catch (error: any) {
    alert(error.message);
  }
};

onMounted(onRefresh);

onUnmounted(() => {
  documentStore.stopAllDeleteJobPolling();
});
</script>
