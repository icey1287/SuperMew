import { describe, expect, it } from 'vitest';
import { applyRunEvent, initialRunEventState } from './runEventReducer';
import { SseFrameDecoder } from './runEventStream';
import type { RunEventV1 } from '@/types/generated/run-event-v1';

function event(sequence: number, type: RunEventV1['type'], data = {}): RunEventV1 {
  return {
    schema_version: 1,
    event_id: `evt_${sequence}`,
    sequence,
    run_id: 'run_1',
    thread_id: 'thread-1',
    type,
    timestamp: '2026-07-14T00:00:00Z',
    data,
  };
}

describe('run event reducer', () => {
  it('deduplicates sequence and replaces deltas with completed content', () => {
    let state = initialRunEventState('run_1', 'thread-1');
    state = applyRunEvent(state, event(1, 'run.created', { status: 'pending' }));
    state = applyRunEvent(state, event(2, 'message.delta', { delta: 'hel' }));
    const duplicate = applyRunEvent(state, event(2, 'message.delta', { delta: 'duplicate' }));
    expect(duplicate).toBe(state);
    state = applyRunEvent(state, event(3, 'message.completed', { content: 'hello' }));
    expect(state.messageText).toBe('hello');
    expect(state.lastSequence).toBe(3);
  });

  it('detects gaps and ignores deltas after terminal', () => {
    let state = initialRunEventState('run_1', 'thread-1');
    state = applyRunEvent(state, event(1, 'run.started'));
    state = applyRunEvent(state, event(3, 'run.completed'));
    state = applyRunEvent(state, event(4, 'message.delta', { delta: 'late' }));
    expect(state.hasGap).toBe(true);
    expect(state.messageText).toBe('');
    expect(state.status).toBe('completed');
  });

  it('safely records unknown event types', () => {
    const state = applyRunEvent(
      initialRunEventState('run_1', 'thread-1'),
      { ...event(1, 'warning.created'), type: 'future.event' } as any
    );
    expect(state.unknownEventTypes).toEqual(['future.event']);
  });

  it('captures a retryable typed provider failure', () => {
    const state = applyRunEvent(
      initialRunEventState('run_1', 'thread-1'),
      event(1, 'run.failed', {
        error: {
          code: 'MODEL_RATE_LIMITED',
          message: '模型服务繁忙，请稍后重试',
          retryable: true,
          category: 'provider',
          stage: 'generation',
          provider: 'openai-compatible',
          retry_after_seconds: 3,
        },
      })
    );

    expect(state.status).toBe('failed');
    expect(state.error).toEqual({
      code: 'MODEL_RATE_LIMITED',
      message: '上游模型服务当前繁忙，请稍后重试',
      retryable: true,
      category: 'provider',
      stage: 'generation',
      provider: 'openai-compatible',
      retryAfterSeconds: 3,
    });
  });

  it('supports legacy run.failed error_code payloads', () => {
    const state = applyRunEvent(
      initialRunEventState('run_1', 'thread-1'),
      event(1, 'run.failed', { error_code: 'VECTOR_STORE_UNAVAILABLE' })
    );

    expect(state.error?.code).toBe('VECTOR_STORE_UNAVAILABLE');
    expect(state.error?.retryable).toBe(true);
    expect(state.error?.message).toContain('知识检索服务');
  });

  it('records recoverable tool failures and warnings without terminating the run', () => {
    let state = initialRunEventState('run_1', 'thread-1');
    state = applyRunEvent(
      state,
      event(1, 'tool.failed', {
        tool_name: 'rerank',
        fallback_applied: true,
        error: {
          code: 'RERANK_TIMEOUT',
          message: '精排超时，已回退原排序',
          retryable: true,
        },
      })
    );
    state = applyRunEvent(
      state,
      event(2, 'warning.created', {
        error: {
          code: 'RERANK_TIMEOUT',
          message: '精排已降级',
          retryable: true,
        },
      })
    );

    expect(state.terminal).toBe(false);
    expect(state.toolFailures[0]).toMatchObject({
      toolName: 'rerank',
      fallbackApplied: true,
      error: { code: 'RERANK_TIMEOUT', retryable: true },
    });
    expect(state.warnings[0]).toMatchObject({
      code: 'RERANK_TIMEOUT',
      message: '相关性排序服务响应超时',
    });
  });

  it('keeps no-knowledge as a successful business outcome', () => {
    let state = initialRunEventState('run_1', 'thread-1');
    state = applyRunEvent(
      state,
      event(1, 'retrieval.completed', { outcome: 'no_knowledge' })
    );
    state = applyRunEvent(state, event(2, 'run.completed'));

    expect(state.status).toBe('completed');
    expect(state.error).toBeNull();
  });

  it('hydrates cancellation errors and clears stale errors on completion', () => {
    let state = applyRunEvent(
      initialRunEventState('run_1', 'thread-1'),
      event(1, 'run.cancelled', {
        error_code: 'RUN_CANCELLED',
        error: {
          code: 'RUN_CANCELLED',
          message: '运行已由用户取消。',
          retryable: false,
          category: 'run',
          stage: 'cancellation',
        },
      })
    );

    expect(state.status).toBe('cancelled');
    expect(state.error).toMatchObject({
      code: 'RUN_CANCELLED',
      retryable: false,
      category: 'run',
      stage: 'cancellation',
    });

    state = applyRunEvent(state, event(2, 'run.completed'));
    expect(state.status).toBe('completed');
    expect(state.error).toBeNull();
  });
});

describe('SSE frame decoder', () => {
  it('handles split frames, CRLF, ids and heartbeat comments', () => {
    const decoder = new SseFrameDecoder();
    expect(decoder.push(': heartbeat\r\n\r\n')).toEqual([]);
    const payload = JSON.stringify(event(1, 'run.created'));
    expect(decoder.push(`id: 1\r\nevent: run.created\r\ndata: ${payload.slice(0, 20)}`)).toEqual([]);
    const events = decoder.push(`${payload.slice(20)}\r\n\r\n`);
    expect(events).toHaveLength(1);
    expect(events[0].sequence).toBe(1);
  });
});
