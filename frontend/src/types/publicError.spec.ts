import { describe, expect, it } from 'vitest';
import { normalizePublicErrorInfo, publicErrorMessage } from './publicError';

describe('web research public errors', () => {
  const messages = [
    ['WEB_SEARCH_UNAVAILABLE', '网络搜索请求失败，未能获取本次搜索结果'],
    ['WEB_FETCH_UNAVAILABLE', '网页内容提取失败，未能获取本次来源内容'],
    ['WEB_INVALID_SEARCH_RESPONSE', '网络搜索服务返回了无法解析的结果'],
    ['WEB_INVALID_EXTRACT_RESPONSE', '网页内容提取服务返回了无法解析的结果'],
    ['WEB_INVALID_CONTENT', '网页内容无效，无法作为回答依据'],
    ['WEB_DEADLINE_EXCEEDED', '本次网络检索已达到运行时限'],
    ['WEB_RESEARCH_DISABLED', '网络检索能力尚未启用'],
    ['WEB_RESEARCH_CLOSED', '网络检索服务已关闭'],
    ['WEB_RESEARCH_NOT_STARTED', '网络检索服务尚未启动'],
    ['WEB_RESEARCH_RUNTIME_NOT_CONFIGURED', '网络检索服务尚未配置'],
    ['WEB_INVALID_INPUT', '网络检索参数无效，请调整查询'],
    ['WEB_INPUT_TOO_LARGE', '网络检索输入过长，请缩小查询范围'],
    ['WEB_OUTPUT_TOO_LARGE', '网络检索结果超过大小限制'],
    ['WEB_INVALID_EVIDENCE', '网络检索证据无效，无法作为回答依据'],
    ['WEB_INVALID_SOURCE_ID', '网络来源编号格式无效'],
    ['WEB_SOURCE_NOT_FOUND', '该网络来源不属于本次运行或已失效'],
    ['WEB_SOURCE_UNKNOWN', '回答引用了本次运行中未知的网络来源'],
    ['WEB_SOURCE_CONTEXT_CLOSED', '本次运行的网络来源上下文已关闭'],
    ['WEB_SOURCE_LIMIT', '本次运行的网络来源数量已达到上限'],
    ['WEB_SOURCE_INVALID_CONTENT', '网络来源内容无效，无法作为回答依据'],
    ['WEB_FETCH_BUDGET_EXHAUSTED', '本次运行的网页提取额度已用完，请基于已有结果回答'],
  ];

  it.each(messages)(
    'maps %s without exposing server details or changing retryability',
    (code, message) => {
      expect(publicErrorMessage(code)).toBe(message);
      for (const retryable of [false, true]) {
        expect(
          normalizePublicErrorInfo({
            error_code: code,
            message: 'upstream secret response',
            retryable,
          })
        ).toMatchObject({ code, message, retryable });
      }
    }
  );

  it('replays a non-retryable search failure without calling it a temporary outage', () => {
    expect(
      normalizePublicErrorInfo({
        error_code: 'WEB_SEARCH_UNAVAILABLE',
        retryable: false,
        message: '服务暂时不可用，请稍后重试',
      })
    ).toMatchObject({
      code: 'WEB_SEARCH_UNAVAILABLE',
      message: '网络搜索请求失败，未能获取本次搜索结果',
      retryable: false,
    });
  });
});

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

  it('keeps model call budget failures specific', () => {
    expect(publicErrorMessage('MODEL_CALL_LIMIT_EXCEEDED')).toBe(
      '模型调用次数达到本次运行上限，请缩小问题范围后重试'
    );
  });

  it('describes automatic context trimming without reporting a service outage', () => {
    expect(
      normalizePublicErrorInfo({
        code: 'CONTEXT_TRIMMED',
        message: '服务暂时不可用，请稍后重试',
        retryable: false,
      })
    ).toMatchObject({
      code: 'CONTEXT_TRIMMED',
      message: '已自动整理较早上下文以继续运行',
      retryable: false,
    });
  });
});
