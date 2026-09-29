"""问财调用的请求级归属；不记录密钥、请求正文或响应内容。"""

from contextvars import ContextVar

calling_user_id: ContextVar[int | None] = ContextVar("calling_user_id", default=None)
