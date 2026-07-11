<template>
  <section class="upload-section">
    <div class="upload-section-head">
      <span class="upload-title-icon"><i class="fa-solid fa-cloud-arrow-up"></i></span>
      <div>
        <h2>快速入库</h2>
        <p>解析结构、三级分块并写入混合索引。</p>
      </div>
    </div>

    <input
      ref="fileInputRef"
      type="file"
      accept=".pdf,.doc,.docx,.xls,.xlsx,.html,.htm"
      hidden
      @change="onFileSelect"
    />

    <button
      type="button"
      class="upload-dropzone"
      @click="triggerFileSelect"
      @dragover.prevent
      @drop.prevent="onFileDrop"
    >
      <span class="dropzone-icon"><i class="fa-solid fa-arrow-up-from-bracket"></i></span>
      <strong>{{ documentStore.selectedFile ? documentStore.selectedFile.name : '拖放文件到这里' }}</strong>
      <span>
        {{ documentStore.selectedFile
          ? formatFileSize(documentStore.selectedFile.size)
          : '或点击选择 PDF、Word、Excel、HTML 文件' }}
      </span>
    </button>

    <div v-if="documentStore.selectedFile" class="selected-file">
      <span class="selected-file-icon"><i class="fa-regular fa-file-lines"></i></span>
      <span class="selected-file-copy">
        <strong>{{ documentStore.selectedFile.name }}</strong>
        <small>{{ formatFileSize(documentStore.selectedFile.size) }} · 等待上传</small>
      </span>
      <button
        type="button"
        class="btn-primary"
        :disabled="documentStore.isUploading"
        @click="onUpload"
      >
        <i :class="documentStore.isUploading ? 'fa-solid fa-spinner fa-spin' : 'fa-solid fa-arrow-up'"></i>
        {{ documentStore.isUploading ? '处理中' : '开始上传' }}
      </button>
    </div>

    <div
      v-if="documentStore.uploadSteps.length"
      :class="['upload-progress', { collapsed: documentStore.uploadProgressCollapsed }]"
    >
      <button type="button" class="upload-progress-header" @click="onToggleCollapse">
        <span>
          <strong>{{ documentStore.uploadProgress || '上传进度' }}</strong>
          <small>{{ completedSteps }} / {{ documentStore.uploadSteps.length }} 个阶段完成</small>
        </span>
        <span class="upload-toggle">
          {{ documentStore.uploadProgressCollapsed ? '展开' : '收起' }}
          <i :class="documentStore.uploadProgressCollapsed ? 'fa-solid fa-chevron-down' : 'fa-solid fa-chevron-up'"></i>
        </span>
      </button>

      <div v-show="!documentStore.uploadProgressCollapsed" class="upload-step-list">
        <div
          v-for="step in documentStore.uploadSteps"
          :key="step.key"
          :class="['upload-step', 'upload-step-' + step.status]"
        >
          <div class="upload-step-header">
            <span class="upload-step-label">
              <i :class="stepIcon(step.status)"></i>
              {{ step.label }}
            </span>
            <span class="upload-step-percent">{{ step.percent }}%</span>
          </div>
          <div class="upload-step-bar">
            <div class="upload-step-fill" :style="{ width: step.percent + '%' }"></div>
          </div>
          <div v-if="step.message" class="upload-step-message">{{ step.message }}</div>
        </div>
      </div>
    </div>

    <div class="upload-pipeline-note">
      <div><span>01</span><p><strong>结构解析</strong><small>识别章节、表格与页面</small></p></div>
      <div><span>02</span><p><strong>三级分块</strong><small>保留父子上下文关系</small></p></div>
      <div><span>03</span><p><strong>混合索引</strong><small>Dense + BM25 同步写入</small></p></div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import { useDocumentStore } from '@/stores/documents';
import type { UploadStep } from '@/types/document';

const documentStore = useDocumentStore();
const fileInputRef = ref<HTMLInputElement | null>(null);

const completedSteps = computed(() =>
  documentStore.uploadSteps.filter((step) => step.status === 'completed').length
);

const triggerFileSelect = () => {
  fileInputRef.value?.click();
};

const setSelectedFile = (file: File) => {
  documentStore.selectedFile = file;
  documentStore.uploadProgress = '';
  documentStore.uploadSteps = documentStore.createUploadSteps();
  documentStore.uploadProgressCollapsed = false;
  documentStore.activeUploadJobId = '';
};

const onFileSelect = (event: Event) => {
  const files = (event.target as HTMLInputElement).files;
  if (files?.length) setSelectedFile(files[0]);
};

const onFileDrop = (event: DragEvent) => {
  const file = event.dataTransfer?.files?.[0];
  if (file) setSelectedFile(file);
};

const onUpload = async () => {
  try {
    await documentStore.uploadDocument();
  } catch (error: any) {
    alert('上传文档失败：' + error.message);
  }
};

const onToggleCollapse = () => {
  documentStore.uploadProgressCollapsed = !documentStore.uploadProgressCollapsed;
};

const formatFileSize = (bytes: number) => {
  if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
};

const stepIcon = (status: UploadStep['status']) => {
  if (status === 'completed') return 'fa-solid fa-check';
  if (status === 'running') return 'fa-solid fa-spinner fa-spin';
  if (status === 'failed') return 'fa-solid fa-xmark';
  return 'fa-solid fa-circle';
};
</script>
