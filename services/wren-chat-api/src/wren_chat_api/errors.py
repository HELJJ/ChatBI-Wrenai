"""Typed service errors with stable public mappings."""

from typing import ClassVar


class ChatServiceError(Exception):
    """Base service error that never exposes its internal diagnostic via str()."""

    code: ClassVar[str]
    http_status: ClassVar[int]
    public_message: ClassVar[str]

    def __init__(
        self,
        internal_message: str | None = None,
        *,
        cause: Exception | None = None,
    ) -> None:
        self.internal_message = internal_message
        super().__init__(self.public_message)
        if cause is not None:
            self.__cause__ = cause


class AuthenticationFailed(ChatServiceError):
    code = "AUTHENTICATION_FAILED"
    http_status = 401
    public_message = "Authentication failed."


class SessionBusy(ChatServiceError):
    code = "SESSION_BUSY"
    http_status = 409
    public_message = "Another request is already running for this session."


class QuestionUnanswerable(ChatServiceError):
    code = "QUESTION_UNANSWERABLE"
    http_status = 422
    public_message = "Unable to complete the data question."


class CapacityExceeded(ChatServiceError):
    code = "CAPACITY_EXCEEDED"
    http_status = 429
    public_message = "The service is temporarily at capacity."


class PersistenceFailed(ChatServiceError):
    code = "PERSISTENCE_FAILED"
    http_status = 500
    public_message = "Unable to persist the request state."


class UpstreamFailed(ChatServiceError):
    code = "UPSTREAM_FAILED"
    http_status = 502
    public_message = "A required upstream service failed."


class RequestTimedOut(ChatServiceError):
    code = "REQUEST_TIMED_OUT"
    http_status = 504
    public_message = "The request timed out."


class ReadOnlySqlRequired(ChatServiceError):
    code = "READ_ONLY_SQL_REQUIRED"
    http_status = 422
    public_message = "Only single read-only queries are allowed."


class ResultTooLarge(ChatServiceError):
    code = "RESULT_TOO_LARGE"
    http_status = 413
    public_message = "The query result exceeded the configured size limit."
