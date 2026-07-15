import { describe, expect, it } from 'vitest';
import { getPublicError } from './api';

describe('getPublicError', () => {
  it('preserves the backend public error contract', () => {
    const error = getPublicError({
      response: {
        status: 429,
        data: {
          error: {
            code: 'MODEL_RATE_LIMITED',
            message: '上游模型服务当前繁忙，请稍后重试',
            retryable: true,
            category: 'provider',
            stage: 'generation',
            retry_after: 2,
            request_id: 'req_123',
          },
        },
      },
    });

    expect(error).toMatchObject({
      code: 'MODEL_RATE_LIMITED',
      message: '上游模型服务当前繁忙，请稍后重试',
      retryable: true,
      category: 'provider',
      stage: 'generation',
      retryAfterSeconds: 2,
      requestId: 'req_123',
    });
  });

  it('does not expose an uncontracted upstream response body', () => {
    const error = getPublicError({
      response: {
        status: 500,
        data: {
          detail: 'secret provider response and API key',
        },
      },
      message: 'secret transport message',
    });

    expect(error.code).toBe('INTERNAL_ERROR');
    expect(error.retryable).toBe(true);
    expect(error.message).not.toContain('secret');
    expect(error.message).toBe('服务暂时不可用，请稍后重试');
  });

  it('uses the local stable message even if a provider envelope contains raw text', () => {
    const error = getPublicError({
      response: {
        status: 503,
        data: {
          error: {
            code: 'VECTOR_STORE_UNAVAILABLE',
            message: 'secret milvus host and credential',
            retryable: true,
            category: 'provider',
          },
        },
      },
    });

    expect(error.message).toBe('知识检索服务暂时不可用，请稍后重试');
    expect(error.message).not.toContain('secret');
  });

  it('normalizes transport timeouts without exposing the raw exception', () => {
    const error = getPublicError({
      code: 'ECONNABORTED',
      message: 'socket timeout while sending secret headers',
    });

    expect(error).toMatchObject({
      code: 'REQUEST_TIMEOUT',
      retryable: true,
      message: '请求超时，请稍后重试',
    });
  });

  it('uses Retry-After headers when the response body omits retry timing', () => {
    const error = getPublicError({
      response: {
        status: 429,
        headers: new Headers({ 'Retry-After': '3.5' }),
        data: {
          error: {
            code: 'MODEL_RATE_LIMITED',
            message: 'busy',
            retryable: true,
            category: 'provider',
          },
        },
      },
    });

    expect(error.retryAfterSeconds).toBe(3.5);
  });

  it('never retries earlier than either body or Retry-After header requests', () => {
    const error = getPublicError({
      response: {
        status: 429,
        headers: new Headers({ 'Retry-After': '5' }),
        data: {
          error: {
            code: 'MODEL_RATE_LIMITED',
            message: 'busy',
            retryable: true,
            category: 'provider',
            retry_after: 1,
          },
        },
      },
    });

    expect(error.retryAfterSeconds).toBe(5);
  });

  it('accepts a direct public error emitted by a stream event', () => {
    const error = getPublicError({
      code: 'RERANK_TIMEOUT',
      message: 'raw upstream timeout text',
      retryable: true,
      category: 'provider',
    });

    expect(error).toMatchObject({
      code: 'RERANK_TIMEOUT',
      message: '相关性排序服务响应超时',
      retryable: true,
    });
  });
});
