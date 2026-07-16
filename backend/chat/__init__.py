"""Thread history primitives and an internal-only legacy Chat Implementation.

Public Agent execution enters through ``backend.runs``.  The historical
``backend.chat.service`` module remains importable only by explicit path for
compatibility tests; package-level execution helpers are intentionally absent.
"""

__all__: list[str] = []
