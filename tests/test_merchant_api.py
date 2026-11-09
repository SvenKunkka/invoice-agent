"""Merchant-open API boundaries with fake modules and fictional local orders."""
import json
import sqlite3
import sys
import types

import pytest
from fastapi.testclient import TestClient

from invoice_agent.app import create_app
from invoice_agent.models import OrderInput, Profile


NUMBER = "202605030001"


class FakeBrowser:
    async def apply(self, *args, **kwargs):
        raise AssertionError("Merchant inspection must not submit an invoice")

    async def send(self, *args, **kwargs):
        raise AssertionError("Opening a merchant chat must not send a message")

    async def close(self):
        pass


def install_merchant(monkeypatch, helper, inspector=None):
    module = types.ModuleType("invoice_agent.merchant")
    module.open_merchant_chat = helper
    if inspector is not None:
        module.inspect_merchant_chat = inspector
    monkeypatch.setitem(sys.modules, "invoice_agent.merchant", module)


def seed(store, platform="jd", **changes):
    data = dict(platform=platform, order_no=NUMBER, product="虚构测试鼠标", shop="虚构原商家",
                amount_fen=8800, paid_at="2026-05-03", trade_status="completed",
                invoice_status="unrequested", company_purchase=True)
    data.update(changes)
    return store.upsert(OrderInput(**data).model_dump(mode="json"))


def headers(client, workspace="live"):
    return {"X-Local-Token": client.get("/api/bootstrap").json()["token"], "X-Workspace": workspace}


@pytest.mark.parametrize("changes,workspace", [
    ({"company_purchase": False}, "live"),
    ({"company_purchase": False, "scope_excluded": True}, "live"),
    ({}, "demo"),
])
def test_merchant_open_requires_live_confirmed_nonexcluded_company_order(tmp_path, monkeypatch, changes, workspace):
    calls = []

    async def helper(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Ineligible order must not open a merchant chat")

    def factory(root):
        raise AssertionError("Rejected merchant request must not initialize a browser")

    install_merchant(monkeypatch, helper)
    app = create_app(tmp_path, testing=True, browser_factory=factory)
    store = app.state.stores[workspace]
    oid = seed(store, **changes)
    before = store.get(oid)
    with TestClient(app) as client:
        response = client.post(f"/api/orders/{oid}/merchant/open", headers=headers(client, workspace), json={})
        assert response.status_code == 409
        assert calls == [] and store.jobs() == []
        assert store.get(oid) == before and store.application(oid) is None
        assert not (store.root / "merchant-inspections").exists()


@pytest.mark.parametrize("platform", ["jd", "taobao"])
@pytest.mark.parametrize("journal", [None, "uncertain"])
def test_merchant_open_saves_inspection_forces_unsent_and_preserves_records(tmp_path, monkeypatch, platform, journal):
    calls = []
    browser = FakeBrowser()

    async def helper(manager, order):
        assert manager is browser and order == before
        calls.append(order["id"])
        # A helper's optimistic self-report cannot create a sent-message result.
        return dict(merchant_verified=True, recipient="虚构原商家", message_sent=True)

    install_merchant(monkeypatch, helper)
    app = create_app(tmp_path, testing=True, browser_factory=lambda root: browser)
    store = app.state.stores["live"]
    oid = seed(store, platform)
    store.save_profile(Profile(company_name="虚构测试企业", tax_id="TEST00000000000000"))
    if journal:
        store.reserve_form_inspection(oid)
    before = store.get(oid)

    def table_counts():
        with sqlite3.connect(store.db_path) as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            # Names originate from the controlled application schema, not input.
            return {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                    for name in names if name not in ("jobs", "events", "sqlite_sequence")}

    baseline = table_counts()
    with TestClient(app) as client:
        h = headers(client)
        route = f"/api/orders/{oid}/merchant"
        assert client.get(route + "/inspection", headers=h).status_code == 404
        response = client.post(route + "/open", headers=h, json={})
        assert response.status_code == 200
        job = client.get("/api/jobs/" + response.json()["job_id"], headers=h).json()
        assert job["status"] == "done"
        saved = client.get(route + "/inspection", headers=h).json()
        assert saved["order_id"] == oid and saved["observed_at"]
        assert saved["message_sent"] is False and saved["recipient"] == "虚构原商家"
        path = store.root / "merchant-inspections" / f"{oid}.json"
        assert json.loads(path.read_text()) == saved
        assert path.stat().st_mode & 0o777 == 0o600
        assert calls == [oid] and store.get(oid) == before
        assert store.application(oid) == journal
        assert table_counts() == baseline  # No send/application/preview/invoice records added.
        assert client.get(route + "/inspection", headers=headers(client, "demo")).status_code == 404


@pytest.mark.parametrize("changes", [
    {"company_purchase": False},
    {"company_purchase": False, "scope_excluded": True},
])
def test_merchant_open_rechecks_company_scope_when_queued_task_starts(tmp_path, monkeypatch, changes):
    calls = []

    async def helper(manager, order):
        calls.append(order["id"])
        return {"message_sent": False}

    def factory(root):
        raise AssertionError("Revoked eligibility must be checked before opening the browser")

    install_merchant(monkeypatch, helper)
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
        response = client.post(f"/api/orders/{oid}/merchant/open", headers=h, json={})
        assert response.status_code == 200
        job = client.get("/api/jobs/" + response.json()["job_id"], headers=h).json()
        assert job["status"] == "needs_attention"
        assert calls == [] and store.application(oid) is None
        assert not (store.root / "merchant-inspections").exists()


def observed_chat(platform="jd"):
    return ("https://im.jd.com/chat?shopId=fictional" if platform == "jd"
            else "https://webww.taobao.com/chat?shopId=fictional")


def write_open_result(store, oid, **changes):
    proof = dict(order_id=oid, shop_verified=True, contact_activated=True,
                 current_url=observed_chat(oid.split("-", 1)[0]), message_sent=False)
    proof.update(changes)
    folder = store.root / "merchant-inspections"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{oid}.json"
    path.write_text(json.dumps(proof), encoding="utf-8")
    return path


@pytest.mark.parametrize("case,expected", [
    ("no_open_record", 409),
    ("wrong_order", 409),
    ("shop_unverified", 409),
    ("contact_not_activated", 409),
    ("missing_url", 409),
    ("external_url", 422),
    ("other_platform_url", 422),
    ("http_url", 422),
    ("credential_url", 422),
    ("company_unconfirmed", 409),
    ("excluded", 409),
    ("demo", 409),
])
def test_merchant_inspect_requires_verified_same_order_open_record(tmp_path, monkeypatch, case, expected):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Rejected inspection must neither reopen nor inspect a chat")

    def factory(root):
        raise AssertionError("Invalid chat provenance must not initialize a browser")

    install_merchant(monkeypatch, forbidden, inspector=forbidden)
    app = create_app(tmp_path, testing=True, browser_factory=factory)
    workspace = "demo" if case == "demo" else "live"
    store = app.state.stores[workspace]
    changes = {"company_purchase": False} if case in ("company_unconfirmed", "excluded") else {}
    if case == "excluded":
        changes["scope_excluded"] = True
    oid = seed(store, **changes)
    alterations = {
        "wrong_order": {"order_id": "jd-202605039999"},
        "shop_unverified": {"shop_verified": False},
        "contact_not_activated": {"contact_activated": False},
        "missing_url": {"current_url": ""},
        "external_url": {"current_url": "https://invalid.example/chat"},
        "other_platform_url": {"current_url": observed_chat("taobao")},
        "http_url": {"current_url": "http://im.jd.com/chat"},
        "credential_url": {"current_url": "https://user:secret@im.jd.com/chat"},
    }.get(case, {})
    if case != "no_open_record":
        write_open_result(store, oid, **alterations)
    before = store.get(oid)
    with TestClient(app) as client:
        # Even an official-looking URL supplied by the caller is not provenance.
        response = client.post(f"/api/orders/{oid}/merchant/inspect", headers=headers(client, workspace),
                               json={"current_url": observed_chat(), "observed_chat_url": observed_chat()})
        assert response.status_code == expected
        assert store.jobs() == [] and store.get(oid) == before
        assert store.application(oid) is None


@pytest.mark.parametrize("platform", ["jd", "taobao"])
@pytest.mark.parametrize("url_field", ["current_url", "observed_chat_url"])
def test_merchant_inspect_uses_observed_url_only_and_never_reopens_or_sends(tmp_path, monkeypatch, platform, url_field):
    calls = []
    browser = FakeBrowser()
    chosen = observed_chat(platform)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Inspection must not replay the contact activation")

    async def inspector(manager, order, observed_url):
        assert manager is browser and order == before and observed_url == chosen
        calls.append((order["id"], observed_url))
        return dict(observed_chat_url="https://invalid.example/must-not-replace-provenance",
                    message_sent=True, recipient="虚构原商家", input_found=False)

    install_merchant(monkeypatch, forbidden, inspector=inspector)
    app = create_app(tmp_path, testing=True, browser_factory=lambda root: browser)
    store = app.state.stores["live"]
    oid = seed(store, platform)
    proof_changes = {url_field: chosen}
    if url_field == "observed_chat_url":
        proof_changes["current_url"] = observed_chat(platform) + "&old=true"
    path = write_open_result(store, oid, **proof_changes)
    before = store.get(oid)
    with TestClient(app) as client:
        h = headers(client)
        response = client.post(f"/api/orders/{oid}/merchant/inspect", headers=h,
                               json={"observed_chat_url": "https://invalid.example/body-cannot-select-target"})
        assert response.status_code == 200
        job = client.get("/api/jobs/" + response.json()["job_id"], headers=h).json()
        assert job["status"] == "done"
        saved = client.get(f"/api/orders/{oid}/merchant/inspection", headers=h).json()
        assert saved["order_id"] == oid and saved["observed_at"]
        assert saved["observed_chat_url"] == chosen and saved["message_sent"] is False
        assert saved["input_found"] is False
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text()) == saved
        assert calls == [(oid, chosen)] and store.get(oid) == before
        assert store.application(oid) is None and store.invoices() == []


@pytest.mark.parametrize("changes", [
    {"company_purchase": False},
    {"company_purchase": False, "scope_excluded": True},
])
def test_merchant_inspect_rechecks_company_scope_before_helper(tmp_path, monkeypatch, changes):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Scope revoked while queued must prevent any chat helper")

    def factory(root):
        raise AssertionError("Revoked scope must be checked before browser initialization")

    install_merchant(monkeypatch, forbidden, inspector=forbidden)
    app = create_app(tmp_path, testing=True, browser_factory=factory)
    store = app.state.stores["live"]
    oid = seed(store)
    path = write_open_result(store, oid)
    original_proof = path.read_bytes()
    original = store.save_job

    def save_job(job):
        original(job)
        if job["status"] == "running":
            store.patch(oid, changes)

    monkeypatch.setattr(store, "save_job", save_job)
    with TestClient(app) as client:
        h = headers(client)
        response = client.post(f"/api/orders/{oid}/merchant/inspect", headers=h, json={})
        assert response.status_code == 200
        job = client.get("/api/jobs/" + response.json()["job_id"], headers=h).json()
        assert job["status"] == "needs_attention"
        assert path.read_bytes() == original_proof and store.application(oid) is None
