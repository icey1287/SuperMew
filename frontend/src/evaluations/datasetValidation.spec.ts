import { describe, expect, it } from 'vitest';

import { normalizeRagEvaluationDataset } from './datasetValidation';

const dataset = {
  schema_version: 1 as const,
  name: 'rag_eval',
  cases: [
    {
      id: 'case-1',
      question: '问题一',
      expected: {},
    },
  ],
};

describe('normalizeRagEvaluationDataset', () => {
  it('accepts an exported dataset wrapper', () => {
    expect(normalizeRagEvaluationDataset({ dataset })).toEqual(dataset);
  });

  it('reports duplicate case ids with the normalized id and positions', () => {
    expect(() =>
      normalizeRagEvaluationDataset({
        ...dataset,
        cases: [dataset.cases[0], { ...dataset.cases[0], id: ' case-1 ', question: '问题二' }],
      })
    ).toThrow('Case ID “case-1” 重复（第 1、2 项）');
  });
});
