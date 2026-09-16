import { describe, expect, it } from 'vitest';
import { runActiveDurationMs } from './runDuration';

describe('runActiveDurationMs', () => {
  it('keeps malformed timestamps and clock rollback from creating negative or NaN durations', () => {
    const message = { text: '', isUser: false, runActiveDurationMs: 9000 };
    expect(runActiveDurationMs({ ...message, runActiveStartedAt: 'invalid' }, 0)).toBe(9000);
    expect(runActiveDurationMs({ ...message, runActiveStartedAt: '2026-09-16' }, 0)).toBe(9000);
    expect(runActiveDurationMs({ ...message, runActiveDurationMs: Number.NaN }, 0)).toBe(0);
    expect(runActiveDurationMs(null, 0)).toBe(0);
  });
});
