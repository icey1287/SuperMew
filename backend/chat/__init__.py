__all__ = [
    "chat_with_agent",
    "chat_with_agent_stream",
    "storage",
]


def __getattr__(name: str):
    if name in __all__:
        from backend.chat import service

        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
