import json
import sqlite3
import pytest
from invoice_agent.store import Store, Conflict
from invoice_agent.models import Profile, OrderInput


def item(**patch):
    return OrderInput(**({"platform": "jd", "order_no": "123456789012", "product": "测试商品", "shop": "测试店铺",
                        "paid_at": "2026-11-06", "amount_fen": 9900, "trade_status": "completed"} | patch)).model_dump(mode="json")


def test_profile_and_orders_encrypted_and_persist(tmp_path):
    s = Store(tmp_path)
    s.save_profile(Profile(company_name="内部测试企业", tax_id="91310000123456789X"))
    s.upsert(item())
    raw = (tmp_path / "invoice-agent.sqlite3").read_bytes()
    assert "内部测试企业".encode() not in raw
    assert "测试商品".encode() not in raw
    assert Store(tmp_path).profile().company_name == "内部测试企业"
    assert len(Store(tmp_path).orders()) == 1


def test_scan_preserves_user_confirmation(tmp_path):
    s = Store(tmp_path)
    s.upsert(item())
    s.patch("jd-123456789012", {"company_purchase": True, "special_status": "yes", "evidence": "商家确认"})
    s.upsert(item(source="browser"))
    o = s.get("jd-123456789012")
    assert o["company_purchase"] is True and o["special_status"] == "yes"


def test_scan_preserves_explicit_scope_exclusion(tmp_path):
    s = Store(tmp_path)
    oid = s.upsert(item())
    s.patch(oid, {"company_purchase": False, "scope_excluded": True, "scope_reason": "用户排除本次范围"})
    s.upsert(item(source="browser"))
    o = s.get(oid)
    assert o["scope_excluded"] is True and o["company_purchase"] is False
    assert o["scope_reason"] == "用户排除本次范围"


@pytest.mark.parametrize("status", ["issued", "pending", "downloaded"])
def test_generic_list_does_not_erase_known_invoice_detail(tmp_path, status):
    s = Store(tmp_path)
    oid = s.upsert(item(invoice_status=status, invoice_type="ordinary",
                        invoice_buyer="虚构测试公司", invoice_tax_id="TEST00000000000000"))
    s.upsert(item(source="browser", invoice_status="unknown"))
    o = s.get(oid)
    assert o["invoice_status"] == status
    assert o["invoice_type"] == "ordinary"
    assert o["invoice_buyer"] == "虚构测试公司"
    assert o["invoice_tax_id"] == "TEST00000000000000"


def test_exclusion_and_consumption_are_serialized(tmp_path):
    s = Store(tmp_path)
    oid = s.upsert(item())
    p = s.prepare(oid, "special", "live")
    s.patch(oid, {"scope_excluded": True, "company_purchase": False})
    with pytest.raises(Conflict, match="变更"):
        s.consume(p["id"])
    p = s.prepare(oid, "special", "live")
    s.consume(p["id"])
    with pytest.raises(Conflict, match="尚未核对"):
        s.patch(oid, {"scope_excluded": True})


def test_preview_is_single_use_and_snapshot_bound(tmp_path):
    s = Store(tmp_path)
    s.upsert(item())
    p = s.prepare("jd-123456789012", "special", "live")
    s.patch("jd-123456789012", {"company_purchase": True})
    with pytest.raises(Conflict, match="变更"):
        s.consume(p["id"])
    p = s.prepare("jd-123456789012", "special", "live")
    s.consume(p["id"])
    with pytest.raises(Conflict):
        s.consume(p["id"])


def test_uncertain_journal_blocks_new_preview(tmp_path):
    s = Store(tmp_path)
    s.upsert(item())
    p = s.prepare("jd-123456789012", "special", "live")
    s.consume(p["id"])
    s.finish_application("jd-123456789012", "uncertain")
    with pytest.raises(Conflict):
        s.prepare("jd-123456789012", "special", "live")


def test_expiry(tmp_path):
    s = Store(tmp_path)
    s.upsert(item())
    p = s.prepare("jd-123456789012", "special", "live")
    with sqlite3.connect(s.db_path) as db:
        db.execute("UPDATE previews SET expires=0 WHERE id=?", (p["id"],))
    with pytest.raises(Conflict, match="过期"):
        s.consume(p["id"])


def test_new_refund_observation_overrides_old_manual_state(tmp_path):
    s = Store(tmp_path)
    s.upsert(item())
    s.patch("jd-123456789012", {"trade_status": "completed", "company_purchase": True})
    s.upsert(item(source="browser", trade_status="partial_refund"))
    actual = s.get("jd-123456789012")
    assert actual["trade_status"] == "partial_refund" and actual["company_purchase"] is True


def test_restart_turns_unfinished_submit_into_attention(tmp_path):
    s = Store(tmp_path)
    s.upsert(item())
    preview = s.prepare("jd-123456789012", "special", "live")
    s.consume(preview["id"])
    restored = Store(tmp_path)
    assert restored.application("jd-123456789012") == "interrupted"
    with pytest.raises(Conflict):
        restored.prepare("jd-123456789012", "special", "live")
