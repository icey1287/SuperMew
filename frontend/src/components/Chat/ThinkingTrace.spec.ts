// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, h, nextTick, type App } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import ThinkingTrace from './ThinkingTrace.vue';
import { useChatStore } from '@/stores/chat';
import { applyRunEvent, initialRunEventState } from '@/events/runEventReducer';
import type { Message, RunEventV1 } from '@/types/chat';

describe('ThinkingTrace timing', () => {
  const epoch = Date.parse('2026-09-16T00:00:00Z');
  let app: App<Element> | null;
  let root: HTMLDivElement;
  let pinia: ReturnType<typeof createPinia>;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(epoch);
    pinia = createPinia();
    setActivePinia(pinia);
    root = document.createElement('div');
    document.body.appendChild(root);
    app = null;
  });

  afterEach(() => {
    app?.unmount();
    root.remove();
    vi.useRealTimers();
  });

  async function mount(message: () => Message) {
    app = createApp({ render: () => h(ThinkingTrace, { msg: message(), msgIndex: 0 }) });
    app.use(pinia);
    app.mount(root);
    await nextTick();
  }

  const stepTimes = () =>
    Array.from(root.querySelectorAll('.thinking-trace-time'), (item) => item.textContent);

  it('shows per-step durations for both main and grouped steps, including zero', async () => {
    const store = useChatStore();
    const steps = [
      { label: '检索完成', elapsed_ms: 5100, stage_elapsed_ms: 2100 },
      { label: '合并完成', elapsed_ms: 5100, stage_elapsed_ms: 0 },
      { label: '评估完成', group: 'sub-1', elapsed_ms: 9000, stage_elapsed_ms: 3900 },
      { label: '缺少步骤耗时', elapsed_ms: 9000 },
    ];
    const message: Message = {
      text: '',
      isUser: false,
      ragSteps: steps,
      _groupedSteps: store.groupRagSteps(steps),
    };
    await mount(() => message);
    expect(stepTimes()).toEqual(['2.1s', '0.0s', '3.9s']);
  });

  it('preserves total and step times through repeated HITL, remounting and event replay', async () => {
    const store = useChatStore();
    store.threadId = 'thread-timing';
    store.messagesByThread[store.threadId] = [
      { runId: 'run-timing', text: '', isUser: false, isThinking: true },
    ];
    store.messages = store.messagesByThread[store.threadId];
    let state = initialRunEventState('run-timing', store.threadId);
    const events: RunEventV1[] = [];
    async function emit(type: RunEventV1['type'], seconds: number, data = {}) {
      vi.setSystemTime(epoch + seconds * 1000);
      const event: RunEventV1 = {
        schema_version: 1,
        event_id: `event-${events.length + 1}`,
        sequence: events.length + 1,
        run_id: state.runId,
        thread_id: state.threadId,
        type,
        timestamp: new Date().toISOString(),
        data,
      };
      events.push(event);
      state = applyRunEvent(state, event);
      store.projectRunState(state);
      await vi.advanceTimersByTimeAsync(0);
      await nextTick();
    }

    await emit('run.started', 0);
    await emit('tool.progress', 5, {
      step: { label: '首次检索', elapsed_ms: 5000, stage_elapsed_ms: 2000 },
    });
    await mount(() => store.messages[0]);
    expect(root.textContent).toContain('累计运行 5 秒');
    await emit('run.waiting_input', 9);
    await emit('hitl.required', 9, { prompt: '请选择角色' });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(root.textContent).toContain('累计运行 9 秒');
    app?.unmount();
    app = null;
    expect(vi.getTimerCount()).toBe(0);

    await emit('hitl.resumed', 69, { answer: '角色 A' });
    await emit('run.started', 70);
    await emit('tool.progress', 72, {
      step: { label: 'HITL 检索', elapsed_ms: 2000, stage_elapsed_ms: 2000 },
    });
    await mount(() => store.messages[0]);
    expect(root.textContent).toContain('累计运行 11 秒');
    expect(stepTimes()).toEqual(['2.0s', '2.0s']);
    await vi.advanceTimersByTimeAsync(3000);
    expect(root.textContent).toContain('累计运行 14 秒');

    await emit('run.waiting_input', 75);
    await emit('hitl.required', 75, { prompt: '请选择范围' });
    await emit('hitl.resumed', 135, { answer: '全部' });
    await emit('run.started', 136);
    await emit('run.completed', 140);
    await vi.advanceTimersByTimeAsync(1000);
    expect(root.textContent).toContain('累计运行 18 秒');

    state = events.reduce(applyRunEvent, initialRunEventState('run-timing', store.threadId));
    store.projectRunState(state);
    await nextTick();
    expect(root.textContent).toContain('累计运行 18 秒');
    expect(stepTimes()).toEqual(['2.0s', '2.0s']);
  });
});
