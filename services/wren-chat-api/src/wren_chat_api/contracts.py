"""Strict public API and internal audit data contracts."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)


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


class ServerInfo(StrictModel):
    """Server identity fields extracted from the uploaded report."""

    hostname: str | None = None
    os: str | None = None
    kernel: str | None = None


class CheckItem(StrictModel):
    """One check finding: failed items drive remediation, passed ones are
    listed for completeness."""

    check_item: str = Field(min_length=1)
    passed: bool
    severity: Severity
    current_status: str = Field(min_length=1)
    risk_description: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)


class AnalysisModule(StrictModel):
    """One check module from the report grouping its check items."""

    module: str = Field(min_length=1)
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    check_items: list[CheckItem] = Field(default_factory=list)


class SecurityAnalysis(StrictModel):
    """Parsed model output for one analyzed server report."""

    server_info: ServerInfo = Field(default_factory=ServerInfo)
    modules: list[AnalysisModule] = Field(default_factory=list)
    # None when a truncated output was salvaged before the model generated
    # its summary; no placeholder text is fabricated.
    summary: str | None = Field(default=None, min_length=1)


class SecurityAnalysisResponse(StrictModel):
    """Public success response of the security-report analysis endpoint."""

    filename: Filename
    server_info: ServerInfo
    modules: list[AnalysisModule]
    summary: str | None = None
    # True when the model output hit the token limit and only the fully
    # generated check items were recovered; the last, truncated item is
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
    """One batch entry: a component (version optional) checked against its
    descriptions. Without a version the judgment is component-only."""

    id: ItemId
    component: ComponentName
    version: ComponentVersion | None = None
    vulnerability_descriptions: list[VulnerabilityDescription] = Field(
        min_length=1,
        max_length=50,
    )

    @field_validator("version", mode="before")
    @classmethod
    def blank_version_means_absent(cls, value: object) -> object:
        """Callers with an unknown version often send "" or null; both mean
        the version plays no part in the judgment."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


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
    """One judged entry: 1 when any description's affected component (and,
    when a version was supplied, its affected range covering that version)
    corresponds to the input component, 0 otherwise."""

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


class RiskAssessmentStats(StrictModel):
    """Extracted 风险等级统计 payload of one report.

    Field names follow the caller's ledger contract verbatim (camelCase).
    Counts come from the 高/中/低 rows of the 风险等级统计 table (很高/很低
    rows are out of caliber), rates from the same rows' percentage column,
    and the final evaluation from the report's concluding sentence."""

    filename: Filename
    riskHigh: int = Field(ge=0)
    riskHighRate: float = Field(ge=0, le=1)
    riskMedium: int = Field(ge=0)
    riskMediumRate: float = Field(ge=0, le=1)
    riskLow: int = Field(ge=0)
    riskLowRate: float = Field(ge=0, le=1)
    finalEvaluationCode: Literal["H", "M", "L"]
    finalEvaluationName: Literal["高风险", "中风险", "低风险"]


class RiskAssessmentExtractResponse(StrictModel):
    """Public success envelope of the risk-assessment extraction endpoint.

    Carries the caller-required business status (200 mirrors the HTTP
    status on success); business failures use the endpoint's HTTP-200
    RiskAssessmentFailure envelope instead."""

    code: Literal[200] = 200
    message: str = "success"
    data: RiskAssessmentStats


class RiskAssessmentFailure(StrictModel):
    """HTTP-200 failure envelope of the risk-assessment extraction endpoint.

    Business failures answer with HTTP 200 per the caller's gateway
    convention; ``code`` mirrors the typed error's status so clients and
    the REQUESTS metric keep full granularity, ``data`` is always null,
    and ``message`` is the error's public human-readable message."""

    code: int = Field(ge=400)
    message: str = Field(min_length=1)
    data: None = None
