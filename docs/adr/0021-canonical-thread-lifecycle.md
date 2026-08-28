# ADR-0021：Canonical Thread 生命周期与 Message 分页

- 状态：已接受
- 日期：2026-07-17

## 背景

Run/Event 已成为公开执行 Interface，Thread 还需要独立拥有创建、列表、历史分页、状态投影和
删除规则。若 Run 预留隐式创建 Thread，或由前端自行选择路径身份，调用者就必须理解多套浅
Interface；Message 全量串行读取和用 Run 状态覆盖 Thread 状态也会混淆两个生命周期。

## 决策

建立 `backend.threads` application Module，集中拥有 Thread 创建、列表、最近 Message 分页和
删除规则。`/v1/threads` 是唯一 HTTP Adapter。Run Module 默认拒绝不存在的 Thread，不再把
路径参数隐式升级为持久资源。

正式创建 Interface 只接受可选标题，并由服务端生成 `thread_<uuid>`。所有 Thread path、Run
schema 与 Event contract 共享
`^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$`；Thread ID 由服务端生成，前端不能选择。

canonical Message 使用 required `id`、`run_id`（可为 null）、`sequence`、`status`、
`role=user|assistant|system`、`content`、aware UTC `timestamp` 与 `rag_trace`。未携带 `before`
时返回最新一页；携带时返回 sequence 小于 cursor 的前一页。每页对外按 sequence 升序，
`previous_cursor` 只在仍有更早 Message 时存在。前端显式触发加载更早页，不再在首屏全量遍历
Thread。

Thread 列表在单次查询中投影 `thread_status`、`active_run_id` 与 `active_run_status`。执行中、
等待输入或取消中的状态属于 Run，不得写回 Thread status。若同一 Thread 同时存在执行 Run 与
排队 Run，投影优先当前执行/等待/取消 Run，再选择 pending 或最早 queued Run。

Thread version 定义为 append version：创建 Run 时追加用户与 assistant Message，因此递增
两次；更新既有 assistant Message 的正文、RAG Trace 或终态不递增。Run 创建响应中的 version
可直接作为下一轮 optimistic write 的预期版本。

删除在 Thread 行锁内检查 Run ledger。只有状态明确属于 terminal 集合的 Run 才允许删除；
任何已知或未来未知非终态均 fail-closed。不存在和不属于当前用户的 Thread 对外保持同一
not-found 语义。

## 结果

调用者跨越一个 Thread Interface 即获得安全身份、所有权、最近页历史、UTC 契约、Run 投影、
append version 和删除保护的 Leverage。Thread 生命周期知识集中在一个 Module，提高 Locality；
Run 与 HTTP Adapter 不再各自实现创建或历史规则。

代价是调用者必须在创建 Run 前显式创建 Thread，并使用 canonical Message 与 cursor 契约。
