"""Sanitized provider failures for acquisition audits; no response bodies."""
class ProviderCallError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def failure_summary(error: BaseException) -> dict:
    return {"code": getattr(error, "code", "DATA_CALL_FAILED"),
            "status_code": getattr(error, "status_code", None),
            "retryable": getattr(error, "code", "") not in {
                "AUTHENTICATION_REJECTED", "CAPABILITY_FORBIDDEN", "PROVIDER_QUOTA_EXHAUSTED",
                'CALENDAR_YEAR_UNSUPPORTED', 'CALENDAR_COVERAGE_INCOMPLETE', 'CALENDAR_EXCHANGE_CONFLICT',
                'CALENDAR_NOTICE_INVALID', 'PUBLIC_SOURCE_INVALID', 'PUBLIC_SOURCE_TOO_LARGE',
                'PUBLIC_SOURCE_REDIRECT_REJECTED', 'PUBLIC_SOURCE_REDIRECT_LIMIT',
                'RISK_INVENTORY_INVALID', 'RISK_INVENTORY_INCOMPLETE',
                "DOCUMENT_REDIRECT_REJECTED", "DOCUMENT_REDIRECT_LIMIT", "DOCUMENT_TOO_LARGE"}}
