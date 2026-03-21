# SuperMew 项目重构计划：采用 LangChain 官方 State + Store 架构

## 背景

当前项目的 Agent 实现使用的是**旧版/简化写法**，手动管理对话历史和状态，**未采用 LangChain 官方推荐的架构**。

### 当前问题

| 特性 | 官方推荐 | 当前实现 |
|------|---------|---------|
| 会话状态 (短期记忆) | `checkpointer` | 手动 JSON 文件 (`ConversationStorage`) |
| 长期记忆 | `store` | 未使用 |
| 持久化 | PostgreSQL | JSON 文件（无并发安全） |
| 状态标识 | `thread_id` | 手动 `user_id` + `session_id` |

### 现有基础设施

| 组件 | 技术 | 用途 |
|------|------|------|
| 向量数据库 | Milvus | RAG 文档检索 |
| 关系数据库 | PostgreSQL（待引入） | checkpointer + store |

---

## 目标

将项目从手动状态管理重构为 LangChain 官方推荐的架构：

1. **使用 `checkpointer`**：替代 `ConversationStorage`，实现自动会话状态持久化
2. **使用 `store`**：管理长期记忆（如用户偏好、实体信息）
3. **使用 `thread_id`**：替代手动的 `user_id/session_id` 标识
4. **引入 PostgreSQL**：共享数据库连接，同时支持 checkpointer 和 store

---

## 架构方案（方案 B：混用生产级）

```
┌─────────────────────────────────────────────────────────┐
│                    SuperMew Agent                        │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐     ┌─────────────┐     ┌───────────┐  │
│  │ Checkpointer│     │    Store    │     │  Milvus   │  │
│  │ (PostgreSQL)│     │(PostgreSQL) │     │ (向量库)   │  │
│  └─────────────┘     └─────────────┘     └───────────┘  │
│        │                   │                  │         │
│   会话状态持久化         长期记忆           RAG检索      │
│   (高频读写)           (低频读写)        (已有)         │
└─────────────────────────────────────────────────────────┘
```

| 组件 | 实现 | 原因 |
|------|------|------|
| Checkpointer | `PostgresSaver`（或 `InMemorySaver` 开发用） | 会话状态高频读写，PostgreSQL 提供持久化 |
| Store | `PostgresStore` | 长期记忆低频，PostgreSQL 足够 |
| 向量检索 | Milvus（已有） | RAG 文档检索 |

---

## 实施计划

### 阶段一：依赖安装 + 开发环境验证（Day 1）

#### 1.1 安装 PostgreSQL 相关依赖 - 已完成

```bash
uv add "langgraph-checkpoint-postgres[psycopg]"
```

更新 `pyproject.toml`：

```toml
[project]
dependencies = [
    # ... 现有依赖
    "langgraph-checkpoint-postgres[psycopg]>=0.1.0",
]
```

#### 1.2 启动 PostgreSQL（Docker）

```bash
docker run -d \
  --name supermew-postgres \
  -e POSTGRES_DB=supermew \
  -e POSTGRES_USER=supermew \
  -e POSTGRES_PASSWORD=supermew \
  -p 5432:5432 \
  postgres:16-alpine
```

#### 1.3 添加环境变量

更新 `backend/.env`：

```bash
# PostgreSQL（checkpointer + store）
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=supermew
POSTGRES_USER=supermew
POSTGRES_PASSWORD=supermew
```

---

### 阶段二：最小改动替换存储层（Day 1-2）

#### 2.1 修改 `agent.py` — 添加 checkpointer

```python
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware

# 从环境变量构建连接字符串
DB_URI = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

# 创建 Checkpointer（短期记忆）
checkpointer = PostgresSaver.from_conn_string(DB_URI)
checkpointer.setup()  # 自动创建表

# 创建 Store（长期记忆）
store = PostgresStore.from_conn_string(DB_URI)
store.setup()  # 自动创建表

# 读取系统提示词
soul_prompt_path = Path(__file__).parent / "soul" / "soul.md"
system_prompt = soul_prompt_path.read_text(encoding="utf-8")

# 创建 Agent
agent = create_agent(
    model=model,
    tools=[get_current_weather, search_knowledge_base],
    system_prompt=system_prompt,
    checkpointer=checkpointer,
    middleware=[
        SummarizationMiddleware(
            model=summary_model,
            trigger=("tokens", 80000),
            keep=("messages", 12),
        ),
    ],
)
```

#### 2.2 修改 `chat_with_agent` 调用方式

```python
# 之前
messages = storage.load(user_id, session_id)
messages.append(HumanMessage(content=user_text))
result = agent.invoke({"messages": messages}, config={"recursion_limit": 8})
storage.save(user_id, session_id, messages)

# 之后
config = {"configurable": {"thread_id": f"{user_id}_{session_id}"}}
result = agent.invoke(
    {"messages": [{"role": "user", "content": user_text}]},
    config={**config, "recursion_limit": 8}
)
# 无需手动保存！checkpointer 自动持久化
```

#### 2.3 修改 `chat_with_agent_stream` 调用方式

```python
# 之后
config = {"configurable": {"thread_id": f"{user_id}_{session_id}"}}
async for msg, metadata in agent.astream(
    {"messages": [{"role": "user", "content": user_text}]},
    config={**config, "recursion_limit": 8},
    stream_mode="messages",
):
    # 流式处理...
# 无需手动保存！
```

#### 2.4 清理旧代码

- 删除 `ConversationStorage` 类
- 删除 `storage` 全局实例
- 删除 `storage.load()` / `storage.save()` 调用
- 删除 `data/customer_service_history.json` 相关逻辑

---

### 阶段三：长期记忆集成（Day 3）

#### 3.1 定义记忆操作函数

```python
from datetime import datetime
import uuid

def save_user_memory(store, user_id: str, memory: dict):
    """存储用户记忆到 PostgreSQL Store"""
    namespace = ("user_memories", user_id)
    store.put(namespace, str(uuid.uuid4()), {
        "content": memory,
        "created_at": datetime.now().isoformat(),
    })

def get_user_memories(store, user_id: str) -> list:
    """获取用户所有记忆"""
    namespace = ("user_memories", user_id)
    return store.list(namespace)

def search_user_memories(store, user_id: str, query: str) -> list:
    """搜索用户记忆（支持向量检索）"""
    namespace = ("user_memories", user_id)
    return store.search(namespace, query=query)
```

#### 3.2 在 Agent 中集成记忆检索

在 `create_agent_instance()` 中，通过 middleware 或工具获取记忆：

```python
# 示例：通过工具访问 store
def get_user_memory_tool(store):
    """获取当前用户记忆的工具"""
    def _invoke(user_id: str) -> str:
        memories = get_user_memories(store, user_id)
        if not memories:
            return "暂无用户记忆"
        return "\n".join([m.value["content"] for m in memories])
    return _invoke
```

---

### 阶段四：数据迁移（Day 3-4，可选）

#### 4.1 迁移现有会话数据

```python
# 迁移脚本：json -> PostgreSQL
def migrate_history_to_checkpointer(storage: ConversationStorage, checkpointer):
    """将现有 JSON 数据迁移到 checkpointer"""
    data = storage._load()
    for user_id, sessions in data.items():
        for session_id, session_data in sessions.items():
            config = {"configurable": {"thread_id": f"{user_id}_{session_id}"}}
            # 写入 checkpointer
            checkpointer.put(config, {"messages": session_data["messages"]})
```

#### 4.2 验证迁移

1. 启动新系统
2. 用旧的 `user_id_session_id` 发起请求
3. 确认能恢复历史对话

---

### 阶段五：可选重构（Day 5，可选）

评估是否需要 `StateGraph`：

- 如果需要自定义节点（RAG 节点、记忆节点），再完整重构
- 否则保持 `create_agent`，仅替换存储层

---

## 依赖更新

```toml
[project]
dependencies = [
    # ... 现有依赖
    "langgraph-checkpoint-postgres[psycopg]>=0.1.0",
]
```

---

## 模块影响分析

### 受影响模块

| 模块 | 影响程度 | 说明 |
|------|---------|------|
| `agent.py` | **高** | 替换 `ConversationStorage` → `PostgresSaver` |
| `api.py` | **高** | 会话 API 直接调用 `storage._load()` |

### 不受影响模块

| 模块 | 原因 |
|------|------|
| `rag_pipeline.py` | 独立的 RAG 图，与会话存储无关 |
| `tools.py` | 独立工具函数 |
| `app.py` | FastAPI 入口，不涉及存储 |
| `document_loader.py` | 文档加载，与会话无关 |
| `parent_chunk_store.py` | 独立的父级分块 JSON，与会话分开 |
| `milvus_writer.py` | 向量写入，与会话无关 |
| `milvus_client.py` | Milvus 客户端，独立 |
| `embedding.py` | 嵌入服务，独立 |
| `config.py` | 配置管理，独立 |

---

## 技术问题与解决方案

### 问题 1：`api.py` 直接调用 `storage._load()`

**问题描述：** `api.py` 第45行、68行、91行直接访问 `storage._load()`，暴露了内部存储结构。

**解决方案：** 封装统一的 `SessionManager` 类

```python
# agent.py 新增
from langgraph.checkpoint.postgres import PostgresSaver

class SessionManager:
    """封装 checkpointer 的会话管理，兼容原有 API"""

    def __init__(self, checkpointer: PostgresSaver):
        self.checkpointer = checkpointer

    def get_messages(self, user_id: str, session_id: str) -> list:
        """获取会话消息"""
        config = {"configurable": {"thread_id": f"{user_id}_{session_id}"}}
        data = self.checkpointer.get(config)
        if data and "channel_values" in data:
            return data["channel_values"].get("messages", [])
        return []

    def list_sessions(self, user_id: str) -> list:
        """列出用户所有会话"""
        # 需要额外维护会话索引（见问题3）

    def delete_session(self, user_id: str, session_id: str) -> bool:
        """删除会话"""
        config = {"configurable": {"thread_id": f"{user_id}_{session_id}"}}
        self.checkpointer.delete(config)
        return True
```

**复杂度：** 中等

---

### 问题 2：`rag_trace` 元数据无法存储在 checkpointer

**问题描述：** 当前逻辑将 `rag_trace` 作为 `extra_message_data` 单独存储，PostgreSQL Saver 只存储 `messages`。

**解决方案：** 利用 LangChain 消息的 `additional_kwargs`

```python
# 修改 agent.py 中的保存逻辑
from langchain_core.messages import AIMessage

# 获取 RAG trace
rag_context = get_last_rag_context(clear=True)
rag_trace = rag_context.get("rag_trace") if rag_context else None

# 将 rag_trace 嵌入消息的 additional_kwargs
messages.append(AIMessage(
    content=response_content,
    additional_kwargs={"rag_trace": rag_trace}
))

# checkpointer 自动保存整个 messages 列表，包括 additional_kwargs
```

**优点：** 无需额外存储，消息和元数据天然绑定

**复杂度：** 低 ⭐

---

### 问题 3：API 兼容性（会话列表）

**问题描述：** `checkpointer` 本身不维护会话索引，`GET /sessions/{user_id}` 无法实现。

**解决方案：** PostgreSQL 索引表

```sql
-- sessions 表维护会话索引
CREATE TABLE IF NOT EXISTS sessions_index (
    thread_id VARCHAR(255) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    session_id VARCHAR(255) NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW(),
    message_count INT DEFAULT 0
);

CREATE INDEX idx_sessions_user ON sessions_index(user_id);
```

**更新 `SessionManager`：**

```python
class SessionManager:
    def __init__(self, checkpointer, db_pool):
        self.checkpointer = checkpointer
        self.db_pool = db_pool

    def list_sessions(self, user_id: str) -> list:
        """从索引表获取会话列表"""
        with self.db_pool.connection() as conn:
            rows = conn.execute(
                "SELECT session_id, updated_at, message_count "
                "FROM sessions_index WHERE user_id = %s "
                "ORDER BY updated_at DESC",
                [user_id]
            ).fetchall()
            return [{"session_id": r[0], "updated_at": r[1], "message_count": r[2]} for r in rows]

    def update_session_index(self, user_id: str, session_id: str, message_count: int):
        """每次对话后更新索引"""
        thread_id = f"{user_id}_{session_id}"
        with self.db_pool.connection() as conn:
            conn.execute("""
                INSERT INTO sessions_index (thread_id, user_id, session_id, message_count, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (thread_id) DO UPDATE SET
                    message_count = EXCLUDED.message_count,
                    updated_at = NOW()
            """, [thread_id, user_id, session_id, message_count])
```

**复杂度：** 中等

---

### API 兼容性对照表

| API 端点 | checkpointer 替代方案 | 状态 |
|---------|----------------------|------|
| `GET /sessions/{user_id}` | 索引表 | ✅ 可解决 |
| `GET /sessions/{user_id}/{session_id}` | `checkpointer.get(thread_id)` | ✅ 可解决 |
| `DELETE /sessions/{user_id}/{session_id}` | `checkpointer.delete(thread_id)` | ✅ 可解决 |

---

## 风险与注意事项

### 兼容性

| 兼容性项 | 状态 | 说明 |
|---------|------|------|
| `create_agent` + `checkpointer` | ✅ 确认支持 | 官方文档示例 |
| `SummarizationMiddleware` | ✅ 兼容 | 可与 checkpointer 叠加 |
| 流式输出 (`astream`) | ✅ 兼容 | checkpointer 自动保存 |
| `store` 参数 | ⚠️ 待验证 | 某些版本可能不支持直接传 store |

### 数据迁移

- 现有 `customer_service_history.json` **先保留**，确认迁移成功后再删除
- 建议：先并行运行（`checkpointer` 新数据，JSON 旧数据），确认稳定后再清理

### PostgreSQL 连接

- 生产环境建议使用连接池（`psycopg.pool`）
- 注意设置 `sslmode`（生产环境）

---

## 预期收益

| 指标 | 当前 | 重构后 |
|------|------|--------|
| 存储代码 | ~50 行 (`ConversationStorage`) | ~10 行 (checkpointer 配置) |
| 会话恢复 | 手动 JSON 文件 | 自动 (`thread_id`) |
| 并发安全 | ❌ JSON 文件有竞态风险 | ✅ PostgreSQL 事务支持 |
| 流式 + 状态 | 需手动保存 | ✅ 自动保存 |
| 长期记忆 | 无 | ✅ `PostgresStore` 可扩展 |
| 官方支持 | 无 | ✅ LangChain 官方架构 |

---

## 验收标准

1. ✅ 对话功能正常（流式输出）
2. ✅ 相同 `thread_id` 的请求能恢复会话历史
3. ✅ `PostgresSaver` 正确持久化会话状态
4. ✅ 长期记忆可通过 `PostgresStore` 存储和检索
5. ✅ 删除了 `ConversationStorage` 相关代码
6. ✅ 端到端测试通过

---

## 相关文档

- [LangChain create_agent + checkpointer](https://docs.langchain.com/oss/python/langchain/short-term-memory)
- [LangGraph Store + PostgreSQL](https://docs.langchain.com/oss/python/langgraph/add-memory#example-using-postgres-store)
- [LangGraph Checkpointing](https://docs.langchain.com/oss/python/langgraph/persistence)
