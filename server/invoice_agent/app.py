import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import re
import secrets
import threading
import tempfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import demo
from .engine import decide, merchant_draft
from .models import ImportRequest, OrderInput, OrderPatch, Platform, Profile, ScanRequest, ReconcileRequest, platform_url
from .store import Conflict, Store, now

logger = logging.getLogger(__name__)
VERSION = "0.1.0"


def create_app(data_root: Path | None = None, *, testing=False, browser_factory=None, preview_only: bool | None = None):
    if preview_only is None:
        preview_only = os.environ.get("INVOICE_AGENT_PREVIEW_ONLY", "0") == "1"
    root = Path(data_root or os.environ.get("INVOICE_AGENT_DATA", Path.home() / "Library/Application Support/InvoiceAgent"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    stores = {"live": Store(root / "live")}
    if testing:
        stores["demo"] = Store(root / "demo")
    token = secrets.token_urlsafe(32)
    browser = None
    connections = {p: dict(platform=p, status="disconnected", message="尚未打开专用浏览器") for p in ("taobao", "jd")}
    active = set()
    active_lock = threading.Lock()

    def get_browser():
        nonlocal browser
        if browser is None:
            if browser_factory:
                browser = browser_factory(root / "live")
            else:
                from .browser import BrowserManager
                browser = BrowserManager(root / "live")
        return browser

    @asynccontextmanager
    async def lifespan(app):
        yield
        if browser:
            await browser.close()

    app = FastAPI(title="票夹 · 本地取票助手", version=VERSION, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None)
    app.state.stores = stores

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = urlparse("http://" + request.headers.get("host", "")).hostname
        allowed = {"localhost", "127.0.0.1", "::1"} | ({"testserver"} if testing else set())
        if host not in allowed:
            return JSONResponse({"detail": "仅允许从本机地址访问"}, status_code=403)
        if request.url.path.startswith("/api"):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "来源校验未通过，请在本机工具页面操作"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "不允许外部网页访问本地数据"}, status_code=403)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                received = request.headers.get("x-local-token", "")
                if not secrets.compare_digest(received, token):
                    return JSONResponse({"detail": "请刷新页面后重试"}, status_code=403)
            try:
                if int(request.headers.get("content-length", "0")) > 3 * 1024 * 1024:
                    return JSONResponse({"detail": "导入内容不能超过 3 MiB"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "无效请求长度"}, status_code=400)
        response = await call_next(request)
        response.headers.update({
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store" if request.url.path.startswith("/api") else "no-cache",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        })
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc).strip("'")}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        errors = [f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}" for e in exc.errors()[:5]]
        return JSONResponse({"detail": "；".join(errors)}, status_code=422)

    def context(request):
        space = request.headers.get("x-workspace", request.query_params.get("workspace", "live"))
        if space not in stores:
            raise HTTPException(422, "工作区不可用")
        return space, stores[space]

    def persist_coverage(store, platform, body, report, imported, invalid, status):
        folder = store.root / "coverage"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = dict(scan_id=secrets.token_hex(12), platform=platform,
                       requested_start_date=body.start_date.isoformat(), requested_end_date=body.end_date.isoformat(),
                       max_pages=body.max_pages, recorded_at=now(), imported=imported, invalid=invalid,
                       status=status, report=report)
        if status != "done" or invalid:
            payload["report"] = dict(report, complete=False)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        fd, temporary = tempfile.mkstemp(prefix=platform + "-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(encoded)
            os.replace(temporary, folder / f"{platform}.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        history = folder / "history.jsonl"
        with history.open("a", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def enriched(store):
        profile = store.profile()
        return [dict(scope_excluded=False, scope_reason="") | o | {"decision": decide(o, profile, store.application(o["id"]))} for o in store.orders()]

    def invoice_list(store, space):
        return [{k: v for k, v in i.items() if k != "path"} | {
            "download_url": f"/api/invoices/{i['id']}/file?{urlencode({'workspace': space, 'token': token})}"
        } for i in store.invoices()]

    def bootstrap(space, store):
        orders = enriched(store)
        return dict(token=token, workspace=space, orders=orders, live_submit_enabled=not preview_only,
                    summary=dict(total=len(orders), company_total=sum(o["company_purchase"] and not o["scope_excluded"] for o in orders),
                                 excluded=sum(o["scope_excluded"] for o in orders),
                                 actionable=sum(o["decision"]["queue"] in ("P0", "P1", "P3") for o in orders),
                                 special=sum(o["decision"]["queue"] == "P1" for o in orders),
                                 merchant=sum(o["company_purchase"] and not o["scope_excluded"] and (o["decision"]["queue"] == "P2" or o["invoice_status"] == "pending") for o in orders),
                                 downloaded=sum(o["company_purchase"] and not o["scope_excluded"] and o["invoice_status"] == "downloaded" for o in orders),
                                 amount_fen=sum(o["amount_fen"] for o in orders if o["company_purchase"] and not o["scope_excluded"] and o["trade_status"] in ("paid", "completed"))),
                    profile=store.profile().public(), connections=list(connections.values()) if space == "live" else [
                        dict(platform=p, status="disconnected", message="示例工作区不会连接真实平台") for p in connections],
                    invoices=invoice_list(store, space), jobs=store.jobs(), events=store.events(), version=VERSION,
                    data_dir=str(store.root))

    async def run_job(store, ident, key, function):
        job = store.job(ident)
        try:
            result = await function()
            job.update(status="done", message=result or "任务完成")
        except Exception as exc:
            from .browser import BrowserAttention
            if isinstance(exc, BrowserAttention):
                job.update(status="needs_attention", message=str(exc))
            elif isinstance(exc, (Conflict, ValueError, KeyError)):
                job.update(status="needs_attention", message=str(exc))
            else:
                # Do not expose stack traces, private page URLs or user data.
                logger.error("Job failed: %s", type(exc).__name__)
                job.update(status="failed", message="操作未完成。请在官方页面核对登录、页面及操作结果，再处理任务。")
            if key.startswith("scan:") or key.startswith("open:"):
                platform = key.split(":")[1]
                connections[platform].update(status="blocked", message=job["message"])
        finally:
            store.save_job(job)
            store.event(job["kind"], job["message"])
            with active_lock:
                active.discard(key)

    def schedule(store, tasks, kind, key, function):
        with active_lock:
            if key in active:
                raise Conflict("同类任务正在运行，请等待当前任务完成")
            ident = secrets.token_hex(12)
            store.save_job(dict(id=ident, kind=kind, status="running", message="正在处理", created_at=now()))
            active.add(key)
        tasks.add_task(run_job, store, ident, key, function)
        return {"job_id": ident}

    @app.get("/api/bootstrap")
    def read_bootstrap(request: Request):
        return bootstrap(*context(request))

    @app.get("/api/health")
    def health():
        return {"app": "personal-invoice-agent", "version": VERSION}

    @app.get("/api/profile")
    def read_profile(request: Request):
        return context(request)[1].profile().public()

    @app.put("/api/profile")
    def save_profile(profile: Profile, request: Request):
        context(request)[1].save_profile(profile)
        return profile.public()

    if testing:
        @app.post("/api/demo/seed")
        def seed(request: Request):
            space, store = context(request)
            if space != "demo":
                raise Conflict("测试数据只能载入隔离工作区")
            demo.seed(store)
            return bootstrap(space, store)

    @app.get("/api/orders")
    def orders(request: Request):
        return enriched(context(request)[1])

    @app.post("/api/orders/import")
    def import_orders(body: ImportRequest, request: Request):
        space, store = context(request)
        imported = skipped = 0
        seen = {o["id"] for o in store.orders()}
        for model in body.orders:
            ident = f"{model.platform}-{model.order_no}"
            if ident in seen:
                skipped += 1
                continue
            data = model.model_dump(mode="json")
            data["source"] = "demo" if space == "demo" else "manual"
            # Imported files cannot assert purchase qualification without user review.
            data["company_purchase"] = False
            if data["invoice_status"] == "downloaded":
                data["invoice_status"] = "issued"
            store.upsert(data)
            seen.add(ident)
            imported += 1
        store.event("import", f"已导入 {imported} 笔订单，跳过 {skipped} 笔重复；导入订单不会自动纳入公司采购")
        return dict(imported=imported, skipped=skipped)

    @app.patch("/api/orders/{oid}")
    def patch_order(oid: str, patch: OrderPatch, request: Request):
        store = context(request)[1]
        values = patch.model_dump(exclude_none=True)
        if values.get("scope_excluded") and values.get("company_purchase"):
            raise HTTPException(422, "已排除范围的订单不能同时确认为公司采购")
        if values.get("scope_excluded"):
            values["company_purchase"] = False
        elif values.get("company_purchase"):
            values["scope_excluded"] = False
            values["scope_reason"] = ""
        platform = store.get(oid)["platform"]
        for field in ("order_url", "invoice_url"):
            if field in values:
                values[field] = platform_url(values[field], platform)
        if values.get("invoice_status") == "downloaded":
            raise HTTPException(422, "已归档状态由实际文件保存结果产生")
        if store.application(oid) in ("submitting", "uncertain", "interrupted"):
            raise Conflict("该订单的申请结果尚未核对，暂不允许修改记录以重新申请；请先在平台核对")
        order = store.patch(oid, values)
        store.event("review", "已保存订单人工核对信息", oid)
        return order | {"decision": decide(order, store.profile(), store.application(oid))}

    @app.post("/api/orders/{oid}/reconcile")
    def reconcile(oid: str, body: ReconcileRequest, request: Request):
        space, store = context(request)
        store.get(oid)
        with active_lock:
            if f"apply:{space}:{oid}" in active:
                raise Conflict("该订单申请任务仍在运行，请等待结束")
        if store.application(oid) not in ("uncertain", "interrupted", "submitted", "failed"):
            raise Conflict("该订单没有需要核对的申请记录")
        status = "failed" if body.outcome == "not_submitted" else "submitted"
        store.finish_application(oid, status)
        store.patch(oid, {"invoice_status": {"not_submitted": "unrequested", "submitted": "pending", "issued": "issued"}[body.outcome]}, user=False)
        store.event("reconcile", "用户已在官方页面核对申请结果：" + body.evidence, oid)
        return {"ok": True}

    @app.get("/api/orders/{oid}/draft")
    def draft(oid: str, request: Request, kind: str = "ask"):
        if kind not in ("ask", "request", "remind"):
            raise HTTPException(422, "不支持的消息类型")
        store = context(request)[1]
        order = store.get(oid)
        if not order["company_purchase"] or order.get("scope_excluded"):
            raise Conflict("请先确认该订单属于公司采购，再生成公司开票消息")
        return dict(kind=kind, text=merchant_draft(order, store.profile(), kind))

    @app.post("/api/orders/{oid}/preview")
    def preview(oid: str, request: Request):
        space, store = context(request)
        order, profile = store.get(oid), store.profile()
        decision = decide(order, profile, store.application(oid))
        if decision["action"] not in ("apply_special", "apply_ordinary"):
            raise Conflict(decision["reason"])
        if not profile.configured:
            raise Conflict("请先在设置填写完整的企业名称及税号")
        inv_type = "special" if decision["action"] == "apply_special" else "ordinary"
        if inv_type == "special" and not all((profile.address, profile.phone, profile.bank_name, profile.bank_account)):
            raise Conflict("专票申请前请补充注册地址、电话、开户银行和银行账号")
        return store.prepare(oid, inv_type, space)

    @app.post("/api/previews/{ident}/execute")
    def execute(ident: str, request: Request, tasks: BackgroundTasks):
        space, store = context(request)
        if space == "live" and preview_only:
            raise Conflict("真实开票提交已暂停，仍可核对申请预览")
        # Durable submitting journal is written before any browser action.
        snapshot = store.consume(ident)
        oid = snapshot["order"]["id"]

        async def apply():
            try:
                if space == "demo":
                    result = dict(status="submitted", message="示例申请已记录；未向淘宝或京东提交")
                else:
                    result = await get_browser().apply(snapshot["order"], snapshot["profile"], snapshot["invoice_type"])
                status = result.get("status", "uncertain")
                if status not in ("submitted", "uncertain"):
                    status = "uncertain"
                store.finish_application(oid, status)
                if status == "submitted":
                    store.patch(oid, {"invoice_status": "pending", "invoice_type": snapshot["invoice_type"]}, user=False)
                else:
                    raise Conflict(result.get("message", "提交结果不明，请到官方订单详情核对，禁止重试"))
                return result["message"]
            except Exception as exc:
                status = "failed" if getattr(exc, "safe_to_retry", False) else "uncertain"
                store.finish_application(oid, status)
                raise

        return schedule(store, tasks, "申请发票" if space == "live" else "示例申请", f"apply:{space}:{oid}", apply)

    @app.post("/api/orders/{oid}/download")
    def download(oid: str, request: Request, tasks: BackgroundTasks):
        space, store = context(request)
        order = store.get(oid)
        decision = decide(order, store.profile(), store.application(oid))
        if decision["action"] != "download":
            raise Conflict(decision["reason"])

        async def archive():
            destination = store.root / "invoices" / order["paid_at"][:4] / order["paid_at"][5:7]
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            if space == "demo":
                path = destination / f"EXAMPLE_{order['order_no']}.xml"
                if not path.exists():
                    path.write_text(f'<?xml version="1.0" encoding="utf-8"?><ExampleInvoice><Warning>EXAMPLE ONLY - NOT A TAX INVOICE</Warning><Order>{order["order_no"]}</Order><TotalFen>{order["amount_fen"]}</TotalFen></ExampleInvoice>')
            else:
                result = await get_browser().download(order, destination)
                path = Path(result["path"]).resolve()
            if not path.is_relative_to((store.root / "invoices").resolve()) or not path.is_file():
                raise ValueError("下载文件位置异常")
            os.chmod(path, 0o600)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            invoice = dict(id=secrets.token_hex(12), order_id=oid, platform=order["platform"], order_no=order["order_no"],
                           product=order["product"], invoice_type=order["invoice_type"], amount_fen=order["amount_fen"],
                           filename=path.name, path=str(path), sha256=digest, created_at=now(),
                           verification="example" if space == "demo" else "unverified")
            store.add_invoice(invoice)
            store.patch(oid, {"invoice_status": "downloaded"}, user=False)
            return "示例原文件已归档（不是税务发票）" if space == "demo" else "原始发票文件已归档；金额来自订单，票面及税务真实性待核验"

        return schedule(store, tasks, "下载归档", f"download:{space}:{oid}", archive)

    @app.post("/api/orders/{oid}/inspect")
    def inspect_order_route(oid: str, request: Request, tasks: BackgroundTasks, invoice_details: bool = False):
        space, store = context(request)
        if space != "live":
            raise Conflict("示例工作区不读取真实订单详情")
        order = store.get(oid)

        async def inspect():
            from .inspection import inspect_order
            if invoice_details:
                result = await inspect_order(get_browser(), order, store.profile().model_dump(), invoice_details=True)
            else:
                result = await inspect_order(get_browser(), order, store.profile().model_dump())
            observed_url = platform_url(result.get("observed_order_url", ""), order["platform"])
            result.update(order_id=oid, inspected_at=now(), read_only=True)
            observations = result.get("observations", {})
            updates = {"order_url": observed_url} if observed_url else {}
            invoice_detail = result.get("invoice_detail") or {}
            if invoice_detail.get("verified_order") and invoice_detail.get("observed_url"):
                updates["invoice_url"] = platform_url(invoice_detail["observed_url"], order["platform"])
            trade = observations.get("trade", "unknown")
            if trade in ("paid", "completed", "unpaid", "closed", "refunded", "partial_refund"):
                updates["trade_status"] = trade
            invoice = observations.get("invoice", {})
            if invoice.get("invoice_status") in ("unrequested", "pending", "issued", "rejected"):
                updates["invoice_status"] = invoice["invoice_status"]
            if invoice.get("invoice_type") in ("special", "ordinary"):
                updates["invoice_type"] = invoice["invoice_type"]
            buyer = observations.get("buyer", {})
            if buyer.get("invoice_buyer") and buyer.get("invoice_tax_id"):
                updates.update(invoice_buyer=buyer["invoice_buyer"], invoice_tax_id=buyer["invoice_tax_id"])
            amount = observations.get("transaction", {}).get("amount_fen")
            result["amount_mismatch"] = amount is not None and amount != order["amount_fen"]
            folder = store.root / "inspections"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=oid + "-", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    json.dump(result, output, ensure_ascii=False, indent=2)
                os.replace(temporary, folder / f"{oid}.json")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            if updates:
                store.patch(oid, updates, user=False)
            missing = result.get("missing_fields", [])
            message = "已只读核对订单详情；未提交任何开票申请"
            if result["amount_mismatch"]:
                message += "；详情金额与列表记录不一致，需复核"
            if missing:
                message += "；仍缺少：" + "、".join(missing)
            return message

        return schedule(store, tasks, "核对订单详情", f"inspect:{space}:{oid}", inspect)

    @app.get("/api/orders/{oid}/inspection")
    def read_order_inspection(oid: str, request: Request):
        store = context(request)[1]
        store.get(oid)
        path = store.root / "inspections" / f"{oid}.json"
        if not path.is_file():
            raise KeyError("尚未保存此订单的详情检查")
        return json.loads(path.read_text())

    @app.post("/api/orders/{oid}/inspect-form")
    def inspect_invoice_form_route(oid: str, request: Request, tasks: BackgroundTasks, current_only: bool = False):
        space, store = context(request)
        if space != "live":
            raise Conflict("示例工作区不会进入真实开票表单")
        if preview_only and not current_only:
            raise Conflict("真实开票入口已暂停，未打开平台表单")
        order = store.get(oid)
        if current_only and store.application(oid) != "uncertain":
            raise Conflict("仅能继续只读核对已有结果不明记录的当前表单")
        if (not order["company_purchase"] or order.get("scope_excluded")
                or order["trade_status"] not in ("paid", "completed")
                or order["invoice_status"] != "unrequested" or order["amount_fen"] <= 0):
            raise Conflict("仅对已确认公司采购、有效交易且明确未申请的订单核对开票表单")

        async def inspect_form():
            from .form_inspection import inspect_invoice_form
            from .browser import BrowserAttention
            snapshot = store.get(oid) if current_only else store.reserve_form_inspection(oid)
            failure = None
            try:
                if current_only:
                    from .form_inspection import read_current_invoice_form
                    result = await read_current_invoice_form(get_browser(), snapshot, {})
                else:
                    result = await inspect_invoice_form(get_browser(), snapshot, {})
            except BrowserAttention as exc:
                result = getattr(exc, "observation", None)
                if not isinstance(result, dict):
                    raise
                failure = exc
            result.update(order_id=oid, inspected_at=now(), profile_transmitted=False)
            folder = store.root / "form-inspections"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=oid + "-", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    json.dump(result, output, ensure_ascii=False, indent=2)
                os.replace(temporary, folder / f"{oid}.json")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            # An entry can itself submit an application. Do not clear the
            # journal merely because a form or a final button is now visible.
            if failure is not None:
                raise failure
            return "已记录申请入口后的页面；未填写资料或点击最终提交，先核对平台结果，禁止直接重试"

        return schedule(store, tasks, "核对开票表单", f"inspect:{space}:{oid}", inspect_form)

    @app.get("/api/orders/{oid}/form-inspection")
    def read_form_inspection(oid: str, request: Request):
        store = context(request)[1]
        store.get(oid)
        path = store.root / "form-inspections" / f"{oid}.json"
        if not path.is_file():
            raise KeyError("尚无该订单的开票入口检查记录")
        return json.loads(path.read_text())

    @app.post("/api/orders/{oid}/merchant/open")
    def open_merchant_chat_route(oid: str, request: Request, tasks: BackgroundTasks):
        space, store = context(request)
        order = store.get(oid)
        if space != "live" or not order["company_purchase"] or order.get("scope_excluded"):
            raise Conflict("仅为真实工作区已确认公司采购的订单打开原商家会话")

        async def open_chat():
            from .merchant import open_merchant_chat
            from .browser import BrowserAttention
            current = store.get(oid)
            if not current["company_purchase"] or current.get("scope_excluded"):
                raise Conflict("该订单已不在公司采购范围，未打开商家会话")
            failure = None
            try:
                result = await open_merchant_chat(get_browser(), current)
            except BrowserAttention as exc:
                result = getattr(exc, "observation", None)
                if not isinstance(result, dict):
                    raise
                failure = exc
            result.update(order_id=oid, observed_at=now(), message_sent=False)
            folder = store.root / "merchant-inspections"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=oid + "-", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    json.dump(result, output, ensure_ascii=False, indent=2)
                os.replace(temporary, folder / f"{oid}.json")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            if failure is not None:
                raise failure
            return "已核对原商家联系入口；未发送消息，需继续核对会话收件人和发送控件"

        return schedule(store, tasks, "核对商家会话", f"inspect:{space}:{oid}", open_chat)

    @app.get("/api/orders/{oid}/merchant/inspection")
    def read_merchant_inspection(oid: str, request: Request):
        store = context(request)[1]
        store.get(oid)
        path = store.root / "merchant-inspections" / f"{oid}.json"
        if not path.is_file():
            raise KeyError("尚无该订单的商家会话检查记录")
        return json.loads(path.read_text())

    @app.post("/api/orders/{oid}/merchant/inspect")
    def inspect_merchant_chat_route(oid: str, request: Request, tasks: BackgroundTasks):
        space, store = context(request)
        order = store.get(oid)
        path = store.root / "merchant-inspections" / f"{oid}.json"
        if space != "live" or not order["company_purchase"] or order.get("scope_excluded"):
            raise Conflict("仅只读核对真实已确认公司采购的原商家会话")
        if not path.is_file():
            raise Conflict("尚无从该订单原商家实际观察到的会话链接，未猜测地址")
        previous = json.loads(path.read_text())
        if previous.get("order_id") != oid or not previous.get("shop_verified") or not previous.get("contact_activated"):
            raise Conflict("尚未核验该订单原商家会话来源")
        observed_url = platform_url(previous.get("observed_chat_url") or previous.get("current_url", ""), order["platform"])
        if not observed_url:
            raise Conflict("缺少已观察的官方会话链接")

        async def inspect_chat():
            from .merchant import inspect_merchant_chat
            current = store.get(oid)
            if not current["company_purchase"] or current.get("scope_excluded"):
                raise Conflict("该订单已不在公司采购范围，未读取商家会话")
            result = await inspect_merchant_chat(get_browser(), current, observed_url)
            result.update(order_id=oid, observed_at=now(), observed_chat_url=observed_url,
                          shop_verified=True, contact_activated=True, message_sent=False)
            fd, temporary = tempfile.mkstemp(prefix=oid + "-", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    json.dump(result, output, ensure_ascii=False, indent=2)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return "已只读核对已观察的原商家会话页面；未输入或发送消息"

        return schedule(store, tasks, "读取商家会话", f"inspect:{space}:{oid}", inspect_chat)

    def unresolved_reference(store, platform, number):
        if not re.fullmatch(r"\d{6,40}", number):
            raise HTTPException(422, "订单编号无效")
        coverage = store.root / "coverage" / f"{platform}.json"
        if not coverage.is_file():
            raise KeyError("尚无该平台的历史采集记录")
        records = json.loads(coverage.read_text()).get("report", {}).get("unresolved_records", [])
        matches = [r for r in records if r.get("order_no") == number and r.get("platform") == platform]
        if len(matches) != 1:
            raise Conflict("采集记录中没有唯一对应的未解析订单，未开始检查")
        record = matches[0]
        date.fromisoformat(record["paid_at"])
        return record

    @app.post("/api/connections/{platform}/unresolved/{number}/inspect")
    def inspect_unresolved(platform: Platform, number: str, request: Request, tasks: BackgroundTasks):
        space, store = context(request)
        if space != "live":
            raise Conflict("示例工作区不读取真实订单详情")
        reference = unresolved_reference(store, platform, number)

        async def inspect():
            from .inspection import inspect_order
            order = dict(platform=platform, order_no=number, paid_at=reference["paid_at"],
                         order_url=platform_url(reference.get("observed_url", ""), platform))
            result = await inspect_order(get_browser(), order, {})
            result.update(reference=reference, inspected_at=now(), read_only=True, record_imported=False)
            folder = store.root / "inspections"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix="unresolved-", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    json.dump(result, output, ensure_ascii=False, indent=2)
                os.replace(temporary, folder / f"unresolved-{platform}-{number}.json")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return "已只读核对未解析订单；未猜测金额、未导入或申请开票"

        return schedule(store, tasks, "核对未解析订单", f"inspect:{space}:{platform}-{number}", inspect)

    @app.get("/api/connections/{platform}/unresolved/{number}/inspection")
    def read_unresolved_inspection(platform: Platform, number: str, request: Request):
        space, store = context(request)
        if space != "live":
            raise Conflict("示例工作区无真实检查记录")
        unresolved_reference(store, platform, number)
        path = store.root / "inspections" / f"unresolved-{platform}-{number}.json"
        if not path.is_file():
            raise KeyError("尚无该订单的只读详情检查记录")
        return json.loads(path.read_text())

    @app.get("/api/invoices/{ident}/file")
    def invoice_file(ident: str, request: Request):
        space, store = context(request)
        invoice = next((i for i in store.invoices() if i["id"] == ident), None)
        if not invoice:
            raise KeyError("当前工作区未找到发票")
        path = Path(invoice["path"]).resolve()
        if not path.is_relative_to((store.root / "invoices").resolve()) or not path.is_file():
            raise KeyError("原始文件不存在或位置异常")
        return FileResponse(path, filename=invoice["filename"], media_type="application/octet-stream")

    @app.post("/api/connections/{platform}/open")
    def open_platform(platform: Platform, request: Request, tasks: BackgroundTasks):
        space, store = context(request)
        if space != "live":
            raise Conflict("请返回真实工作区后打开平台；示例不会连接真实账号")

        async def open_browser():
            msg = await get_browser().open(platform)
            connections[platform].update(status="open", message="请在专用浏览器手动登录，然后扫描订单")
            return msg or "专用浏览器已打开，请手动登录"

        return schedule(store, tasks, "打开登录浏览器", f"open:{platform}", open_browser)

    @app.post("/api/connections/{platform}/scan")
    def scan(platform: Platform, body: ScanRequest, request: Request, tasks: BackgroundTasks):
        space, store = context(request)
        if space != "live":
            raise Conflict("示例工作区不扫描真实账号")

        async def scan_orders():
            manager = get_browser()
            try:
                data = await manager.scan(platform, body.start_date.isoformat(), body.end_date.isoformat(), body.max_pages)
            except Exception:
                persist_coverage(store, platform, body, getattr(manager, "last_scan_report", {}), 0, 0, "needs_attention")
                raise
            imported = 0
            invalid = 0
            for raw in data:
                try:
                    order = OrderInput(**raw)
                except ValueError:
                    invalid += 1
                    continue
                store.upsert(order.model_dump(mode="json"))
                imported += 1
            report = getattr(manager, "last_scan_report", {})
            persist_coverage(store, platform, body, report, imported, invalid, "done")
            complete = report.get("complete", False)
            message = f"已读取 {imported} 笔订单；" + ("本次翻页已结束" if complete else "范围尚未完整覆盖")
            if invalid:
                message += f"；另有 {invalid} 笔字段未通过校验，未导入，请人工核对"
            if report.get("reason"):
                message += "。" + str(report["reason"])
            connections[platform].update(status="ready", message=message)
            return message

        return schedule(store, tasks, "扫描订单", f"scan:{platform}", scan_orders)

    @app.get("/api/coverage")
    def read_coverage(request: Request):
        store = context(request)[1]
        result = {}
        for platform in ("taobao", "jd"):
            path = store.root / "coverage" / f"{platform}.json"
            if path.is_file():
                result[platform] = json.loads(path.read_text())
        if browser is not None:
            report = getattr(browser, "last_scan_report", {})
            platform = report.get("platform")
            with active_lock:
                running = platform in ("taobao", "jd") and f"scan:{platform}" in active
            if running:
                snapshot = json.loads(json.dumps(report))
                snapshot["complete"] = False
                result[platform] = dict(platform=platform, status="running", imported=None, invalid=None,
                                        requested_start_date=report.get("start_date"), requested_end_date=report.get("end_date"),
                                        recorded_at=now(), report=snapshot)
        return result

    @app.post("/api/connections/{platform}/check")
    async def check_platform(platform: Platform, request: Request):
        space, store = context(request)
        if space != "live":
            raise Conflict("示例工作区不读取真实平台")
        status = await get_browser().check(platform)
        connections[platform].update(status)
        return connections[platform]

    @app.post("/api/connections/close")
    async def close_connections(request: Request):
        if any(key.startswith(("scan:", "apply:", "download:", "open:", "inspect:")) for key in active):
            raise Conflict("浏览器任务尚在运行，请先等待完成")
        if browser:
            await browser.close()
        for conn in connections.values():
            conn.update(status="disconnected", message="专用浏览器已关闭，登录会话保留在本机")
        return {"ok": True}

    @app.get("/api/jobs/{ident}")
    def read_job(ident: str, request: Request):
        return context(request)[1].job(ident)

    @app.get("/api/export")
    def export(request: Request, month: str = "", scope: str = "company"):
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(422, "请选择有效月份（YYYY-MM）")
        space, store = context(request)
        if scope not in ("company", "all"):
            raise HTTPException(422, "导出范围必须为 company 或 all")
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["工作区", "平台", "订单号", "商品", "店铺", "日期", "订单金额(元)", "发票类型", "发票状态", "公司采购", "下一步", "说明"])
        def cell(value):
            value = str(value)
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value
        for o in enriched(store):
            if o["paid_at"].startswith(month) and (scope == "all" or (o["company_purchase"] and not o["scope_excluded"])):
                writer.writerow([cell(v) for v in ["示例" if space == "demo" else "真实", "淘宝" if o["platform"] == "taobao" else "京东",
                    "'" + o["order_no"], o["product"], o["shop"], o["paid_at"], f"{o['amount_fen']/100:.2f}",
                    {"special": "专票", "ordinary": "普票", "unknown": "待确认"}[o["invoice_type"]], o["invoice_status"],
                    "已排除" if o["scope_excluded"] else "已确认" if o["company_purchase"] else "未纳入", o["decision"]["label"], "金额来自订单，票面和税务真实性未核验"]])
        return Response(content=buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="invoice-summary-{space}-{month}.csv"'})

    web = Path(__file__).resolve().parents[2] / "web" / "dist"
    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="dashboard")
    else:
        @app.get("/")
        def build_required():
            return {"message": "前端尚未构建，请运行项目启动脚本", "docs": "/api/docs"}
    return app
