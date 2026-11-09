from fastapi.testclient import TestClient

from invoice_agent.app import create_app
from invoice_agent.models import Profile


def test_preview_only_allows_a_demo_application_and_blocks_live_submission(tmp_path):
    def browser_must_not_open(*_):
        raise AssertionError("The isolated smoke test must not contact a shopping platform")

    app = create_app(tmp_path, testing=True, preview_only=True, browser_factory=browser_must_not_open)
    live = app.state.stores["live"]
    live.save_profile(Profile(company_name="虚构测试企业", tax_id="91310000123456789X",
                              address="示例地址", phone="021-12345678", bank_name="示例银行", bank_account="123456789012"))
    oid = live.upsert(dict(platform="jd", order_no="888888888901", product="虚构测试键盘", shop="示例店铺",
                           amount_fen=8800, paid_at="2026-11-06", trade_status="completed", invoice_status="unrequested",
                           company_purchase=True, special_status="yes", evidence="虚构页面支持专票"))
    with TestClient(app) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["live_submit_enabled"] is False
        live_headers = {"X-Local-Token": bootstrap["token"], "X-Workspace": "live"}
        preview = client.post(f"/api/orders/{oid}/preview", headers=live_headers, json={})
        assert preview.status_code == 200
        assert client.post(f"/api/previews/{preview.json()['id']}/execute", headers=live_headers, json={}).status_code == 409
        assert client.post(f"/api/orders/{oid}/inspect-form", headers=live_headers, json={}).status_code == 409
        assert live.application(oid) is None and not live.jobs()

        demo_headers = live_headers | {"X-Workspace": "demo"}
        seed = client.post("/api/demo/seed", headers=demo_headers, json={})
        assert seed.status_code == 200
        demo_order = next(o for o in seed.json()["orders"] if o["decision"]["action"] == "apply_special")
        demo_preview = client.post(f"/api/orders/{demo_order['id']}/preview", headers=demo_headers, json={})
        assert demo_preview.status_code == 200
        execute = client.post(f"/api/previews/{demo_preview.json()['id']}/execute", headers=demo_headers, json={})
        assert execute.status_code == 200
        job = client.get(f"/api/jobs/{execute.json()['job_id']}", headers=demo_headers).json()
        assert job["status"] == "done" and "未向淘宝或京东提交" in job["message"]
        assert live.application(oid) is None and live.get(oid)["invoice_status"] == "unrequested"
