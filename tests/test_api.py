import csv
import io
import pytest
from fastapi.testclient import TestClient
from invoice_agent.app import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, testing=True)) as c:
        yield c


def headers(c, workspace="demo"):
    return {"X-Local-Token": c.get("/api/bootstrap").json()["token"], "X-Workspace": workspace}


def seed(c):
    h = headers(c)
    assert c.post("/api/demo/seed", headers=h, json={}).status_code == 200
    return h


def test_real_workspace_empty_and_demo_is_separate(client):
    assert client.get("/api/bootstrap").json()["orders"] == []
    h = seed(client)
    assert len(client.get("/api/bootstrap", headers=h).json()["orders"]) >= 6
    assert client.get("/api/bootstrap").json()["orders"] == []
    assert client.post("/api/demo/seed", headers=headers(client, "live"), json={}).status_code == 409


def test_installed_app_exposes_only_live_workspace(tmp_path):
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as c:
        data = c.get("/api/bootstrap").json()
        assert data["workspace"] == "live" and data["orders"] == []
        assert c.get("/api/bootstrap?workspace=demo").status_code == 422
        assert c.get("/api/bootstrap", headers={"X-Workspace": "demo"}).status_code == 422
        assert c.post("/api/demo/seed", headers={"X-Local-Token": data["token"]}, json={}).status_code in (404, 405)
        assert not (tmp_path / "demo").exists()


def test_untrusted_site_and_missing_token_cannot_mutate(client):
    assert client.post("/api/demo/seed", json={}).status_code == 403
    assert client.get("/api/bootstrap", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/bootstrap", headers={"Host": "evil.example"}).status_code == 403


def test_preview_approval_replay_and_state(client):
    h = seed(client)
    data = client.get("/api/bootstrap", headers=h).json()
    oid = next(o["id"] for o in data["orders"] if o["decision"]["action"] == "apply_special")
    p = client.post(f"/api/orders/{oid}/preview", headers=h, json={})
    assert p.status_code == 200
    ident = p.json()["id"]
    execute = client.post(f"/api/previews/{ident}/execute", headers=h, json={})
    assert execute.status_code == 200
    job = client.get("/api/jobs/" + execute.json()["job_id"], headers=h).json()
    assert job["status"] == "done"
    assert client.post(f"/api/previews/{ident}/execute", headers=h, json={}).status_code == 409
    order = next(o for o in client.get("/api/bootstrap", headers=h).json()["orders"] if o["id"] == oid)
    assert order["invoice_status"] == "pending"


def test_profile_change_invalidates_preview(client):
    h = seed(client)
    d = client.get("/api/bootstrap", headers=h).json()
    o = next(o for o in d["orders"] if o["decision"]["action"] == "apply_special")
    p = client.post(f"/api/orders/{o['id']}/preview", headers=h, json={}).json()
    profile = {k: v for k, v in d["profile"].items() if k != "configured"}
    profile["company_name"] = "修改后的示例企业"
    assert client.put("/api/profile", headers=h, json=profile).status_code == 200
    assert client.post(f"/api/previews/{p['id']}/execute", headers=h, json={}).status_code == 409


def test_archive_and_csv_are_accessible_in_same_workspace(client):
    h = seed(client)
    orders = client.get("/api/bootstrap", headers=h).json()["orders"]
    o = next(o for o in orders if o["decision"]["action"] == "download")
    r = client.post(f"/api/orders/{o['id']}/download", headers=h, json={})
    assert r.status_code == 200
    d = client.get("/api/bootstrap", headers=h).json()
    assert len(d["invoices"]) == 1
    invoice = d["invoices"][0]
    result = client.get(invoice["download_url"], headers=h)
    assert result.status_code == 200 and b"EXAMPLE" in result.content
    assert client.get(f"/api/invoices/{invoice['id']}/file").status_code == 404
    export = client.get("/api/export?month=2026-11", headers=h)
    assert export.status_code == 200
    rows = list(csv.reader(io.StringIO(export.content.decode("utf-8-sig"))))
    assert len(rows) > 6 and "示例" in export.text


def test_import_validates_external_values_and_duplicate_protection(client):
    h = headers(client, "live")
    o = dict(platform="jd", order_no="123456789012", product="=SUM(1,2)", shop="测试店",
             amount_fen=1230, paid_at="2026-11-06", trade_status="completed")
    assert client.post("/api/orders/import", headers=h, json={"orders": [o]}).status_code == 200
    assert client.post("/api/orders/import", headers=h, json={"orders": [o]}).json()["skipped"] == 1
    csv_text = client.get("/api/export?month=2026-11&scope=all", headers=h).text
    assert "'=SUM" in csv_text
    o["amount_fen"] = 12.3
    assert client.post("/api/orders/import", headers=h, json={"orders": [o]}).status_code == 422


def test_demo_cannot_open_real_browser(client):
    assert client.post("/api/connections/jd/open", headers=headers(client), json={}).status_code == 409


def test_unapproved_ordinary_draft_returns_readable_error(client):
    h = seed(client)
    data = client.get("/api/bootstrap", headers=h).json()
    profile = {k: v for k, v in data["profile"].items() if k != "configured"}
    profile["allow_ordinary"] = False
    client.put("/api/profile", headers=h, json=profile)
    o = next(o for o in data["orders"] if o["special_status"] == "no")
    response = client.get(f"/api/orders/{o['id']}/draft?kind=request", headers=h)
    assert response.status_code == 422
    assert "普票" in response.json()["detail"]


def test_import_cannot_mark_archived_without_file(client):
    h = headers(client, "live")
    o = dict(platform="jd", order_no="123456789012", product="测试商品", shop="测试店", amount_fen=1230,
             paid_at="2026-11-06", trade_status="completed", invoice_status="downloaded", company_purchase=True)
    client.post("/api/orders/import", headers=h, json={"orders": [o]})
    actual = client.get("/api/bootstrap", headers=h).json()["orders"][0]
    assert actual["invoice_status"] != "downloaded" and actual["company_purchase"] is False


def test_manual_page_links_remain_platform_scoped(client):
    h = seed(client)
    oid = "jd-202611060008"
    assert client.patch(f"/api/orders/{oid}", headers=h, json={"order_url": "https://evil.example/"}).status_code == 422
    assert client.patch(f"/api/orders/{oid}", headers=h, json={"order_url": "https://trade.jd.com/trade/orderInfo.action?orderId=202611060008"}).status_code == 200


def test_submission_failure_requires_explicit_reconciliation(tmp_path):
    class Browser:
        async def apply(self, *args):
            return {"status": "uncertain", "message": "提交后无法确认"}
        async def close(self):
            pass
    app = create_app(tmp_path, testing=True, browser_factory=lambda root: Browser())
    with TestClient(app) as c:
        h = headers(c, "live")
        # Use seeded fictional records only in this isolated temporary test store.
        from invoice_agent.demo import seed as fixture_seed
        fixture_seed(app.state.stores["live"])
        oid = "jd-202611060001"
        preview = c.post(f"/api/orders/{oid}/preview", headers=h, json={}).json()
        job_id = c.post(f"/api/previews/{preview['id']}/execute", headers=h, json={}).json()["job_id"]
        assert c.get(f"/api/jobs/{job_id}", headers=h).json()["status"] == "needs_attention"
        assert c.post(f"/api/orders/{oid}/preview", headers=h, json={}).status_code == 409
        assert c.post(f"/api/orders/{oid}/reconcile", headers=h,
                      json={"outcome": "not_submitted", "evidence": "核对"}).status_code == 422
        assert c.post(f"/api/orders/{oid}/reconcile", headers=h,
                      json={"outcome": "not_submitted", "evidence": "已在官方订单详情核对，页面明确显示未申请"}).status_code == 200
        assert c.post(f"/api/orders/{oid}/preview", headers=h, json={}).status_code == 200


def test_scan_keeps_valid_records_when_one_is_invalid(tmp_path):
    class Browser:
        last_scan_report = {"pages": 1, "complete": False, "reason": "指定范围覆盖未验证"}
        async def scan(self, *args):
            o = dict(platform="jd", order_no="123456789012", product="测试商品", shop="测试店", amount_fen=1230,
                     paid_at="2026-11-06", trade_status="completed", source="browser")
            return [o, o | {"order_no": "123456789013", "amount_fen": -1}]
        async def close(self):
            pass
    with TestClient(create_app(tmp_path, testing=True, browser_factory=lambda root: Browser())) as c:
        h = headers(c, "live")
        response = c.post("/api/connections/jd/scan", headers=h, json={"start_date": "2026-11-01", "end_date": "2026-11-06", "max_pages": 1})
        j = c.get("/api/jobs/" + response.json()["job_id"], headers=h).json()
        assert j["status"] == "done" and "1 笔字段" in j["message"]
        assert len(c.get("/api/bootstrap", headers=h).json()["orders"]) == 1


def test_scan_persists_request_and_page_coverage(tmp_path):
    class Browser:
        last_scan_report = {"complete": True, "pages": [{"page": 1, "parsed": 1}], "range_verified": True}
        async def scan(self, *args):
            return [dict(platform="jd", order_no="123456789012", product="测试键盘", shop="测试店", amount_fen=1230,
                         paid_at="2026-10-06", trade_status="completed", source="browser")]
        async def close(self):
            pass
    with TestClient(create_app(tmp_path, testing=True, browser_factory=lambda root: Browser())) as c:
        h = headers(c, "live")
        c.post("/api/connections/jd/scan", headers=h, json={"start_date": "2026-04-16", "end_date": "2026-11-06", "max_pages": 100})
        r = c.get("/api/coverage", headers=h)
        assert r.status_code == 200
        coverage = r.json()["jd"]
        assert coverage["requested_start_date"] == "2026-04-16" and coverage["imported"] == 1
        assert coverage["report"]["complete"] is True
        assert c.get("/api/coverage", headers=headers(c, "demo")).json() == {}


def test_inspection_is_read_only_and_unknown_does_not_erase_state(tmp_path, monkeypatch):
    async def inspect(manager, order, profile):
        return {"observed_order_url": "https://trade.jd.com/trade/orderInfo.action?orderId=202611060001",
                "observations": {"invoice": {"invoice_status": "unknown", "invoice_type": "unknown", "special_status": "unknown"},
                                 "trade": "completed", "transaction": {"amount_fen": 32900}, "buyer": {}},
                "invoice_controls": [], "diagnostic_path": "", "missing_fields": ["invoice_status"], "complete": False}
    monkeypatch.setattr("invoice_agent.inspection.inspect_order", inspect)
    class Browser:
        async def close(self):
            pass
    app = create_app(tmp_path, testing=True, browser_factory=lambda root: Browser())
    from invoice_agent.demo import seed as fixture_seed
    fixture_seed(app.state.stores["live"])
    with TestClient(app) as c:
        h = headers(c, "live");oid = "jd-202611060001"
        r = c.post(f"/api/orders/{oid}/inspect", headers=h, json={})
        assert r.status_code == 200
        result = c.get(f"/api/orders/{oid}/inspection", headers=h).json()
        assert result["read_only"] is True and result["missing_fields"] == ["invoice_status"]
        assert app.state.stores["live"].get(oid)["invoice_status"] == "unrequested"
        assert app.state.stores["live"].application(oid) is None
        assert c.post(f"/api/orders/{oid}/inspect", headers=headers(c, "demo"), json={}).status_code == 409


def test_unrelated_import_is_not_company_purchase_or_monthly_export(client):
    h = headers(client, "live")
    item = dict(platform="jd", order_no="888888888801", product="虚构家用鱼缸", shop="示例店", amount_fen=25900, paid_at="2026-11-06", trade_status="completed")
    client.post("/api/orders/import", headers=h, json={"orders": [item]})
    oid = "jd-888888888801"
    d = client.get("/api/bootstrap", headers=h).json()
    assert d["summary"]["total"] == 1 and d["summary"]["company_total"] == 0
    assert d["summary"]["amount_fen"] == 0
    assert "虚构家用鱼缸" not in client.get("/api/export?month=2026-11", headers=h).text
    assert client.post(f"/api/orders/{oid}/preview", headers=h, json={}).status_code == 409
    r = client.patch(f"/api/orders/{oid}", headers=h, json={"scope_excluded": True, "scope_reason": "不在键盘鼠标耳机范围"})
    assert r.status_code == 200 and r.json()["company_purchase"] is False
    assert r.json()["decision"]["action"] == "skip"
    assert client.get(f"/api/orders/{oid}/draft", headers=h).status_code == 409
    assert client.patch(f"/api/orders/{oid}", headers=h, json={"scope_excluded": True, "company_purchase": True}).status_code == 422
    d = client.get("/api/bootstrap", headers=h).json()
    assert d["summary"]["excluded"] == 1 and d["summary"]["company_total"] == 0
    r = client.patch(f"/api/orders/{oid}", headers=h, json={"company_purchase": True})
    assert r.status_code == 200 and r.json()["scope_excluded"] is False
    assert "虚构家用鱼缸" in client.get("/api/export?month=2026-11", headers=h).text
    r = client.patch(f"/api/orders/{oid}", headers=h, json={"scope_excluded": True})
    assert r.json()["company_purchase"] is False
