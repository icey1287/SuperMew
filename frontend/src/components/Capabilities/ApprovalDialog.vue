<template>
  <Teleport to="body">
    <div
      v-if="store.approvalOpen && selectedSkill && draft"
      class="approval-backdrop"
      role="presentation"
      @click.self="cancel"
    >
      <section
        ref="dialogRef"
        class="approval-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="approval-dialog-title"
        aria-describedby="approval-dialog-description"
        tabindex="-1"
        @keydown="handleKeydown"
      >
        <header class="approval-header">
          <span class="approval-shield" aria-hidden="true">
            <i class="fa-solid fa-shield-halved"></i>
          </span>
          <div>
            <span>Run-bound approval</span>
            <h2 id="approval-dialog-title">确认高风险 Tool 预授权</h2>
          </div>
          <button type="button" aria-label="取消工具预授权" :disabled="submitting" @click="cancel">
            <i class="fa-solid fa-xmark"></i>
          </button>
        </header>

        <div class="approval-content">
          <p id="approval-dialog-description" class="approval-intro">
            <strong>{{ skillLabel(selectedSkill.name) }}</strong>
            需要在创建 Run 前获得一次明确授权。授权只包含下面的 Tool 名称，不包含参数、Secret
            或可复用凭证。
          </p>

          <section class="approval-section" aria-labelledby="approval-tools-title">
            <div class="approval-section-heading">
              <span id="approval-tools-title">本次预授权 Tool</span>
              <small>{{ draft.toolNames.length }} 项</small>
            </div>
            <ul class="approval-tool-list">
              <li v-for="toolName in draft.toolNames" :key="toolName">
                <span><i class="fa-solid fa-terminal" aria-hidden="true"></i></span>
                <code>{{ toolName }}</code>
                <small>{{ toolDescription(toolName) }}</small>
              </li>
            </ul>
          </section>

          <div class="approval-policy-grid">
            <section>
              <span><i class="fa-solid fa-network-wired" aria-hidden="true"></i> 网络策略</span>
              <strong>{{
                selectedSkill.network_policies.map(networkLabel).join(' · ') || '无网络'
              }}</strong>
            </section>
            <section>
              <span><i class="fa-solid fa-lock" aria-hidden="true"></i> 资源范围</span>
              <strong>{{
                selectedSkill.resource_scopes.map(scopeLabel).join(' · ') || '无额外资源'
              }}</strong>
            </section>
          </div>

          <section class="approval-binding">
            <span class="approval-binding-icon" aria-hidden="true">
              <i class="fa-solid fa-link"></i>
            </span>
            <div>
              <strong>绑定当前 Thread 与即将创建的单个 Run</strong>
              <p>
                Thread：<code>{{ store.activeThreadId || '发送时创建的新 Thread' }}</code>
              </p>
              <p>
                服务端会把 names-only Approval Grant 绑定用户、Tenant、Thread 和 Run；Run
                结束后不能复用，也不会为后续 Run 自动续权。
              </p>
            </div>
          </section>

          <p v-if="errorMessage" class="approval-error" role="alert">{{ errorMessage }}</p>
        </div>

        <footer class="approval-actions">
          <button ref="cancelButtonRef" type="button" :disabled="submitting" @click="cancel">
            取消，保留草稿
          </button>
          <button type="button" class="is-confirm" :disabled="submitting" @click="confirm">
            <i
              :class="submitting ? 'fa-solid fa-spinner fa-spin' : 'fa-solid fa-shield-halved'"
              aria-hidden="true"
            ></i>
            {{ submitting ? '正在创建 Run…' : '确认并发送' }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { useCapabilityStore } from '@/stores/capabilities';
import { useChatStore } from '@/stores/chat';
import { getPublicError } from '@/utils/api';

const store = useCapabilityStore();
const chatStore = useChatStore();
const dialogRef = ref<HTMLElement | null>(null);
const cancelButtonRef = ref<HTMLButtonElement | null>(null);
const submitting = ref(false);
const errorMessage = ref('');
let returnFocus: HTMLElement | null = null;

const selectedSkill = computed(() => store.selectedSkill);
const draft = computed(() => store.pendingApprovalDraft);

const skillLabel = (name: string) => {
  const labels: Record<string, string> = {
    sandbox: 'Sandbox',
    'sql-assistant': 'SQL Assistant',
    'web-research': 'Web Research',
    'knowledge-base': '知识库问答',
  };
  return labels[name] || name;
};

const toolDescription = (toolName: string) =>
  store.tools.find((tool) => tool.name === toolName)?.description ||
  '受 Registry 策略约束的执行能力';

const networkLabel = (policy: string) => {
  const labels: Record<string, string> = {
    none: '无网络',
    restricted: '受限公网',
    'private-data': '私有数据网络',
  };
  return labels[policy] || policy;
};

const scopeLabel = (scope: string) => {
  const labels: Record<string, string> = {
    none: '无额外资源',
    'public-web': '公开网页',
    'private-data-read': '私有数据只读',
    'code-execution': '隔离代码执行',
    'knowledge-read': '知识库只读',
  };
  return labels[scope] || scope;
};

const cancel = () => {
  if (submitting.value) return;
  errorMessage.value = '';
  store.closeApproval();
};

const confirm = async () => {
  if (submitting.value) return;
  submitting.value = true;
  errorMessage.value = '';
  try {
    store.confirmPendingApproval();
    await chatStore.handleSend({ approvalConfirmed: true });
    const target = returnFocus;
    returnFocus = null;
    await nextTick();
    target?.focus();
  } catch (error) {
    store.clearApprovalConfirmation();
    try {
      store.openApproval();
    } catch (approvalError) {
      store.approvalOpen = Boolean(store.pendingApprovalDraft);
      if (!store.approvalOpen) {
        errorMessage.value = getPublicError(approvalError).message;
      }
    }
    errorMessage.value ||= getPublicError(error).message;
    await nextTick();
    dialogRef.value?.focus();
  } finally {
    submitting.value = false;
  }
};

const trapFocus = (event: KeyboardEvent) => {
  const dialog = dialogRef.value;
  if (!dialog) return;
  const focusable = Array.from(
    dialog.querySelectorAll<HTMLElement>('button:not(:disabled), [tabindex]:not([tabindex="-1"])')
  );
  if (!focusable.length) return;
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

const handleKeydown = (event: KeyboardEvent) => {
  if (event.key === 'Escape') {
    event.preventDefault();
    cancel();
  } else if (event.key === 'Tab') {
    trapFocus(event);
  }
};

watch(
  () => store.approvalOpen,
  async (open) => {
    if (open) {
      if (!returnFocus) {
        returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      }
      errorMessage.value = '';
      await nextTick();
      cancelButtonRef.value?.focus();
      return;
    }
    if (submitting.value) return;
    const target = returnFocus;
    returnFocus = null;
    await nextTick();
    target?.focus();
  }
);
</script>

<style scoped>
.approval-backdrop {
  position: fixed;
  z-index: 1150;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(5, 7, 14, 0.74);
  backdrop-filter: blur(16px);
}

.approval-dialog {
  width: min(640px, 100%);
  overflow: hidden;
  border: 1px solid rgba(255, 190, 92, 0.28);
  border-radius: 22px;
  color: var(--text);
  background:
    radial-gradient(circle at 10% 0%, rgba(255, 190, 92, 0.1), transparent 35%),
    var(--surface-strong);
  box-shadow: var(--shadow);
}

.approval-header {
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr) 36px;
  align-items: center;
  gap: 12px;
  padding: 18px 20px;
  border-bottom: 1px solid var(--line);
}

.approval-shield {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border-radius: 13px;
  color: var(--warning);
  background: rgba(255, 190, 92, 0.1);
  font-size: 15px;
}

.approval-header span {
  color: var(--warning);
  font-size: 8px;
  font-weight: 760;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}

.approval-header h2 {
  margin-top: 4px;
  font-size: 18px;
  letter-spacing: -0.025em;
}

.approval-header > button {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: 11px;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
}

.approval-content {
  display: grid;
  gap: 15px;
  max-height: min(600px, calc(100vh - 190px));
  overflow-y: auto;
  padding: 18px 20px;
}

.approval-intro {
  color: var(--muted);
  font-size: 9px;
  line-height: 1.7;
}

.approval-intro strong {
  color: var(--text);
}

.approval-section {
  display: grid;
  gap: 8px;
}

.approval-section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--text-soft);
  font-size: 9px;
  font-weight: 680;
}

.approval-section-heading small {
  color: var(--muted);
  font-size: 7px;
}

.approval-tool-list {
  display: grid;
  gap: 7px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.approval-tool-list li {
  display: grid;
  grid-template-columns: 30px auto minmax(0, 1fr);
  align-items: center;
  gap: 9px;
  padding: 9px;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--surface-soft);
}

.approval-tool-list li > span {
  display: grid;
  width: 30px;
  height: 30px;
  place-items: center;
  border-radius: 9px;
  color: var(--warning);
  background: rgba(255, 190, 92, 0.08);
}

.approval-tool-list code {
  color: var(--lilac);
  font-size: 8px;
}

.approval-tool-list small {
  overflow: hidden;
  color: var(--muted);
  font-size: 7px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.approval-policy-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.approval-policy-grid section {
  display: grid;
  gap: 5px;
  padding: 11px;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--surface-soft);
}

.approval-policy-grid span {
  color: var(--muted);
  font-size: 7px;
}

.approval-policy-grid strong {
  color: var(--text-soft);
  font-size: 8px;
}

.approval-binding {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 10px;
  padding: 12px;
  border: 1px solid rgba(168, 246, 209, 0.18);
  border-radius: 12px;
  background: rgba(168, 246, 209, 0.05);
}

.approval-binding-icon {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border-radius: 10px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.08);
}

.approval-binding strong {
  color: var(--text-soft);
  font-size: 9px;
}

.approval-binding p {
  margin-top: 5px;
  color: var(--muted);
  font-size: 7px;
  line-height: 1.55;
}

.approval-binding code {
  color: var(--mint);
}

.approval-error {
  padding: 9px 11px;
  border: 1px solid rgba(255, 105, 135, 0.2);
  border-radius: 10px;
  color: var(--danger);
  background: rgba(255, 105, 135, 0.05);
  font-size: 8px;
  line-height: 1.5;
}

.approval-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 13px 20px;
  border-top: 1px solid var(--line);
}

.approval-actions button {
  min-height: 36px;
  padding: 0 14px;
  border: 1px solid var(--line-strong);
  border-radius: 10px;
  color: var(--text-soft);
  background: transparent;
  cursor: pointer;
  font-size: 8px;
  font-weight: 680;
}

.approval-actions button.is-confirm {
  border-color: rgba(255, 190, 92, 0.35);
  color: #191207;
  background: var(--warning);
}

.approval-actions button:disabled {
  cursor: wait;
  opacity: 0.58;
}

@media (max-width: 640px) {
  .approval-backdrop {
    align-items: end;
    padding: 10px;
  }

  .approval-dialog {
    border-radius: 18px;
  }

  .approval-policy-grid {
    grid-template-columns: 1fr;
  }

  .approval-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }
}
</style>
