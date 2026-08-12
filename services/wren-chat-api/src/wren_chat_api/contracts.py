"""Strict public API and internal audit data contracts."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints


class StrictModel(BaseModel):
    """Immutable contract that rejects fields outside the declared schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)


SessionId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]
Question = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000),
]
Answer = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
ErrorCode = Annotated[
    str,
    StringConstraints(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$"),
]


class ChatRequest(StrictModel):
    """Public request accepted by the chat endpoint."""

    session_id: SessionId
    question: Question


class ChatResponse(StrictModel):
    """Public success response; execution evidence remains in the audit store."""

    session_id: SessionId
    answer: Answer


class ErrorBody(StrictModel):
    """Stable, non-sensitive public error details."""

    code: ErrorCode
    message: str = Field(min_length=1)


class ErrorResponse(StrictModel):
    """Stable public error envelope."""

    error: ErrorBody


class AttemptError(StrictModel):
    """Structured SQL planning or execution error persisted in audit storage."""

    code: ErrorCode
    phase: str = Field(min_length=1)
    message: str = Field(min_length=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class AttemptResult(StrictModel):
    """Normalized columns and rows persisted for a successful SQL attempt."""

    columns: list[str]
    rows: list[dict[str, JsonValue]]
