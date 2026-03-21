"""SuperMew 应用启动入口"""
import sys
from pathlib import Path

# 确保能导入 backend 模块
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "backend"))

import uvicorn
from backend.config import HOST, PORT


def main():
    uvicorn.run(
        "backend.app:app",
        host=HOST,
        port=PORT,
        reload=True,
    )


if __name__ == "__main__":
    main()