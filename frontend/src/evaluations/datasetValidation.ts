import type { RagEvaluationDataset } from '../types/evaluations';

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value);

export const normalizeRagEvaluationDataset = (value: unknown): RagEvaluationDataset => {
  if (!isRecord(value)) throw new Error('Dataset JSON 必须是对象');
  const raw = isRecord(value.dataset) ? value.dataset : value;
  const schemaVersion = raw.schema_version;
  const name = raw.name;
  const cases = raw.cases;

  if (schemaVersion !== 1) throw new Error('schema_version 必须为 1');
  if (typeof name !== 'string' || !name.trim()) throw new Error('Dataset name 不能为空');
  if (!Array.isArray(cases) || !cases.length) throw new Error('Dataset 至少需要一个 Case');

  const positionsById = new Map<string, number[]>();
  cases.forEach((item, index) => {
    if (!isRecord(item)) throw new Error(`Case ${index + 1} 必须是对象`);
    const id = item.id;
    if (typeof id !== 'string' || !id.trim()) throw new Error(`Case ${index + 1} 缺少 id`);
    const normalizedId = id.trim();
    const positions = positionsById.get(normalizedId) || [];
    positions.push(index + 1);
    positionsById.set(normalizedId, positions);
    if (typeof item.question !== 'string' || !item.question.trim()) {
      throw new Error(`Case ${normalizedId} 缺少 question`);
    }
    if (!isRecord(item.expected)) throw new Error(`Case ${normalizedId} 缺少 expected`);
  });

  const duplicate = [...positionsById].find(([, positions]) => positions.length > 1);
  if (duplicate) {
    const [id, positions] = duplicate;
    throw new Error(`Case ID “${id}” 重复（第 ${positions.join('、')} 项）`);
  }

  return raw as unknown as RagEvaluationDataset;
};
