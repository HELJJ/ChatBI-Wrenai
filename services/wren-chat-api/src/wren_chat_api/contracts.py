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


Severity = Literal["严重", "高危", "中危", "低危", "提示"]
RiskLevel = Literal["严重", "高危", "中危", "低危"]


class ServerInfo(StrictModel):
    """Server identity fields extracted from the uploaded report."""

    hostname: str | None = None
    os: str | None = None
    kernel: str | None = None


class RiskItem(StrictModel):
    """One check finding: failed items drive remediation, passed ones are
    listed for completeness."""

    check_item: str = Field(min_length=1)
    passed: bool
    severity: Severity
    current_status: str = Field(min_length=1)
    risk_description: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)


class SecurityAnalysis(StrictModel):
    """Parsed model output for one analyzed server report."""

    server_info: ServerInfo = Field(default_factory=ServerInfo)
    risk_level: RiskLevel
    risk_items: list[RiskItem] = Field(default_factory=list)
    # None when a truncated output was salvaged before the model generated
    # its summary; no placeholder text is fabricated.
    summary: str | None = Field(default=None, min_length=1)


class SecurityAnalysisResponse(StrictModel):
    """Public success response of the security-report analysis endpoint."""

    filename: Filename
    server_info: ServerInfo
    risk_level: RiskLevel
    risk_items: list[RiskItem]
    summary: str | None = None
    # True when the model output hit the token limit and only the fully
    # generated risk items were recovered; the last, truncated item is
    # dropped and missing fields fall back to derived values.
    partial: bool = False


class PentestRiskItem(StrictModel):
    """One risk entry extracted from the 安全风险项 section of a pentest
    record (test id, name, and verdict classification)."""

    testCode: str = Field(min_length=1)
    testName: str = Field(min_length=1)
    testResult: str = Field(min_length=1)


ComponentName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
ComponentVersion = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]
VulnerabilityDescription = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000),
]
ItemId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]


class RiskSelfCheckItem(StrictModel):
    """One batch entry: a component+version checked against its descriptions."""

    id: ItemId
    component: ComponentName
    version: ComponentVersion
    vulnerability_descriptions: list[VulnerabilityDescription] = Field(
        min_length=1,
        max_length=50,
    )


class RiskSelfCheckRequest(StrictModel):
    """Public request accepted by the risk self-check endpoint.

    The wire name is ``list`` (kept via alias) because it shadows the
    built-in when used as a Python field name in an annotated assignment.
    """

    items: list[RiskSelfCheckItem] = Field(
        alias="list",
        min_length=1,
        max_length=50,
    )


class RiskSelfCheckResultItem(StrictModel):
    """One judged entry: 1 when any description hits the component at the
    requested version, 0 otherwise."""

    id: ItemId
    component: ComponentName
    matched: Literal[0, 1]


class RiskSelfCheckErrorItem(StrictModel):
    """One entry whose judgment failed; ``error`` carries the typed error's
    public message. A failed judgment is never reported as matched=0."""

    id: ItemId
    error: str = Field(min_length=1)


class RiskSelfCheckResponse(StrictModel):
    """Public success response of the risk self-check endpoint: results in
    request order, failed entries interleaved as error items."""

    data: list[RiskSelfCheckResultItem | RiskSelfCheckErrorItem]


class PentestExtractResponse(StrictModel):
    """Public success response of the pentest-record extraction endpoint.

    Validation diagnostics (anti-hallucination hits, count mismatches,
    page failures) are intentionally not part of the public contract;
    they are logged server-side for the manual-review queue."""

    filename: Filename
    risk_items: list[PentestRiskItem] = Field(default_factory=list)
