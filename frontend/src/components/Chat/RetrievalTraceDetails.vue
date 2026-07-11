<template>
  <div v-if="msg.ragTrace" class="message-meta">
    <details class="reasoning-details">
      <summary>检索过程</summary>
      <div class="reasoning-content">
        <div class="trace-line">
          工具：{{ msg.ragTrace.tool_used ? msg.ragTrace.tool_name : '未使用' }}
        </div>
        <div v-if="msg.ragTrace.retrieval_stage" class="trace-line">
          检索阶段：{{ msg.ragTrace.retrieval_stage }}
        </div>
        <div v-if="msg.ragTrace.retrieval_status" class="trace-line">
          检索状态：{{ formatRetrievalStatus(msg.ragTrace.retrieval_status) }}
        </div>
        <div v-if="msg.ragTrace.hitl_resumed" class="trace-line">
          HITL续跑：是
          <span v-if="msg.ragTrace.hitl_answer">（补充：{{ msg.ragTrace.hitl_answer }}）</span>
        </div>
        <div v-if="msg.ragTrace.hitl_resume_strategy" class="trace-line">
          HITL续跑策略：{{ formatHitlResumeStrategy(msg.ragTrace.hitl_resume_strategy) }}
        </div>
        <div v-if="msg.ragTrace.evidence_relevance || msg.ragTrace.evidence_answerability" class="trace-line">
          证据评分：
          相关性 {{ msg.ragTrace.evidence_relevance || '—' }} /
          可回答性 {{ msg.ragTrace.evidence_answerability || '—' }}
          <span v-if="msg.ragTrace.evidence_confidence !== null && msg.ragTrace.evidence_confidence !== undefined">
            / 置信度 {{ Number(msg.ragTrace.evidence_confidence).toFixed(2) }}
          </span>
        </div>
        <div v-if="msg.ragTrace.evidence_ambiguity && msg.ragTrace.evidence_ambiguity !== 'none'" class="trace-line">
          歧义类型：{{ msg.ragTrace.evidence_ambiguity }}
        </div>
        <div v-if="msg.ragTrace.hitl_prompt" class="trace-line">
          HITL提示：{{ msg.ragTrace.hitl_prompt }}
        </div>
        <div v-if="msg.ragTrace.hitl_options && msg.ragTrace.hitl_options.length" class="trace-line">
          HITL选项：{{ msg.ragTrace.hitl_options.join(' / ') }}
        </div>
        <div v-if="msg.ragTrace.route" class="trace-line">
          评分决策：{{ msg.ragTrace.route }}
        </div>
        <div v-if="msg.ragTrace.retrieval_pipeline" class="trace-line">
          检索流水线：{{ msg.ragTrace.retrieval_pipeline }}
        </div>
        <div v-if="msg.ragTrace.retrieval_mode" class="trace-line">
          检索模式：{{ msg.ragTrace.retrieval_mode }}
        </div>
        <div v-if="msg.ragTrace.candidate_k !== null && msg.ragTrace.candidate_k !== undefined" class="trace-line">
          {{ formatCandidateKLabel(msg.ragTrace) }}
        </div>
        <div v-if="hasRetrievalFunnel(msg.ragTrace)" class="trace-line trace-funnel">
          检索漏斗：Milvus 召回 {{ msg.ragTrace.recall_count ?? '—' }}
          → 合并后 {{ msg.ragTrace.post_merge_candidate_count ?? '—' }}
          → 精排输入 {{ msg.ragTrace.candidate_count ?? '—' }}
          → 输出 top{{ msg.ragTrace.retrieval_top_k ?? (msg.ragTrace.retrieved_chunks || []).length }}
          （{{ (msg.ragTrace.retrieved_chunks || []).length }} 条）
        </div>
        <div v-if="msg.ragTrace.leaf_retrieve_level" class="trace-line">
          叶子召回层级：L{{ msg.ragTrace.leaf_retrieve_level }}
        </div>
        <div v-if="msg.ragTrace.auto_merge_enabled !== null && msg.ragTrace.auto_merge_enabled !== undefined" class="trace-line">
          Auto-merging启用：{{ msg.ragTrace.auto_merge_enabled ? '是' : '否' }}
        </div>
        <div v-if="msg.ragTrace.auto_merge_applied !== null && msg.ragTrace.auto_merge_applied !== undefined" class="trace-line">
          Auto-merging应用：{{ msg.ragTrace.auto_merge_applied ? '是' : '否' }}
        </div>
        <div v-if="msg.ragTrace.auto_merge_threshold" class="trace-line">
          合并阈值：{{ msg.ragTrace.auto_merge_threshold }}
        </div>
        <div v-if="msg.ragTrace.auto_merge_replaced_chunks" class="trace-line">
          合并替换片段：{{ msg.ragTrace.auto_merge_replaced_chunks }}
        </div>
        <div v-if="msg.ragTrace.auto_merge_steps" class="trace-line">
          合并轮次：{{ msg.ragTrace.auto_merge_steps }}
        </div>
        <div v-if="msg.ragTrace.rerank_enabled !== null && msg.ragTrace.rerank_enabled !== undefined" class="trace-line">
          Rerank已配置：{{ msg.ragTrace.rerank_enabled ? '是' : '否' }}
        </div>
        <div v-if="msg.ragTrace.rerank_applied !== null && msg.ragTrace.rerank_applied !== undefined" class="trace-line">
          Rerank已执行：{{ msg.ragTrace.rerank_applied ? '是' : '否' }}
        </div>
        <div v-if="msg.ragTrace.rerank_model" class="trace-line">
          Rerank模型：{{ msg.ragTrace.rerank_model }}
        </div>
        <div v-if="msg.ragTrace.rerank_error" class="trace-line">
          Rerank状态：{{ msg.ragTrace.rerank_error }}
        </div>
        <div v-if="msg.ragTrace.rewrite_method" class="trace-line">
          查询重写方式：{{ formatRewriteMethod(msg.ragTrace.rewrite_method) }}
        </div>
        <div v-if="msg.ragTrace.step_back_question" class="trace-line">
          退步问题：{{ msg.ragTrace.step_back_question }}
        </div>
        <div v-if="msg.ragTrace.hyde_document" class="trace-line">
          HyDE 假设文档：{{ msg.ragTrace.hyde_document }}
        </div>
        <div v-if="msg.ragTrace.rewritten_query" class="trace-line">
          重写检索查询：{{ msg.ragTrace.rewritten_query }}
        </div>
        
        <!-- 复杂度路由信息 -->
        <div v-if="msg.ragTrace.complexity" class="trace-line trace-complexity">
          问题复杂度：
          <span :class="msg.ragTrace.complexity === 'complex' ? 'tag-complex' : 'tag-simple'">
            {{ msg.ragTrace.complexity === 'complex' ? '复杂' : '简单' }}
          </span>
          <span v-if="msg.ragTrace.complexity_reason" class="trace-detail">（{{ msg.ragTrace.complexity_reason }}）</span>
        </div>
        
        <div v-if="msg.ragTrace.sub_questions && msg.ragTrace.sub_questions.length" class="trace-line">
          <div class="trace-sub-questions">
            <div class="trace-sub-qs-header">子问题分解（{{ msg.ragTrace.sub_questions.length }} 个）：</div>
            <ul class="trace-sub-qs-list">
              <li v-for="(sq, sqIdx) in msg.ragTrace.sub_questions" :key="sqIdx" class="trace-sub-q-item">
                <span class="trace-sub-q-index">{{ sqIdx + 1 }}.</span> {{ sq }}
              </li>
            </ul>
          </div>
        </div>
        <div v-if="msg.ragTrace.sub_agent_count" class="trace-line">
          并行子 Agent 数量：{{ msg.ragTrace.sub_agent_count }}
        </div>
        <div v-if="msg.ragTrace.synthesis_merged_count" class="trace-line">
          合成合并文档数：{{ msg.ragTrace.synthesis_merged_count }}
        </div>
        
        <!-- 子 Agent 检索详情（可折叠） -->
        <div v-if="msg.ragTrace.sub_traces && msg.ragTrace.sub_traces.length" class="trace-sub-traces">
          <details class="sub-traces-details">
            <summary class="sub-traces-title">
              <i class="fas fa-layer-group"></i> 子 Agent 检索详情（{{ msg.ragTrace.sub_traces.length }} 个）
            </summary>
            <div v-for="(st, stIdx) in msg.ragTrace.sub_traces" :key="stIdx" class="sub-trace-block">
              <div class="sub-trace-header">
                子问题 {{ stIdx + 1 }}：{{ msg.ragTrace.sub_questions?.[stIdx] || st.query || '—' }}
              </div>
              <div v-if="st.retrieval_stage" class="trace-line">检索阶段：{{ st.retrieval_stage }}</div>
              <div v-if="st.retrieval_mode" class="trace-line">检索模式：{{ st.retrieval_mode }}</div>
              <div v-if="st.route" class="trace-line">评分决策：{{ st.route }}</div>
              <div v-if="st.retrieved_chunks && st.retrieved_chunks.length" class="sub-trace-chunks">
                检索到 {{ st.retrieved_chunks.length }} 个片段
              </div>
            </div>
          </details>
        </div>
        
        <div v-if="msg.ragTrace.initial_retrieved_chunks && msg.ragTrace.initial_retrieved_chunks.length" class="sources">
          <div class="sources-title">初次检索结果</div>
          <ul class="sources-list">
            <li v-for="(chunk, sIndex) in msg.ragTrace.initial_retrieved_chunks" :key="sIndex" class="source-item">
              <div class="source-title-line">
                <span class="source-file">{{ chunk.filename }}</span>
                <span v-if="chunk.page_number" class="source-page">（第 {{ chunk.page_number }} 页）</span>
              </div>
              <div class="source-meta-line">
                <span class="source-page">RRF名次：#{{ chunk.rrf_rank || (sIndex + 1) }}</span>
                <span v-if="chunk.rerank_score !== null && chunk.rerank_score !== undefined" class="source-page">
                  Rerank分数：{{ Number(chunk.rerank_score).toFixed(4) }}
                </span>
              </div>
              <div v-if="chunk.text" class="source-excerpt">{{ chunk.text }}</div>
            </li>
          </ul>
        </div>
        
        <div v-if="msg.ragTrace.rewrite_retrieved_chunks && msg.ragTrace.rewrite_retrieved_chunks.length" class="sources">
          <div class="sources-title">重写后检索结果</div>
          <ul class="sources-list">
            <li v-for="(chunk, sIndex) in msg.ragTrace.rewrite_retrieved_chunks" :key="sIndex" class="source-item">
              <div class="source-title-line">
                <span class="source-file">{{ chunk.filename }}</span>
                <span v-if="chunk.page_number" class="source-page">（第 {{ chunk.page_number }} 页）</span>
              </div>
              <div class="source-meta-line">
                <span class="source-page">RRF名次：#{{ chunk.rrf_rank || (sIndex + 1) }}</span>
                <span v-if="chunk.rerank_score !== null && chunk.rerank_score !== undefined" class="source-page">
                  Rerank分数：{{ Number(chunk.rerank_score).toFixed(4) }}
                </span>
              </div>
              <div v-if="chunk.text" class="source-excerpt">{{ chunk.text }}</div>
            </li>
          </ul>
        </div>
      </div>
    </details>
  </div>
</template>

<script setup lang="ts">
import type { Message, RagTrace } from '@/types/chat';

defineProps<{
  msg: Message;
}>();

const formatCandidateKLabel = (trace: RagTrace) => {
  if (!trace || trace.candidate_k === null || trace.candidate_k === undefined) {
    return '';
  }
  const k = trace.candidate_k;
  if (trace.candidate_k_config_error) {
    return `Milvus 候选池：${k}（${trace.candidate_k_config_error}，已回退倍数计算）`;
  }
  if (trace.candidate_k_source === 'env') {
    return `Milvus 候选池：${k}（环境变量 RETRIEVAL_CANDIDATE_K）`;
  }
  const multiplier = trace.retrieval_candidate_multiplier;
  if (multiplier !== null && multiplier !== undefined) {
    return `Milvus 候选池：${k}（top_k × ${multiplier}）`;
  }
  return `Milvus 候选池：${k}`;
};

const hasRetrievalFunnel = (trace: RagTrace) => {
  if (!trace) {
    return false;
  }
  return trace.recall_count !== null && trace.recall_count !== undefined
    || trace.post_merge_candidate_count !== null && trace.post_merge_candidate_count !== undefined
    || trace.candidate_count !== null && trace.candidate_count !== undefined;
};

const formatRetrievalStatus = (status: string) => {
  const labels: Record<string, string> = {
    answerable: '可回答',
    partial: '部分证据',
    needs_rewrite: '需要改写',
    needs_clarification: '需要补充条件',
    needs_scope_selection: '需要选择方向',
    no_knowledge: '无可用知识',
  };
  return labels[status] || status;
};

const formatHitlResumeStrategy = (strategy: string) => {
  const labels: Record<string, string> = {
    targeted_retrieval: '基于用户补充的针对性检索',
  };
  return labels[strategy] || strategy;
};

const formatRewriteMethod = (method: string) => {
  const labels: Record<string, string> = {
    step_back: 'Step-back',
    hyde: 'HyDE',
  };
  return labels[method] || method;
};
</script>
