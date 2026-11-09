import pytest
from invoice_agent.engine import decide, merchant_draft
from invoice_agent.models import OrderInput, Profile


def order(**changes):
    return OrderInput(**(dict(platform="jd", order_no="123456789012", product="测试采购", shop="测试店铺", amount_fen=10000,
                      paid_at="2026-11-06", trade_status="completed", invoice_status="unrequested") | changes)).model_dump(mode="json")


def test_unknown_special_never_falls_back():
    d = decide(order(company_purchase=True, ordinary_supported=True), Profile(allow_ordinary=True))
    assert d["action"] == "ask_special"


def test_zero_displayed_amount_requires_manual_review():
    d = decide(order(amount_fen=0, company_purchase=True, special_status="yes", evidence="商家支持"), Profile())
    assert d["action"] == "manual" and "零金额" in d["label"]


def test_special_requires_evidence_and_business_confirmation():
    assert decide(order(special_status="yes", evidence="页面提供专票"), Profile())["action"] == "manual"
    assert decide(order(special_status="yes", company_purchase=True), Profile())["action"] == "manual"
    assert decide(order(special_status="yes", company_purchase=True, evidence="页面提供专票"), Profile())["action"] == "apply_special"


@pytest.mark.parametrize("status", ["refunded", "partial_refund", "unpaid", "closed", "unknown"])
def test_refunds_and_unknown_transactions_cannot_apply(status):
    o = order(company_purchase=True, special_status="yes", evidence="页面提供专票")
    o["trade_status"] = status
    assert decide(o, Profile())["action"] in ("manual", "skip")


def test_ordinary_requires_explicit_rejection_and_opt_in():
    o = order(company_purchase=True, special_status="no", evidence="商家明确仅普票", ordinary_supported=True)
    assert decide(o, Profile())["action"] == "manual"
    assert decide(o, Profile(allow_ordinary=True))["action"] == "apply_ordinary"


def test_existing_invoice_buyer_must_be_verified():
    o = order(invoice_status="issued", company_purchase=True)
    p = Profile(company_name="示例科技公司", tax_id="91310000123456789X")
    assert decide(o, p)["action"] == "manual"
    o.update(invoice_buyer=p.company_name, invoice_tax_id=p.tax_id)
    assert decide(o, p)["action"] == "download"


def test_pending_or_journal_blocks_repeat():
    o = order(company_purchase=True, special_status="yes", evidence="已确认")
    assert decide(o, Profile(), "uncertain")["action"] == "manual"
    assert decide(o, Profile(), "submitted")["action"] == "wait"


def test_scope_excluded_never_requests_and_uncertain_is_still_visible():
    o = order(scope_excluded=True, scope_reason="与本次范围无关")
    assert decide(o, Profile())["action"] == "skip"
    assert decide(o, Profile(), "uncertain")["label"] == "核对提交结果"
    del o["scope_excluded"]
    del o["scope_reason"]
    assert decide(o, Profile())["label"] == "未纳入公司采购"


def test_money_is_strict_integer_and_urls_are_platform_scoped():
    with pytest.raises(ValueError):
        order(amount_fen=1.2)
    with pytest.raises(ValueError):
        order(order_url="https://evil.example/collect")


def test_request_draft_cannot_downgrade_unknown_special():
    text = merchant_draft(order(company_purchase=True), Profile(allow_ordinary=True), "request")
    assert "是否支持" in text and "申请增值税普通发票" not in text


def test_request_draft_requires_normal_opt_in():
    o = order(company_purchase=True, special_status="no", ordinary_supported=True, evidence="商家明确不支持专票")
    with pytest.raises(ValueError):
        merchant_draft(o, Profile(), "request")
