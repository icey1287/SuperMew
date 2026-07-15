"""Deprecated compatibility entrypoint for the versioned RAG evaluation CLI.

The old module executed a remote LangSmith experiment at import time and read
runtime configuration implicitly. Keep this filename for existing bookmarks,
but require the same explicit command and inputs as ``scripts/evaluate_rag.py``.
"""

from scripts.evaluate_rag import main


if __name__ == "__main__":
    raise SystemExit(main())
