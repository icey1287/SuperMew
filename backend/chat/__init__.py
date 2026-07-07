from importlib import import_module

__all__ = [
    "chat_with_agent",
    "chat_with_agent_stream",
]


def __getattr__(name: str):
    if name in __all__:
        service = import_module("backend.chat.service")
        return getattr(service, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
