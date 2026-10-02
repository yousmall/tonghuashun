"""Sanitized provider failures for acquisition audits; no response bodies."""
class ProviderCallError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def failure_summary(error: BaseException) -> dict:
    return {"code": getattr(error, "code", "DATA_CALL_FAILED"),
            "status_code": getattr(error, "status_code", None),
            "retryable": getattr(error, "code", "") not in {"AUTHENTICATION_REJECTED", "CAPABILITY_FORBIDDEN"}}
