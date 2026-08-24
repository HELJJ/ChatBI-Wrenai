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
    public_message = "身份验证失败，请检查 API 密钥后重试。"


class SessionBusy(ChatServiceError):
    code = "SESSION_BUSY"
    http_status = 409
    public_message = "该会话正在处理上一个查询，请等待其完成后再提问。"


class SessionLeaseLost(ChatServiceError):
    code = "SESSION_LEASE_LOST"
    http_status = 409
    public_message = "会话状态异常（处理权丢失），请重新提问或开启新会话。"


class QuestionUnanswerable(ChatServiceError):
    code = "QUESTION_UNANSWERABLE"
    http_status = 422
    public_message = "无法完成该数据问题，请缩小问题范围、指定数据表名或换个问法重试。"


class CapacityExceeded(ChatServiceError):
    code = "CAPACITY_EXCEEDED"
    http_status = 429
    public_message = "服务当前繁忙，请稍后重试。"


class PersistenceFailed(ChatServiceError):
    code = "PERSISTENCE_FAILED"
    http_status = 500
    public_message = "服务状态存储异常，请稍后重试。"


class InternalError(ChatServiceError):
    code = "INTERNAL_ERROR"
    http_status = 500
    public_message = "服务内部错误，请稍后重试。"


class UpstreamFailed(ChatServiceError):
    code = "UPSTREAM_FAILED"
    http_status = 502
    public_message = "上游模型服务异常，请稍后重试。"


class RequestTimedOut(ChatServiceError):
    code = "REQUEST_TIMED_OUT"
    http_status = 504
    public_message = "查询超时，请简化问题、缩小查询范围后重试。"


class ReadOnlySqlRequired(ChatServiceError):
    code = "READ_ONLY_SQL_REQUIRED"
    http_status = 422
    public_message = "仅允许单条只读查询，请修改后重试。"


class ResultTooLarge(ChatServiceError):
    code = "RESULT_TOO_LARGE"
    http_status = 413
    public_message = "查询结果超出大小限制，请缩小时间范围或减少返回字段后重试。"


class InvalidFinalAnswer(ChatServiceError):
    code = "INVALID_FINAL_ANSWER"
    http_status = 502
    public_message = "模型未能生成有效回答，请重试或换种问法。"


class InvalidReportFile(ChatServiceError):
    code = "INVALID_REPORT_FILE"
    http_status = 422
    public_message = (
        "上传的文件不是有效的 Markdown 检查报告：需 .md/.markdown 扩展名、"
        "UTF-8 编码且内容非空，请检查后重新上传。"
    )


class ReportTooLarge(ChatServiceError):
    code = "REPORT_TOO_LARGE"
    http_status = 413
    public_message = "上传的报告文件超过大小限制，请精简报告内容后重试。"


class InvalidAnalysisResult(ChatServiceError):
    code = "INVALID_ANALYSIS_RESULT"
    http_status = 502
    public_message = "模型未能生成有效的分析结果，请重试。"


class InvalidExtractionResult(ChatServiceError):
    code = "INVALID_EXTRACTION_RESULT"
    http_status = 502
    public_message = "提取结果未通过校验，请重试或更换清晰的 PDF 文件后重新上传。"

    def __init__(
        self,
        internal_message: str | None = None,
        *,
        trace_id: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(internal_message, cause=cause)
        if trace_id:
            # Instance-level shadow so callers can correlate the public
            # error with the matching service-log line.
            self.public_message = (
                f"{self.public_message[:-1]}，"
                f"追踪号 {trace_id} 可在服务日志中检索本次详情。"
            )


class InvalidPentestFile(ChatServiceError):
    code = "INVALID_PENTEST_FILE"
    http_status = 422
    public_message = (
        "上传的文件不是可解析的渗透测试记录单 PDF：需 .pdf 扩展名、"
        "有效的 PDF 文件头且文档未加密损坏，请检查后重新上传。"
    )
