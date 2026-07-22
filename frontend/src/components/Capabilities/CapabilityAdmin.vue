<template>
  <div class="capability-admin-page">
    <header class="admin-header">
      <div>
        <span class="eyebrow">Capability control plane</span>
        <h1>Skill 与 Tool 管理</h1>
        <p>配置内建与自定义 Skill、受限 HTTPS Tool，以及 SQL Assistant 和网页检索。</p>
      </div>
      <div class="header-actions">
        <button type="button" class="secondary-button" :disabled="store.loading" @click="refresh">
          <i class="fa-solid fa-rotate" :class="{ 'fa-spin': store.loading }"></i>
          刷新
        </button>
        <button type="button" class="primary-button" @click="openCreateSkill">
          <i class="fa-solid fa-plus"></i>
          新建 Skill
        </button>
        <button type="button" class="primary-button is-lilac" @click="openCreateTool">
          <i class="fa-solid fa-plug"></i>
          新建 Tool
        </button>
      </div>
    </header>

    <div v-if="store.error" class="admin-alert is-error" role="alert">
      <i class="fa-solid fa-triangle-exclamation"></i>
      <span>{{ store.error }}</span>
      <button type="button" @click="refresh">重试</button>
    </div>
    <div v-else-if="store.notice" class="admin-alert is-success" role="status">
      <i class="fa-solid fa-circle-check"></i>
      <span>{{ store.notice }}</span>
      <button type="button" aria-label="关闭提示" @click="store.clearNotice">
        <i class="fa-solid fa-xmark"></i>
      </button>
    </div>

    <section v-if="store.loading && !store.controlPlane" class="loading-state" role="status">
      <i class="fa-solid fa-spinner fa-spin"></i>
      <strong>正在读取能力控制面</strong>
      <p>同步 Skill、Tool、SQL Assistant 与网页检索配置…</p>
    </section>

    <template v-else-if="store.controlPlane">
      <section class="summary-grid" aria-label="能力控制面摘要">
        <article>
          <span class="summary-icon is-mint"><i class="fa-solid fa-wand-magic-sparkles"></i></span>
          <div>
            <small>Skills</small>
            <strong>{{ store.skills.length }} 个</strong>
            <p>{{ enabledSkillCount }} 个已启用，包含 {{ customSkillCount }} 个自定义 Skill。</p>
          </div>
        </article>
        <article>
          <span class="summary-icon is-lilac"><i class="fa-solid fa-plug-circle-bolt"></i></span>
          <div>
            <small>Custom Tools</small>
            <strong>{{ store.customTools.length }} 个</strong>
            <p>{{ enabledToolCount }} 个已启用；仅允许固定公共 HTTPS JSON Endpoint。</p>
          </div>
        </article>
      </section>

      <section class="provider-grid">
        <article class="provider-card">
          <div class="provider-card-head">
            <span class="provider-icon"><i class="fa-solid fa-globe"></i></span>
            <div>
              <span class="section-kicker">Web research</span>
              <h2>Tavily Keyless</h2>
            </div>
            <button
              type="button"
              :class="['switch-button', { active: store.controlPlane.web_research.enabled }]"
              :aria-pressed="store.controlPlane.web_research.enabled"
              :disabled="store.saving"
              @click="toggleWebResearch"
            >
              <span></span>
              <strong>{{ store.controlPlane.web_research.enabled ? '已启用' : '已停用' }}</strong>
            </button>
          </div>
          <p>
            使用 <code>X-Tavily-Access-Mode: keyless</code>，不需要 API Key。搜索仍经过 DNS
            Pin、SSRF、超时与响应大小边界。
          </p>
          <div class="provider-meta">
            <span><i class="fa-solid fa-key"></i> 无 API Key</span>
            <span><i class="fa-solid fa-shield-halved"></i> Public HTTPS only</span>
          </div>
        </article>

        <article class="provider-card sql-status-card">
          <div class="provider-card-head">
            <span class="provider-icon is-lilac"><i class="fa-solid fa-database"></i></span>
            <div>
              <span class="section-kicker">Private data</span>
              <h2>SQL Assistant</h2>
            </div>
            <span
              :class="[
                'status-pill',
                store.controlPlane.sql_assistant.enabled ? 'is-enabled' : 'is-disabled',
              ]"
            >
              {{ store.controlPlane.sql_assistant.enabled ? '已启用' : '已停用' }}
            </span>
          </div>
          <p>DSN 本身不会写入数据库或返回前端；这里只保存服务端环境变量名称与只读策略。</p>
          <div class="provider-meta">
            <span>
              <i class="fa-solid fa-vault"></i>
              {{ store.controlPlane.sql_assistant.dsn_secret_name }}
            </span>
            <span :class="{ 'is-danger': !store.controlPlane.sql_assistant.dsn_configured }">
              <i class="fa-solid fa-circle"></i>
              {{
                store.controlPlane.sql_assistant.dsn_configured ? 'Secret 已配置' : 'Secret 未配置'
              }}
            </span>
          </div>
        </article>
      </section>

      <section class="admin-section sql-section">
        <div class="section-heading">
          <div>
            <span class="section-kicker">SQL policy</span>
            <h2>SQL Assistant 配置</h2>
            <p>配置独立只读凭据引用、Schema/Table 白名单、敏感列和查询预算。</p>
          </div>
        </div>

        <form class="sql-form" @submit.prevent="saveSqlAssistant">
          <div class="form-grid">
            <label class="toggle-field wide-field">
              <input v-model="sqlDraft.enabled" type="checkbox" />
              <span>
                <strong>启用 SQL Assistant</strong>
                <small>启用前必须在 API/Worker 环境中提供下面指定的 Secret。</small>
              </span>
            </label>

            <label class="form-field">
              <span>DSN Secret 名称</span>
              <input
                v-model.trim="sqlDraft.dsnSecretName"
                type="text"
                maxlength="128"
                placeholder="SQL_ASSISTANT_DSN"
                autocomplete="off"
              />
              <small>例如 ANALYTICS_READER_DSN；前端永远不接收 DSN 值。</small>
            </label>
            <label class="form-field">
              <span>预期数据库角色</span>
              <input
                v-model.trim="sqlDraft.expectedRole"
                type="text"
                maxlength="63"
                placeholder="analytics_reader"
                autocomplete="off"
              />
              <small>用于校验 DSN username 与独立只读角色是否一致。</small>
            </label>
            <label class="form-field">
              <span>允许的 Schemas</span>
              <textarea
                v-model="sqlDraft.allowedSchemas"
                rows="4"
                placeholder="analytics&#10;reporting"
              ></textarea>
              <small>逗号或换行分隔。</small>
            </label>
            <label class="form-field">
              <span>允许的 Tables</span>
              <textarea
                v-model="sqlDraft.allowedTables"
                rows="4"
                placeholder="analytics.orders&#10;analytics.customers"
              ></textarea>
              <small>使用 schema.table；为空时仍受允许 Schema 限制。</small>
            </label>
            <label class="form-field wide-field">
              <span>敏感列</span>
              <textarea
                v-model="sqlDraft.sensitiveColumns"
                rows="3"
                placeholder="analytics.customers.phone&#10;analytics.customers.email"
              ></textarea>
              <small>这些列会被策略层拒绝读取。</small>
            </label>
          </div>

          <details class="advanced-settings">
            <summary>查询与结果预算</summary>
            <div class="budget-grid">
              <label class="form-field">
                <span>Statement Timeout（秒）</span>
                <input
                  v-model.number="sqlDraft.statementTimeoutSeconds"
                  type="number"
                  min="0.001"
                  max="120"
                  step="0.1"
                />
              </label>
              <label class="form-field">
                <span>最大返回行数</span>
                <input
                  v-model.number="sqlDraft.maxRows"
                  type="number"
                  min="1"
                  max="10000"
                  step="1"
                />
              </label>
              <label class="form-field">
                <span>最大结果字节</span>
                <input
                  v-model.number="sqlDraft.maxResultBytes"
                  type="number"
                  min="1024"
                  max="16777216"
                  step="1024"
                />
              </label>
              <label class="form-field">
                <span>最大估算 Cost</span>
                <input
                  v-model.number="sqlDraft.maxEstimatedCost"
                  type="number"
                  min="0.001"
                  max="1000000000"
                  step="1"
                />
              </label>
              <label class="form-field">
                <span>最大估算行数</span>
                <input
                  v-model.number="sqlDraft.maxEstimatedRows"
                  type="number"
                  min="1"
                  max="1000000000"
                  step="1"
                />
              </label>
              <label class="form-field">
                <span>最大估算字节</span>
                <input
                  v-model.number="sqlDraft.maxEstimatedBytes"
                  type="number"
                  min="1024"
                  max="1073741824"
                  step="1024"
                />
              </label>
              <label class="form-field">
                <span>Catalog Cache TTL（秒）</span>
                <input
                  v-model.number="sqlDraft.catalogCacheTtlSeconds"
                  type="number"
                  min="1"
                  max="3600"
                  step="1"
                />
              </label>
            </div>
          </details>

          <p v-if="sqlFormError" class="form-error">{{ sqlFormError }}</p>
          <footer class="form-footer">
            <span>保存后立即应用到当前 Runtime。</span>
            <button type="submit" class="primary-button" :disabled="store.saving">
              <i v-if="store.saving" class="fa-solid fa-spinner fa-spin"></i>
              <i v-else class="fa-solid fa-floppy-disk"></i>
              保存 SQL 配置
            </button>
          </footer>
        </form>
      </section>

      <section class="admin-section">
        <div class="section-heading">
          <div>
            <span class="section-kicker">Skill registry</span>
            <h2>Skills</h2>
            <p>四个内建 Skill 可编辑或停用；自定义 Skill 还可以删除。</p>
          </div>
          <button type="button" class="secondary-button" @click="openCreateSkill">
            <i class="fa-solid fa-plus"></i>
            新建自定义 Skill
          </button>
        </div>

        <div class="table-wrap">
          <table class="admin-table">
            <thead>
              <tr>
                <th>Skill</th>
                <th>Allowed Tools</th>
                <th>访问要求</th>
                <th>状态</th>
                <th><span class="sr-only">操作</span></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="skill in store.skills" :key="skill.name">
                <td>
                  <div class="identity-cell">
                    <span class="identity-icon"
                      ><i class="fa-solid fa-wand-magic-sparkles"></i
                    ></span>
                    <div>
                      <strong>{{ skill.name }}</strong>
                      <small
                        >{{ skill.source === 'builtin' ? '内建' : '自定义' }} · v{{
                          skill.version
                        }}</small
                      >
                      <p>{{ skill.description }}</p>
                    </div>
                  </div>
                </td>
                <td>
                  <div v-if="skill.allowed_tools.length" class="tag-list">
                    <span v-for="tool in skill.allowed_tools" :key="tool">{{ tool }}</span>
                  </div>
                  <span v-else class="muted">无 Tool</span>
                </td>
                <td>
                  <div class="requirement-copy">
                    <span v-if="skill.required_roles.length">
                      Role: {{ skill.required_roles.join(', ') }}
                    </span>
                    <span v-if="skill.required_secrets.length">
                      Secret: {{ skill.required_secrets.join(', ') }}
                    </span>
                    <span v-if="!skill.required_roles.length && !skill.required_secrets.length"
                      >无额外要求</span
                    >
                  </div>
                </td>
                <td>
                  <span :class="['status-pill', skill.enabled ? 'is-enabled' : 'is-disabled']">
                    {{ skill.enabled ? '已启用' : '已停用' }}
                  </span>
                </td>
                <td>
                  <div class="row-actions">
                    <button
                      type="button"
                      :aria-label="`编辑 ${skill.name}`"
                      @click="openEditSkill(skill)"
                    >
                      <i class="fa-regular fa-pen-to-square"></i>
                    </button>
                    <button
                      v-if="skill.source === 'custom'"
                      type="button"
                      class="danger-action"
                      :disabled="store.saving"
                      :aria-label="`删除 ${skill.name}`"
                      @click="removeSkill(skill)"
                    >
                      <i class="fa-regular fa-trash-can"></i>
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="admin-section">
        <div class="section-heading">
          <div>
            <span class="section-kicker">Declarative integrations</span>
            <h2>自定义 HTTPS Tools</h2>
            <p>支持 GET/POST JSON、JSON Schema、静态 Header 与环境 Secret Header 引用。</p>
          </div>
          <button type="button" class="secondary-button" @click="openCreateTool">
            <i class="fa-solid fa-plus"></i>
            新建自定义 Tool
          </button>
        </div>

        <div v-if="!store.customTools.length" class="empty-tools">
          <i class="fa-solid fa-plug-circle-plus"></i>
          <strong>还没有自定义 Tool</strong>
          <p>创建一个固定公共 HTTPS JSON Endpoint，再将它绑定到自定义 Skill。</p>
          <button type="button" class="primary-button" @click="openCreateTool">
            创建第一个 Tool
          </button>
        </div>

        <div v-else class="table-wrap">
          <table class="admin-table tool-table">
            <thead>
              <tr>
                <th>Tool</th>
                <th>Endpoint</th>
                <th>策略</th>
                <th>状态</th>
                <th><span class="sr-only">操作</span></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="tool in store.customTools" :key="tool.name">
                <td>
                  <div class="identity-cell">
                    <span class="identity-icon is-lilac"><i class="fa-solid fa-plug"></i></span>
                    <div>
                      <strong>{{ tool.name }}</strong>
                      <small>{{ tool.group }} · v{{ tool.version }}</small>
                      <p>{{ tool.description }}</p>
                    </div>
                  </div>
                </td>
                <td>
                  <span class="method-chip">{{ tool.method }}</span>
                  <code class="endpoint-code">{{ tool.endpoint }}</code>
                </td>
                <td>
                  <div class="requirement-copy">
                    <span
                      >{{ tool.timeout_seconds }}s ·
                      {{ formatBytes(tool.max_response_bytes) }}</span
                    >
                    <span v-if="tool.requires_approval">需要人工审批</span>
                    <span v-if="tool.required_roles.length"
                      >Role: {{ tool.required_roles.join(', ') }}</span
                    >
                    <span v-if="Object.keys(tool.secret_headers).length">
                      {{ Object.keys(tool.secret_headers).length }} 个 Secret Header
                    </span>
                  </div>
                </td>
                <td>
                  <span :class="['status-pill', tool.enabled ? 'is-enabled' : 'is-disabled']">
                    {{ tool.enabled ? '已启用' : '已停用' }}
                  </span>
                </td>
                <td>
                  <div class="row-actions">
                    <button
                      type="button"
                      :aria-label="`编辑 ${tool.name}`"
                      @click="openEditTool(tool)"
                    >
                      <i class="fa-regular fa-pen-to-square"></i>
                    </button>
                    <button
                      type="button"
                      class="danger-action"
                      :disabled="store.saving"
                      :aria-label="`删除 ${tool.name}`"
                      @click="removeTool(tool)"
                    >
                      <i class="fa-regular fa-trash-can"></i>
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <Teleport to="body">
      <div v-if="skillModalOpen" class="modal-backdrop" @click.self="closeSkillModal">
        <section
          class="admin-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="skill-form-title"
          @keydown.esc="closeSkillModal"
        >
          <header>
            <div>
              <span class="section-kicker">{{ editingSkill ? 'Edit skill' : 'Create skill' }}</span>
              <h2 id="skill-form-title">
                {{ editingSkill ? `编辑 ${editingSkill.name}` : '新建自定义 Skill' }}
              </h2>
              <p>Skill 保存指令与允许使用的 Tool；保存后立即生效。</p>
            </div>
            <button type="button" aria-label="关闭" @click="closeSkillModal">
              <i class="fa-solid fa-xmark"></i>
            </button>
          </header>
          <form @submit.prevent="saveSkill">
            <div class="form-grid">
              <label class="form-field wide-field">
                <span>Skill 名称</span>
                <input
                  v-model.trim="skillName"
                  type="text"
                  maxlength="64"
                  pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
                  placeholder="release-research"
                  :disabled="Boolean(editingSkill)"
                  autocomplete="off"
                />
                <small>小写 kebab-case；创建后不可改名。</small>
              </label>
              <label class="form-field wide-field">
                <span>描述</span>
                <input
                  v-model="skillDraft.description"
                  type="text"
                  maxlength="500"
                  placeholder="说明这个 Skill 何时使用"
                />
              </label>
              <label class="form-field wide-field">
                <span>Skill 指令</span>
                <textarea
                  v-model="skillDraft.instructions"
                  rows="10"
                  placeholder="# Workflow&#10;描述目标、步骤、边界和输出要求。"
                ></textarea>
              </label>
              <label class="form-field">
                <span>Required Roles</span>
                <input v-model="skillDraft.requiredRoles" type="text" placeholder="admin" />
                <small>逗号或换行分隔。</small>
              </label>
              <label class="form-field">
                <span>Required Secrets</span>
                <input
                  v-model="skillDraft.requiredSecrets"
                  type="text"
                  placeholder="OPTIONAL_SECRET_NAME"
                />
                <small>只填写环境变量名称，不填写 Secret 值。</small>
              </label>
            </div>

            <fieldset class="tool-picker">
              <legend>Allowed Tools</legend>
              <label v-for="tool in selectableTools" :key="tool.name">
                <input v-model="skillDraft.allowedTools" type="checkbox" :value="tool.name" />
                <span>
                  <strong>{{ tool.name }}</strong>
                  <small>{{ tool.kind }} · {{ tool.description }}</small>
                </span>
                <em v-if="!tool.enabled">已停用</em>
              </label>
            </fieldset>

            <label class="toggle-field modal-toggle">
              <input v-model="skillDraft.enabled" type="checkbox" />
              <span>
                <strong>启用 Skill</strong>
                <small>保存后立即启用或停用。</small>
              </span>
            </label>

            <p v-if="skillFormError" class="form-error">{{ skillFormError }}</p>
            <footer>
              <button type="button" class="secondary-button" @click="closeSkillModal">取消</button>
              <button type="submit" class="primary-button" :disabled="store.saving">
                <i v-if="store.saving" class="fa-solid fa-spinner fa-spin"></i>
                <i v-else class="fa-solid fa-floppy-disk"></i>
                保存 Skill
              </button>
            </footer>
          </form>
        </section>
      </div>

      <div v-if="toolModalOpen" class="modal-backdrop" @click.self="closeToolModal">
        <section
          class="admin-modal is-wide"
          role="dialog"
          aria-modal="true"
          aria-labelledby="tool-form-title"
          @keydown.esc="closeToolModal"
        >
          <header>
            <div>
              <span class="section-kicker">{{ editingTool ? 'Edit tool' : 'Create tool' }}</span>
              <h2 id="tool-form-title">
                {{ editingTool ? `编辑 ${editingTool.name}` : '新建自定义 HTTPS Tool' }}
              </h2>
              <p>只支持声明式公共 HTTPS JSON 调用，不执行上传的 Python、Shell 或任意服务端代码。</p>
            </div>
            <button type="button" aria-label="关闭" @click="closeToolModal">
              <i class="fa-solid fa-xmark"></i>
            </button>
          </header>
          <form @submit.prevent="saveTool">
            <div class="form-grid">
              <label class="form-field">
                <span>Tool 名称</span>
                <input
                  v-model.trim="toolName"
                  type="text"
                  maxlength="128"
                  pattern="[a-z][a-z0-9_.-]*"
                  placeholder="release_lookup"
                  :disabled="Boolean(editingTool)"
                  autocomplete="off"
                />
                <small>创建后不可改名，且不能覆盖内建 Tool。</small>
              </label>
              <label class="form-field">
                <span>分组</span>
                <input
                  v-model.trim="toolDraft.group"
                  type="text"
                  maxlength="128"
                  placeholder="custom-http"
                />
              </label>
              <label class="form-field wide-field">
                <span>描述</span>
                <input
                  v-model="toolDraft.description"
                  type="text"
                  maxlength="1000"
                  placeholder="说明 Tool 的用途和返回内容"
                />
              </label>
              <label class="form-field endpoint-field">
                <span>固定 HTTPS Endpoint</span>
                <input
                  v-model.trim="toolDraft.endpoint"
                  type="url"
                  maxlength="2048"
                  placeholder="https://api.example-service.com/v1/search"
                />
                <small>必须是公共 FQDN 或全局 IP；禁止凭据、Fragment、私网与特殊用途地址。</small>
              </label>
              <label class="form-field method-field">
                <span>Method</span>
                <select v-model="toolDraft.method">
                  <option value="GET">GET</option>
                  <option value="POST">POST</option>
                </select>
              </label>
              <label class="form-field wide-field">
                <span>Input JSON Schema</span>
                <textarea v-model="toolDraft.inputSchema" rows="10" spellcheck="false"></textarea>
              </label>
              <label class="form-field">
                <span>静态 Headers（JSON）</span>
                <textarea
                  v-model="toolDraft.staticHeaders"
                  rows="5"
                  spellcheck="false"
                  placeholder='{"X-Client":"SuperMew"}'
                ></textarea>
                <small>Authorization、X-API-Key 等敏感 Header 必须使用右侧 Secret 引用。</small>
              </label>
              <label class="form-field">
                <span>Secret Headers（JSON）</span>
                <textarea
                  v-model="toolDraft.secretHeaders"
                  rows="5"
                  spellcheck="false"
                  placeholder='{"Authorization":"RELEASE_API_TOKEN"}'
                ></textarea>
                <small>值是环境变量名称，真实 Secret 不进入控制面。</small>
              </label>
              <label class="form-field">
                <span>Required Roles</span>
                <input v-model="toolDraft.requiredRoles" type="text" placeholder="admin" />
              </label>
              <label class="form-field">
                <span>Timeout（秒）</span>
                <input
                  v-model.number="toolDraft.timeoutSeconds"
                  type="number"
                  min="0.001"
                  max="120"
                  step="0.1"
                />
              </label>
              <label class="form-field">
                <span>最大响应字节</span>
                <input
                  v-model.number="toolDraft.maxResponseBytes"
                  type="number"
                  min="1024"
                  max="8388608"
                  step="1024"
                />
              </label>
            </div>

            <div class="toggle-grid">
              <label class="toggle-field">
                <input v-model="toolDraft.enabled" type="checkbox" />
                <span><strong>启用</strong><small>保存后立即可用。</small></span>
              </label>
              <label class="toggle-field">
                <input v-model="toolDraft.idempotent" type="checkbox" />
                <span><strong>幂等</strong><small>声明重复调用不会产生额外副作用。</small></span>
              </label>
              <label class="toggle-field">
                <input v-model="toolDraft.requiresApproval" type="checkbox" />
                <span
                  ><strong>需要审批</strong><small>每个 Run 必须显式授权后才能调用。</small></span
                >
              </label>
            </div>

            <p v-if="toolFormError" class="form-error">{{ toolFormError }}</p>
            <footer>
              <button type="button" class="secondary-button" @click="closeToolModal">取消</button>
              <button type="submit" class="primary-button" :disabled="store.saving">
                <i v-if="store.saving" class="fa-solid fa-spinner fa-spin"></i>
                <i v-else class="fa-solid fa-floppy-disk"></i>
                保存 Tool
              </button>
            </footer>
          </form>
        </section>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import {
  buildManagedHttpToolPayload,
  buildManagedSkillPayload,
  buildSqlAssistantPayload,
  type HttpToolFormDraft,
  type SkillFormDraft,
  type SqlAssistantFormDraft,
} from '@/capabilities/capabilityForms';
import { useCapabilityAdminStore } from '@/stores/capabilityAdmin';
import type { ManagedHttpTool, ManagedSkill, SqlAssistantConfig } from '@/types/capabilities';

const store = useCapabilityAdminStore();

const skillModalOpen = ref(false);
const editingSkill = ref<ManagedSkill | null>(null);
const skillName = ref('');
const skillFormError = ref('');
const skillDraft = reactive<SkillFormDraft>({
  description: '',
  instructions: '',
  allowedTools: [],
  requiredRoles: '',
  requiredSecrets: '',
  enabled: true,
});

const toolModalOpen = ref(false);
const editingTool = ref<ManagedHttpTool | null>(null);
const toolName = ref('');
const toolFormError = ref('');
const toolDraft = reactive<HttpToolFormDraft>({
  description: '',
  group: 'custom-http',
  endpoint: '',
  method: 'POST',
  inputSchema: JSON.stringify(
    {
      type: 'object',
      properties: { query: { type: 'string', minLength: 1 } },
      required: ['query'],
      additionalProperties: false,
    },
    null,
    2
  ),
  staticHeaders: '{}',
  secretHeaders: '{}',
  requiredRoles: '',
  requiresApproval: false,
  idempotent: true,
  timeoutSeconds: 20,
  maxResponseBytes: 262_144,
  enabled: true,
});

const sqlFormError = ref('');
const sqlDraft = reactive<SqlAssistantFormDraft>({
  enabled: false,
  dsnSecretName: 'SQL_ASSISTANT_DSN',
  expectedRole: '',
  allowedSchemas: '',
  allowedTables: '',
  sensitiveColumns: '',
  statementTimeoutSeconds: 10,
  maxRows: 200,
  maxResultBytes: 262_144,
  maxEstimatedCost: 100_000,
  maxEstimatedRows: 100_000,
  maxEstimatedBytes: 8_388_608,
  catalogCacheTtlSeconds: 300,
});

const enabledSkillCount = computed(() => store.skills.filter((skill) => skill.enabled).length);
const customSkillCount = computed(
  () => store.skills.filter((skill) => skill.source === 'custom').length
);
const enabledToolCount = computed(() => store.customTools.filter((tool) => tool.enabled).length);
const selectableTools = computed(() => {
  if (!store.controlPlane) return [];
  const builtin = store.controlPlane.builtin_tools.map((tool) => ({
    name: tool.name,
    description: tool.description,
    kind: '内建',
    enabled: true,
  }));
  const custom = store.controlPlane.custom_tools.map((tool) => ({
    name: tool.name,
    description: tool.description,
    kind: '自定义',
    enabled: tool.enabled,
  }));
  return [...builtin, ...custom].sort((left, right) => left.name.localeCompare(right.name));
});

function hydrateSql(config: SqlAssistantConfig) {
  Object.assign(sqlDraft, {
    enabled: config.enabled,
    dsnSecretName: config.dsn_secret_name,
    expectedRole: config.expected_role,
    allowedSchemas: config.allowed_schemas.join('\n'),
    allowedTables: config.allowed_tables.join('\n'),
    sensitiveColumns: config.sensitive_columns.join('\n'),
    statementTimeoutSeconds: config.statement_timeout_seconds,
    maxRows: config.max_rows,
    maxResultBytes: config.max_result_bytes,
    maxEstimatedCost: config.max_estimated_cost,
    maxEstimatedRows: config.max_estimated_rows,
    maxEstimatedBytes: config.max_estimated_bytes,
    catalogCacheTtlSeconds: config.catalog_cache_ttl_seconds,
  });
  sqlFormError.value = '';
}

watch(
  () => store.controlPlane?.sql_assistant,
  (config) => {
    if (config) hydrateSql(config);
  },
  { immediate: true }
);

async function refresh() {
  await store.fetchControlPlane().catch(() => undefined);
}

async function toggleWebResearch() {
  if (!store.controlPlane) return;
  await store.updateWebResearch(!store.controlPlane.web_research.enabled).catch(() => undefined);
}

async function saveSqlAssistant() {
  sqlFormError.value = '';
  try {
    const payload = buildSqlAssistantPayload(sqlDraft);
    await store.updateSqlAssistant(payload);
  } catch (error) {
    sqlFormError.value = error instanceof Error ? error.message : 'SQL Assistant 配置无效';
  }
}

function openCreateSkill() {
  editingSkill.value = null;
  skillName.value = '';
  skillFormError.value = '';
  Object.assign(skillDraft, {
    description: '',
    instructions: '# Workflow\n\n描述目标、步骤、边界和输出要求。',
    allowedTools: [],
    requiredRoles: '',
    requiredSecrets: '',
    enabled: true,
  });
  skillModalOpen.value = true;
}

function openEditSkill(skill: ManagedSkill) {
  editingSkill.value = skill;
  skillName.value = skill.name;
  skillFormError.value = '';
  Object.assign(skillDraft, {
    description: skill.description,
    instructions: skill.instructions,
    allowedTools: [...skill.allowed_tools],
    requiredRoles: skill.required_roles.join('\n'),
    requiredSecrets: skill.required_secrets.join('\n'),
    enabled: skill.enabled,
  });
  skillModalOpen.value = true;
}

function closeSkillModal() {
  if (store.saving) return;
  skillModalOpen.value = false;
  editingSkill.value = null;
  skillFormError.value = '';
}

async function saveSkill() {
  skillFormError.value = '';
  const name = skillName.value.trim();
  if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name)) {
    skillFormError.value = 'Skill 名称必须使用小写 kebab-case';
    return;
  }
  try {
    const payload = buildManagedSkillPayload(skillDraft);
    if (editingSkill.value) await store.updateSkill(name, payload);
    else await store.createSkill(name, payload);
    closeSkillModal();
  } catch (error) {
    skillFormError.value = error instanceof Error ? error.message : 'Skill 配置无效';
  }
}

async function removeSkill(skill: ManagedSkill) {
  if (!window.confirm(`确定删除自定义 Skill「${skill.name}」吗？`)) return;
  await store.deleteSkill(skill.name).catch(() => undefined);
}

function resetToolDraft() {
  Object.assign(toolDraft, {
    description: '',
    group: 'custom-http',
    endpoint: '',
    method: 'POST' as const,
    inputSchema: JSON.stringify(
      {
        type: 'object',
        properties: { query: { type: 'string', minLength: 1 } },
        required: ['query'],
        additionalProperties: false,
      },
      null,
      2
    ),
    staticHeaders: '{}',
    secretHeaders: '{}',
    requiredRoles: '',
    requiresApproval: false,
    idempotent: true,
    timeoutSeconds: 20,
    maxResponseBytes: 262_144,
    enabled: true,
  });
}

function openCreateTool() {
  editingTool.value = null;
  toolName.value = '';
  toolFormError.value = '';
  resetToolDraft();
  toolModalOpen.value = true;
}

function openEditTool(tool: ManagedHttpTool) {
  editingTool.value = tool;
  toolName.value = tool.name;
  toolFormError.value = '';
  Object.assign(toolDraft, {
    description: tool.description,
    group: tool.group,
    endpoint: tool.endpoint,
    method: tool.method,
    inputSchema: JSON.stringify(tool.input_schema, null, 2),
    staticHeaders: JSON.stringify(tool.static_headers, null, 2),
    secretHeaders: JSON.stringify(tool.secret_headers, null, 2),
    requiredRoles: tool.required_roles.join('\n'),
    requiresApproval: tool.requires_approval,
    idempotent: tool.idempotent,
    timeoutSeconds: tool.timeout_seconds,
    maxResponseBytes: tool.max_response_bytes,
    enabled: tool.enabled,
  });
  toolModalOpen.value = true;
}

function closeToolModal() {
  if (store.saving) return;
  toolModalOpen.value = false;
  editingTool.value = null;
  toolFormError.value = '';
}

async function saveTool() {
  toolFormError.value = '';
  const name = toolName.value.trim();
  if (!/^[a-z][a-z0-9_.-]{0,127}$/.test(name)) {
    toolFormError.value = 'Tool 名称必须以小写字母开头，只能包含小写字母、数字、点、下划线或连字符';
    return;
  }
  try {
    const payload = buildManagedHttpToolPayload(toolDraft);
    if (editingTool.value) await store.updateTool(name, payload);
    else await store.createTool(name, payload);
    closeToolModal();
  } catch (error) {
    toolFormError.value = error instanceof Error ? error.message : 'Tool 配置无效';
  }
}

async function removeTool(tool: ManagedHttpTool) {
  if (!window.confirm(`确定删除自定义 Tool「${tool.name}」吗？仍被 Skill 引用时服务端会拒绝。`))
    return;
  await store.deleteTool(tool.name).catch(() => undefined);
}

function formatBytes(value: number): string {
  if (value >= 1_048_576) return `${(value / 1_048_576).toFixed(1)} MiB`;
  return `${Math.round(value / 1024)} KiB`;
}

onMounted(() => {
  if (!store.controlPlane && !store.loading) void refresh();
});
</script>

<style scoped>
.capability-admin-page {
  width: 100%;
  height: 100%;
  padding: 24px 28px 42px;
  overflow-y: auto;
}

.admin-header,
.section-heading,
.provider-card-head,
.form-footer,
.admin-modal header,
.admin-modal footer,
.row-actions {
  display: flex;
  align-items: center;
}

.admin-header {
  justify-content: space-between;
  gap: 24px;
}

.eyebrow,
.section-kicker {
  color: var(--mint);
  font-size: var(--font-micro);
  font-weight: 760;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.admin-header h1 {
  margin-top: 5px;
  font-size: clamp(25px, 2.5vw, 34px);
  letter-spacing: -0.04em;
}

.admin-header p,
.section-heading p {
  margin-top: 7px;
  color: var(--muted);
  font-size: var(--font-ui);
  line-height: 1.65;
}

.header-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.primary-button,
.secondary-button {
  display: inline-flex;
  min-height: 39px;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 0 13px;
  border: 1px solid transparent;
  border-radius: 10px;
  cursor: pointer;
  font-size: var(--font-small);
  font-weight: 720;
}

.primary-button {
  color: var(--mint-ink);
  background: var(--mint);
}

.primary-button.is-lilac {
  color: #161126;
  background: var(--lilac);
}

.secondary-button {
  border-color: var(--line);
  color: var(--text-soft);
  background: var(--surface);
}

.secondary-button:hover:not(:disabled) {
  border-color: var(--line-strong);
  color: var(--text);
  background: var(--surface-hover);
}

.admin-alert {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 16px;
  padding: 11px 13px;
  border: 1px solid var(--line);
  border-radius: 12px;
  font-size: var(--font-small);
}

.admin-alert span {
  flex: 1;
}

.admin-alert button {
  color: inherit;
  background: transparent;
  cursor: pointer;
}

.admin-alert.is-error {
  border-color: rgba(255, 105, 122, 0.3);
  color: var(--danger);
  background: var(--danger-soft);
}

.admin-alert.is-success {
  border-color: rgba(112, 228, 183, 0.25);
  color: var(--success);
  background: rgba(112, 228, 183, 0.06);
}

.loading-state,
.empty-tools {
  display: grid;
  min-height: 230px;
  margin-top: 20px;
  place-items: center;
  align-content: center;
  gap: 8px;
  border: 1px dashed var(--line-strong);
  border-radius: 18px;
  color: var(--muted);
  text-align: center;
}

.loading-state > i,
.empty-tools > i {
  color: var(--mint);
  font-size: 22px;
}

.loading-state strong,
.empty-tools strong {
  color: var(--text);
}

.loading-state p,
.empty-tools p {
  font-size: var(--font-small);
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 20px;
}

.summary-grid article {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 12px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--surface);
  box-shadow: var(--card-shadow);
}

.summary-icon,
.provider-icon,
.identity-icon {
  display: grid;
  flex: none;
  place-items: center;
  color: var(--warning);
  background: var(--warning-soft);
}

.summary-icon {
  width: 38px;
  height: 38px;
  border-radius: 12px;
}

.summary-icon.is-mint,
.provider-icon {
  color: var(--mint);
  background: rgba(112, 228, 183, 0.08);
}

.summary-icon.is-lilac,
.provider-icon.is-lilac,
.identity-icon.is-lilac {
  color: var(--lilac);
  background: rgba(200, 185, 255, 0.08);
}

.summary-grid small,
.summary-grid strong,
.summary-grid p {
  display: block;
}

.summary-grid small {
  color: var(--muted);
  font-size: var(--font-micro);
  text-transform: uppercase;
}

.summary-grid strong {
  margin-top: 3px;
  font-size: var(--font-title-sm);
}

.summary-grid p {
  margin-top: 5px;
  color: var(--muted);
  font-size: var(--font-caption);
  line-height: 1.45;
}

.provider-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}

.provider-card,
.admin-section {
  border: 1px solid var(--line);
  background: var(--surface-soft);
  box-shadow: var(--card-shadow);
}

.provider-card {
  padding: 17px;
  border-radius: 17px;
}

.provider-card-head {
  gap: 11px;
}

.provider-icon {
  width: 40px;
  height: 40px;
  border-radius: 13px;
}

.provider-card h2 {
  margin-top: 3px;
  font-size: 17px;
}

.provider-card-head > div {
  flex: 1;
}

.provider-card > p {
  margin-top: 13px;
  color: var(--muted);
  font-size: var(--font-small);
  line-height: 1.6;
}

.provider-card code,
.endpoint-code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.provider-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 13px;
}

.provider-meta span,
.method-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--text-soft);
  background: var(--surface);
  font-size: var(--font-micro);
}

.provider-meta .is-danger {
  color: var(--danger);
}

.provider-meta .fa-circle {
  font-size: 5px;
}

.switch-button {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--muted);
  background: var(--surface);
  cursor: pointer;
  font-size: var(--font-micro);
}

.switch-button > span {
  position: relative;
  width: 28px;
  height: 16px;
  border-radius: 999px;
  background: var(--muted-strong);
}

.switch-button > span::after {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 999px;
  background: white;
  content: '';
  transition: transform 160ms ease;
}

.switch-button.active {
  border-color: rgba(112, 228, 183, 0.24);
  color: var(--success);
}

.switch-button.active > span {
  background: var(--mint-strong);
}

.switch-button.active > span::after {
  transform: translateX(12px);
}

.status-pill {
  display: inline-flex;
  padding: 5px 8px;
  border-radius: 999px;
  font-size: var(--font-micro);
  font-weight: 720;
}

.status-pill.is-enabled {
  color: var(--success);
  background: rgba(112, 228, 183, 0.08);
}

.status-pill.is-disabled {
  color: var(--muted);
  background: var(--surface);
}

.admin-section {
  margin-top: 14px;
  padding: 19px;
  border-radius: 18px;
}

.section-heading {
  justify-content: space-between;
  gap: 18px;
}

.section-heading h2 {
  margin-top: 4px;
  font-size: 20px;
}

.sql-form {
  margin-top: 17px;
}

.form-grid,
.budget-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.form-field {
  display: grid;
  min-width: 0;
  gap: 7px;
}

.form-field.wide-field,
.endpoint-field {
  grid-column: 1 / -1;
}

.method-field {
  grid-column: 1 / -1;
  max-width: 220px;
}

.form-field > span {
  color: var(--text-soft);
  font-size: var(--font-small);
  font-weight: 680;
}

.form-field input,
.form-field select,
.form-field textarea {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 10px;
  outline: 0;
  color: var(--text);
  background: var(--surface);
  font-size: var(--font-ui);
}

.form-field input,
.form-field select {
  min-height: 41px;
  padding: 0 11px;
}

.form-field textarea {
  padding: 10px 11px;
  resize: vertical;
  line-height: 1.55;
}

.form-field input:focus,
.form-field select:focus,
.form-field textarea:focus {
  border-color: rgba(168, 246, 209, 0.38);
}

.form-field small,
.toggle-field small {
  color: var(--muted-strong);
  font-size: var(--font-micro);
  line-height: 1.45;
}

.toggle-field {
  display: flex;
  min-height: 58px;
  align-items: flex-start;
  gap: 10px;
  padding: 11px;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--surface);
  cursor: pointer;
}

.toggle-field.wide-field {
  grid-column: 1 / -1;
}

.toggle-field input,
.tool-picker input {
  margin-top: 3px;
  accent-color: var(--mint-strong);
}

.toggle-field strong,
.toggle-field small {
  display: block;
}

.toggle-field strong {
  color: var(--text-soft);
  font-size: var(--font-small);
}

.advanced-settings {
  margin-top: 13px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-soft);
}

.advanced-settings summary {
  padding: 11px 13px;
  color: var(--text-soft);
  cursor: pointer;
  font-size: var(--font-small);
  font-weight: 700;
}

.budget-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  padding: 0 13px 13px;
}

.form-footer {
  justify-content: space-between;
  gap: 16px;
  margin-top: 15px;
}

.form-footer > span {
  color: var(--muted);
  font-size: var(--font-caption);
  line-height: 1.5;
}

.form-error {
  margin-top: 13px;
  padding: 9px 10px;
  border: 1px solid rgba(255, 105, 122, 0.3);
  border-radius: 9px;
  color: var(--danger);
  background: var(--danger-soft);
  font-size: var(--font-small);
}

.table-wrap {
  margin-top: 16px;
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: 13px;
}

.admin-table {
  width: 100%;
  min-width: 860px;
  border-collapse: collapse;
}

.admin-table th,
.admin-table td {
  padding: 12px 13px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: middle;
}

.admin-table th {
  color: var(--muted);
  background: var(--surface);
  font-size: var(--font-micro);
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.admin-table tbody tr:last-child td {
  border-bottom: 0;
}

.identity-cell {
  display: flex;
  min-width: 250px;
  align-items: flex-start;
  gap: 10px;
}

.identity-icon {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  color: var(--mint);
  background: rgba(112, 228, 183, 0.07);
}

.identity-cell strong,
.identity-cell small,
.identity-cell p {
  display: block;
}

.identity-cell strong {
  font-size: var(--font-ui);
}

.identity-cell small {
  margin-top: 2px;
  color: var(--muted);
  font-size: var(--font-micro);
}

.identity-cell p {
  max-width: 390px;
  margin-top: 4px;
  color: var(--text-soft);
  font-size: var(--font-caption);
  line-height: 1.45;
}

.tag-list {
  display: flex;
  max-width: 330px;
  flex-wrap: wrap;
  gap: 5px;
}

.tag-list span,
.method-chip {
  color: var(--lilac);
  background: rgba(200, 185, 255, 0.06);
}

.tag-list span {
  padding: 4px 6px;
  border: 1px solid rgba(200, 185, 255, 0.16);
  border-radius: 7px;
  font-size: var(--font-micro);
}

.requirement-copy {
  display: grid;
  gap: 3px;
  color: var(--muted);
  font-size: var(--font-caption);
}

.muted {
  color: var(--muted);
  font-size: var(--font-caption);
}

.row-actions {
  justify-content: flex-end;
  gap: 4px;
}

.row-actions button,
.admin-modal header > button {
  display: grid;
  width: 32px;
  height: 32px;
  place-items: center;
  border: 1px solid transparent;
  border-radius: 9px;
  color: var(--muted);
  background: transparent;
  cursor: pointer;
}

.row-actions button:hover:not(:disabled),
.admin-modal header > button:hover {
  border-color: var(--line);
  color: var(--text);
  background: var(--surface-hover);
}

.row-actions .danger-action:hover:not(:disabled) {
  color: var(--danger);
  background: var(--danger-soft);
}

.tool-table td:nth-child(2) {
  max-width: 360px;
}

.endpoint-code {
  display: block;
  margin-top: 6px;
  overflow: hidden;
  color: var(--text-soft);
  font-size: var(--font-caption);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.empty-tools {
  min-height: 190px;
}

.empty-tools .primary-button {
  margin-top: 5px;
}

.modal-backdrop {
  position: fixed;
  z-index: 1300;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 20px;
  background: rgba(3, 5, 12, 0.72);
  backdrop-filter: blur(12px);
}

.admin-modal {
  width: min(720px, 100%);
  max-height: min(880px, calc(100vh - 40px));
  overflow-y: auto;
  border: 1px solid var(--line-strong);
  border-radius: 21px;
  background: var(--surface-strong);
  box-shadow: 0 28px 90px rgba(0, 0, 0, 0.44);
}

.admin-modal.is-wide {
  width: min(900px, 100%);
}

.admin-modal header {
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding: 20px 21px 16px;
  border-bottom: 1px solid var(--line);
}

.admin-modal h2 {
  margin-top: 4px;
  font-size: 19px;
}

.admin-modal header p {
  margin-top: 5px;
  color: var(--muted);
  font-size: var(--font-small);
  line-height: 1.55;
}

.admin-modal form {
  padding: 18px 21px 21px;
}

.admin-modal footer {
  justify-content: flex-end;
  gap: 8px;
  margin-top: 17px;
}

.tool-picker {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 7px;
  max-height: 300px;
  margin-top: 14px;
  padding: 11px;
  overflow-y: auto;
  border: 1px solid var(--line);
  border-radius: 12px;
}

.tool-picker legend {
  padding: 0 5px;
  color: var(--text-soft);
  font-size: var(--font-small);
  font-weight: 680;
}

.tool-picker label {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface-soft);
  cursor: pointer;
}

.tool-picker label > span {
  min-width: 0;
  flex: 1;
}

.tool-picker strong,
.tool-picker small {
  display: block;
}

.tool-picker strong {
  font-size: var(--font-caption);
}

.tool-picker small {
  margin-top: 3px;
  overflow: hidden;
  color: var(--muted);
  font-size: var(--font-micro);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-picker em {
  color: var(--warning);
  font-size: var(--font-micro);
  font-style: normal;
}

.modal-toggle {
  margin-top: 12px;
}

.toggle-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: 13px;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}

@media (max-width: 1180px) {
  .budget-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 820px) {
  .capability-admin-page {
    padding: 17px 13px 32px;
  }

  .admin-header,
  .section-heading,
  .form-footer {
    align-items: stretch;
    flex-direction: column;
  }

  .header-actions {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .summary-grid,
  .provider-grid,
  .form-grid,
  .budget-grid,
  .tool-picker,
  .toggle-grid {
    grid-template-columns: 1fr;
  }

  .form-footer .primary-button {
    width: 100%;
  }
}

@media (max-width: 560px) {
  .header-actions {
    grid-template-columns: 1fr;
  }

  .provider-card-head {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .switch-button,
  .sql-status-card .status-pill {
    margin-left: 51px;
  }
}
</style>
