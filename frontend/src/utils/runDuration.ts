import type { Message } from '@/types/chat';

// Completed running intervals plus the current interval; HITL waiting is excluded.
export function runActiveDurationMs(message: Message | null, now: number): number {
  const recorded = message?.runActiveDurationMs;
  const duration = typeof recorded === 'number' && Number.isFinite(recorded) ? recorded : 0;
  const startedAt = message?.runActiveStartedAt;
  const started = startedAt ? Date.parse(startedAt) : Number.NaN;
  const active = Number.isFinite(started) ? Math.max(now - started, 0) : 0;
  return Math.max(duration, 0) + active;
}
