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
