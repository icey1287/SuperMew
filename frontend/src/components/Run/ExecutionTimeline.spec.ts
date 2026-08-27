// @vitest-environment jsdom

import { afterEach, describe, expect, it } from 'vitest';
import { createApp, nextTick, type App } from 'vue';
import ExecutionTimeline from './ExecutionTimeline.vue';
import type { RunTimelineItem } from '@/events/runEventReducer';

const item = (
  id: string,
  guardrailDecision: string,
  status: RunTimelineItem['status']
): RunTimelineItem => ({
  id,
  sequence: 1,
  kind: 'tool',
  eventType: 'tool.completed',
  status,
  title: id,
  detail: null,
  timestamp: '2026-08-27T00:00:00Z',
  toolName: id,
  toolCallId: id,
  durationMs: 1,
  resultSize: 1,
  guardrailDecision,
  guardrailReasonCode: guardrailDecision === 'ALLOW' ? 'ALLOWED' : 'TEST_DENIED',
  error: null,
});

describe('ExecutionTimeline', () => {
  let app: App<Element> | null = null;
  let root: HTMLDivElement | null = null;

  afterEach(() => {
    app?.unmount();
    root?.remove();
    app = null;
    root = null;
  });

  it('hides routine ALLOW audit labels while preserving actionable denials', async () => {
    root = document.createElement('div');
    document.body.appendChild(root);
    app = createApp(ExecutionTimeline, {
      items: [item('allowed_tool', 'ALLOW', 'completed'), item('denied_tool', 'DENY', 'denied')],
    });
    app.mount(root);
    await nextTick();

    expect(root.textContent).not.toContain('策略允许');
    expect(root.textContent).toContain('策略拒绝');
    expect(root.querySelectorAll('.guardrail-chip')).toHaveLength(1);
  });
});
