import type { RunEventV1 } from '@/types/generated/run-event-v1';
import {
  getPublicError,
  getPublicErrorFromResponse,
  type PublicRequestError,
} from '@/utils/api';

const TERMINAL_TYPES = new Set(['run.completed', 'run.failed', 'run.cancelled']);

export class SseFrameDecoder {
  private buffer = '';

  push(chunk: string): RunEventV1[] {
    this.buffer = (this.buffer + chunk)
      .replace(/\r\n/g, '\n')
      .replace(/\r(?!$)/g, '\n');
    const events: RunEventV1[] = [];
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

  private parseFrame(frame: string): RunEventV1 | null {
    if (!frame || frame.startsWith(':')) return null;
    const dataLines = frame
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart());
    if (!dataLines.length) return null;
    let payload: RunEventV1;
    try {
      payload = JSON.parse(dataLines.join('\n')) as RunEventV1;
    } catch {
      throw getPublicError({
        code: 'STREAM_PROTOCOL_ERROR',
        retryable: false,
        category: 'stream',
      });
    }
    if (payload.schema_version !== 1 || !Number.isInteger(payload.sequence)) {
      throw getPublicError({
        code: 'STREAM_PROTOCOL_ERROR',
        retryable: false,
        category: 'stream',
      });
    }
    return payload;
  }
}

export interface RunEventStreamOptions {
  runId: string;
  token: string;
  after?: number;
  signal?: AbortSignal;
  onEvent: (event: RunEventV1) => void;
  onReconnect?: (attempt: number) => void;
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

export async function connectRunEventStream(options: RunEventStreamOptions): Promise<void> {
  let lastSequence = Math.max(options.after || 0, 0);
  let reconnectAttempt = 0;

  while (!options.signal?.aborted) {
    let reconnectError: PublicRequestError | null = null;
    let callbackFailure: unknown;
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

      const reader = response.body.getReader();
      const textDecoder = new TextDecoder();
      const frameDecoder = new SseFrameDecoder();
      while (!options.signal?.aborted) {
        const { done, value } = await reader.read();
        if (done) break;
        const events = frameDecoder.push(textDecoder.decode(value, { stream: true }));
        for (const event of events) {
          if (event.sequence <= lastSequence) continue;
          lastSequence = event.sequence;
          try {
            options.onEvent(event);
          } catch (error) {
            callbackFailure = error;
            throw error;
          }
          if (TERMINAL_TYPES.has(event.type)) return;
        }
      }
      reconnectError = getPublicError(new TypeError('event stream closed before terminal event'));
      reconnectAttempt += 1;
    } catch (error: unknown) {
      if (callbackFailure === error) throw error;
      const publicError = getPublicError(error);
      if (options.signal?.aborted || publicError.code === 'REQUEST_CANCELLED') return;
      if (!publicError.retryable) throw publicError;
      reconnectError = publicError;
      reconnectAttempt += 1;
    }

    options.onReconnect?.(reconnectAttempt);
    await waitForReconnect(
      reconnectDelayMs(reconnectAttempt, reconnectError),
      options.signal
    );
  }
}
