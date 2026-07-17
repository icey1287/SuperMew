import type { RuntimeRunEvent } from '@/events/runEventReducer';
import { isThreadId, requireThreadId } from '@/threads/threadId';
import { getPublicError, getPublicErrorFromResponse, type PublicRequestError } from '@/utils/api';

type UnknownRecord = Record<string, unknown>;

const TERMINAL_TYPES = new Set(['run.completed', 'run.failed', 'run.cancelled']);

function asRecord(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function protocolError(retryable = false): PublicRequestError {
  return getPublicError({
    code: 'STREAM_PROTOCOL_ERROR',
    retryable,
    category: 'stream',
  });
}

function parseEventPayload(value: unknown): RuntimeRunEvent {
  const payload = asRecord(value);
  const data = asRecord(payload?.data);
  if (
    payload?.schema_version !== 1 ||
    !Number.isInteger(payload.sequence) ||
    Number(payload.sequence) <= 0 ||
    typeof payload.event_id !== 'string' ||
    !payload.event_id ||
    typeof payload.run_id !== 'string' ||
    !payload.run_id ||
    !isThreadId(payload.thread_id) ||
    typeof payload.type !== 'string' ||
    !payload.type ||
    typeof payload.timestamp !== 'string' ||
    !data
  ) {
    throw protocolError();
  }
  return payload as unknown as RuntimeRunEvent;
}

export class SseFrameDecoder {
  private buffer = '';

  push(chunk: string): RuntimeRunEvent[] {
    this.buffer = (this.buffer + chunk).replace(/\r\n/g, '\n').replace(/\r(?!$)/g, '\n');
    const events: RuntimeRunEvent[] = [];
    let end = this.buffer.indexOf('\n\n');
    while (end !== -1) {
      const frame = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      const event = this.parseFrame(frame);
      if (event) events.push(event);
      end = this.buffer.indexOf('\n\n');
    }
    return events;
  }

  private parseFrame(frame: string): RuntimeRunEvent | null {
    if (!frame) return null;
    const dataLines = frame
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart());
    if (!dataLines.length) return null;
    try {
      return parseEventPayload(JSON.parse(dataLines.join('\n')));
    } catch (error) {
      if (error instanceof SyntaxError) throw protocolError();
      throw error;
    }
  }
}

export interface RunEventStreamOptions {
  runId: string;
  threadId: string;
  token: string;
  after?: number;
  signal?: AbortSignal;
  onEvent: (event: RuntimeRunEvent) => void;
  onOpen?: (lastSequence: number) => void;
  onReconnect?: (attempt: number, lastSequence: number, error: PublicRequestError) => void;
  onCursor?: (lastSequence: number) => void;
  pauseWhen?: (event: RuntimeRunEvent) => boolean;
}

function reconnectDelayMs(attempt: number, error: PublicRequestError): number {
  const exponential = Math.min(5000, 250 * 2 ** Math.min(attempt, 4));
  const retryAfter = Math.max((error.retryAfterSeconds || 0) * 1000, 0);
  return Math.max(exponential, retryAfter);
}

async function waitForReconnect(delayMs: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return;
  await new Promise<void>((resolve) => {
    const timer = setTimeout(finish, delayMs);
    function finish() {
      clearTimeout(timer);
      signal?.removeEventListener('abort', finish);
      resolve();
    }
    signal?.addEventListener('abort', finish, { once: true });
  });
}

async function cancelReader(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<void> {
  if (typeof reader.cancel !== 'function') return;
  try {
    await reader.cancel();
  } catch {
    // The authoritative Run continues; closing this reader is best effort only.
  }
}

export async function connectRunEventStream(options: RunEventStreamOptions): Promise<number> {
  requireThreadId(options.threadId);
  let lastSequence = Math.max(options.after || 0, 0);
  let reconnectAttempt = 0;

  while (!options.signal?.aborted) {
    let reconnectError: PublicRequestError;
    let callbackFailure: unknown;
    let activeReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    try {
      const response = await fetch(`/v1/runs/${encodeURIComponent(options.runId)}/stream`, {
        headers: {
          Authorization: `Bearer ${options.token}`,
          'Last-Event-ID': String(lastSequence),
        },
        signal: options.signal,
      });
      if (!response.ok) {
        throw await getPublicErrorFromResponse(response);
      }
      if (!response.body) {
        throw getPublicError(new TypeError('event stream response has no body'));
      }

      reconnectAttempt = 0;
      options.onOpen?.(lastSequence);
      const reader = response.body.getReader();
      activeReader = reader;
      const textDecoder = new TextDecoder();
      const frameDecoder = new SseFrameDecoder();
      while (!options.signal?.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        const events = frameDecoder.push(textDecoder.decode(value, { stream: true }));
        for (const event of events) {
          if (event.run_id !== options.runId || event.thread_id !== options.threadId) {
            throw protocolError();
          }
          if (event.sequence <= lastSequence) continue;
          if (event.sequence !== lastSequence + 1) {
            throw protocolError(true);
          }
          try {
            options.onEvent(event);
          } catch (error) {
            callbackFailure = error;
            throw error;
          }
          lastSequence = event.sequence;
          options.onCursor?.(lastSequence);
          if (TERMINAL_TYPES.has(event.type) || options.pauseWhen?.(event)) {
            await cancelReader(reader);
            return lastSequence;
          }
        }
      }
      reconnectError = getPublicError(
        new TypeError('event stream closed before a terminal or pause event')
      );
      reconnectAttempt += 1;
    } catch (error: unknown) {
      if (activeReader) await cancelReader(activeReader);
      if (callbackFailure === error) throw error;
      const publicError = getPublicError(error);
      if (options.signal?.aborted || publicError.code === 'REQUEST_CANCELLED') {
        return lastSequence;
      }
      if (!publicError.retryable) throw publicError;
      reconnectError = publicError;
      reconnectAttempt += 1;
    }

    options.onReconnect?.(reconnectAttempt, lastSequence, reconnectError);
    await waitForReconnect(reconnectDelayMs(reconnectAttempt, reconnectError), options.signal);
  }
  return lastSequence;
}
