import { afterEach, describe, expect, it, vi } from 'vitest';
import { connectRunEventStream, SseFrameDecoder } from './runEventStream';
import type { RunEventV1 } from '@/types/generated/run-event-v1';

function terminalEvent(sequence = 1): RunEventV1 {
  return {
    schema_version: 1,
    event_id: `evt_${sequence}`,
    sequence,
    run_id: 'run_1',
    thread_id: 'thread-1',
    type: 'run.completed',
    timestamp: '2026-07-15T00:00:00Z',
    data: { status: 'succeeded' },
  };
}

function terminalResponse(): Response {
  const bytes = new TextEncoder().encode(
    `data: ${JSON.stringify(terminalEvent())}\n\n`
  );
  let readCount = 0;
  return {
    ok: true,
    status: 200,
    headers: new Headers(),
    body: {
      getReader: () => ({
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

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('connectRunEventStream', () => {
  it('decodes a CRLF frame delimiter split across chunks', () => {
    const decoder = new SseFrameDecoder();
    const payload = JSON.stringify(terminalEvent());

    expect(decoder.push(`data: ${payload}\r`)).toEqual([]);
    expect(decoder.push('\n\r')).toEqual([]);
    expect(decoder.push('\n')).toEqual([terminalEvent()]);
  });

  it('rejects a malformed event frame without reconnecting forever', async () => {
    const bytes = new TextEncoder().encode('data: {not-json}\n\n');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      body: {
        getReader: () => ({
          read: vi
            .fn()
            .mockResolvedValueOnce({ done: false, value: bytes })
            .mockResolvedValueOnce({ done: true, value: undefined }),
        }),
      },
    } as unknown as Response);
    vi.stubGlobal('fetch', fetchMock);
    const onReconnect = vi.fn();

    await expect(
      connectRunEventStream({
        runId: 'run_1',
        token: 'token',
        onEvent: vi.fn(),
        onReconnect,
      })
    ).rejects.toMatchObject({
      code: 'STREAM_PROTOCOL_ERROR',
      retryable: false,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onReconnect).not.toHaveBeenCalled();
  });

  it('does not reconnect a non-retryable HTTP failure', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      errorResponse(401, {
        code: 'AUTHENTICATION_REQUIRED',
        message: '登录已过期',
        retryable: false,
      })
    );
    vi.stubGlobal('fetch', fetchMock);
    const onReconnect = vi.fn();

    await expect(
      connectRunEventStream({
        runId: 'run_1',
        token: 'token',
        onEvent: vi.fn(),
        onReconnect,
      })
    ).rejects.toMatchObject({
      code: 'AUTHENTICATION_REQUIRED',
      retryable: false,
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onReconnect).not.toHaveBeenCalled();
  });

  it('reconnects retryable failures and waits for Retry-After', async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        errorResponse(
          429,
          {
            code: 'MODEL_RATE_LIMITED',
            message: 'busy',
            retryable: true,
            category: 'provider',
          },
          { 'Retry-After': '2' }
        )
      )
      .mockResolvedValueOnce(terminalResponse());
    vi.stubGlobal('fetch', fetchMock);
    const onEvent = vi.fn();
    const onReconnect = vi.fn();

    const connected = connectRunEventStream({
      runId: 'run_1',
      token: 'token',
      onEvent,
      onReconnect,
    });
    await vi.advanceTimersByTimeAsync(0);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onReconnect).toHaveBeenCalledWith(1);
    await vi.advanceTimersByTimeAsync(1999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await connected;

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'run.completed' })
    );
  });

  it('reconnects a network transport failure with exponential backoff', async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('secret socket detail'))
      .mockResolvedValueOnce(terminalResponse());
    vi.stubGlobal('fetch', fetchMock);
    const onReconnect = vi.fn();

    const connected = connectRunEventStream({
      runId: 'run_1',
      token: 'token',
      onEvent: vi.fn(),
      onReconnect,
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(500);
    await connected;

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onReconnect).toHaveBeenCalledWith(1);
  });

  it('honors a non-retryable public error even on a 5xx response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      errorResponse(503, {
        code: 'PROVIDER_AUTHENTICATION_FAILED',
        message: 'provider config unavailable',
        retryable: false,
        category: 'provider',
      })
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(
      connectRunEventStream({
        runId: 'run_1',
        token: 'token',
        onEvent: vi.fn(),
      })
    ).rejects.toMatchObject({
      code: 'PROVIDER_AUTHENTICATION_FAILED',
      retryable: false,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
