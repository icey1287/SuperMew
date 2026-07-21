import { describe, expect, it } from 'vitest';

import { gateValueFacts, metricTrendDomain, trendPosition } from './reportPresentation';

describe('evaluation report presentation', () => {
  it('keeps ratio metrics on a stable zero-to-one domain', () => {
    expect(metricTrendDomain('hit_at_10', [0.72, 0.81, 0.78])).toEqual({ min: 0, max: 1 });
  });

  it('keeps negative rewrite deltas visible', () => {
    const domain = metricTrendDomain('rewrite_recall_delta_at_10', [-0.2, 0.1]);

    expect(domain.min).toBeLessThan(-0.2);
    expect(domain.max).toBeGreaterThan(0.1);
  });

  it('uses a local padded domain for latency instead of forcing zero-to-one', () => {
    const domain = metricTrendDomain(
      'latency_p95_ms',
      [31_000, 35_000, 33_500],
      [1, 32_000, 33_000]
    );

    expect(domain.min).toBeGreaterThan(30_000);
    expect(domain.max).toBeGreaterThan(35_000);
    expect(domain.max - domain.min).toBeLessThan(20_000);
    expect(trendPosition(33_000, domain)).toBeGreaterThan(8);
    expect(trendPosition(33_000, domain)).toBeLessThan(42);
  });

  it('shows actual, absolute threshold, baseline, and regression threshold separately', () => {
    expect(
      gateValueFacts({
        name: 'metric:latency_p95_ms',
        metric: 'latency_p95_ms',
        status: 'failed',
        actual: 35_890,
        threshold: 1,
        baseline: 32_000,
        baseline_threshold: 33_000,
        detail: 'above maximum 1',
      })
    ).toEqual([
      { key: 'actual', label: '实际', value: 35_890 },
      { key: 'threshold', label: '绝对阈值', value: 1 },
      { key: 'baseline', label: 'Baseline', value: 32_000 },
      { key: 'baseline_threshold', label: '回归阈值', value: 33_000 },
    ]);
  });
});
