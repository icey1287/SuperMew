import type { RagGateResult } from '../types/evaluations';

export interface MetricTrendDomain {
  min: number;
  max: number;
}

export interface GateValueFact {
  key: 'actual' | 'threshold' | 'baseline' | 'baseline_threshold';
  label: string;
  value: number | null;
}

const finiteValues = (values: Array<number | null | undefined>) =>
  values.filter((value): value is number => typeof value === 'number' && Number.isFinite(value));

const usesUnitInterval = (metric: string) =>
  /(^|_)(accuracy|correctness|groundedness|relevance|completeness|recall|precision|mrr|ndcg|hit|coverage|rate)(_|$)/.test(
    metric
  );

const canBeNegative = (metric: string) => /delta|gain|change|regression/.test(metric);

export const metricTrendDomain = (
  metric: string,
  values: number[],
  references: Array<number | null | undefined> = []
): MetricTrendDomain => {
  if (usesUnitInterval(metric) && !canBeNegative(metric)) return { min: 0, max: 1 };

  const observations = finiteValues(values);
  if (!observations.length) return { min: 0, max: 1 };

  const observedMin = Math.min(...observations);
  const observedMax = Math.max(...observations);
  const observedSpan = observedMax - observedMin;
  const proximitySpan = observedSpan || Math.max(Math.abs(observedMax) * 0.1, 1);
  const nearbyReferences = finiteValues(references).filter(
    (value) => value >= observedMin - proximitySpan * 2 && value <= observedMax + proximitySpan * 2
  );
  const candidates = [...observations, ...nearbyReferences];
  const rawMin = Math.min(...candidates);
  const rawMax = Math.max(...candidates);
  const rawSpan = rawMax - rawMin;
  const padding = rawSpan ? rawSpan * 0.12 : Math.max(Math.abs(rawMax) * 0.1, 1);
  const minimum = rawMin - padding;

  return {
    min: canBeNegative(metric) ? minimum : Math.max(0, minimum),
    max: rawMax + padding,
  };
};

export const trendPosition = (value: number, domain: MetricTrendDomain) => {
  const span = domain.max - domain.min || 1;
  const ratio = Math.min(Math.max((value - domain.min) / span, 0), 1);
  return 42 - ratio * 34;
};

export const gateValueFacts = (gate: RagGateResult): GateValueFact[] => [
  { key: 'actual', label: '实际', value: gate.actual ?? null },
  { key: 'threshold', label: '绝对阈值', value: gate.threshold ?? null },
  { key: 'baseline', label: 'Baseline', value: gate.baseline ?? null },
  {
    key: 'baseline_threshold',
    label: '回归阈值',
    value: gate.baseline_threshold ?? null,
  },
];
