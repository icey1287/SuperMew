import os
from pathlib import Path
from typing import Optional

# 加载根目录 .env（仅从文件读取，不读取系统环境变量）这样方便管理配置吧
BASE_DIR = Path(__file__).resolve().parent.parent # ../ -> 回到上一级目录
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env", override=True)

# ===== 通用 API 配置（支持多种服务商） =====
API_KEY = os.getenv("API_KEY", "")
BASE_URL = os.getenv("BASE_URL", "https://api.siliconflow.cn/v1") 
# 默认用硅基流动

# ===== 模型配置 =====
# 主模型 - 根据使用的服务商选择对应模型
MODEL = os.getenv("MODEL", "Qwen/Qwen3.5-122B-A10B")
# 嵌入模型
EMBEDDER = os.getenv("EMBEDDER", "Qwen/Qwen3-Embedding-4B")
# 重排序模型
RERANK_MODEL = os.getenv("RERANK_MODEL", "Qwen/Qwen3-Reranker-4B")
RERANK_BINDING_HOST = os.getenv("RERANK_BINDING_HOST", "https://api.siliconflow.cn/v1")

# ===== Grader (RAG pipeline) =====
GRADE_MODEL = os.getenv("GRADE_MODEL", "Qwen/Qwen3.5-122B-A10B")

# ===== Milvus =====
MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1")
MILVUS_PORT = int(os.getenv("MILVUS_PORT", "19530"))
MILVUS_COLLECTION = os.getenv("MILVUS_COLLECTION", "embeddings_collection")

# ===== Auto-merging =====
AUTO_MERGE_ENABLED = os.getenv("AUTO_MERGE_ENABLED", "true").lower() != "false"
AUTO_MERGE_THRESHOLD = int(os.getenv("AUTO_MERGE_THRESHOLD", "2"))
LEAF_RETRIEVE_LEVEL = int(os.getenv("LEAF_RETRIEVE_LEVEL", "3"))

# ===== Tools =====
AMAP_WEATHER_API = os.getenv("AMAP_WEATHER_API")
AMAP_API_KEY = os.getenv("AMAP_API_KEY")

# ===== Server =====
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
