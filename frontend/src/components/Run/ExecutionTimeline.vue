<template>
  <div v-if="items.length" class="execution-timeline" aria-label="工具执行时间线">
    <article v-for="item in items" :key="item.id" class="execution-item">
      <span :class="['execution-node', `is-${item.status}`]" aria-hidden="true">
        <i :class="statusIcon(item.status)"></i>
      </span>
      <div class="execution-copy">
        <div class="execution-heading">
          <strong>{{ item.title }}</strong>
          <span v-if="item.durationMs !== null">{{ formatDuration(item.durationMs) }}</span>
        </div>
        <p v-if="item.detail">{{ item.detail }}</p>
        <div v-if="item.toolName || shouldShowGuardrail(item.guardrailDecision)" class="execution-meta">
          <code v-if="item.toolName">{{ item.toolName }}</code>
          <span
            v-if="shouldShowGuardrail(item.guardrailDecision)"
            :class="guardrailClass(item.guardrailDecision)"
          >
            <i class="fa-solid fa-shield-halved"></i>
            {{ guardrailLabel(item.guardrailDecision) }}
          </span>
          <span v-if="item.resultSize !== null">{{ formatBytes(item.resultSize) }}</span>
        </div>
        <p v-if="item.error" class="execution-error">{{ item.error.message }}</p>
      </div>
    </article>
  </div>

  <div v-else class="execution-empty">
    <i class="fa-solid fa-route" aria-hidden="true"></i>
    <span>Run 开始后，公开 Tool Event 会显示在这里。</span>
  </div>
</template>

<script setup lang="ts">
import type { RunTimelineItem, RunTimelineStatus } from '@/events/runEventReducer';

defineProps<{
  items: RunTimelineItem[];
}>();

const statusIcon = (status: RunTimelineStatus) => {
  if (status === 'running') return 'fa-solid fa-spinner fa-spin';
  if (status === 'completed') return 'fa-solid fa-check';
  if (status === 'warning') return 'fa-solid fa-triangle-exclamation';
  if (status === 'denied') return 'fa-solid fa-shield-halved';
  if (status === 'failed') return 'fa-solid fa-xmark';
  return 'fa-regular fa-clock';
};

const formatDuration = (durationMs: number) => {
  if (durationMs < 1000) return `${durationMs}ms`;
  return `${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)}s`;
};

const formatBytes = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

const shouldShowGuardrail = (decision: string | null) =>
  decision === 'DENY' || decision === 'REQUIRE_APPROVAL';

const guardrailLabel = (decision: string | null) => {
  if (decision === 'REQUIRE_APPROVAL') return '需要审批';
  return '策略拒绝';
};

const guardrailClass = (decision: string | null) => [
  'guardrail-chip',
  decision === 'REQUIRE_APPROVAL' ? 'is-approval' : 'is-deny',
];
</script>

<style scoped>
.execution-timeline {
  display: grid;
  gap: 0;
}

.execution-item {
  position: relative;
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr);
  gap: 10px;
  padding-bottom: 14px;
}

.execution-item:not(:last-child)::after {
  position: absolute;
  top: 22px;
  bottom: 0;
  left: 11px;
  width: 1px;
  background: var(--line);
  content: '';
}

.execution-node {
  z-index: 1;
  display: grid;
  width: 24px;
  height: 24px;
  place-items: center;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  color: var(--muted);
  background: var(--surface-strong);
  font-size: var(--font-micro);
}

.execution-node.is-running {
  border-color: rgba(168, 246, 209, 0.35);
  color: var(--mint);
}

.execution-node.is-completed {
  color: var(--success);
}

.execution-node.is-failed,
.execution-node.is-denied {
  color: var(--danger);
}

.execution-node.is-warning {
  color: var(--warning);
}

.execution-copy {
  min-width: 0;
  padding-top: 2px;
}

.execution-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.execution-heading strong {
  color: var(--text-soft);
  font-size: var(--font-small);
  font-weight: 680;
}

.execution-heading > span,
.execution-copy > p,
.execution-meta {
  color: var(--muted);
  font-size: var(--font-micro);
}

.execution-copy > p {
  margin-top: 4px;
  line-height: 1.5;
}

.execution-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
  margin-top: 6px;
}

.execution-meta code {
  overflow: hidden;
  max-width: 100%;
  padding: 2px 5px;
  border: 1px solid var(--line);
  border-radius: 5px;
  color: var(--lilac);
  background: var(--surface-soft);
  font-size: var(--font-micro);
  text-overflow: ellipsis;
}

.guardrail-chip {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 2px 5px;
  border-radius: 999px;
  background: var(--surface);
}

.guardrail-chip.is-approval {
  color: var(--warning);
}

.guardrail-chip.is-deny,
.execution-error {
  color: var(--danger) !important;
}

.execution-empty {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px;
  border: 1px dashed var(--line);
  border-radius: 11px;
  color: var(--muted);
  font-size: var(--font-caption);
  line-height: 1.5;
}
</style>
