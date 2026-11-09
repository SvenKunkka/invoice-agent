"""API checks with fictional coverage only; no shopping account is opened."""
import json

import pytest
from fastapi.testclient import TestClient

from invoice_agent.app import create_app
from invoice_agent.models import Profile


NUMBER = "202605010001"
OTHER = "202605010002"


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def apply(self, *args, **kwargs):
        raise AssertionError("Read-only inspection must not apply for an invoice")

    async def open(self, *args, **kwargs):
        raise AssertionError("The inspection stub must not open an account")

    async def close(self):
        self.closed = True


def reference(platform="jd", number=NUMBER):
    url = (f"https://order.jd.com/order/detail.action?orderId={number}" if platform == "jd"
           else f"https://trade.taobao.com/trade/detail/trade_order_detail.htm?biz_order_id={number}")
    return dict(platform=platform, order_no=number, paid_at="2026-05-01", observed_url=url)


def write_coverage(store, platform, records):
    folder = store.root / "coverage"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{platform}.json").write_text(
        json.dumps({"report": {"unresolved_records": records}}, ensure_ascii=False), encoding="utf-8")


def headers(client, workspace="live"):
    return {"X-Local-Token": client.get("/api/bootstrap").json()["token"], "X-Workspace": workspace}


@pytest.mark.parametrize("case,expected", [
    ("no_coverage", 404),
    ("empty_records", 409),
    ("wrong_platform", 409),
    ("wrong_number", 409),
    ("duplicate_reference", 409),
    ("invalid_number", 422),
    ("invalid_platform", 422),
    ("demo", 409),
])
def test_unresolved_rejects_untrusted_references_before_browser(tmp_path, monkeypatch, case, expected):
    factories, inspections = [], []

    def factory(root):
        factories.append(root)
        raise AssertionError("Rejected requests must not create a browser")

    async def inspect(*args, **kwargs):
        inspections.append(args)
        raise AssertionError("Rejected requests must not inspect a page")

    monkeypatch.setattr("invoice_agent.inspection.inspect_order", inspect)
    app = create_app(tmp_path, testing=True, browser_factory=factory)
    records = {
        "empty_records": [],
        "wrong_platform": [reference("taobao")],
        "wrong_number": [reference(number=OTHER)],
        "duplicate_reference": [reference(), reference()],
    }.get(case, [reference()])
    if case != "no_coverage":
        write_coverage(app.state.stores["live"], "jd", records)
    if case == "demo":
        # Even a valid reference in the demo store must not reach a real browser.
        write_coverage(app.state.stores["demo"], "jd", [reference()])
    platform = "invalid" if case == "invalid_platform" else "jd"
    number = "not-an-order" if case == "invalid_number" else NUMBER
    route = f"/api/connections/{platform}/unresolved/{number}"
    with TestClient(app) as client:
        h = headers(client, "demo" if case == "demo" else "live")
        assert client.post(route + "/inspect", headers=h, json={}).status_code == expected
        assert client.get(route + "/inspection", headers=h).status_code == expected
        assert factories == inspections == []
        for store in app.state.stores.values():
            assert store.orders() == [] and store.jobs() == []
            assert store.application(f"{platform}-{NUMBER}") is None
            assert not (store.root / "inspections").exists()


@pytest.mark.parametrize("platform", ["jd", "taobao"])
def test_unresolved_saves_only_read_only_observation_for_exact_reference(tmp_path, monkeypatch, platform):
    factories, inspections = [], []
    browser = FakeBrowser()

    def factory(root):
        factories.append(root)
        return browser

    selected = reference(platform)

    async def inspect(manager, order, profile):
        inspections.append((manager, dict(order), dict(profile)))
        assert manager is browser
        assert order == {"platform": platform, "order_no": NUMBER, "paid_at": selected["paid_at"],
                         "order_url": selected["observed_url"]}
        assert profile == {}  # Local company credentials do not enter this path.
        return dict(observed_order_url=selected["observed_url"],
                    observations={"trade": "completed", "transaction": {"amount_fen": None}},
                    missing_fields=["transaction.amount_fen"], complete=False)

    monkeypatch.setattr("invoice_agent.inspection.inspect_order", inspect)
    app = create_app(tmp_path, testing=True, browser_factory=factory)
    store = app.state.stores["live"]
    store.save_profile(Profile(company_name="虚构测试企业", tax_id="TEST00000000000000"))
    # Only the exact platform/number pair is eligible, even in a mixed report.
    other_platform = "taobao" if platform == "jd" else "jd"
    write_coverage(store, platform, [selected, reference(other_platform), reference(platform, OTHER)])
    route = f"/api/connections/{platform}/unresolved/{NUMBER}"
    with TestClient(app) as client:
        h = headers(client)
        assert client.get(route + "/inspection", headers=h).status_code == 404
        response = client.post(route + "/inspect", headers=h, json={})
        assert response.status_code == 200
        job = client.get("/api/jobs/" + response.json()["job_id"], headers=h).json()
        assert job["status"] == "done"
        result = client.get(route + "/inspection", headers=h).json()
        assert result["read_only"] is True and result["record_imported"] is False
        assert result["reference"] == selected and result["inspected_at"]
        assert result["observations"]["transaction"]["amount_fen"] is None
        assert result["missing_fields"] == ["transaction.amount_fen"]
        path = store.root / "inspections" / f"unresolved-{platform}-{NUMBER}.json"
        assert json.loads(path.read_text()) == result
        assert path.stat().st_mode & 0o777 == 0o600
        assert len(factories) == len(inspections) == 1
        assert store.orders() == store.invoices() == []
        assert store.application(f"{platform}-{NUMBER}") is None
        data = client.get("/api/bootstrap", headers=h).json()
        assert data["orders"] == [] and data["summary"]["company_total"] == 0
        # A stale result must not be served after the report becomes ambiguous.
        write_coverage(store, platform, [selected, selected])
        assert client.get(route + "/inspection", headers=h).status_code == 409
        assert len(factories) == len(inspections) == 1
    assert browser.closed is True
