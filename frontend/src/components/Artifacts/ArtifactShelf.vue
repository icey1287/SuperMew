<template>
  <section v-if="artifacts.length" :class="['artifact-shelf', { compact }]" aria-label="Artifacts">
    <div class="artifact-shelf-heading">
      <span>
        <i class="fa-solid fa-cube" aria-hidden="true"></i>
        Artifacts
      </span>
      <small>{{ artifacts.length }} 项</small>
    </div>

    <div class="artifact-grid">
      <article v-for="artifact in artifacts" :key="artifact.artifactId" class="artifact-card">
        <span class="artifact-icon" aria-hidden="true">
          <i :class="artifactIcon(artifact.mediaType)"></i>
        </span>
        <span class="artifact-copy">
          <strong>{{ artifact.name }}</strong>
          <small>
            {{ artifact.mediaType }}
            <template v-if="artifact.sizeBytes !== null">
              · {{ formatBytes(artifact.sizeBytes) }}</template
            >
          </small>
          <small v-if="artifact.toolName">来自 {{ artifact.toolName }}</small>
        </span>
        <button
          type="button"
          :disabled="!isFetchableArtifactUri(artifact.uri) || loadingId === artifact.artifactId"
          :title="artifactActionTitle(artifact)"
          :aria-label="artifactActionTitle(artifact)"
          @click="openArtifact(artifact)"
        >
          <i
            :class="
              loadingId === artifact.artifactId
                ? 'fa-solid fa-spinner fa-spin'
                : canPreview(artifact.mediaType)
                  ? 'fa-regular fa-eye'
                  : 'fa-solid fa-download'
            "
          ></i>
        </button>
      </article>
    </div>

    <p v-if="errorMessage" class="artifact-error" role="status">{{ errorMessage }}</p>
  </section>

  <Teleport to="body">
    <div
      v-if="preview"
      class="artifact-preview-backdrop"
      role="presentation"
      @click.self="closePreview"
    >
      <section
        ref="dialogRef"
        class="artifact-preview-dialog"
        role="dialog"
        aria-modal="true"
        :aria-label="`预览 ${preview.name}`"
        tabindex="-1"
        @keydown="handleDialogKeydown"
      >
        <header>
          <div>
            <span>Artifact preview</span>
            <h2>{{ preview.name }}</h2>
          </div>
          <button
            ref="closeButtonRef"
            type="button"
            aria-label="关闭 Artifact 预览"
            @click="closePreview"
          >
            <i class="fa-solid fa-xmark"></i>
          </button>
        </header>

        <div class="artifact-preview-body">
          <img v-if="preview.kind === 'image'" :src="preview.content" :alt="preview.name" />
          <pre v-else><code>{{ preview.content }}</code></pre>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref } from 'vue';
import { fetchArtifact, isFetchableArtifactUri } from '@/artifacts/artifactClient';
import type { RunArtifactState } from '@/events/runEventReducer';
import { getPublicError } from '@/utils/api';

defineProps<{
  artifacts: RunArtifactState[];
  compact?: boolean;
}>();

interface ArtifactPreview {
  name: string;
  kind: 'image' | 'text';
  content: string;
  objectUrl: boolean;
}

const loadingId = ref('');
const errorMessage = ref('');
const preview = ref<ArtifactPreview | null>(null);
const dialogRef = ref<HTMLElement | null>(null);
const closeButtonRef = ref<HTMLButtonElement | null>(null);
let returnFocus: HTMLElement | null = null;

const canPreview = (mediaType: string) =>
  mediaType.startsWith('image/') ||
  mediaType.startsWith('text/') ||
  mediaType === 'application/json' ||
  mediaType.endsWith('+json');

const artifactIcon = (mediaType: string) => {
  if (mediaType.startsWith('image/')) return 'fa-regular fa-image';
  if (mediaType === 'application/json' || mediaType.endsWith('+json')) {
    return 'fa-solid fa-code';
  }
  if (mediaType.includes('pdf')) return 'fa-regular fa-file-pdf';
  if (mediaType.startsWith('text/')) return 'fa-regular fa-file-lines';
  return 'fa-regular fa-file';
};

const formatBytes = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

const artifactActionTitle = (artifact: RunArtifactState) => {
  if (!isFetchableArtifactUri(artifact.uri)) return '该 Artifact 仅记录身份，暂无鉴权下载地址';
  return canPreview(artifact.mediaType) ? `预览 ${artifact.name}` : `下载 ${artifact.name}`;
};

const downloadBlob = (blob: Blob, name: string) => {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  anchor.rel = 'noopener';
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
};

const clearPreview = () => {
  if (preview.value?.objectUrl) URL.revokeObjectURL(preview.value.content);
  preview.value = null;
};

const closePreview = async () => {
  if (!preview.value) return;
  const target = returnFocus;
  returnFocus = null;
  clearPreview();
  await nextTick();
  if (target?.isConnected) target.focus();
};

const trapFocus = (event: KeyboardEvent) => {
  const dialog = dialogRef.value;
  if (!dialog) return;
  const focusable = Array.from(
    dialog.querySelectorAll<HTMLElement>(
      'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])'
    )
  );
  if (!focusable.length) {
    event.preventDefault();
    dialog.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
};

const handleDialogKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    void closePreview();
  } else if (event.key === 'Tab') {
    trapFocus(event);
  }
};

const openArtifact = async (artifact: RunArtifactState) => {
  if (!isFetchableArtifactUri(artifact.uri) || loadingId.value) return;
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  loadingId.value = artifact.artifactId;
  errorMessage.value = '';
  try {
    const blob = await fetchArtifact(artifact.uri);
    if (!canPreview(artifact.mediaType)) {
      downloadBlob(blob, artifact.name);
      return;
    }
    clearPreview();
    returnFocus = trigger;
    if (artifact.mediaType.startsWith('image/')) {
      preview.value = {
        name: artifact.name,
        kind: 'image',
        content: URL.createObjectURL(blob),
        objectUrl: true,
      };
    } else {
      preview.value = {
        name: artifact.name,
        kind: 'text',
        content: (await blob.text()).slice(0, 1_000_000),
        objectUrl: false,
      };
    }
    await nextTick();
    closeButtonRef.value?.focus();
  } catch (error) {
    errorMessage.value = `Artifact 打开失败：${getPublicError(error).message}`;
  } finally {
    loadingId.value = '';
  }
};

onBeforeUnmount(() => {
  returnFocus = null;
  clearPreview();
});
</script>

<style scoped>
.artifact-shelf {
  display: grid;
  gap: 9px;
  margin-top: 14px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface-soft);
}

.artifact-shelf-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.artifact-shelf-heading > span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--lilac);
  font-size: 8px;
  font-weight: 760;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.artifact-shelf-heading small {
  color: var(--muted);
  font-size: 7px;
}

.artifact-grid {
  display: grid;
  gap: 7px;
}

.artifact-card {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) 30px;
  align-items: center;
  gap: 8px;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--surface);
}

.artifact-icon,
.artifact-card > button {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  border-radius: 9px;
}

.artifact-icon {
  color: var(--mint);
  background: rgba(168, 246, 209, 0.08);
  font-size: 11px;
}

.artifact-copy {
  min-width: 0;
}

.artifact-copy strong,
.artifact-copy small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-copy strong {
  color: var(--text-soft);
  font-size: 8px;
}

.artifact-copy small {
  margin-top: 2px;
  color: var(--muted);
  font-size: 7px;
}

.artifact-card > button {
  border: 1px solid var(--line);
  color: var(--text-soft);
  background: transparent;
  cursor: pointer;
}

.artifact-card > button:hover:not(:disabled) {
  border-color: var(--mint);
  color: var(--mint);
  background: rgba(168, 246, 209, 0.06);
}

.artifact-error {
  color: var(--danger);
  font-size: 8px;
  line-height: 1.5;
}

.artifact-preview-backdrop {
  position: fixed;
  z-index: 1200;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(5, 7, 14, 0.76);
  backdrop-filter: blur(14px);
}

.artifact-preview-dialog {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  width: min(900px, 100%);
  max-height: min(760px, calc(100vh - 48px));
  overflow: hidden;
  border: 1px solid var(--line-strong);
  border-radius: 20px;
  background: var(--surface-strong);
  box-shadow: var(--shadow);
}

.artifact-preview-dialog > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 15px 17px;
  border-bottom: 1px solid var(--line);
}

.artifact-preview-dialog header span {
  color: var(--mint);
  font-size: 8px;
  font-weight: 760;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.artifact-preview-dialog h2 {
  margin-top: 4px;
  color: var(--text);
  font-size: 15px;
}

.artifact-preview-dialog header button {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: 10px;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
}

.artifact-preview-body {
  min-height: 0;
  overflow: auto;
  padding: 18px;
}

.artifact-preview-body img {
  display: block;
  max-width: 100%;
  margin: auto;
  border-radius: 12px;
}

.artifact-preview-body pre {
  min-height: 100%;
  padding: 16px;
  overflow: auto;
  border-radius: 12px;
  color: #d7dae5;
  background: rgba(5, 7, 14, 0.78);
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 11px;
  line-height: 1.65;
  white-space: pre-wrap;
}

.compact {
  padding: 10px;
}

@media (max-width: 640px) {
  .artifact-preview-backdrop {
    padding: 10px;
  }

  .artifact-preview-dialog {
    max-height: calc(100vh - 20px);
  }
}
</style>
