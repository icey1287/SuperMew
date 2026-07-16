import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  connectRunEventStream,
  SseFrameDecoder,
} from './runEventStream';
import type { RuntimeRunEvent } from './runEventReducer';

function event(
  sequence: number,
  type: RuntimeRunEvent['type'] = 'run.completed',
  overrides: Partial<RuntimeRunEvent> = {}
): RuntimeRunEvent {
  return {
    schema_version: 1,
    event_id: `evt_${sequence}`,
    sequence,
    run_id: 'run_1',
    thread_id: 'thread-1',
    type,
    timestamp: '2026-07-15T00:00:00Z',
    data: {},
    ...overrides,
  };
}

function streamResponse(
  events: RuntimeRunEvent[],
  cancel = vi.fn(async () => undefined)
): Response {
  const bytes = new TextEncoder().encode(
    events.map((item) => `data: ${JSON.stringify(item)}\n\n`).join('')
  );
  let readCount = 0;
  return {
    ok: true,
    status: 200,
    headers: new Headers(),
    body: {
      getReader: () => ({
        cancel,
        read: vi.fn(async () => {
          readCount += 1;
          return readCount === 1
            ? { done: false, value: bytes }
            : { done: true, value: undefined };
        }),
      }),
    },
  } as unknown as Response;
}

function errorResponse(
  status: number,
  error: Record<string, unknown>,
  headers: Record<string, string> = {}
): Response {
  return {
    ok: false,
    status,
    headers: new Headers(headers),
    json: vi.fn(async () => ({ error })),
  } as unknown as Response;
}

function requestHeaders(fetchMock: ReturnType<typeof vi.fn>, call: number) {
  return fetchMock.mock.calls[call][1]?.headers as Record<string, string>;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('SseFrameDecoder', () => {
  it('handles split CRLF frames, ids and heartbeat comments', () => {
    const decoder = new SseFrameDecoder();
    expect(decoder.push(': heartbeat\r\n\r\n')).toEqual([]);
    const payload = JSON.stringify(event(1));
    expect(
      decoder.push(`id: 1\r\nevent: run.completed\r\ndata: ${payload.slice(0, 20)}`)
    ).toEqual([]);
    expect(decoder.push(`${payload.slice(20)}\r\n\r`)).toEqual([]);
    expect(decoder.push('\n')).toEqual([event(1)]);
  });

  it('rejects malformed envelopes before they cross the reducer seam', () => {
    const decoder = new SseFrameDecoder();
    expect(() =>
      decoder.push('data: {"schema_version":1,"sequence":0}\n\n')
    ).toThrowError(expect.objectContaining({ code: 'STREAM_PROTOCOL_ERROR' }));
  });
});

describe('connectRunEventStream', () => {
  it('sends Bearer and Last-Event-ID, reports open, and stops on terminal', async () => {
    const cancel = vi.fn(async () => undefined);
    const fetchMock = vi.fn().mockResolvedValue(streamResponse([event(4)], cancel));
    vi.stubGlobal('fetch', fetchMock);
    const onOpen = vi.fn();
    const onEvent = vi.fn();

    const cursor = await connectRunEventStream({
      runId: 'run_1',
      threadId: 'thread-1',
      token: 'secret-token',
      after: 3,
      onOpen,
      onEvent,
    });

    expect(cursor).toBe(4);
    expect(requestHeaders(fetchMock, 0)).toEqual({
      Authorization: 'Bearer secret-token',
      'Last-Event-ID': '3',
    });
    expect(onOpen).toHaveBeenCalledWith(3);
    expect(onEvent).toHaveBeenCalledWith(event(4));
    expect(cancel).toHaveBeenCalledOnce();
  });

  it('rejects malformed or cross-Run events without reconnecting forever', async () => {
    const malformed = new TextEncoder().encode('data: {not-json}\n\n');
    const malformedResponse = {
      ok: true,
      status: 200,
      headers: new Headers(),
      body: {
        getReader: () => ({
          read: vi.fn().mockResolvedValueOnce({ done: false, value: malformed }),
        }),
      },
    } as unknown as Response;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(malformedResponse)
      .mockResolvedValueOnce(
        streamResponse([event(1, 'run.completed', { run_id: 'run_other' })])
      );
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      connectRunEventStream({
        runId: 'run_1',
        threadId: 'thread-1',
        token: 'token',
        onEvent: vi.fn(),
      })
    ).rejects.toMatchObject({ code: 'STREAM_PROTOCOL_ERROR', retryable: false });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await expect(
      connectRunEventStream({
        runId: 'run_1',
        threadId: 'thread-1',
        token: 'token',
        onEvent: vi.fn(),
      })
    ).rejects.toMatchObject({ code: 'STREAM_PROTOCOL_ERROR', retryable: false });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('does not advance the cursor on a gap and replays from the last contiguous event', async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(streamResponse([event(2, 'message.delta')]))
      .mockResolvedValueOnce(streamResponse([event(1)]));
    vi.stubGlobal('fetch', fetchMock);
    const onEvent = vi.fn();
    const onReconnect = vi.fn();

    const connected = connectRunEventStream({
      runId: 'run_1',
      threadId: 'thread-1',
      token: 'token',
      onEvent,
      onReconnect,
    });
    await vi.advanceTimersByTimeAsync(0);

    expect(onEvent).not.toHaveBeenCalled();
    expect(onReconnect).toHaveBeenCalledWith(
      1,
      0,
      expect.objectContaining({ code: 'STREAM_PROTOCOL_ERROR', retryable: true })
    );
    expect(requestHeaders(fetchMock, 0)['Last-Event-ID']).toBe('0');

    await vi.advanceTimersByTimeAsync(500);
    await connected;
    expect(requestHeaders(fetchMock, 1)['Last-Event-ID']).toBe('0');
    expect(onEvent).toHaveBeenCalledOnce();
  });

  it('reconnects a premature EOF from the last applied sequence and calls onOpen again', async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(streamResponse([event(1, 'run.started')]))
      .mockResolvedValueOnce(streamResponse([event(2)]));
    vi.stubGlobal('fetch', fetchMock);
    const onOpen = vi.fn();
    const onReconnect = vi.fn();

    const connected = connectRunEventStream({
      runId: 'run_1',
      threadId: 'thread-1',
      token: 'token',
      onEvent: vi.fn(),
      onOpen,
      onReconnect,
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(onReconnect).toHaveBeenCalledWith(
      1,
      1,
      expect.objectContaining({ code: 'NETWORK_UNAVAILABLE' })
    );

    await vi.advanceTimersByTimeAsync(500);
    await connected;
    expect(requestHeaders(fetchMock, 1)['Last-Event-ID']).toBe('1');
    expect(onOpen.mock.calls).toEqual([[0], [1]]);
  });

  it('treats hitl.required as a resumable connection pause', async () => {
    const cancel = vi.fn(async () => undefined);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(streamResponse([event(1, 'hitl.required')], cancel))
    );

    const cursor = await connectRunEventStream({
      runId: 'run_1',
      threadId: 'thread-1',
      token: 'token',
      onEvent: vi.fn(),
      pauseWhen: (item) => item.type === 'hitl.required',
    });

    expect(cursor).toBe(1);
    expect(cancel).toHaveBeenCalledOnce();
  });

  it('does not reconnect non-retryable HTTP failures', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      errorResponse(401, {
        code: 'AUTHENTICATION_REQUIRED',
        message: '登录已过期',
        retryable: false,
      })
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      connectRunEventStream({
        runId: 'run_1',
        threadId: 'thread-1',
        token: 'token',
        onEvent: vi.fn(),
        onReconnect: vi.fn(),
      })
    ).rejects.toMatchObject({
      code: 'AUTHENTICATION_REQUIRED',
      retryable: false,
    });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('honors Retry-After for retryable HTTP failures', async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        errorResponse(
          429,
          { code: 'MODEL_RATE_LIMITED', retryable: true, category: 'provider' },
          { 'Retry-After': '2' }
        )
      )
      .mockResolvedValueOnce(streamResponse([event(1)]));
    vi.stubGlobal('fetch', fetchMock);

    const connected = connectRunEventStream({
      runId: 'run_1',
      threadId: 'thread-1',
      token: 'token',
      onEvent: vi.fn(),
    });
    await vi.advanceTimersByTimeAsync(1999);
    expect(fetchMock).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    await connected;
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
