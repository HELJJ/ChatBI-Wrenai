import pytest
from pydantic import ValidationError

from wren_chat_api.contracts import (
    AttemptError,
    AttemptResult,
    ChatRequest,
    ChatResponse,
    ErrorBody,
    ErrorResponse,
)
from wren_chat_api.errors import (
    AuthenticationFailed,
    CapacityExceeded,
    PersistenceFailed,
    QuestionUnanswerable,
    RequestTimedOut,
    SessionBusy,
    UpstreamFailed,
)
from wren_chat_api.identity import derive_thread_id


def test_chat_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(session_id="s-1", question="count orders", sql="SELECT 1")


@pytest.mark.parametrize(
    "session_id", ["", "has space", "x" * 129, "中文会话", "session/001"]
)
def test_chat_request_rejects_invalid_session_id(session_id: str) -> None:
    with pytest.raises(ValidationError):
        ChatRequest(session_id=session_id, question="count orders")


def test_question_is_trimmed() -> None:
    request = ChatRequest(session_id="s-1", question="  count orders  ")

    assert request.question == "count orders"


def test_blank_question_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(session_id="s-1", question="   ")


def test_success_response_has_exactly_two_fields() -> None:
    response = ChatResponse(session_id="s-1", answer="42")

    assert response.model_dump() == {"session_id": "s-1", "answer": "42"}


def test_error_response_has_stable_envelope() -> None:
    response = ErrorResponse(
        error=ErrorBody(code="SESSION_BUSY", message="Session is busy.")
    )

    assert response.model_dump() == {
        "error": {"code": "SESSION_BUSY", "message": "Session is busy."}
    }


def test_attempt_contracts_have_stable_json_shapes() -> None:
    error = AttemptError(
        code="INVALID_SQL",
        phase="SQL_PLANNING",
        message="Column does not exist",
        metadata={},
    )
    result = AttemptResult(
        columns=["total_sales"],
        rows=[{"total_sales": "1280000.00"}],
    )

    assert error.model_dump() == {
        "code": "INVALID_SQL",
        "phase": "SQL_PLANNING",
        "message": "Column does not exist",
        "metadata": {},
    }
    assert result.model_dump() == {
        "columns": ["total_sales"],
        "rows": [{"total_sales": "1280000.00"}],
    }


def test_thread_id_is_stable_and_hides_raw_session() -> None:
    thread_id = derive_thread_id("private-session")

    assert thread_id == (
        "wren-chat:7ae99811b2c7c696275f6aca5adcedd71a90d8ab3ced71cd81d7e2c8020628fd"
    )
    assert "private-session" not in thread_id


@pytest.mark.parametrize(
    ("error_type", "code", "status"),
    [
        (AuthenticationFailed, "AUTHENTICATION_FAILED", 401),
        (SessionBusy, "SESSION_BUSY", 409),
        (QuestionUnanswerable, "QUESTION_UNANSWERABLE", 422),
        (CapacityExceeded, "CAPACITY_EXCEEDED", 429),
        (PersistenceFailed, "PERSISTENCE_FAILED", 500),
        (UpstreamFailed, "UPSTREAM_FAILED", 502),
        (RequestTimedOut, "REQUEST_TIMED_OUT", 504),
    ],
)
def test_service_errors_have_stable_public_mapping(
    error_type, code: str, status: int
) -> None:
    error = error_type("internal database password=private")

    assert error.code == code
    assert error.http_status == status
    assert str(error) == error.public_message
    assert "password" not in str(error)
