// @vitest-environment jsdom

import { afterEach, describe, expect, it } from 'vitest';
import { createApp, nextTick, type App } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import KnowledgeContextPanel from './KnowledgeContextPanel.vue';
import { useChatStore } from '@/stores/chat';

describe('KnowledgeContextPanel', () => {
  let app: App<Element> | null = null;
  let root: HTMLDivElement | null = null;

  afterEach(() => {
    app?.unmount();
    root?.remove();
    app = null;
    root = null;
  });

  it('shows total active Run time across HITL execution phases', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const chatStore = useChatStore();
    chatStore.threadId = 'thread_duration_test';
    chatStore.messages = [
      {
        text: '',
        isUser: false,
        isThinking: false,
        runActiveDurationMs: 35_000,
        runActiveStartedAt: null,
        ragSteps: [
          { label: '首次检索完成', elapsed_ms: 19_700 },
          { label: 'HITL 恢复检索完成', elapsed_ms: 4_700 },
        ],
      },
    ];

    root = document.createElement('div');
    document.body.appendChild(root);
    app = createApp(KnowledgeContextPanel);
    app.use(pinia);
    app.mount(root);
    await nextTick();

    expect(root.querySelector('.context-card-heading > span')?.textContent).toBe('35.0s');
  });
});
