"""Strict public API and internal audit data contracts."""

from typing import Annotated, Literal

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
Filename = Annotated[
    str,
    StringConstraints(min_length=1, max_length=255),
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


Severity = Literal["critical", "high", "medium", "low", "info"]
RiskLevel = Literal["critical", "high", "medium", "low"]


class ServerInfo(StrictModel):
    """Server identity fields extracted from the uploaded report."""

    hostname: str | None = None
    os: str | None = None
    kernel: str | None = None


class RiskItem(StrictModel):
    """One finding the model flags for remediation."""

    check_item: str = Field(min_length=1)
    severity: Severity
    current_status: str = Field(min_length=1)
    risk_description: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)


class SecurityAnalysis(StrictModel):
    """Parsed model output for one analyzed server report."""

    server_info: ServerInfo = Field(default_factory=ServerInfo)
    risk_level: RiskLevel
    risk_items: list[RiskItem] = Field(default_factory=list)
    summary: str = Field(min_length=1)


class SecurityAnalysisResponse(StrictModel):
    """Public success response of the security-report analysis endpoint."""

    filename: Filename
    server_info: ServerInfo
    risk_level: RiskLevel
    risk_items: list[RiskItem]
    summary: str
