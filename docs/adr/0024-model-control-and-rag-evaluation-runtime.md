# ADR-0024：持久化模型控制面、运行快照与 RAG 自动评估

- 状态：已接受
- 日期：2026-07-17

## 背景

Answer、Fast、Grader 与 Evaluator 过去由 `MODEL`、`FAST_MODEL`、`GRADE_MODEL` 等环境变量直接决定，部分 RAG Module 还在导入期读取配置并创建模型单例。调用者无法在不重启进程的情况下管理模型，也无法证明一个 Run 在 HITL 恢复、重试或并行子问题期间始终使用同一组模型。

ADR-0014 已建立纯评分 Interface、Dataset fingerprint、baseline 和 GatePolicy，但旧命令行评估仍是进程内同步动作。它缺少 durable job identity、可恢复进度、取消、worker ownership、Case 级结果和面向管理员的产品入口，也没有独立 Evaluator 角色来自动评分生成答案。

## 决策

建立相邻的 Model Control 与 RAG Evaluation 深 Module：

1. `ModelControlService` 是 Model Profile 生命周期、Model Assignment 与 Model Snapshot 的正式 Interface。PostgreSQL 持久化无 Secret 的 Model Profile 和四个角色的当前 Assignment；环境变量只在首次启动且角色尚未分配时提供种子，不再是运行时事实来源。
2. Model Profile 保存 provider、model name、Base URL、timeout、Stream/Structured Output 能力、启用状态和单调 version。`ARK_API_KEY` 仍只由服务端设置读取，不进入数据库、HTTP 响应、Run、Checkpoint、Evaluation Job 或前端状态。
3. Answer 必须支持 Stream；Fast、Grader 与 Evaluator 必须支持 Structured Output。创建 Assignment、修改已分配 Profile 或停用 Profile 时统一验证角色要求；已分配 Profile 不可删除或停用。
4. 每个新 Run 在预留事务中冻结完整 Model Snapshot，并保存 `model_catalog_hash` 与 `model_snapshot_json`。幂等请求哈希包含 catalog hash；Agent、RAG、Memory、并行子问题、HITL resume 与 Tool 调用都从 Request Context 读取同一 Snapshot，不再从环境或全局单例动态选择模型。
5. 建立持久化 RAG Evaluation Dataset、Job 与 Case。Job 创建时冻结 Dataset fingerprint、GatePolicy、可选 baseline 与四角色 Model Snapshot，并由独立 Evaluation Worker 通过 lease、heartbeat、fencing token、最大尝试次数和 orphan recovery 驱动。
6. 每个 Case 使用生产 RAG/HITL Interface 生成安全 Observation，由 Answer 角色生成回答，再由 Evaluator 角色输出结构化 Judge。最终 Report 复用 ADR-0014 的评分、切片、baseline 和 Gate seam。
7. Evaluation Worker 与 Indexing Worker、API 由同一 launcher 监督，但拥有独立 owner identity、并发配置和 shutdown 生命周期。HTTP Interface 只负责 Dataset/Job CRUD、查询 Case 和取消，不在请求内执行模型调用。
8. 管理 Interface 只向 `admin` 开放。模型中心支持 Model Profile CRUD 和四角色 Assignment；RAG 评估工作台支持 Dataset JSON 导入、baseline、启动前检查、Job 进度/取消、历史趋势、Gate、Case 与公开 Evidence identity。
9. 前端 Model Store 与 Evaluation Store 是账号作用域状态的正式 Seam。它们集中处理类型化请求、公开错误、Job 恢复和活动轮询；登录主体变化或退出时必须清除目录、Job、Case 与 timer。

## 不变量

- API Key 或其它 Provider Secret 不得成为 Model Profile、Model Snapshot、Evaluation Job、Case、Report、日志或浏览器字段。
- Model Assignment 变化只影响后续创建的 Run 与 Evaluation Job；已经创建的工作在恢复、重试、HITL 和 worker 重领后仍使用原 Snapshot。
- 模块导入不得读取模型角色环境变量、创建模型单例或启动 Provider 资源。环境默认只通过启动期种子写入持久化控制面。
- Model Profile version 每次修改单调递增；Snapshot 同时保存 profile identity 与 version，不能只保存可变名称。
- 缺失必需 Assignment、Profile 已停用、能力不满足或 API Key 未配置时，新运行 fail-closed；不得静默回退到另一环境模型。
- RAG Evaluation Job 必须有 durable identity 和明确的 queued、running、cancelling、cancelled、succeeded 或 failed 状态；dead worker 的过期 lease 只能由 fencing-aware recovery 处理。
- baseline 必须来自相同 Dataset fingerprint；Dataset 内容变化后不得继续比较旧 baseline。
- Case 响应可以包含问题和生成答案，但 Evidence 只暴露稳定 identity；不得暴露正文、endpoint、Secret、原始异常或私有推理。
- Assignment、Run Snapshot 与 Evaluation Job 各只有一套正式 Interface 和实际 Implementation，遵循 ADR-0023，不保留环境变量热切换或同步在线评估旁路。

## 结果

管理员跨越模型中心的小 Interface，即获得 Profile 生命周期、能力验证、角色分配与新运行快照的 Leverage；运行代码只依赖不可变 Model Snapshot，提高模型选择与恢复语义的 Locality。

RAG Evaluation Job 把长耗时模型评估从 HTTP 生命周期移入可恢复 worker，并让 Dataset、baseline、进度、取消、指标、Gate 与 Case 证据共享一个 durable seam。前端不再只是后端能力的展示层，而是完整的模型与质量控制面。

代价是控制面数据库成为模型选择的事实来源，部署需要运行迁移并保证 worker 与 API 访问同一存储。修改 Assignment 不会立即改变正在运行的工作；若需要比较新模型，必须显式创建新的 Run 或 Evaluation Job。
