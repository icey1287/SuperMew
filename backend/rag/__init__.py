def run_rag_graph(*args, **kwargs):
    """Lazy compatibility interface that avoids loading embedding at import time."""
    from backend.rag.pipeline import run_rag_graph as invoke

    return invoke(*args, **kwargs)


__all__ = ["run_rag_graph"]
