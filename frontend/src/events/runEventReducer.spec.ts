import { describe, expect, it } from 'vitest';
import { applyRunEvent, initialRunEventState, type RuntimeRunEvent } from './runEventReducer';

function event(
  sequence: number,
  type: RuntimeRunEvent['type'],
  data: Record<string, unknown> = {},
  overrides: Partial<RuntimeRunEvent> = {}
): RuntimeRunEvent {
  return {
    schema_version: 1,
    event_id: `evt_${sequence}`,
    sequence,
    run_id: 'run_1',
    thread_id: 'thread-1',
    type,
    timestamp: '2026-07-14T00:00:00Z',
    data,
    ...overrides,
  };
}

describe('run event reducer', () => {
  it('deduplicates sequence and lets message.completed replace deltas and hydrate trace', () => {
    let state = initialRunEventState('run_1', 'thread-1');
    state = applyRunEvent(
      state,
      event(1, 'run.created', {
        status: 'pending',
        user_message_id: 11,
        assistant_message_id: 12,
      })
    );
    state = applyRunEvent(state, event(2, 'message.delta', { content: 'hel' }));
    const duplicate = applyRunEvent(state, event(2, 'message.delta', { content: 'duplicate' }));
    expect(duplicate).toBe(state);

    state = applyRunEvent(
      state,
      event(3, 'message.completed', {
        content: 'hello',
        status: 'completed',
        rag_trace: { retrieval_outcome: 'ANSWERABLE' },
      })
    );

    expect(state).toMatchObject({
      userMessageId: 11,
      assistantMessageId: 12,
      messageText: 'hello',
      messageStatus: 'completed',
      ragTrace: { retrieval_outcome: 'ANSWERABLE' },
      lastSequence: 3,
    });
  });

  it('marks a sequence gap without advancing the cursor or applying the event', () => {
    let state = applyRunEvent(initialRunEventState('run_1', 'thread-1'), event(1, 'run.started'));
    state = applyRunEvent(state, event(3, 'message.delta', { content: 'must-not-apply' }));

    expect(state.hasGap).toBe(true);
    expect(state.lastSequence).toBe(1);
    expect(state.messageText).toBe('');

    state = applyRunEvent(state, event(2, 'message.delta', { content: 'recovered' }));
    expect(state.lastSequence).toBe(2);
    expect(state.messageText).toBe('recovered');
  });

  it('rejects events belonging to another Run or Thread', () => {
    const initial = initialRunEventState('run_1', 'thread-1');

    expect(applyRunEvent(initial, event(1, 'run.started', {}, { run_id: 'run_2' }))).toBe(initial);
    expect(applyRunEvent(initial, event(1, 'run.started', {}, { thread_id: 'thread-2' }))).toBe(
      initial
    );
  });

  it('keeps the first terminal event sticky', () => {
    let state = applyRunEvent(
      initialRunEventState('run_1', 'thread-1'),
      event(1, 'run.cancelled', {
        error: {
          code: 'RUN_CANCELLED',
          message: '运行已由用户取消。',
          retryable: false,
        },
      })
    );
    const cancelled = state;
    state = applyRunEvent(state, event(2, 'run.completed'));

    expect(state).toBe(cancelled);
    expect(state.status).toBe('cancelled');
    expect(state.terminalSequence).toBe(1);
    expect(state.error?.code).toBe('RUN_CANCELLED');
  });

  it('projects same-Run HITL pause and resume state', () => {
    let state = initialRunEventState('run_1', 'thread-1');
    state = applyRunEvent(state, event(1, 'run.waiting_input'));
    state = applyRunEvent(
      state,
      event(2, 'hitl.required', {
        hitl_token: 'hitl_1',
        checkpoint_id: 'checkpoint_1',
        prompt: '请补充角色名',
        options: ['丹瑾', '丹恒'],
        route: 'clarify',
      })
    );

    expect(state.status).toBe('waiting_input');
    expect(state.pendingHitl).toEqual({
      hitlToken: 'hitl_1',
      checkpointId: 'checkpoint_1',
      prompt: '请补充角色名',
      options: ['丹瑾', '丹恒'],
      route: 'clarify',
      retrievalStatus: null,
      originalQuestion: null,
    });

    state = applyRunEvent(state, event(3, 'hitl.resumed', { answer: '丹瑾' }));
    expect(state.status).toBe('running');
    expect(state.pendingHitl).toBeNull();
    expect(state.lastResumeAnswer).toBe('丹瑾');
  });

  it('records progress, cancellation requests, tool failures and warnings', () => {
    let state = initialRunEventState('run_1', 'thread-1');
    state = applyRunEvent(
      state,
      event(1, 'tool.progress', {
        tool_name: 'search_knowledge_base',
        step: { label: '检索中' },
      })
    );
    state = applyRunEvent(
      state,
      event(2, 'tool.failed', {
        tool_name: 'rerank',
        fallback_applied: true,
        error: { code: 'RERANK_TIMEOUT', retryable: true },
      })
    );
    state = applyRunEvent(
      state,
      event(3, 'warning.created', {
        code: 'CANCEL_REQUESTED',
        message: '用户已请求停止运行',
      })
    );

    expect(state.toolProgress).toEqual([
      { toolName: 'search_knowledge_base', step: { label: '检索中' } },
    ]);
    expect(state.toolFailures[0]).toMatchObject({
      toolName: 'rerank',
      fallbackApplied: true,
      error: { code: 'RERANK_TIMEOUT', retryable: true },
    });
    expect(state.status).toBe('cancelling');
    expect(state.warnings[0].code).toBe('CANCEL_REQUESTED');
  });

  it('projects tool lifecycle, public guardrail fields and Artifact identities', () => {
    let state = initialRunEventState('run_1', 'thread-1');
    state = applyRunEvent(
      state,
      event(1, 'tool.started', {
        tool_name: 'sandbox_execute',
        tool_call_id: 'call_1',
      })
    );
    state = applyRunEvent(
      state,
      event(2, 'tool.completed', {
        tool_name: 'sandbox_execute',
        tool_call_id: 'call_1',
        duration_ms: 245,
        result_size: 512,
        guardrail_decision: 'ALLOW',
        reason_code: 'ALLOWED',
      })
    );
    state = applyRunEvent(
      state,
      event(3, 'artifact.created', {
        artifact_id: 'art_result_1',
        name: 'result.json',
        media_type: 'application/json',
        uri: '/api/artifacts/art_result_1',
        size_bytes: 512,
        sha256: 'a'.repeat(64),
        tool_name: 'sandbox_execute',
        tool_call_id: 'call_1',
      })
    );

    expect(state.timeline[0]).toMatchObject({
      id: 'tool:call_1',
      status: 'completed',
      toolName: 'sandbox_execute',
      durationMs: 245,
      resultSize: 512,
      guardrailDecision: 'ALLOW',
      guardrailReasonCode: 'ALLOWED',
    });
    expect(state.artifacts).toEqual([
      expect.objectContaining({
        artifactId: 'art_result_1',
        name: 'result.json',
        mediaType: 'application/json',
        uri: '/api/artifacts/art_result_1',
      }),
    ]);
    expect(state.timeline.at(-1)).toMatchObject({
      kind: 'artifact',
      title: '生成 Artifact：result.json',
    });
  });

  it('captures typed provider failures and safely records unknown events', () => {
    let state = applyRunEvent(
      initialRunEventState('run_1', 'thread-1'),
      event(1, 'future.event', { future: true })
    );
    expect(state.unknownEventTypes).toEqual(['future.event']);

    state = applyRunEvent(
      state,
      event(2, 'run.failed', {
        error: {
          code: 'MODEL_RATE_LIMITED',
          message: 'raw provider message',
          retryable: true,
          category: 'provider',
          retry_after_seconds: 3,
        },
      })
    );
    expect(state.error).toMatchObject({
      code: 'MODEL_RATE_LIMITED',
      message: '上游模型服务当前繁忙，请稍后重试',
      retryable: true,
      retryAfterSeconds: 3,
    });
  });
});
