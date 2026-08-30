# Frontend AGENTS.md

本文件适用于 `frontend/`，并继承仓库根目录 `AGENTS.md`。同级 `CLAUDE.md` 仅导入本文件。修改 `src/events/` 时继续读取 `src/events/AGENTS.md`；跨端字段变化先读取 `../contracts/AGENTS.md`。

## 技术栈

- Vue 3.5、TypeScript 5.9、Pinia、Axios、Vite 8、Sass。
- Markdown/代码展示使用现有 `marked`、DOMPurify、Highlight.js 路径；不要新增未经清洗的 `v-html`。
- 测试：Vitest + jsdom；浏览器 E2E：Playwright Chromium。
- Node：`^20.19.0 || >=22.12.0`；CI 使用 Node 22；包管理器为锁定的 npm 10。

## 命令

```bash
cd frontend
npm ci
npm run dev

# 聚焦/常规质量
npm run format:check
npm run lint
npm run typecheck
npm run test:unit
npm run build:check

# 浏览器流程
npm run test:e2e:install
npm run test:e2e
```

依赖必须通过 npm 更新 `package.json` 与 `package-lock.json`，不要手改 lockfile。生产依赖变更运行 `npm audit --omit=dev --audit-level=high`。

## 目录与状态所有权

```text
frontend/src/
├── App.vue                     # 根 Shell 与按需页面
├── auth/session.ts             # 内存 Access Token、refresh 协调、退出撤销
├── stores/auth.ts              # 登录主体和认证视图状态
├── stores/threads.ts           # Thread 列表、切换、删除
├── stores/runs.ts              # durable Run、cursor、重放、HITL、取消
├── stores/chat.ts              # Run Event → assistant Message/RAG 步骤投影
├── stores/documents.ts         # Document 与构建/清理 Job
├── stores/models.ts            # Model control plane
├── stores/capabilityAdmin.ts   # Skill/Tool 管理状态
├── stores/evaluations.ts       # Dataset/Job/Case/趋势
├── events/runEventStream.ts    # Event v1 SSE 解析和恢复
├── events/runEventReducer.ts   # sequence 验证、去重与确定性投影
├── runs/runClient.ts           # Run HTTP Interface
├── threads/                    # Thread ID 与 client helpers
├── capabilities/              # 能力目录客户端与类型
├── components/                # 页面/组件
├── types/generated/           # 从 contracts 生成，禁止手改
└── utils/                     # API/公开错误等通用 helper
```

Server state、认证和长生命周期 timer 由 Store/专用 Module 拥有。组件负责渲染和用户事件，不在多个组件中复制 refresh、SSE、轮询、重试或取消状态机。

## 浏览器认证

- Access Token 只保存在 `auth/session.ts` 的内存状态，通过 `Authorization: Bearer` 使用。
- 页面恢复调用 `/auth/refresh`，Refresh Token 只由 HttpOnly Cookie 携带。
- 允许 `sessionStorage` 保存的只是“待撤销 tombstone”等非 credential 协调标志；不得保存 token。
- 同标签页 refresh 使用共享 Promise；支持 Web Locks 时跨标签串行。获得锁后重检 generation、tombstone、旧 token 和 username subject。
- Axios 对 401 最多重试一次，并且只在请求仍属于同一 access token/username 时使用刷新结果。
- 登录主体变化或退出时清理 Thread、Run、Document、Model、Capability、Evaluation 状态以及 timer/AbortController；不能让 A 用户请求落入 B 用户状态。

后端认证协议变化同时读取 `../backend/auth/AGENTS.md` 并补前后端竞态测试。

## Thread、Run 与 Event 数据流

正式流程是：

```text
create/reserve Run
→ 接收 Event v1 SSE
→ 校验 run_id/thread_id/schema/sequence
→ reducer 成功投影
→ 推进 cursor
→ Message/Timeline/Artifact 视图
```

- 使用 `/v1/threads/{thread_id}/runs/stream` 创建并取得 `X-Run-ID`、`X-Thread-Version`；恢复时使用 `Last-Event-ID`。
- Event 必须连续。重复 sequence 去重；缺口触发可重试协议错误/补放，不能跳过后继续推进 cursor。
- 只有 `onEvent`/reducer 成功后才更新 `lastSequence` 和持久 cursor。
- `run.completed`、`run.failed`、`run.cancelled` 是 terminal；`message.completed` 负责 assistant Message 正文/状态，两者不可互相替代。
- 关闭 reader、AbortController、切换页面或浏览器断网只停止观察，不取消后端 Run。
- “停止”操作先请求后端 cancel，UI 可显示 `cancelling`，但必须等待权威 terminal Event 才显示 cancelled。
- HITL 清除/恢复的是同一 Run；不要把补充答案作为新普通消息创建第二个 Run。

## Event 投影

`src/events/AGENTS.md` 拥有具体协议规则。跨端 Event 类型或 data 形状变化必须：

1. 修改 `../contracts/run_event_v1.json` 或新增 schema version；
2. 运行生成器；
3. 更新 parser、reducer、store/组件；
4. 增加后端 contract/event 测试和前端 stream/reducer 测试。

未知 Event 类型可以记录以便前向观察，但不能破坏已知状态；未知 schema version 应 fail-closed。

## Store 与组件约定

- Store action 负责请求生命周期、并发去重、AbortController、timer 和 typed error；组件不直接散落 fetch/axios 状态机。
- 账号作用域 Store 必须提供清理入口，并在 auth subject 变化时调用。
- 组件 props/emits 和跨模块对象使用明确 TypeScript 类型；避免 `any`、双重断言和把后端原始 JSON 直接渲染。
- 使用现有公开错误映射；用户文案不显示 endpoint、原始异常、Secret、policy 细节或模型私有推理。
- Thinking/Retrieval 组件只展示公开 RAG Trace：路线、阶段、来源、评分、降级与耗时，不展示 chain of thought。
- Tool timeline 正常 `ALLOW` 不作为告警；仅展示可操作的拒绝、待审批、失败和显式降级。

## HTML、Markdown 与 URL 安全

- 复用现有 Markdown sanitizer 和 URL 处理路径；禁止直接把模型/Tool/文档 HTML 交给 `v-html`。
- 外部 URL、artifact URI 和下载地址由后端授权身份产生；前端不从任意字符串拼接受保护下载或 fetch 目标。
- 不把服务器绝对路径、opaque internal URI 或 raw Tool argument 暴露给用户。
- 新窗口链接使用适当 `rel`；下载和 artifact 视图仍需服务端鉴权。

## 性能与构建

`npm run build:check` 同时执行类型检查、Vite build 和 bundle budget。当前门禁限制入口 JS、单 chunk、stylesheet 和初始 gzip 总量。失败时优先修复静态 import、路由 ownership、代码分割或语言包；不要仅调高阈值。

管理员控制面和重页面应继续按需加载；不要把模型中心、Capability 管理或 Evaluation Workbench 拉回首屏 bundle。

## 测试策略

- 纯状态/协议逻辑使用同目录 `*.spec.ts`，覆盖成功、畸形输入、重复/缺口、终态与竞态。
- Store 测试使用 fake API/timer，验证账号切换、轮询取消、重试次数和 stale response。
- 组件测试验证用户可见行为，不依赖私有实现细节。
- Playwright E2E 使用 route mock，覆盖未登录 Shell 与 `create Run → Event stream → reducer → assistant Message` 的真实浏览器投影，不依赖本地模型/数据库/Redis/Milvus。
- 不用随意 `setTimeout` 修复 flaky；等待明确 DOM、Store 或 Promise 条件，并使用 fake timers 处理退避。

## 按变更类型验证

| 变更 | 最低验证 |
| --- | --- |
| Auth/session | `session.spec.ts` + auth Store/401 测试 |
| Event/Run | `runEventStream.spec.ts` + `runEventReducer.spec.ts` + Run Store |
| Store | 对应 Store 单测，含 cleanup/stale response |
| 组件 | 组件单测 + typecheck；关键流程加 E2E |
| 样式/路由/依赖 | format/lint/typecheck/build:check |
| Contract | 生成器 + 前后端 consumer 测试 |

## 文档更新

用户入口、操作方式或控制面行为变化更新 `../README.md`。前端数据流、命令、Store ownership 或安全约束变化才更新本文件；不要在 AGENTS 中复制组件清单的每个细节。

## Code Review Rules

### 本地状态冒充服务端事实

- 阻止 UI 在 Event 前把 Run/Message 标记 terminal、把断流当取消、或在重连时清空权威内容。
  安全路径：Event reducer 单向投影，cursor 重放，cancel 仅显示过渡状态。

### 跨账号 stale response

- 阻止旧账号的 refresh、401 retry、轮询或 Event 回调写入新账号 Store。
  安全路径：generation/subject/token 检查、AbortController、Store cleanup 和竞态测试。

### 协议被宽松解析

- 阻止缺 schema/run/thread/sequence 的 Event 被接受、缺口后继续推进 cursor、或 unknown schema 猜测解析。
  安全路径：边界严格验证，已知 version 内对 unknown type 前向兼容。

### 未清洗内容渲染

- 阻止 raw model/Tool/Document HTML、任意 URL 或内部路径进入 DOM。
  安全路径：现有 sanitizer、服务端授权 URI 和用户可见脱敏模型。
