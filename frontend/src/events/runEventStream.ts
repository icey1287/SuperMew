import type { RunEventV1 } from '@/types/generated/run-event-v1';

const TERMINAL_TYPES = new Set(['run.completed', 'run.failed', 'run.cancelled']);

export class SseFrameDecoder {
  private buffer = '';

  push(chunk: string): RunEventV1[] {
    this.buffer += chunk.replace(/\r\n/g, '\n');
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
    const payload = JSON.parse(dataLines.join('\n')) as RunEventV1;
    if (payload.schema_version !== 1 || !Number.isInteger(payload.sequence)) return null;
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

export async function connectRunEventStream(options: RunEventStreamOptions): Promise<void> {
  let lastSequence = Math.max(options.after || 0, 0);
  let reconnectAttempt = 0;

  while (!options.signal?.aborted) {
    try {
      const response = await fetch(`/v1/runs/${encodeURIComponent(options.runId)}/stream`, {
        headers: {
          Authorization: `Bearer ${options.token}`,
          'Last-Event-ID': String(lastSequence),
        },
        signal: options.signal,
      });
      if (!response.ok || !response.body) {
        throw new Error(`event stream HTTP ${response.status}`);
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
          options.onEvent(event);
          if (TERMINAL_TYPES.has(event.type)) return;
        }
      }
      reconnectAttempt += 1;
    } catch (error: any) {
      if (options.signal?.aborted || error?.name === 'AbortError') return;
      reconnectAttempt += 1;
    }

    options.onReconnect?.(reconnectAttempt);
    const delay = Math.min(5000, 250 * 2 ** Math.min(reconnectAttempt, 4));
    await new Promise<void>((resolve) => setTimeout(resolve, delay));
  }
}
