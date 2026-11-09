"""Fictional API fixtures for the journalled invoice-entry inspection path."""
import json
import sqlite3
import sys
import types

import pytest
from fastapi.testclient import TestClient

from invoice_agent.app import create_app
from invoice_agent.browser import BrowserAttention
from invoice_agent.models import OrderInput, Profile


NUMBER = "202605020001"


class FakeBrowser:
    async def apply(self, *args, **kwargs):
        raise AssertionError("Inspecting a form must not invoke invoice submission")

    async def open(self, *args, **kwargs):
        raise AssertionError("The fake helper must not open a shopping account")

    async def close(self):
        pass


def install_helper(monkeypatch, helper, current=None):
    # Keep this API test independent of the concurrently implemented DOM helper.
    module = types.ModuleType("invoice_agent.form_inspection")
    module.inspect_invoice_form = helper
    if current is not None:
        module.read_current_invoice_form = current
    monkeypatch.setitem(sys.modules, "invoice_agent.form_inspection", module)


def seed(store, platform="jd", **changes):
    data = dict(platform=platform, order_no=NUMBER, product="虚构测试键盘", shop="虚构测试店",
                amount_fen=12345, paid_at="2026-05-02", trade_status="completed",
                invoice_status="unrequested", company_purchase=True,
                special_status="unknown", ordinary_supported=False)
    data.update(changes)
    oid = store.upsert(OrderInput(**data).model_dump(mode="json"))
    return oid


def headers(client, workspace="live"):
    return {"X-Local-Token": client.get("/api/bootstrap").json()["token"], "X-Workspace": workspace}


def job_for(client, response, h):
    assert response.status_code == 200
    return client.get("/api/jobs/" + response.json()["job_id"], headers=h).json()


@pytest.mark.parametrize("changes,workspace", [
    ({"company_purchase": False}, "live"),
    ({"scope_excluded": True, "company_purchase": False}, "live"),
    *[({"trade_status": status}, "live") for status in
      ("unknown", "unpaid", "closed", "refunded", "partial_refund")],
    *[({"invoice_status": status}, "live") for status in
      ("unknown", "pending", "issued", "downloaded", "rejected")],
    ({"amount_fen": 0}, "live"),
    ({}, "demo"),
])
def test_form_inspection_guards_reject_before_browser_or_journal(tmp_path, monkeypatch, changes, workspace):
    calls, factories = [], []

    async def helper(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Ineligible orders must not enter an invoice form")

    def factory(root):
        factories.append(root)
        raise AssertionError("Ineligible orders must not initialize a browser")

    install_helper(monkeypatch, helper)
    app = create_app(tmp_path, testing=True, browser_factory=factory)
    store = app.state.stores[workspace]
    oid = seed(store, **changes)
    before = store.get(oid)
    with TestClient(app) as client:
        response = client.post(f"/api/orders/{oid}/inspect-form", headers=headers(client, workspace), json={})
        assert response.status_code == 409
        assert calls == factories == []
        assert store.application(oid) is None and store.jobs() == []
        assert store.get(oid) == before
        assert not (store.root / "form-inspections").exists()


@pytest.mark.parametrize("platform", ["jd", "taobao"])
@pytest.mark.parametrize("previous", [None, "failed"])
def test_form_inspection_commits_uncertain_before_helper_and_never_submits(tmp_path, monkeypatch, platform, previous):
    calls, factories = [], []
    browser = FakeBrowser()
    app = None
    oid = f"{platform}-{NUMBER}"

    def assert_committed():
        store = app.state.stores["live"]
        assert store.application(oid) == "uncertain"
        # A separate connection proves the reservation was durably committed.
        with sqlite3.connect(store.db_path) as db:
            assert db.execute("SELECT status FROM applications WHERE order_id=?", (oid,)).fetchone() == ("uncertain",)

    def factory(root):
        assert_committed()
        factories.append(root)
        return browser

    async def helper(manager, order, profile):
        assert_committed()
        assert manager is browser and profile == {}
        assert order == before
        calls.append((dict(order), dict(profile)))
        # Even confident observations must not change eligibility or clear journal.
        return dict(entry_clicked=True, final_submission_clicked=False,
                    invoice_type="special", special_status="yes", ordinary_supported=True,
                    application_status="failed", fields=["company_name", "tax_id"])

    install_helper(monkeypatch, helper)
    app = create_app(tmp_path, testing=True, browser_factory=factory)
    store = app.state.stores["live"]
    seed(store, platform)
    store.save_profile(Profile(company_name="虚构测试企业", tax_id="TEST00000000000000"))
    if previous:
        store.reserve_form_inspection(oid)
        store.finish_application(oid, previous)
    before = store.get(oid)
    with TestClient(app) as client:
        h = headers(client)
        route = f"/api/orders/{oid}"
        assert client.get(route + "/form-inspection", headers=h).status_code == 404
        result = job_for(client, client.post(route + "/inspect-form", headers=h, json={}), h)
        assert result["status"] == "done"
        saved = client.get(route + "/form-inspection", headers=h).json()
        assert saved["order_id"] == oid and saved["inspected_at"]
        assert saved["profile_transmitted"] is False
        assert saved["final_submission_clicked"] is False
        path = store.root / "form-inspections" / f"{oid}.json"
        assert json.loads(path.read_text()) == saved
        assert path.stat().st_mode & 0o777 == 0o600
        assert len(calls) == len(factories) == 1
        assert store.application(oid) == "uncertain"
        assert store.get(oid) == before and store.invoices() == []
        with sqlite3.connect(store.db_path) as db:
            assert db.execute("SELECT count(*) FROM previews").fetchone() == (0,)
        again = client.post(route + "/inspect-form", headers=h, json={})
        if again.status_code == 200:
            assert job_for(client, again, h)["status"] == "needs_attention"
        else:
            assert again.status_code == 409
        assert len(calls) == len(factories) == 1
        assert store.application(oid) == "uncertain" and store.get(oid) == before
        assert client.post(route + "/preview", headers=h, json={}).status_code == 409
        assert client.patch(route, headers=h, json={"company_purchase": False}).status_code == 409


@pytest.mark.parametrize("previous", ["submitting", "uncertain", "interrupted", "submitted"])
def test_existing_application_journal_blocks_form_helper(tmp_path, monkeypatch, previous):
    calls = []

    async def helper(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Existing journal must prevent repeated entry")

    def factory(root):
        raise AssertionError("Reservation must reject before browser initialization")

    install_helper(monkeypatch, helper)
    app = create_app(tmp_path, testing=True, browser_factory=factory)
    store = app.state.stores["live"]
    oid = seed(store)
    store.reserve_form_inspection(oid)
    store.finish_application(oid, previous)
    with TestClient(app) as client:
        h = headers(client)
        response = client.post(f"/api/orders/{oid}/inspect-form", headers=h, json={})
        if response.status_code == 200:
            assert job_for(client, response, h)["status"] == "needs_attention"
        else:
            assert response.status_code == 409
        assert calls == [] and store.application(oid) == previous
        assert not (store.root / "form-inspections").exists()


def test_helper_failure_keeps_uncertain_even_if_attention_would_allow_retry(tmp_path, monkeypatch):
    calls = []

    async def helper(manager, order, profile):
        assert profile == {} and store.application(oid) == "uncertain"
        calls.append(order["id"])
        raise BrowserAttention("虚构页面结构不能识别，入口结果尚需核对")

    install_helper(monkeypatch, helper)
    app = create_app(tmp_path, testing=True, browser_factory=lambda root: FakeBrowser())
    store = app.state.stores["live"]
    oid = seed(store)
    before = store.get(oid)
    with TestClient(app) as client:
        h = headers(client)
        response = client.post(f"/api/orders/{oid}/inspect-form", headers=h, json={})
        assert job_for(client, response, h)["status"] == "needs_attention"
        assert calls == [oid] and store.application(oid) == "uncertain"
        assert store.get(oid) == before and store.invoices() == []
        assert not (store.root / "form-inspections").exists()


@pytest.mark.parametrize("changes", [{"company_purchase": False}, {"trade_status": "partial_refund"}])
def test_reservation_rechecks_latest_state_after_initial_route_guard(tmp_path, monkeypatch, changes):
    async def helper(*args, **kwargs):
        raise AssertionError("The latest stored eligibility must be checked before the helper")

    def factory(root):
        raise AssertionError("Changed eligibility must prevent browser initialization")

    install_helper(monkeypatch, helper)
    app = create_app(tmp_path, testing=True, browser_factory=factory)
    store = app.state.stores["live"]
    oid = seed(store)
    original = store.save_job

    def save_job(job):
        original(job)
        if job["status"] == "running":
            store.patch(oid, changes)

    monkeypatch.setattr(store, "save_job", save_job)
    with TestClient(app) as client:
        h = headers(client)
        response = client.post(f"/api/orders/{oid}/inspect-form", headers=h, json={})
        assert job_for(client, response, h)["status"] == "needs_attention"
        assert store.application(oid) is None
        assert not (store.root / "form-inspections").exists()


@pytest.mark.parametrize("previous", [None, "failed", "submitting", "interrupted", "submitted"])
def test_current_form_requires_an_existing_uncertain_journal(tmp_path, monkeypatch, previous):
    async def forbidden(*args, **kwargs):
        raise AssertionError("An ineligible current-only request must not call either helper")

    def factory(root):
        raise AssertionError("An ineligible current-only request must not initialize a browser")

    install_helper(monkeypatch, forbidden, current=forbidden)
    app = create_app(tmp_path, testing=True, browser_factory=factory)
    store = app.state.stores["live"]
    oid = seed(store)
    if previous:
        store.reserve_form_inspection(oid)
        store.finish_application(oid, previous)
    with TestClient(app) as client:
        response = client.post(f"/api/orders/{oid}/inspect-form?current_only=true", headers=headers(client), json={})
        assert response.status_code == 409
        assert store.application(oid) == previous
        assert store.jobs() == [] and not (store.root / "form-inspections").exists()


def test_current_form_reads_cached_form_without_reserving_or_replaying_entry(tmp_path, monkeypatch):
    calls = []

    async def entry(*args, **kwargs):
        raise AssertionError("Current-only inspection must not replay the entry helper")

    async def current(manager, order, profile):
        assert profile == {} and store.application(oid) == "uncertain"
        calls.append(order["id"])
        return dict(current_only=True, entry_clicked=False, final_submission_clicked=False)

    install_helper(monkeypatch, entry, current=current)
    app = create_app(tmp_path, testing=True, browser_factory=lambda root: FakeBrowser())
    store = app.state.stores["live"]
    oid = seed(store)
    store.reserve_form_inspection(oid)
    before = store.get(oid)

    def reserve(*args, **kwargs):
        raise AssertionError("Cached inspection must not create a new reservation")

    monkeypatch.setattr(store, "reserve_form_inspection", reserve)
    with TestClient(app) as client:
        h = headers(client)
        route = f"/api/orders/{oid}"
        for _ in range(2):
            response = client.post(route + "/inspect-form?current_only=true", headers=h, json={})
            assert job_for(client, response, h)["status"] == "done"
        saved = client.get(route + "/form-inspection", headers=h).json()
        assert saved["current_only"] is True and saved["entry_clicked"] is False
        assert saved["profile_transmitted"] is False
        assert calls == [oid, oid]
        assert store.application(oid) == "uncertain" and store.get(oid) == before


@pytest.mark.parametrize("current_only", [False, True])
def test_attention_observation_is_saved_but_job_and_journal_stay_unresolved(tmp_path, monkeypatch, current_only):
    calls = []

    async def attention(manager, order, profile):
        assert profile == {} and store.application(oid) == "uncertain"
        calls.append(order["id"])
        error = BrowserAttention("虚构表单尚未完成加载，需要继续核对")
        error.observation = dict(entry_clicked=not current_only, current_only=current_only,
                                 final_submission_clicked=False, fields=[], complete=False)
        raise error

    async def forbidden(*args, **kwargs):
        raise AssertionError("Current-only failure must not replay the entry")

    install_helper(monkeypatch, forbidden if current_only else attention, current=attention)
    app = create_app(tmp_path, testing=True, browser_factory=lambda root: FakeBrowser())
    store = app.state.stores["live"]
    oid = seed(store)
    before = store.get(oid)
    if current_only:
        store.reserve_form_inspection(oid)
    with TestClient(app) as client:
        h = headers(client)
        route = f"/api/orders/{oid}"
        response = client.post(route + f"/inspect-form?current_only={str(current_only).lower()}", headers=h, json={})
        assert job_for(client, response, h)["status"] == "needs_attention"
        saved = client.get(route + "/form-inspection", headers=h).json()
        assert saved["complete"] is False and saved["profile_transmitted"] is False
        assert saved["entry_clicked"] is not current_only
        path = store.root / "form-inspections" / f"{oid}.json"
        assert path.stat().st_mode & 0o777 == 0o600
        assert calls == [oid] and store.application(oid) == "uncertain"
        assert store.get(oid) == before and store.invoices() == []
