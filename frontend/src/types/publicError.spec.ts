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

  it('keeps tool guardrail denials specific instead of falling back to an internal error', () => {
    expect(publicErrorMessage('TOOL_GUARDRAIL_DENIED')).toBe('当前工具调用未通过安全策略');
    expect(
      normalizePublicErrorInfo({
        error_code: 'TOOL_GUARDRAIL_DENIED',
        message: '服务暂时不可用，请稍后重试',
      })
    ).toMatchObject({
      code: 'TOOL_GUARDRAIL_DENIED',
      message: '当前工具调用未通过安全策略',
      retryable: false,
    });
  });
});
