# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 常用命令

```bash
# 安装依赖
uv sync

# 启动 Milvus（向量库）
docker compose up -d

# 启动后端服务
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload

# 访问
# 前端: http://127.0.0.1:8000/
# API 文档: http://127.0.0.1:8000/docs
```

## 环境配置

创建 `backend/.env` 文件：

```bash
API_KEY=your_api_key_here
MODEL=Qwen/Qwen3.5-122B-A10B          # 主模型
EMBEDDER=Qwen/Qwen3-Embedding-4B      # 嵌入模型
RERANK_MODEL=Qwen/Qwen3-Reranker-4B   # 重排序模型
BASE_URL=https://api.siliconflow.cn/v1 # 兼容 OpenAI API 的服务商
MILVUS_HOST=127.0.0.1
MILVUS_PORT=19530
```

## 项目架构

```
SuperMew/
├── backend/                 # FastAPI 后端
│   ├── app.py              # 入口、CORS、静态资源
│   ├── api.py              # 聊天/会话/文档 API
│   ├── agent.py            # LangChain Agent + 会话存储
│   ├── config.py           # 环境变量配置
│   ├── schemas.py          # Pydantic 数据模型
│   ├── soul/
│   │   └── soul.md         # Agent 系统提示词
│   ├── tools.py            # 工具：天气查询、知识库检索
│   ├── rag_pipeline.py     # RAG 工作流（LangGraph）
│   ├── rag_utils.py        # 检索、查询重写、HyDE
│   ├── embedding.py        # 稠密向量 + BM25 稀疏向量
│   ├── milvus_client.py    # Milvus 混合检索
│   ├── milvus_writer.py    # 向量写入
│   ├── document_loader.py  # PDF/Word 分块
│   └── parent_chunk_store.py  # 父级分块 DocStore
├── frontend/               # Vue 3 单页应用
│   ├── index.html
│   ├── script.js
│   └── style.css
├── data/                   # 本地存储
│   ├── customer_service_history.json  # 会话历史
│   ├── parent_chunks.json             # 父级分块
│   └── documents/                     # 上传文档
└── docker-compose.yml      # Milvus 向量库
```

## 核心模块

- **RAG 管道** (`rag_pipeline.py`)：基于 LangGraph 构建，包含 retrieve → grade → rewrite → retrieve\_expanded 节点
- **检索** (`rag_utils.py`)：Hybrid Search (Dense + Sparse + RRF) + Jina Rerank
- **分块** (`document_loader.py`)：三级滑动窗口（L1/L2/L3），仅 L3 叶子块写入 Milvus
- **记忆** (`agent.py`)：
  - `SummarizationMiddleware` 在 token 超过 80000 时触发
  - 保留最近 12 条消息（6 轮交互）
  - 使用 `backend/soul/soul.md` 作为系统提示词

## 优化方向

优先优化**上下文工程**和**记忆系统**：

- 改进会话摘要策略
- 考虑集成 Mem0/LangMem 等外部记忆方案
- 优化上下文压缩和注入方式 -- Context Engineering

