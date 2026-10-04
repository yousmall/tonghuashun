"""股票交易文件输入。分析结果仅由服务端计算，不接受客户端画像结论。"""
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

MAX_FILE_BYTES = 5 * 1024 * 1024


class TradeHistoryUpload(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1, max_length=4 * ((MAX_FILE_BYTES + 2) // 3))
    sheet_name: str | None = Field(default=None, max_length=128)
    header_row: int | None = Field(default=None, ge=1, le=30)
    columns: dict[Literal["date", "code", "side", "quantity", "price", "name", "account", "currency", "asset_type", "time", "trade_id"], str] = Field(default_factory=dict)
    # 不允许把旧文件最后日期自动当成今天；历史报告可明确选择截止日。
    as_of: date | None = None
    expected_version: int = Field(default=1, ge=1)
