from datetime import date
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

Platform = Literal["taobao", "jd"]
InvoiceType = Literal["special", "ordinary", "unknown"]
InvoiceStatus = Literal["unrequested", "pending", "issued", "downloaded", "rejected", "unknown"]


def platform_url(value: str, platform: str) -> str:
    if not value:
        return ""
    p = urlparse(value)
    roots = ("taobao.com", "tmall.com", "alibaba.com") if platform == "taobao" else ("jd.com",)
    if (p.scheme != "https" or p.username or p.password or p.port not in (None, 443)
            or not any(p.hostname == r or (p.hostname or "").endswith("." + r) for r in roots)):
        raise ValueError("订单链接必须属于对应平台的 HTTPS 官方域名")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Profile(StrictModel):
    company_name: str = Field(default="", max_length=120)
    tax_id: str = Field(default="", max_length=20)
    address: str = Field(default="", max_length=240)
    phone: str = Field(default="", max_length=40)
    bank_name: str = Field(default="", max_length=120)
    bank_account: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=120)
    allow_ordinary: bool = False

    @field_validator("tax_id")
    @classmethod
    def tax(cls, value):
        value = value.upper()
        if value and (len(value) not in (15, 18, 20) or not value.isascii() or not value.isalnum()):
            raise ValueError("税号需为 15、18 或 20 位字母数字，请核对原始资料")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if value and ("@" not in value or any(c in value for c in "\r\n ")):
            raise ValueError("请填写有效邮箱")
        return value

    @property
    def configured(self):
        return bool(self.company_name and self.tax_id)

    def public(self):
        return self.model_dump() | {"configured": self.configured}


class OrderInput(StrictModel):
    platform: Platform
    order_no: str = Field(pattern=r"^\d{6,40}$")
    product: str = Field(min_length=1, max_length=2000)
    shop: str = Field(min_length=1, max_length=200)
    amount_fen: StrictInt = Field(ge=0, le=100_000_000_00)
    paid_at: date
    trade_status: Literal["paid", "completed", "unpaid", "closed", "refunded", "partial_refund", "unknown"] = "unknown"
    invoice_status: InvoiceStatus = "unknown"
    invoice_type: InvoiceType = "unknown"
    special_status: Literal["yes", "no", "unknown"] = "unknown"
    ordinary_supported: bool = False
    evidence: str = Field(default="", max_length=2000)
    company_purchase: bool = False
    scope_excluded: bool = False
    scope_reason: str = Field(default="", max_length=1000)
    order_url: str = Field(default="", max_length=2048)
    invoice_url: str = Field(default="", max_length=2048)
    source: Literal["manual", "browser", "demo"] = "manual"
    invoice_buyer: str = Field(default="", max_length=120)
    invoice_tax_id: str = Field(default="", max_length=40)

    @model_validator(mode="after")
    def urls(self):
        if self.company_purchase and self.scope_excluded:
            raise ValueError("已排除范围的订单不能同时确认为公司采购")
        self.order_url = platform_url(self.order_url, self.platform)
        self.invoice_url = platform_url(self.invoice_url, self.platform)
        if self.paid_at > datetime.now(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError("订单日期不能晚于今天")
        return self


class OrderPatch(StrictModel):
    order_url: str | None = Field(default=None, max_length=2048)
    invoice_url: str | None = Field(default=None, max_length=2048)
    trade_status: Literal["paid", "completed", "unpaid", "closed", "refunded", "partial_refund", "unknown"] | None = None
    company_purchase: bool | None = None
    scope_excluded: bool | None = None
    scope_reason: str | None = Field(default=None, max_length=1000)
    special_status: Literal["yes", "no", "unknown"] | None = None
    ordinary_supported: bool | None = None
    evidence: str | None = Field(default=None, max_length=2000)
    invoice_status: InvoiceStatus | None = None
    invoice_type: InvoiceType | None = None
    invoice_buyer: str | None = Field(default=None, max_length=120)
    invoice_tax_id: str | None = Field(default=None, max_length=40)


class ImportRequest(StrictModel):
    orders: list[OrderInput] = Field(min_length=1, max_length=5000)


class ScanRequest(StrictModel):
    start_date: date
    end_date: date
    max_pages: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def dates(self):
        if self.start_date > self.end_date or self.end_date > datetime.now(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError("请使用有效日期范围，结束日期不能晚于今天")
        return self


class ReconcileRequest(StrictModel):
    outcome: Literal["not_submitted", "submitted", "issued"]
    evidence: str = Field(min_length=8, max_length=1000)
