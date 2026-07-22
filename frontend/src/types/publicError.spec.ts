import { describe, expect, it } from 'vitest';
import { normalizePublicErrorInfo, publicErrorMessage } from './publicError';

describe('web context budget public error', () => {
  it('keeps the dedicated code and shows a specific message', () => {
    const code = 'WEB_TOOL_RESULT_CONTEXT_BUDGET_EXCEEDED';

    expect(publicErrorMessage(code)).toBe('搜索结果超过上下文预算，请缩小搜索范围后重试');
    expect(
      normalizePublicErrorInfo({
        error: {
          code,
          message: 'server fallback',
          retryable: false,
          category: 'web_research',
        },
      })
    ).toMatchObject({
      code,
      message: '搜索结果超过上下文预算，请缩小搜索范围后重试',
      retryable: false,
    });
  });
});
