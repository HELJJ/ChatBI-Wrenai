"""Deterministic internal conversation identity."""

from hashlib import sha256

_THREAD_PREFIX = "wren-chat:"


def derive_thread_id(session_id: str) -> str:
    """Derive a stable LangGraph thread ID without embedding the raw session ID."""
    digest = sha256(session_id.encode("utf-8")).hexdigest()
    return f"{_THREAD_PREFIX}{digest}"
