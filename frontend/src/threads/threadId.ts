import { getPublicError } from '@/utils/api';

export const THREAD_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$/;

export function isThreadId(value: unknown): value is string {
  return typeof value === 'string' && THREAD_ID_PATTERN.test(value);
}

export function requireThreadId(value: unknown): string {
  if (!isThreadId(value)) {
    throw getPublicError({
      code: 'INVALID_REQUEST',
      message: 'Thread ID 格式不正确',
      retryable: false,
      category: 'thread',
    });
  }
  return value;
}
