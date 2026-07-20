<template>
  <div class="evaluation-page">
    <header class="workspace-header">
      <div>
        <span class="panel-eyebrow">RAG observability & evaluation</span>
        <h1>RAG 评估</h1>
        <p>
          像 LangSmith 一样管理 Dataset、运行全自动 RAG Evaluation Job，并持续观察质量门禁与 Case
          级证据。
        </p>
      </div>
      <div class="workspace-actions">
        <button
          type="button"
          class="secondary-button"
          :disabled="evaluationStore.loading"
          @click="refreshWorkspace"
        >
          <i class="fa-solid fa-rotate" :class="{ 'fa-spin': evaluationStore.loading }"></i>
          刷新
        </button>
        <button type="button" class="primary-button" @click="openDatasetImport">
          <i class="fa-solid fa-file-arrow-up"></i>
          导入 Dataset
        </button>
      </div>
    </header>

    <div v-if="evaluationStore.error" class="workspace-alert is-error" role="alert">
      <i class="fa-solid fa-triangle-exclamation"></i>
      <span>{{ evaluationStore.error }}</span>
      <button type="button" @click="refreshWorkspace">重试</button>
    </div>
    <div v-else-if="evaluationStore.notice" class="workspace-alert is-success" role="status">
      <i class="fa-solid fa-circle-check"></i>
      <span>{{ evaluationStore.notice }}</span>
      <button type="button" aria-label="关闭提示" @click="evaluationStore.notice = ''">
        <i class="fa-solid fa-xmark"></i>
      </button>
    </div>

    <section class="overview-grid" aria-label="RAG 评估概览">
      <article>
        <span class="overview-icon"><i class="fa-solid fa-database"></i></span>
        <div>
          <small>Datasets</small>
          <strong>{{ evaluationStore.datasets.length }}</strong>
          <p>{{ totalDatasetCases }} 个标注 Case</p>
        </div>
      </article>
      <article :class="{ 'is-running': evaluationStore.activeJobs.length }">
        <span class="overview-icon"><i class="fa-solid fa-gears"></i></span>
        <div>
          <small>Active Jobs</small>
          <strong>{{ evaluationStore.activeJobs.length }}</strong>
          <p>
            {{ evaluationStore.activeJobs.length ? 'Worker 正在自动评估' : '当前没有运行任务' }}
          </p>
        </div>
      </article>
      <article>
        <span class="overview-icon"><i class="fa-solid fa-shield-check"></i></span>
        <div>
          <small>Gate Pass Rate</small>
          <strong>{{ gatePassRate }}</strong>
          <p>最近 {{ evaluationStore.completedJobs.length }} 次成功运行</p>
        </div>
      </article>
      <article :class="{ 'is-warning': !modelStore.readyForEvaluation }">
        <span class="overview-icon"><i class="fa-solid fa-microchip"></i></span>
        <div>
          <small>Evaluation Runtime</small>
          <strong>{{ modelStore.readyForEvaluation ? '已就绪' : '需要配置' }}</strong>
          <p>{{ runtimeReadinessText }}</p>
        </div>
      </article>
    </section>

    <section class="launch-panel">
      <div class="launch-copy">
        <span class="section-kicker">New experiment</span>
        <h2>启动自动评估</h2>
        <p>
          Job 创建时冻结 Dataset fingerprint、GatePolicy、baseline
          与四个模型角色，后续控制面变更不会污染结果。
        </p>
      </div>

      <div class="launch-form">
        <label>
          <span>Evaluation Dataset</span>
          <select v-model="selectedDatasetId" aria-label="Evaluation Dataset">
            <option value="" disabled>选择 Dataset</option>
            <option
              v-for="dataset in evaluationStore.datasets"
              :key="dataset.id"
              :value="dataset.id"
            >
              {{ dataset.name }} · {{ dataset.case_count }} cases
            </option>
          </select>
        </label>
        <label>
          <span>Baseline（可选）</span>
          <select
            v-model="selectedBaselineJobId"
            :disabled="!selectedDatasetId"
            aria-label="Baseline Job"
          >
            <option value="">不比较 baseline</option>
            <option v-for="job in compatibleBaselineJobs" :key="job.id" :value="job.id">
              {{ shortJobId(job.id) }} · {{ formatDate(job.finished_at || job.created_at) }}
            </option>
          </select>
        </label>
        <button
          type="button"
          class="run-button"
          :disabled="!canStartEvaluation"
          :title="startButtonTitle"
          @click="startEvaluation"
        >
          <i v-if="evaluationStore.saving" class="fa-solid fa-spinner fa-spin"></i>
          <i v-else class="fa-solid fa-play"></i>
          启动 Evaluation Job
        </button>
      </div>

      <div class="runtime-checks">
        <div class="runtime-check-head">
          <span>启动前检查</span>
          <strong :class="modelStore.readyForEvaluation ? 'is-ready' : 'is-blocked'">
            <i
              :class="
                modelStore.readyForEvaluation
                  ? 'fa-solid fa-circle-check'
                  : 'fa-solid fa-circle-exclamation'
              "
            ></i>
            {{ modelStore.readyForEvaluation ? '全部通过' : '存在阻塞项' }}
          </strong>
        </div>
        <div class="runtime-model-grid">
          <article v-for="role in modelRoles" :key="role.key">
            <span :class="['runtime-role-icon', `is-${role.key}`]"><i :class="role.icon"></i></span>
            <div>
              <small>{{ role.label }}</small>
              <strong>{{ currentAssignment(role.key)?.display_name || '未分配' }}</strong>
              <p>{{ currentAssignment(role.key)?.model_name || role.missingHint }}</p>
            </div>
            <i
              :class="
                currentAssignment(role.key)?.enabled
                  ? 'fa-solid fa-check is-ready'
                  : 'fa-solid fa-xmark is-blocked'
              "
              aria-hidden="true"
            ></i>
          </article>
        </div>
        <p class="secret-note">
          <i
            :class="
              modelStore.apiKeyConfigured ? 'fa-solid fa-lock' : 'fa-solid fa-triangle-exclamation'
            "
          ></i>
          {{
            modelStore.apiKeyConfigured
              ? 'ARK_API_KEY 已由服务端环境提供，Job 快照不会保存 Secret。'
              : 'ARK_API_KEY 尚未配置；请在服务端环境设置，前端不提供 Secret 输入。'
          }}
        </p>
      </div>
    </section>

    <div class="workbench-grid">
      <aside class="job-browser">
        <div class="job-browser-head">
          <div>
            <span class="section-kicker">Experiment history</span>
            <h2>Evaluation Jobs</h2>
          </div>
          <span>{{ filteredJobs.length }}</span>
        </div>
        <div class="job-filters" role="group" aria-label="Evaluation Job 状态筛选">
          <button
            v-for="filter in jobFilters"
            :key="filter.value"
            type="button"
            :class="{ active: jobFilter === filter.value }"
            @click="jobFilter = filter.value"
          >
            {{ filter.label }}
          </button>
        </div>

        <div
          v-if="evaluationStore.loading && !evaluationStore.jobs.length"
          class="job-empty"
          role="status"
        >
          <i class="fa-solid fa-spinner fa-spin"></i>
          <strong>正在读取历史 Job</strong>
        </div>
        <div v-else-if="!filteredJobs.length" class="job-empty">
          <i class="fa-regular fa-folder-open"></i>
          <strong>没有匹配的 Job</strong>
          <p>导入 Dataset 并启动第一次自动评估。</p>
        </div>
        <div v-else class="job-list">
          <button
            v-for="job in filteredJobs"
            :key="job.id"
            type="button"
            :class="['job-card', { active: job.id === evaluationStore.selectedJobId }]"
            @click="selectJob(job.id)"
          >
            <span class="job-card-top">
              <span :class="['status-pill', `is-${job.status}`]">
                <i :class="statusIcon(job.status)"></i>
                {{ statusLabel(job.status) }}
              </span>
              <small>{{ shortJobId(job.id) }}</small>
            </span>
            <strong>{{ job.dataset_name }}</strong>
            <span class="job-progress-row">
              <span><i :style="{ width: `${Math.round(job.progress * 100)}%` }"></i></span>
              <small>{{ job.completed_cases }}/{{ job.total_cases }}</small>
            </span>
            <span class="job-card-meta">
              <small>{{ formatDate(job.created_at) }}</small>
              <strong v-if="job.report" :class="job.report.passed ? 'is-pass' : 'is-fail'">
                {{ job.report.passed ? 'Gate passed' : 'Gate failed' }}
              </strong>
            </span>
          </button>
        </div>
      </aside>

      <main class="experiment-panel">
        <div v-if="!selectedJob" class="experiment-empty">
          <span><i class="fa-solid fa-chart-line"></i></span>
          <h2>选择一个 Evaluation Job</h2>
          <p>这里会展示运行进度、指标趋势、质量门禁与 Case 级评分证据。</p>
        </div>

        <template v-else>
          <header class="experiment-header">
            <div>
              <div class="experiment-title-row">
                <span :class="['status-pill', `is-${selectedJob.status}`]">
                  <i :class="statusIcon(selectedJob.status)"></i>
                  {{ statusLabel(selectedJob.status) }}
                </span>
                <code>{{ selectedJob.id }}</code>
              </div>
              <h2>{{ selectedJob.dataset_name }}</h2>
              <p>
                Dataset {{ selectedJob.dataset_fingerprint.slice(0, 10) }} · Catalog
                {{ selectedJob.model_catalog_hash.slice(0, 10) }} ·
                {{ selectedJob.created_by || 'system' }}
              </p>
            </div>
            <div class="experiment-actions">
              <button
                type="button"
                class="icon-button"
                aria-label="刷新当前 Evaluation Job"
                title="刷新当前 Job"
                @click="refreshSelectedJob"
              >
                <i class="fa-solid fa-rotate"></i>
              </button>
              <button
                v-if="isJobActive(selectedJob.status)"
                type="button"
                class="cancel-button"
                :disabled="evaluationStore.saving || selectedJob.status === 'cancelling'"
                @click="cancelSelectedJob"
              >
                <i class="fa-solid fa-stop"></i>
                {{ selectedJob.status === 'cancelling' ? '取消中' : '取消 Job' }}
              </button>
            </div>
          </header>

          <section v-if="isJobActive(selectedJob.status)" class="live-progress" aria-live="polite">
            <div>
              <span>
                <i class="fa-solid fa-wave-square"></i>
                Evaluation Worker {{ selectedJob.status === 'queued' ? '等待领取' : '正在执行' }}
              </span>
              <strong>{{ Math.round(selectedJob.progress * 100) }}%</strong>
            </div>
            <span class="progress-track"
              ><i :style="{ width: `${selectedJob.progress * 100}%` }"></i
            ></span>
            <p>
              已完成 {{ selectedJob.completed_cases }} / {{ selectedJob.total_cases }} 个 Case · 第
              {{ selectedJob.attempts }} / {{ selectedJob.max_attempts }} 次尝试
            </p>
          </section>

          <section class="job-model-snapshot" aria-label="Job 模型快照">
            <article v-for="role in modelRoles" :key="role.key">
              <small>{{ role.label }}</small>
              <strong>{{ selectedJob.models[role.key]?.display_name || '未记录' }}</strong>
              <span>
                {{ selectedJob.models[role.key]?.model_name || '—' }} · v{{
                  selectedJob.models[role.key]?.profile_version || 0
                }}
              </span>
            </article>
          </section>

          <div class="experiment-tabs" role="tablist" aria-label="Evaluation Job 视图">
            <button
              id="evaluation-report-tab"
              type="button"
              role="tab"
              :aria-selected="activeTab === 'report'"
              :class="{ active: activeTab === 'report' }"
              @click="activeTab = 'report'"
            >
              <i class="fa-solid fa-chart-simple"></i>
              Report
            </button>
            <button
              id="evaluation-cases-tab"
              type="button"
              role="tab"
              :aria-selected="activeTab === 'cases'"
              :class="{ active: activeTab === 'cases' }"
              @click="activeTab = 'cases'"
            >
              <i class="fa-solid fa-list-check"></i>
              Cases
              <span>{{ selectedCases.length }}</span>
            </button>
          </div>

          <section
            v-if="activeTab === 'report'"
            class="report-view"
            role="tabpanel"
            aria-labelledby="evaluation-report-tab"
          >
            <template v-if="selectedJob.report">
              <div
                class="report-verdict"
                :class="selectedJob.report.passed ? 'is-pass' : 'is-fail'"
              >
                <span
                  ><i
                    :class="
                      selectedJob.report.passed
                        ? 'fa-solid fa-shield-check'
                        : 'fa-solid fa-shield-xmark'
                    "
                  ></i
                ></span>
                <div>
                  <small>Quality gate</small>
                  <strong>{{ selectedJob.report.passed ? '评估通过' : '评估未通过' }}</strong>
                  <p>
                    {{ selectedJob.report.observation_count }}/{{
                      selectedJob.report.case_count
                    }}
                    个 Observation， {{ failedGateCount }} 个 Gate 失败。
                  </p>
                </div>
                <time>{{ formatDate(selectedJob.finished_at || selectedJob.updated_at) }}</time>
              </div>

              <div class="metric-grid">
                <article
                  v-for="metric in headlineMetrics"
                  :key="metric.name"
                  :class="metricTone(metric.name, metric.value)"
                >
                  <span>{{ metricLabel(metric.name) }}</span>
                  <strong>{{ formatMetric(metric.name, metric.value) }}</strong>
                  <small>{{ metric.eligible }} eligible cases</small>
                  <i :style="metricBarStyle(metric.name, metric.value)"></i>
                </article>
              </div>

              <div class="analysis-grid">
                <section class="trend-card">
                  <div class="card-heading">
                    <div>
                      <span class="section-kicker">Historical trend</span>
                      <h3>Dataset 指标趋势</h3>
                    </div>
                    <select v-model="trendMetric" aria-label="趋势指标">
                      <option v-for="metric in trendMetricOptions" :key="metric" :value="metric">
                        {{ metricLabel(metric) }}
                      </option>
                    </select>
                  </div>
                  <div v-if="trendEntries.length" class="trend-chart">
                    <div class="trend-scale">
                      <span>{{ formatMetric(trendMetric, trendMax) }}</span>
                      <span>{{ formatMetric(trendMetric, trendMin) }}</span>
                    </div>
                    <svg
                      viewBox="0 0 100 50"
                      preserveAspectRatio="none"
                      aria-label="历史评估指标折线图"
                    >
                      <line x1="0" y1="8" x2="100" y2="8"></line>
                      <line x1="0" y1="25" x2="100" y2="25"></line>
                      <line x1="0" y1="42" x2="100" y2="42"></line>
                      <polyline :points="trendPolyline"></polyline>
                      <circle
                        v-for="point in trendChartPoints"
                        :key="point.jobId"
                        :cx="point.x"
                        :cy="point.y"
                        r="1.5"
                      >
                        <title>
                          {{ point.label }}：{{ formatMetric(trendMetric, point.value) }}
                        </title>
                      </circle>
                    </svg>
                    <div class="trend-labels">
                      <span v-for="entry in trendEntries" :key="entry.job.id">
                        {{ shortJobId(entry.job.id) }}
                      </span>
                    </div>
                  </div>
                  <div v-else class="mini-empty">
                    <i class="fa-solid fa-chart-line"></i>
                    <p>该 Dataset 暂无可比较的历史指标。</p>
                  </div>
                </section>

                <section class="gate-card">
                  <div class="card-heading">
                    <div>
                      <span class="section-kicker">Regression gates</span>
                      <h3>质量门禁</h3>
                    </div>
                    <span>{{ selectedJob.report.gates.length }} rules</span>
                  </div>
                  <div v-if="selectedJob.report.gates.length" class="gate-list">
                    <article
                      v-for="gate in selectedJob.report.gates"
                      :key="`${gate.name}-${gate.metric || ''}`"
                    >
                      <span :class="['gate-icon', `is-${gate.status}`]">
                        <i :class="gateStatusIcon(gate.status)"></i>
                      </span>
                      <div>
                        <strong>{{ gate.name }}</strong>
                        <p>{{ gate.detail || gate.metric || '门禁已执行' }}</p>
                      </div>
                      <small v-if="gate.actual !== null && gate.actual !== undefined">
                        {{ formatMetric(gate.metric || '', gate.actual) }}
                      </small>
                    </article>
                  </div>
                  <div v-else class="mini-empty">
                    <i class="fa-solid fa-shield"></i>
                    <p>本次运行没有产生显式 Gate 结果。</p>
                  </div>
                </section>
              </div>

              <details
                v-if="Object.keys(selectedJob.report.unavailable_metrics).length"
                class="unavailable-metrics"
              >
                <summary>
                  <span><i class="fa-solid fa-circle-info"></i> 不可用指标</span>
                  <small>{{ Object.keys(selectedJob.report.unavailable_metrics).length }}</small>
                </summary>
                <div>
                  <p
                    v-for="(reason, metric) in selectedJob.report.unavailable_metrics"
                    :key="metric"
                  >
                    <strong>{{ metricLabel(String(metric)) }}</strong>
                    <span>{{ reason }}</span>
                  </p>
                </div>
              </details>
            </template>

            <div v-else-if="selectedJob.status === 'failed'" class="terminal-state is-error">
              <span><i class="fa-solid fa-triangle-exclamation"></i></span>
              <h3>Evaluation Job 执行失败</h3>
              <p>{{ jobErrorMessage(selectedJob) }}</p>
              <small v-if="selectedJob.error_code">{{ selectedJob.error_code }}</small>
            </div>
            <div v-else-if="selectedJob.status === 'cancelled'" class="terminal-state">
              <span><i class="fa-solid fa-ban"></i></span>
              <h3>Evaluation Job 已取消</h3>
              <p>已完成的 Case 投影仍可在 Cases 页签查看。</p>
            </div>
            <div v-else class="terminal-state is-running">
              <span><i class="fa-solid fa-flask-vial"></i></span>
              <h3>自动评估进行中</h3>
              <p>Worker 会执行 RAG、生成回答、调用 Evaluator 结构化评分，并在完成后生成 Report。</p>
            </div>
          </section>

          <section v-else class="cases-view" role="tabpanel" aria-labelledby="evaluation-cases-tab">
            <div class="case-toolbar">
              <label class="case-search">
                <i class="fa-solid fa-magnifying-glass"></i>
                <input v-model="caseSearch" type="search" placeholder="搜索 Case ID、问题或答案" />
              </label>
              <label>
                <span class="sr-only">Case 状态</span>
                <select v-model="caseStatusFilter" aria-label="Case 状态">
                  <option value="all">全部状态</option>
                  <option value="completed">已完成</option>
                  <option value="running">运行中</option>
                  <option value="queued">排队中</option>
                  <option value="failed">失败</option>
                  <option value="cancelled">已取消</option>
                </select>
              </label>
              <label>
                <span class="sr-only">Case 结果</span>
                <select v-model="caseVerdictFilter" aria-label="Case 结果">
                  <option value="all">全部结果</option>
                  <option value="passed">通过</option>
                  <option value="failed">未通过</option>
                </select>
              </label>
              <span>{{ filteredCases.length }} / {{ selectedCases.length }}</span>
            </div>

            <div v-if="!selectedCases.length" class="case-empty">
              <i class="fa-solid fa-list-check"></i>
              <strong>Case 结果尚未生成</strong>
              <p>Job 被 Worker 领取后，每个 Dataset Case 都会获得独立的持久化状态。</p>
            </div>
            <div v-else-if="!filteredCases.length" class="case-empty">
              <i class="fa-solid fa-filter-circle-xmark"></i>
              <strong>没有匹配的 Case</strong>
              <p>调整搜索词或筛选条件。</p>
            </div>
            <div v-else class="case-table-wrap">
              <table class="case-table">
                <thead>
                  <tr>
                    <th>Case</th>
                    <th>Question</th>
                    <th>Status</th>
                    <th>Correctness</th>
                    <th>Groundedness</th>
                    <th>Latency</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  <template v-for="caseResult in filteredCases" :key="caseResult.id">
                    <tr :class="{ expanded: expandedCaseIds.has(caseResult.id) }">
                      <td>
                        <strong>{{ caseResult.case_id }}</strong>
                        <small>#{{ caseResult.position + 1 }}</small>
                      </td>
                      <td>
                        <span class="question-cell">{{ caseResult.question }}</span>
                      </td>
                      <td>
                        <span :class="['case-status', `is-${caseResult.status}`]">
                          <i :class="caseStatusIcon(caseResult.status)"></i>
                          {{ caseStatusLabel(caseResult.status) }}
                        </span>
                      </td>
                      <td>
                        {{
                          formatMetric('answer_correctness', caseResult.metrics.answer_correctness)
                        }}
                      </td>
                      <td>{{ formatMetric('groundedness', caseResult.metrics.groundedness) }}</td>
                      <td>{{ formatMetric('duration_ms', caseResult.duration_ms) }}</td>
                      <td>
                        <button
                          type="button"
                          :aria-expanded="expandedCaseIds.has(caseResult.id)"
                          :aria-label="`${expandedCaseIds.has(caseResult.id) ? '收起' : '展开'} ${caseResult.case_id}`"
                          @click="toggleCase(caseResult.id)"
                        >
                          <i
                            :class="
                              expandedCaseIds.has(caseResult.id)
                                ? 'fa-solid fa-chevron-up'
                                : 'fa-solid fa-chevron-down'
                            "
                          ></i>
                        </button>
                      </td>
                    </tr>
                    <tr v-if="expandedCaseIds.has(caseResult.id)" class="case-details-row">
                      <td colspan="7">
                        <div class="case-details">
                          <section>
                            <span class="detail-label">Question</span>
                            <p>{{ caseResult.question }}</p>
                          </section>
                          <section>
                            <span class="detail-label">Generated answer</span>
                            <p>{{ caseResult.generated_answer || '尚未生成回答。' }}</p>
                          </section>
                          <section
                            v-if="caseResult.judge_reason || caseResult.error_code"
                            class="judge-reason"
                          >
                            <span class="detail-label">Evaluator reason</span>
                            <p>{{ caseResult.judge_reason || jobCaseError(caseResult) }}</p>
                          </section>

                          <div class="case-detail-grid">
                            <section>
                              <span class="detail-label">Metrics</span>
                              <div class="detail-metrics">
                                <span v-for="(value, metric) in caseResult.metrics" :key="metric">
                                  <small>{{ metricLabel(String(metric)) }}</small>
                                  <strong>{{ formatMetric(String(metric), value) }}</strong>
                                </span>
                              </div>
                            </section>
                            <section>
                              <span class="detail-label">Execution</span>
                              <dl class="execution-facts">
                                <div>
                                  <dt>Route</dt>
                                  <dd>{{ caseResult.observation?.route || '—' }}</dd>
                                </div>
                                <div>
                                  <dt>Outcome</dt>
                                  <dd>{{ caseResult.observation?.outcome || '—' }}</dd>
                                </div>
                                <div>
                                  <dt>HITL</dt>
                                  <dd>{{ caseResult.observation?.hitl || 'none' }}</dd>
                                </div>
                                <div>
                                  <dt>Provider</dt>
                                  <dd>{{ caseResult.provider_error_code || 'healthy' }}</dd>
                                </div>
                              </dl>
                            </section>
                          </div>

                          <section>
                            <span class="detail-label">Evidence identities</span>
                            <div
                              v-if="caseResult.retrieved_identities.length"
                              class="identity-list"
                            >
                              <article
                                v-for="(identity, index) in caseResult.retrieved_identities"
                                :key="index"
                              >
                                <span>#{{ index + 1 }}</span>
                                <dl>
                                  <div v-for="(value, key) in identity" :key="key">
                                    <dt>{{ key }}</dt>
                                    <dd>{{ renderIdentityValue(value) }}</dd>
                                  </div>
                                </dl>
                              </article>
                            </div>
                            <p v-else class="identity-empty">
                              没有公开 Evidence identity；正文不会进入评估响应。
                            </p>
                          </section>
                        </div>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
            </div>
          </section>
        </template>
      </main>
    </div>

    <Teleport to="body">
      <div v-if="importOpen" class="modal-backdrop" @click.self="closeDatasetImport">
        <section
          class="dataset-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="dataset-import-title"
          @keydown.esc="closeDatasetImport"
        >
          <header>
            <div>
              <span class="section-kicker">Dataset registry</span>
              <h2 id="dataset-import-title">导入 Evaluation Dataset</h2>
              <p>
                上传或粘贴版本化 JSON。服务端会验证 case identity、标注一致性并生成 fingerprint。
              </p>
            </div>
            <button type="button" aria-label="关闭 Dataset 导入" @click="closeDatasetImport">
              <i class="fa-solid fa-xmark"></i>
            </button>
          </header>

          <div class="import-body">
            <div class="import-toolbar">
              <label class="file-picker">
                <i class="fa-solid fa-file-code"></i>
                <span>
                  <strong>选择 JSON 文件</strong>
                  <small>{{ importFileName || '最大大小由服务端请求限制控制' }}</small>
                </span>
                <input type="file" accept=".json,application/json" @change="readDatasetFile" />
              </label>
              <button type="button" @click="loadDatasetExample">
                <i class="fa-regular fa-lightbulb"></i>
                填入示例
              </button>
            </div>

            <label class="dataset-editor">
              <span>Dataset JSON</span>
              <textarea
                ref="datasetEditor"
                v-model="datasetText"
                spellcheck="false"
                placeholder='{"schema_version":1,"name":"rag_eval_v1","cases":[...]}'
                @input="parseDatasetText"
              ></textarea>
            </label>

            <div v-if="datasetPreview" class="dataset-preview">
              <span><i class="fa-solid fa-circle-check"></i></span>
              <div>
                <strong>{{ datasetPreview.name }}</strong>
                <p>
                  {{ datasetPreview.cases.length }} cases · schema v{{
                    datasetPreview.schema_version
                  }}
                </p>
              </div>
              <div>
                <small>Critical</small>
                <strong>{{ datasetPreview.cases.filter((item) => item.critical).length }}</strong>
              </div>
              <div>
                <small>Answerable</small>
                <strong>{{
                  datasetPreview.cases.filter((item) => item.expected?.outcome === 'ANSWERABLE')
                    .length
                }}</strong>
              </div>
            </div>
            <p v-if="importError" class="form-error" role="alert">{{ importError }}</p>
          </div>

          <footer>
            <span
              ><i class="fa-solid fa-shield-halved"></i> Dataset 会持久化，Evidence 正文仍不进入 Job
              响应。</span
            >
            <div>
              <button type="button" class="secondary-button" @click="closeDatasetImport">
                取消
              </button>
              <button
                type="button"
                class="primary-button"
                :disabled="!datasetPreview || evaluationStore.saving"
                @click="importDataset"
              >
                <i v-if="evaluationStore.saving" class="fa-solid fa-spinner fa-spin"></i>
                导入 Dataset
              </button>
            </div>
          </footer>
        </section>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useEvaluationStore } from '@/stores/evaluations';
import { useModelStore } from '@/stores/models';
import type { ModelProfile, ModelRole } from '@/types/models';
import type {
  RagEvaluationCaseResult,
  RagEvaluationCaseStatus,
  RagEvaluationDataset,
  RagEvaluationJob,
  RagEvaluationJobStatus,
  RagGateResult,
} from '@/types/evaluations';
import { getPublicError } from '@/utils/api';

type JobFilter = 'all' | 'active' | 'succeeded' | 'failed';
type CaseVerdictFilter = 'all' | 'passed' | 'failed';

const modelRoles: Array<{
  key: ModelRole;
  label: string;
  icon: string;
  missingHint: string;
}> = [
  {
    key: 'answer',
    label: 'Answer',
    icon: 'fa-regular fa-message',
    missingHint: '需要流式输出能力',
  },
  { key: 'fast', label: 'Fast', icon: 'fa-solid fa-bolt', missingHint: '需要结构化输出能力' },
  {
    key: 'grader',
    label: 'Grader',
    icon: 'fa-solid fa-scale-balanced',
    missingHint: '需要结构化输出能力',
  },
  {
    key: 'evaluator',
    label: 'Evaluator',
    icon: 'fa-solid fa-flask-vial',
    missingHint: '需要结构化输出能力',
  },
];

const jobFilters: Array<{ value: JobFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '运行中' },
  { value: 'succeeded', label: '已完成' },
  { value: 'failed', label: '异常' },
];

const metricLabels: Record<string, string> = {
  answer_correctness: 'Answer correctness',
  groundedness: 'Groundedness',
  answer_relevance: 'Answer relevance',
  completeness: 'Completeness',
  context_relevance: 'Context relevance',
  unsupported_claim_rate: 'Unsupported claims',
  conflict_disclosure_rate: 'Conflict disclosure',
  route_accuracy: 'Route accuracy',
  complexity_accuracy: 'Complexity accuracy',
  outcome_accuracy: 'Outcome accuracy',
  hitl_accuracy: 'HITL accuracy',
  provider_failure_rate: 'Provider failure',
  duration_ms: 'Latency',
  latency_p50_ms: 'Latency p50',
  latency_p95_ms: 'Latency p95',
  recall_at_5: 'Recall@5',
  recall_at_10: 'Recall@10',
  precision_at_5: 'Precision@5',
  precision_at_10: 'Precision@10',
  document_recall_at_5: 'Document recall@5',
  document_recall_at_10: 'Document recall@10',
};

const preferredHeadlineMetrics = [
  'answer_correctness',
  'groundedness',
  'answer_relevance',
  'completeness',
  'context_relevance',
  'unsupported_claim_rate',
  'conflict_disclosure_rate',
  'recall_at_5',
];

const activeStatuses = new Set<RagEvaluationJobStatus>(['queued', 'running', 'cancelling']);
const evaluationStore = useEvaluationStore();
const modelStore = useModelStore();
const selectedDatasetId = ref('');
const selectedBaselineJobId = ref('');
const jobFilter = ref<JobFilter>('all');
const activeTab = ref<'report' | 'cases'>('report');
const trendMetric = ref('answer_correctness');
const caseSearch = ref('');
const caseStatusFilter = ref<'all' | RagEvaluationCaseStatus>('all');
const caseVerdictFilter = ref<CaseVerdictFilter>('all');
const expandedCaseIds = ref(new Set<string>());
const importOpen = ref(false);
const datasetText = ref('');
const datasetPreview = ref<RagEvaluationDataset | null>(null);
const importFileName = ref('');
const importError = ref('');
const datasetEditor = ref<HTMLTextAreaElement | null>(null);

const selectedJob = computed(() => evaluationStore.selectedJob);
const selectedCases = computed(() => evaluationStore.selectedCases);
const selectedDataset = computed(() =>
  evaluationStore.datasets.find((dataset) => dataset.id === selectedDatasetId.value)
);
const totalDatasetCases = computed(() =>
  evaluationStore.datasets.reduce((total, dataset) => total + dataset.case_count, 0)
);
const gatePassRate = computed(() => {
  const completed = evaluationStore.completedJobs;
  if (!completed.length) return '—';
  const passed = completed.filter((job) => job.report?.passed).length;
  return `${Math.round((passed / completed.length) * 100)}%`;
});
const runtimeReadinessText = computed(() => {
  if (!modelStore.apiKeyConfigured) return 'ARK_API_KEY 未配置';
  const missing = modelRoles.filter((role) => !currentAssignment(role.key)?.enabled);
  return missing.length
    ? `缺少 ${missing.map((role) => role.label).join('、')}`
    : '四角色模型快照可创建';
});
const compatibleBaselineJobs = computed(() => {
  const dataset = selectedDataset.value;
  if (!dataset) return [];
  return evaluationStore.completedJobs.filter(
    (job) => job.dataset_fingerprint === dataset.fingerprint
  );
});
const canStartEvaluation = computed(
  () => Boolean(selectedDatasetId.value) && modelStore.readyForEvaluation && !evaluationStore.saving
);
const startButtonTitle = computed(() => {
  if (!selectedDatasetId.value) return '请先选择 Evaluation Dataset';
  if (!modelStore.apiKeyConfigured) return '服务端 ARK_API_KEY 未配置';
  if (!modelStore.readyForEvaluation) return '请先在模型中心完成四个模型角色分配';
  return '创建持久化 Evaluation Job';
});
const filteredJobs = computed(() => {
  if (jobFilter.value === 'all') return evaluationStore.jobs;
  if (jobFilter.value === 'active') {
    return evaluationStore.jobs.filter((job) => activeStatuses.has(job.status));
  }
  if (jobFilter.value === 'failed') {
    return evaluationStore.jobs.filter((job) => ['failed', 'cancelled'].includes(job.status));
  }
  return evaluationStore.jobs.filter((job) => job.status === 'succeeded');
});
const failedGateCount = computed(
  () => selectedJob.value?.report?.gates.filter((gate) => gate.status === 'failed').length || 0
);
const headlineMetrics = computed(() => {
  const metrics = selectedJob.value?.report?.metrics || {};
  const ordered = [
    ...preferredHeadlineMetrics,
    ...Object.keys(metrics).filter((name) => !preferredHeadlineMetrics.includes(name)),
  ];
  return ordered
    .filter((name, index) => ordered.indexOf(name) === index && metrics[name])
    .slice(0, 8)
    .map((name) => ({
      name,
      value: metrics[name]?.value ?? null,
      eligible: metrics[name]?.eligible_cases ?? 0,
    }));
});
const trendMetricOptions = computed(() => {
  const names = new Set<string>();
  evaluationStore.completedJobs
    .filter(
      (job) =>
        !selectedJob.value || job.dataset_fingerprint === selectedJob.value.dataset_fingerprint
    )
    .forEach((job) => Object.keys(job.report?.metrics || {}).forEach((name) => names.add(name)));
  const values = [...names].sort((left, right) => {
    const leftIndex = preferredHeadlineMetrics.indexOf(left);
    const rightIndex = preferredHeadlineMetrics.indexOf(right);
    if (leftIndex >= 0 || rightIndex >= 0) {
      return (leftIndex < 0 ? 999 : leftIndex) - (rightIndex < 0 ? 999 : rightIndex);
    }
    return left.localeCompare(right);
  });
  return values.length ? values : ['answer_correctness'];
});
const trendEntries = computed(() => {
  if (!selectedJob.value) return [];
  return evaluationStore.completedJobs
    .filter((job) => job.dataset_fingerprint === selectedJob.value?.dataset_fingerprint)
    .map((job) => ({ job, value: job.report?.metrics[trendMetric.value]?.value }))
    .filter(
      (entry): entry is { job: RagEvaluationJob; value: number } => typeof entry.value === 'number'
    )
    .sort(
      (left, right) =>
        new Date(left.job.finished_at || left.job.created_at).getTime() -
        new Date(right.job.finished_at || right.job.created_at).getTime()
    )
    .slice(-8);
});
const trendMin = computed(() => Math.min(...trendEntries.value.map((entry) => entry.value), 0));
const trendMax = computed(() => Math.max(...trendEntries.value.map((entry) => entry.value), 1));
const trendChartPoints = computed(() => {
  const entries = trendEntries.value;
  const span = trendMax.value - trendMin.value || 1;
  return entries.map((entry, index) => ({
    jobId: entry.job.id,
    label: shortJobId(entry.job.id),
    value: entry.value,
    x: entries.length === 1 ? 50 : (index / (entries.length - 1)) * 100,
    y: 42 - ((entry.value - trendMin.value) / span) * 34,
  }));
});
const trendPolyline = computed(() =>
  trendChartPoints.value.map((point) => `${point.x},${point.y}`).join(' ')
);
const reportCaseMap = computed(
  () => new Map((selectedJob.value?.report?.cases || []).map((item) => [item.case_id, item]))
);
const filteredCases = computed(() => {
  const query = caseSearch.value.trim().toLocaleLowerCase();
  return selectedCases.value.filter((item) => {
    if (caseStatusFilter.value !== 'all' && item.status !== caseStatusFilter.value) return false;
    const verdict = reportCaseMap.value.get(item.case_id)?.passed;
    if (caseVerdictFilter.value === 'passed' && verdict !== true) return false;
    if (caseVerdictFilter.value === 'failed' && verdict !== false) return false;
    if (!query) return true;
    return [item.case_id, item.question, item.generated_answer || '', item.judge_reason || ''].some(
      (value) => value.toLocaleLowerCase().includes(query)
    );
  });
});

const currentAssignment = (role: ModelRole): ModelProfile | null =>
  modelStore.controlPlane?.assignments[role] || null;

const isJobActive = (status: RagEvaluationJobStatus) => activeStatuses.has(status);

const shortJobId = (jobId: string) => `#${jobId.slice(-8)}`;

const statusLabel = (status: RagEvaluationJobStatus) =>
  ({
    queued: '排队中',
    running: '运行中',
    cancelling: '取消中',
    cancelled: '已取消',
    succeeded: '已完成',
    failed: '失败',
  })[status];

const statusIcon = (status: RagEvaluationJobStatus) =>
  ({
    queued: 'fa-regular fa-clock',
    running: 'fa-solid fa-spinner fa-spin',
    cancelling: 'fa-solid fa-spinner fa-spin',
    cancelled: 'fa-solid fa-ban',
    succeeded: 'fa-solid fa-check',
    failed: 'fa-solid fa-xmark',
  })[status];

const caseStatusLabel = (status: RagEvaluationCaseStatus) =>
  ({
    queued: '排队中',
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
  })[status];

const caseStatusIcon = (status: RagEvaluationCaseStatus) =>
  ({
    queued: 'fa-regular fa-clock',
    running: 'fa-solid fa-spinner fa-spin',
    completed: 'fa-solid fa-check',
    failed: 'fa-solid fa-xmark',
    cancelled: 'fa-solid fa-ban',
  })[status];

const gateStatusIcon = (status: RagGateResult['status']) =>
  status === 'passed'
    ? 'fa-solid fa-check'
    : status === 'failed'
      ? 'fa-solid fa-xmark'
      : 'fa-solid fa-minus';

const metricLabel = (name: string) =>
  metricLabels[name] ||
  name.replaceAll('_', ' ').replace(/\b\w/g, (character) => character.toUpperCase());

const formatMetric = (name: string, value: number | null | undefined) => {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (name.includes('duration') || name.includes('latency') || name.endsWith('_ms')) {
    return value >= 1000 ? `${(value / 1000).toFixed(2)}s` : `${Math.round(value)}ms`;
  }
  if (Math.abs(value) <= 1.000001) return `${Math.round(value * 100)}%`;
  return value.toFixed(2);
};

const metricTone = (name: string, value: number | null) => {
  if (value === null) return 'is-neutral';
  const lowerIsBetter =
    name.includes('failure') || name.includes('unsupported') || name.includes('latency');
  const normalized = name.includes('latency') ? 0.5 : value;
  const good = lowerIsBetter ? normalized <= 0.2 : normalized >= 0.75;
  const warning = lowerIsBetter ? normalized <= 0.5 : normalized >= 0.5;
  return good ? 'is-good' : warning ? 'is-warning' : 'is-bad';
};

const metricBarStyle = (name: string, value: number | null) => {
  if (value === null) return { width: '0%' };
  const ratio = name.includes('latency')
    ? Math.min(value / 5000, 1)
    : Math.min(Math.max(value, 0), 1);
  return { width: `${Math.round(ratio * 100)}%` };
};

const formatDate = (value: string | null) => {
  if (!value) return '—';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
};

const jobErrorMessage = (job: RagEvaluationJob) => {
  const message = job.error?.message;
  return typeof message === 'string'
    ? message
    : 'Worker 未能完成本次评估，请检查错误码与服务日志。';
};

const jobCaseError = (caseResult: RagEvaluationCaseResult) => {
  const message = caseResult.error?.message;
  return typeof message === 'string' ? message : caseResult.error_code || 'Case 执行失败';
};

const renderIdentityValue = (value: unknown) => {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return JSON.stringify(value);
};

const refreshWorkspace = async () => {
  await Promise.allSettled([evaluationStore.initialize(), modelStore.fetchControlPlane()]);
  if (!selectedDatasetId.value && evaluationStore.datasets.length) {
    selectedDatasetId.value = evaluationStore.datasets[0].id;
  }
};

const refreshSelectedJob = async () => {
  try {
    await evaluationStore.refreshSelectedJob();
  } catch {
    // Store keeps the last durable projection and exposes the public error.
  }
};

const selectJob = async (jobId: string) => {
  try {
    await evaluationStore.selectJob(jobId);
    activeTab.value = 'report';
  } catch {
    // The selected identity remains visible for retry.
  }
};

const startEvaluation = async () => {
  if (!canStartEvaluation.value) return;
  try {
    await evaluationStore.createJob({
      dataset_id: selectedDatasetId.value,
      baseline_job_id: selectedBaselineJobId.value || null,
    });
    activeTab.value = 'report';
  } catch {
    // Store exposes the authoritative failure.
  }
};

const cancelSelectedJob = async () => {
  if (!selectedJob.value || !isJobActive(selectedJob.value.status)) return;
  try {
    await evaluationStore.cancelJob(selectedJob.value.id);
    evaluationStore.startPolling();
  } catch {
    // Store exposes the authoritative failure.
  }
};

const toggleCase = (caseId: string) => {
  const next = new Set(expandedCaseIds.value);
  if (next.has(caseId)) next.delete(caseId);
  else next.add(caseId);
  expandedCaseIds.value = next;
};

const openDatasetImport = async () => {
  importOpen.value = true;
  importError.value = '';
  datasetText.value = '';
  datasetPreview.value = null;
  importFileName.value = '';
  await nextTick();
  datasetEditor.value?.focus();
};

const closeDatasetImport = () => {
  if (evaluationStore.saving) return;
  importOpen.value = false;
  importError.value = '';
};

const normalizeDataset = (value: unknown): RagEvaluationDataset => {
  if (!value || typeof value !== 'object') throw new Error('Dataset JSON 必须是对象');
  const container = value as Record<string, unknown>;
  const raw =
    container.dataset && typeof container.dataset === 'object' ? container.dataset : container;
  const dataset = raw as Partial<RagEvaluationDataset>;
  if (dataset.schema_version !== 1) throw new Error('schema_version 必须为 1');
  if (typeof dataset.name !== 'string' || !dataset.name.trim())
    throw new Error('Dataset name 不能为空');
  if (!Array.isArray(dataset.cases) || !dataset.cases.length)
    throw new Error('Dataset 至少需要一个 Case');
  dataset.cases.forEach((item, index) => {
    if (!item || typeof item !== 'object') throw new Error(`Case ${index + 1} 必须是对象`);
    if (typeof item.id !== 'string' || !item.id.trim())
      throw new Error(`Case ${index + 1} 缺少 id`);
    if (typeof item.question !== 'string' || !item.question.trim()) {
      throw new Error(`Case ${item.id || index + 1} 缺少 question`);
    }
    if (!item.expected || typeof item.expected !== 'object') {
      throw new Error(`Case ${item.id || index + 1} 缺少 expected`);
    }
  });
  return raw as RagEvaluationDataset;
};

const parseDatasetText = () => {
  datasetPreview.value = null;
  importError.value = '';
  if (!datasetText.value.trim()) return;
  try {
    datasetPreview.value = normalizeDataset(JSON.parse(datasetText.value));
  } catch (error) {
    importError.value = error instanceof Error ? error.message : 'Dataset JSON 无效';
  }
};

const readDatasetFile = async (event: Event) => {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  importFileName.value = file.name;
  try {
    datasetText.value = await file.text();
    parseDatasetText();
  } catch {
    importError.value = '无法读取所选 JSON 文件';
  } finally {
    input.value = '';
  }
};

const loadDatasetExample = () => {
  const example: RagEvaluationDataset = {
    schema_version: 1,
    name: 'rag_quality_eval_v1',
    cases: [
      {
        id: 'no-knowledge-001',
        tags: ['smoke', 'routing'],
        critical: true,
        question: '知识库没有覆盖的问题应该如何回答？',
        expected: {
          complexity: 'simple',
          route: 'no_knowledge',
          outcome: 'NO_KNOWLEDGE',
          hitl: 'none',
          acceptable_abstention: true,
        },
      },
    ],
  };
  datasetText.value = JSON.stringify(example, null, 2);
  importFileName.value = '';
  parseDatasetText();
};

const importDataset = async () => {
  if (!datasetPreview.value || evaluationStore.saving) return;
  importError.value = '';
  try {
    const record = await evaluationStore.createDataset(datasetPreview.value);
    selectedDatasetId.value = record.id;
    selectedBaselineJobId.value = '';
    closeDatasetImport();
  } catch (error) {
    importError.value = getPublicError(error).message;
  }
};

watch(
  () => evaluationStore.datasets.map((dataset) => dataset.id).join(','),
  () => {
    if (!selectedDatasetId.value && evaluationStore.datasets.length) {
      selectedDatasetId.value = evaluationStore.datasets[0].id;
    }
  }
);

watch(selectedDatasetId, () => {
  if (
    selectedBaselineJobId.value &&
    !compatibleBaselineJobs.value.some((job) => job.id === selectedBaselineJobId.value)
  ) {
    selectedBaselineJobId.value = '';
  }
});

watch(
  () => evaluationStore.selectedJobId,
  () => {
    expandedCaseIds.value = new Set();
  }
);

watch(trendMetricOptions, (options) => {
  if (!options.includes(trendMetric.value)) trendMetric.value = options[0] || 'answer_correctness';
});

onMounted(refreshWorkspace);

onUnmounted(() => {
  evaluationStore.stopPolling();
});
</script>

<style scoped>
.evaluation-page {
  width: 100%;
  height: 100%;
  padding: 25px;
  overflow-y: auto;
}

.workspace-header,
.workspace-actions,
.workspace-alert,
.launch-form,
.runtime-check-head,
.job-browser-head,
.job-card-top,
.job-progress-row,
.job-card-meta,
.experiment-header,
.experiment-title-row,
.experiment-actions,
.live-progress > div,
.card-heading,
.report-verdict,
.case-toolbar,
.dataset-modal header,
.dataset-modal footer,
.import-toolbar,
.dataset-preview {
  display: flex;
  align-items: center;
}

.workspace-header {
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding-bottom: 21px;
  border-bottom: 1px solid var(--line);
}

.workspace-header h1 {
  margin-top: 5px;
  font-size: 26px;
  letter-spacing: -0.045em;
}

.workspace-header p {
  max-width: 760px;
  margin-top: 7px;
  color: var(--muted);
  font-size: var(--font-body);
  line-height: 1.55;
}

.workspace-actions {
  gap: 8px;
}

.primary-button,
.secondary-button,
.run-button,
.cancel-button {
  display: inline-flex;
  min-height: 39px;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 0 14px;
  border-radius: 11px;
  cursor: pointer;
  font-size: var(--font-body);
  font-weight: 720;
}

.primary-button,
.run-button {
  color: #111820;
  background: linear-gradient(135deg, var(--mint), var(--lilac));
  box-shadow: 0 10px 24px rgba(116, 225, 183, 0.12);
}

html[data-theme='light'] .primary-button,
html[data-theme='light'] .run-button {
  color: white;
  background: linear-gradient(135deg, var(--mint-strong), var(--lilac-strong));
}

.secondary-button {
  border: 1px solid var(--line);
  color: var(--text-soft);
  background: var(--surface);
}

.cancel-button {
  border: 1px solid rgba(255, 105, 122, 0.26);
  color: var(--danger);
  background: var(--danger-soft);
}

.workspace-alert {
  gap: 9px;
  margin-top: 14px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 11px;
  font-size: var(--font-body);
}

.workspace-alert span {
  flex: 1;
}

.workspace-alert button {
  padding: 4px 7px;
  border-radius: 7px;
  color: inherit;
  background: transparent;
  cursor: pointer;
}

.workspace-alert.is-error {
  border-color: rgba(255, 105, 122, 0.34);
  color: var(--danger);
  background: var(--danger-soft);
}

.workspace-alert.is-success {
  border-color: rgba(112, 228, 183, 0.28);
  color: var(--success);
  background: rgba(112, 228, 183, 0.07);
}

.overview-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin: 18px 0;
}

.overview-grid article {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 15px;
  background: var(--surface);
}

.overview-grid article.is-running {
  border-color: rgba(112, 228, 183, 0.25);
  background: rgba(112, 228, 183, 0.05);
}

.overview-grid article.is-warning {
  border-color: rgba(244, 199, 109, 0.25);
  background: var(--warning-soft);
}

.overview-icon {
  display: grid;
  width: 33px;
  height: 33px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 10px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.08);
}

.overview-grid small,
.overview-grid strong,
.overview-grid p {
  display: block;
}

.overview-grid small {
  color: var(--muted);
  font-size: var(--font-caption);
}

.overview-grid strong {
  margin-top: 4px;
  font-size: 18px;
}

.overview-grid p {
  margin-top: 4px;
  color: var(--muted-strong);
  font-size: var(--font-caption);
}

.launch-panel,
.job-browser,
.experiment-panel {
  border: 1px solid var(--line);
  border-radius: 18px;
  background: var(--surface);
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.025);
}

.launch-panel {
  display: grid;
  grid-template-columns: minmax(180px, 0.72fr) minmax(430px, 1.45fr) minmax(360px, 1.2fr);
  gap: 18px;
  align-items: center;
  padding: 17px;
}

.section-kicker {
  color: var(--mint);
  font-size: var(--font-caption);
  font-weight: 780;
  letter-spacing: 0.13em;
  text-transform: uppercase;
}

.launch-copy h2,
.job-browser-head h2 {
  margin-top: 4px;
  font-size: 18px;
}

.launch-copy p {
  margin-top: 7px;
  color: var(--muted);
  font-size: var(--font-small);
  line-height: 1.55;
}

.launch-form {
  align-items: flex-end;
  gap: 8px;
}

.launch-form label {
  display: grid;
  min-width: 0;
  flex: 1;
  gap: 6px;
}

.launch-form label > span {
  color: var(--text-soft);
  font-size: var(--font-caption);
  font-weight: 650;
}

.launch-form select,
.case-toolbar select,
.card-heading select {
  min-height: 39px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  outline: 0;
  color: var(--text);
  background: var(--surface-strong);
  font-size: var(--font-small);
}

.run-button {
  flex: 0 0 auto;
  white-space: nowrap;
}

.runtime-checks {
  min-width: 0;
  padding-left: 17px;
  border-left: 1px solid var(--line);
}

.runtime-check-head {
  justify-content: space-between;
  gap: 10px;
  color: var(--muted);
  font-size: var(--font-caption);
}

.runtime-check-head strong {
  font-size: var(--font-caption);
}

.is-ready,
.is-pass {
  color: var(--success) !important;
}

.is-blocked,
.is-fail {
  color: var(--danger) !important;
}

.runtime-model-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  margin-top: 8px;
}

.runtime-model-grid article {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 7px;
  padding: 7px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface-soft);
}

.runtime-role-icon {
  display: grid;
  width: 26px;
  height: 26px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 8px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.07);
  font-size: var(--font-small);
}

.runtime-role-icon.is-fast {
  color: var(--warning);
}

.runtime-role-icon.is-grader {
  color: var(--lilac);
}

.runtime-role-icon.is-evaluator {
  color: #8ab8ff;
}

.runtime-model-grid article > div {
  min-width: 0;
  flex: 1;
}

.runtime-model-grid small,
.runtime-model-grid strong,
.runtime-model-grid p {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.runtime-model-grid small {
  color: var(--muted);
  font-size: var(--font-micro);
}

.runtime-model-grid strong {
  margin-top: 2px;
  font-size: var(--font-caption);
}

.runtime-model-grid p {
  margin-top: 2px;
  color: var(--muted-strong);
  font-size: var(--font-micro);
}

.runtime-model-grid article > i {
  font-size: var(--font-caption);
}

.secret-note {
  margin-top: 7px;
  color: var(--muted);
  font-size: var(--font-micro);
  line-height: 1.4;
}

.workbench-grid {
  display: grid;
  grid-template-columns: 245px minmax(0, 1fr);
  gap: 12px;
  min-height: 580px;
  margin-top: 12px;
}

.job-browser {
  display: flex;
  min-height: 0;
  flex-direction: column;
  padding: 14px;
}

.job-browser-head {
  justify-content: space-between;
  gap: 8px;
}

.job-browser-head > span {
  display: grid;
  min-width: 25px;
  height: 25px;
  place-items: center;
  border-radius: 8px;
  color: var(--muted);
  background: var(--surface-soft);
  font-size: var(--font-caption);
}

.job-filters {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 3px;
  margin-top: 12px;
  padding: 3px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface-soft);
}

.job-filters button {
  padding: 6px 3px;
  border-radius: 6px;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
  font-size: var(--font-micro);
}

.job-filters button.active {
  color: var(--text);
  background: var(--surface-hover);
}

.job-list {
  display: grid;
  gap: 6px;
  margin-top: 10px;
}

.job-card {
  display: grid;
  gap: 7px;
  width: 100%;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 11px;
  color: var(--text);
  background: var(--surface-soft);
  cursor: pointer;
  text-align: left;
  transition:
    border-color 160ms ease,
    background 160ms ease,
    transform 160ms ease;
}

.job-card:hover,
.job-card.active {
  border-color: rgba(168, 246, 209, 0.23);
  background: var(--surface-hover);
  transform: translateX(2px);
}

.job-card-top,
.job-card-meta {
  justify-content: space-between;
  gap: 8px;
}

.job-card-top > small,
.job-card-meta small {
  color: var(--muted-strong);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: var(--font-micro);
}

.job-card > strong {
  overflow: hidden;
  font-size: var(--font-small);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.job-progress-row {
  gap: 7px;
}

.job-progress-row > span {
  height: 3px;
  flex: 1;
  overflow: hidden;
  border-radius: 999px;
  background: var(--line);
}

.job-progress-row > span i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--mint), var(--lilac));
}

.job-progress-row small,
.job-card-meta strong {
  font-size: var(--font-micro);
}

.status-pill {
  display: inline-flex;
  width: fit-content;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  border-radius: 999px;
  font-size: var(--font-micro);
  font-weight: 720;
}

.status-pill.is-succeeded,
.status-pill.is-running {
  color: var(--success);
  background: rgba(112, 228, 183, 0.08);
}

.status-pill.is-queued,
.status-pill.is-cancelling {
  color: var(--warning);
  background: var(--warning-soft);
}

.status-pill.is-failed,
.status-pill.is-cancelled {
  color: var(--danger);
  background: var(--danger-soft);
}

.job-empty {
  display: grid;
  flex: 1;
  place-items: center;
  align-content: center;
  gap: 7px;
  color: var(--muted);
  text-align: center;
}

.job-empty i {
  color: var(--mint);
  font-size: 16px;
}

.job-empty strong {
  color: var(--text);
  font-size: var(--font-small);
}

.job-empty p {
  font-size: var(--font-micro);
  line-height: 1.4;
}

.experiment-panel {
  min-width: 0;
  overflow: hidden;
}

.experiment-empty {
  display: grid;
  min-height: 560px;
  place-items: center;
  align-content: center;
  gap: 9px;
  color: var(--muted);
  text-align: center;
}

.experiment-empty > span,
.terminal-state > span {
  display: grid;
  width: 48px;
  height: 48px;
  place-items: center;
  border-radius: 15px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.08);
  font-size: 18px;
}

.experiment-empty h2 {
  color: var(--text);
  font-size: 18px;
}

.experiment-empty p {
  font-size: var(--font-small);
}

.experiment-header {
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 17px;
  border-bottom: 1px solid var(--line);
}

.experiment-title-row {
  gap: 8px;
}

.experiment-title-row code {
  color: var(--muted-strong);
  font-size: var(--font-micro);
}

.experiment-header h2 {
  margin-top: 8px;
  font-size: 20px;
}

.experiment-header p {
  margin-top: 5px;
  color: var(--muted);
  font-size: var(--font-micro);
}

.experiment-actions {
  gap: 6px;
}

.icon-button {
  display: grid;
  width: 38px;
  height: 38px;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: 10px;
  color: var(--muted);
  background: var(--surface);
  cursor: pointer;
}

.live-progress {
  margin: 13px 15px 0;
  padding: 12px;
  border: 1px solid rgba(112, 228, 183, 0.18);
  border-radius: 12px;
  background: rgba(112, 228, 183, 0.05);
}

.live-progress > div {
  justify-content: space-between;
  gap: 10px;
  color: var(--success);
  font-size: var(--font-small);
}

.live-progress > div span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.progress-track {
  display: block;
  height: 6px;
  margin-top: 9px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--line);
}

.progress-track i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--mint), var(--lilac));
  transition: width 300ms ease;
}

.live-progress p {
  margin-top: 6px;
  color: var(--muted);
  font-size: var(--font-micro);
}

.job-model-snapshot {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 7px;
  margin: 13px 15px 0;
}

.job-model-snapshot article {
  min-width: 0;
  padding: 9px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface-soft);
}

.job-model-snapshot small,
.job-model-snapshot strong,
.job-model-snapshot span {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.job-model-snapshot small {
  color: var(--muted);
  font-size: var(--font-micro);
  text-transform: uppercase;
}

.job-model-snapshot strong {
  margin-top: 3px;
  font-size: var(--font-caption);
}

.job-model-snapshot span {
  margin-top: 3px;
  color: var(--muted-strong);
  font-size: var(--font-micro);
}

.experiment-tabs {
  display: flex;
  gap: 4px;
  margin-top: 13px;
  padding: 0 15px;
  border-bottom: 1px solid var(--line);
}

.experiment-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 11px;
  border-bottom: 2px solid transparent;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
  font-size: var(--font-small);
  font-weight: 680;
}

.experiment-tabs button.active {
  border-bottom-color: var(--mint);
  color: var(--text);
}

.experiment-tabs button span {
  padding: 2px 5px;
  border-radius: 999px;
  background: var(--surface-hover);
  font-size: var(--font-micro);
}

.report-view,
.cases-view {
  padding: 14px 15px 17px;
}

.report-verdict {
  gap: 11px;
  padding: 13px;
  border: 1px solid rgba(112, 228, 183, 0.2);
  border-radius: 13px;
  background: rgba(112, 228, 183, 0.05);
}

.report-verdict.is-fail {
  border-color: rgba(255, 105, 122, 0.22);
  background: var(--danger-soft);
}

.report-verdict > span {
  display: grid;
  width: 37px;
  height: 37px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 11px;
  background: rgba(112, 228, 183, 0.09);
}

.report-verdict.is-fail > span {
  background: rgba(255, 105, 122, 0.09);
}

.report-verdict > div {
  min-width: 0;
  flex: 1;
}

.report-verdict small,
.report-verdict strong,
.report-verdict p {
  display: block;
}

.report-verdict small {
  font-size: var(--font-micro);
  text-transform: uppercase;
}

.report-verdict strong {
  margin-top: 2px;
  font-size: 16px;
}

.report-verdict p,
.report-verdict time {
  margin-top: 3px;
  color: var(--muted);
  font-size: var(--font-micro);
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 7px;
  margin-top: 10px;
}

.metric-grid article {
  position: relative;
  overflow: hidden;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--surface-soft);
}

.metric-grid article > span,
.metric-grid article > strong,
.metric-grid article > small {
  display: block;
}

.metric-grid article > span {
  color: var(--muted);
  font-size: var(--font-micro);
}

.metric-grid article > strong {
  margin-top: 6px;
  font-size: 20px;
}

.metric-grid article > small {
  margin-top: 3px;
  color: var(--muted-strong);
  font-size: var(--font-micro);
}

.metric-grid article > i {
  position: absolute;
  bottom: 0;
  left: 0;
  height: 2px;
  background: var(--muted);
}

.metric-grid article.is-good > strong {
  color: var(--success);
}

.metric-grid article.is-good > i {
  background: var(--success);
}

.metric-grid article.is-warning > strong {
  color: var(--warning);
}

.metric-grid article.is-warning > i {
  background: var(--warning);
}

.metric-grid article.is-bad > strong {
  color: var(--danger);
}

.metric-grid article.is-bad > i {
  background: var(--danger);
}

.analysis-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(280px, 0.85fr);
  gap: 8px;
  margin-top: 10px;
}

.trend-card,
.gate-card,
.unavailable-metrics {
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-soft);
}

.card-heading {
  justify-content: space-between;
  gap: 10px;
}

.card-heading h3 {
  margin-top: 3px;
  font-size: var(--font-body);
}

.card-heading select {
  min-height: 31px;
  max-width: 180px;
}

.card-heading > span {
  color: var(--muted);
  font-size: var(--font-micro);
}

.trend-chart {
  display: grid;
  grid-template-columns: 42px minmax(0, 1fr);
  margin-top: 13px;
}

.trend-scale {
  display: flex;
  justify-content: space-between;
  flex-direction: column;
  padding: 2px 7px 15px 0;
  color: var(--muted-strong);
  font-size: var(--font-micro);
}

.trend-chart svg {
  width: 100%;
  height: 130px;
  overflow: visible;
}

.trend-chart line {
  stroke: var(--line);
  stroke-width: 0.4;
}

.trend-chart polyline {
  fill: none;
  stroke: var(--mint);
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.4;
  vector-effect: non-scaling-stroke;
}

.trend-chart circle {
  fill: var(--surface-strong);
  stroke: var(--mint);
  stroke-width: 0.8;
  vector-effect: non-scaling-stroke;
}

.trend-labels {
  grid-column: 2;
  display: flex;
  justify-content: space-between;
  gap: 3px;
  color: var(--muted-strong);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: var(--font-micro);
}

.gate-list {
  display: grid;
  gap: 5px;
  max-height: 175px;
  margin-top: 10px;
  overflow-y: auto;
}

.gate-list article {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface);
}

.gate-icon {
  display: grid;
  width: 23px;
  height: 23px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 7px;
  font-size: var(--font-micro);
}

.gate-icon.is-passed {
  color: var(--success);
  background: rgba(112, 228, 183, 0.08);
}

.gate-icon.is-failed {
  color: var(--danger);
  background: var(--danger-soft);
}

.gate-icon.is-skipped {
  color: var(--muted);
  background: var(--surface-hover);
}

.gate-list article > div {
  min-width: 0;
  flex: 1;
}

.gate-list strong,
.gate-list p {
  display: block;
}

.gate-list strong {
  font-size: var(--font-micro);
}

.gate-list p {
  margin-top: 2px;
  overflow: hidden;
  color: var(--muted);
  font-size: var(--font-micro);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.gate-list article > small {
  color: var(--text-soft);
  font-size: var(--font-micro);
}

.mini-empty {
  display: grid;
  min-height: 130px;
  place-items: center;
  align-content: center;
  gap: 7px;
  color: var(--muted);
  font-size: var(--font-caption);
}

.unavailable-metrics {
  margin-top: 10px;
}

.unavailable-metrics summary {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: var(--muted);
  cursor: pointer;
  font-size: var(--font-caption);
  list-style: none;
}

.unavailable-metrics summary::-webkit-details-marker {
  display: none;
}

.unavailable-metrics summary small {
  padding: 2px 5px;
  border-radius: 999px;
  background: var(--surface-hover);
}

.unavailable-metrics > div {
  display: grid;
  gap: 5px;
  margin-top: 10px;
}

.unavailable-metrics p {
  display: grid;
  grid-template-columns: minmax(130px, 0.3fr) minmax(0, 1fr);
  gap: 8px;
  padding: 7px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: var(--font-micro);
}

.unavailable-metrics strong {
  color: var(--text-soft);
}

.terminal-state,
.case-empty {
  display: grid;
  min-height: 300px;
  place-items: center;
  align-content: center;
  gap: 8px;
  color: var(--muted);
  text-align: center;
}

.terminal-state.is-error > span {
  color: var(--danger);
  background: var(--danger-soft);
}

.terminal-state h3,
.case-empty strong {
  color: var(--text);
  font-size: var(--font-body);
}

.terminal-state p,
.case-empty p {
  max-width: 500px;
  font-size: var(--font-caption);
  line-height: 1.5;
}

.terminal-state small {
  color: var(--danger);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: var(--font-micro);
}

.case-toolbar {
  gap: 7px;
  margin-bottom: 10px;
}

.case-search {
  display: flex;
  min-height: 37px;
  min-width: 190px;
  flex: 1;
  align-items: center;
  gap: 7px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  color: var(--muted);
  background: var(--surface-soft);
}

.case-search input {
  min-width: 0;
  flex: 1;
  border: 0;
  outline: 0;
  color: var(--text);
  background: transparent;
  font-size: var(--font-caption);
}

.case-toolbar > span {
  color: var(--muted);
  font-size: var(--font-micro);
}

.case-empty {
  min-height: 300px;
}

.case-empty > i {
  color: var(--mint);
  font-size: 18px;
}

.case-table-wrap {
  overflow-x: auto;
}

.case-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
}

.case-table th {
  padding: 8px 7px;
  border-bottom: 1px solid var(--line);
  color: var(--muted-strong);
  font-size: var(--font-micro);
  font-weight: 760;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.case-table td {
  padding: 9px 7px;
  border-bottom: 1px solid var(--line);
  color: var(--text-soft);
  font-size: var(--font-caption);
  vertical-align: middle;
}

.case-table tr.expanded > td {
  border-bottom-color: transparent;
  background: var(--surface-soft);
}

.case-table td:first-child strong,
.case-table td:first-child small {
  display: block;
}

.case-table td:first-child strong {
  color: var(--text);
  font-size: var(--font-caption);
}

.case-table td:first-child small {
  margin-top: 2px;
  color: var(--muted-strong);
  font-size: var(--font-micro);
}

.question-cell {
  display: block;
  min-width: 170px;
  max-width: 330px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.case-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--font-micro);
}

.case-status.is-completed {
  color: var(--success);
}

.case-status.is-running,
.case-status.is-queued {
  color: var(--warning);
}

.case-status.is-failed,
.case-status.is-cancelled {
  color: var(--danger);
}

.case-table td:last-child button {
  display: grid;
  width: 27px;
  height: 27px;
  place-items: center;
  border-radius: 8px;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
}

.case-table td:last-child button:hover {
  color: var(--text);
  background: var(--surface-hover);
}

.case-details-row td {
  padding: 0 7px 10px;
}

.case-details {
  display: grid;
  gap: 10px;
  padding: 13px;
  border: 1px solid var(--line);
  border-radius: 0 0 12px 12px;
  background: var(--surface-soft);
}

.detail-label {
  display: block;
  margin-bottom: 5px;
  color: var(--mint);
  font-size: var(--font-micro);
  font-weight: 760;
  letter-spacing: 0.09em;
  text-transform: uppercase;
}

.case-details section > p {
  color: var(--text-soft);
  font-size: var(--font-caption);
  line-height: 1.65;
  white-space: pre-wrap;
}

.judge-reason {
  padding: 9px;
  border-left: 2px solid var(--lilac);
  border-radius: 0 8px 8px 0;
  background: rgba(200, 185, 255, 0.05);
}

.case-detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.45fr) minmax(210px, 0.55fr);
  gap: 9px;
}

.detail-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 5px;
}

.detail-metrics > span {
  padding: 7px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}

.detail-metrics small,
.detail-metrics strong {
  display: block;
}

.detail-metrics small {
  overflow: hidden;
  color: var(--muted);
  font-size: var(--font-micro);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-metrics strong {
  margin-top: 4px;
  color: var(--text);
  font-size: var(--font-small);
}

.execution-facts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 5px;
}

.execution-facts div {
  padding: 7px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}

.execution-facts dt {
  color: var(--muted);
  font-size: var(--font-micro);
}

.execution-facts dd {
  margin-top: 3px;
  overflow: hidden;
  color: var(--text);
  font-size: var(--font-micro);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.identity-list {
  display: grid;
  gap: 5px;
}

.identity-list article {
  display: grid;
  grid-template-columns: 25px minmax(0, 1fr);
  gap: 7px;
  padding: 7px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
}

.identity-list article > span {
  display: grid;
  width: 23px;
  height: 23px;
  place-items: center;
  border-radius: 7px;
  color: var(--lilac);
  background: rgba(200, 185, 255, 0.07);
  font-size: var(--font-micro);
}

.identity-list dl {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  gap: 7px 13px;
}

.identity-list dl div {
  min-width: 100px;
}

.identity-list dt,
.identity-list dd {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.identity-list dt {
  color: var(--muted);
  font-size: var(--font-micro);
}

.identity-list dd {
  max-width: 280px;
  margin-top: 2px;
  color: var(--text-soft);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: var(--font-micro);
}

.identity-empty {
  color: var(--muted) !important;
  font-size: var(--font-micro) !important;
}

.modal-backdrop {
  position: fixed;
  z-index: 1200;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(3, 5, 12, 0.72);
  backdrop-filter: blur(12px);
}

.dataset-modal {
  width: min(760px, 100%);
  max-height: min(840px, calc(100vh - 40px));
  overflow-y: auto;
  border: 1px solid var(--line-strong);
  border-radius: 22px;
  background: var(--surface-strong);
  box-shadow: 0 28px 90px rgba(0, 0, 0, 0.42);
}

.dataset-modal header {
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding: 21px 22px 17px;
  border-bottom: 1px solid var(--line);
}

.dataset-modal header h2 {
  margin-top: 4px;
  font-size: 18px;
}

.dataset-modal header p {
  margin-top: 6px;
  color: var(--muted);
  font-size: var(--font-small);
}

.dataset-modal header > button {
  display: grid;
  width: 32px;
  height: 32px;
  place-items: center;
  border-radius: 9px;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
}

.import-body {
  padding: 18px 22px;
}

.import-toolbar {
  align-items: stretch;
  gap: 8px;
}

.file-picker {
  display: flex;
  min-width: 0;
  flex: 1;
  align-items: center;
  gap: 10px;
  padding: 10px;
  border: 1px dashed rgba(168, 246, 209, 0.27);
  border-radius: 11px;
  color: var(--mint);
  background: rgba(168, 246, 209, 0.04);
  cursor: pointer;
}

.file-picker > span {
  min-width: 0;
  flex: 1;
}

.file-picker strong,
.file-picker small {
  display: block;
}

.file-picker strong {
  font-size: var(--font-small);
}

.file-picker small {
  margin-top: 3px;
  overflow: hidden;
  color: var(--muted);
  font-size: var(--font-micro);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-picker input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}

.import-toolbar > button {
  padding: 0 12px;
  border: 1px solid var(--line);
  border-radius: 11px;
  color: var(--text-soft);
  background: var(--surface);
  cursor: pointer;
  font-size: var(--font-caption);
}

.dataset-editor {
  display: grid;
  gap: 7px;
  margin-top: 13px;
}

.dataset-editor > span {
  color: var(--text-soft);
  font-size: var(--font-caption);
  font-weight: 680;
}

.dataset-editor textarea {
  width: 100%;
  min-height: 310px;
  resize: vertical;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 12px;
  outline: 0;
  color: var(--text);
  background: #0d101a;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: var(--font-small);
  line-height: 1.55;
}

html[data-theme='light'] .dataset-editor textarea {
  background: rgba(239, 243, 249, 0.9);
}

.dataset-editor textarea:focus {
  border-color: rgba(168, 246, 209, 0.35);
}

.dataset-preview {
  gap: 9px;
  margin-top: 11px;
  padding: 9px 10px;
  border: 1px solid rgba(112, 228, 183, 0.18);
  border-radius: 10px;
  color: var(--success);
  background: rgba(112, 228, 183, 0.05);
}

.dataset-preview > div:nth-child(2) {
  min-width: 0;
  flex: 1;
}

.dataset-preview strong,
.dataset-preview p,
.dataset-preview small {
  display: block;
}

.dataset-preview strong {
  font-size: var(--font-small);
}

.dataset-preview p,
.dataset-preview small {
  margin-top: 2px;
  color: var(--muted);
  font-size: var(--font-micro);
}

.dataset-preview > div:not(:nth-child(2)) {
  min-width: 64px;
  padding-left: 10px;
  border-left: 1px solid var(--line);
}

.form-error {
  margin-top: 10px;
  padding: 9px 10px;
  border: 1px solid rgba(255, 105, 122, 0.3);
  border-radius: 9px;
  color: var(--danger);
  background: var(--danger-soft);
  font-size: var(--font-small);
}

.dataset-modal footer {
  justify-content: space-between;
  gap: 13px;
  padding: 14px 22px 19px;
  border-top: 1px solid var(--line);
}

.dataset-modal footer > span {
  color: var(--muted);
  font-size: var(--font-micro);
}

.dataset-modal footer > div {
  display: flex;
  gap: 8px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}

@media (max-width: 1420px) {
  .launch-panel {
    grid-template-columns: minmax(180px, 0.55fr) minmax(460px, 1.45fr);
  }

  .runtime-checks {
    grid-column: 1 / -1;
    padding: 12px 0 0;
    border-top: 1px solid var(--line);
    border-left: 0;
  }

  .runtime-model-grid {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}

@media (max-width: 1080px) {
  .overview-grid,
  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .launch-panel {
    grid-template-columns: 1fr;
  }

  .runtime-checks {
    grid-column: auto;
  }

  .workbench-grid {
    grid-template-columns: 210px minmax(0, 1fr);
  }

  .analysis-grid,
  .case-detail-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 820px) {
  .evaluation-page {
    padding: 17px 13px;
  }

  .workspace-header,
  .dataset-modal footer {
    align-items: stretch;
    flex-direction: column;
  }

  .workspace-actions,
  .dataset-modal footer > div {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }

  .launch-form {
    display: grid;
    grid-template-columns: 1fr;
  }

  .runtime-model-grid,
  .job-model-snapshot {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .workbench-grid {
    grid-template-columns: 1fr;
  }

  .job-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .detail-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 560px) {
  .overview-grid,
  .metric-grid,
  .runtime-model-grid,
  .job-model-snapshot,
  .job-list {
    grid-template-columns: 1fr;
  }

  .case-toolbar,
  .import-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .case-toolbar select,
  .import-toolbar > button {
    width: 100%;
    min-height: 37px;
  }

  .dataset-preview {
    align-items: flex-start;
    flex-wrap: wrap;
  }
}
</style>
