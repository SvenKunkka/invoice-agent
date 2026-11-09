import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from .models import OrderInput, Profile


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(Exception):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        key = self.root / "encryption.key"
        try:
            with key.open("xb") as f:
                os.chmod(key, 0o600)
                f.write(Fernet.generate_key())
        except FileExistsError:
            pass
        os.chmod(key, 0o600)
        self.cipher = Fernet(key.read_bytes())
        self.db_path = self.root / "invoice-agent.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS applications(order_id TEXT PRIMARY KEY, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS previews(id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, invoice_type TEXT NOT NULL, expires REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS invoices(id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, payload BLOB NOT NULL);
            """)
            db.execute("UPDATE applications SET status='interrupted' WHERE status='submitting'")
            for row in db.execute("SELECT id,payload FROM jobs").fetchall():
                job = self.unpack(row[1])
                if job["status"] == "running":
                    job.update(status="needs_attention", message="上次运行已中断，请先核对平台操作结果")
                    db.execute("UPDATE jobs SET payload=? WHERE id=?", (self.pack(job), row[0]))
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def pack(self, obj):
        return self.cipher.encrypt(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode())

    def unpack(self, payload):
        return json.loads(self.cipher.decrypt(payload))

    def profile(self) -> Profile:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM meta WHERE key='profile'").fetchone()
        return Profile(**self.unpack(row[0])) if row else Profile()

    def save_profile(self, profile: Profile):
        with self.connect() as db:
            db.execute("INSERT INTO meta VALUES('profile',?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                       (self.pack(profile.model_dump()),))
        self.event("profile", "企业开票资料已在本地加密保存")

    def orders(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM orders").fetchall()
        return sorted((self.unpack(r[0]) for r in rows), key=lambda x: x["paid_at"], reverse=True)

    def get(self, order_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            raise KeyError("未找到该订单")
        return self.unpack(row[0])

    def upsert(self, data: dict):
        clean = OrderInput(**data).model_dump(mode="json")
        oid = f"{clean['platform']}-{clean['order_no']}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM orders WHERE id=?", (oid,)).fetchone()
            existing = self.unpack(row[0]) if row else {}
            user_fields = existing.get("user_fields", {})
            # A fresh observed refund or issued invoice always wins over old user state.
            if clean["source"] == "browser":
                for field, value in user_fields.items():
                    if field not in ("trade_status", "invoice_status", "invoice_type", "invoice_buyer", "invoice_tax_id"):
                        clean[field] = value
                if clean["invoice_status"] == "unknown" and existing.get("invoice_status") in ("downloaded", "issued", "pending"):
                    # A generic list link contains less information than an
                    # earlier explicit invoice detail; missing is not a reversal.
                    clean["invoice_status"] = existing["invoice_status"]
                    for field in ("invoice_type", "invoice_buyer", "invoice_tax_id"):
                        if clean.get(field) in (None, "", "unknown") and existing.get(field):
                            clean[field] = existing[field]
            else:
                clean.update(user_fields)
            clean.update(id=oid, updated_at=now(), user_fields=user_fields)
            db.execute("INSERT INTO orders VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (oid, self.pack(clean)))
        return oid

    def patch(self, oid: str, values: dict, *, user=True):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if user:
                application = db.execute("SELECT status FROM applications WHERE order_id=?", (oid,)).fetchone()
                if application and application[0] in ("submitting", "uncertain", "interrupted"):
                    raise Conflict("该订单的申请结果尚未核对，暂不允许修改记录；请先在平台核对")
            row = db.execute("SELECT payload FROM orders WHERE id=?", (oid,)).fetchone()
            if not row:
                raise KeyError("未找到该订单")
            order = self.unpack(row[0])
            order.update(values, updated_at=now())
            if user:
                order.setdefault("user_fields", {}).update(values)
            db.execute("UPDATE orders SET payload=? WHERE id=?", (self.pack(order), oid))
        return order

    def application(self, oid):
        with self.connect() as db:
            row = db.execute("SELECT status FROM applications WHERE order_id=?", (oid,)).fetchone()
        return row[0] if row else None

    def reserve_form_inspection(self, oid):
        """Journal an authorized entry click before it can affect the platform."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM orders WHERE id=?", (oid,)).fetchone()
            if not row:
                raise KeyError("未找到该订单")
            order = self.unpack(row[0])
            if (not order["company_purchase"] or order.get("scope_excluded")
                    or order["trade_status"] not in ("paid", "completed")
                    or order["invoice_status"] != "unrequested" or order["amount_fen"] <= 0):
                raise Conflict("仅对已确认公司采购、有效交易且明确未申请的订单核对开票表单")
            previous = db.execute("SELECT status FROM applications WHERE order_id=?", (oid,)).fetchone()
            if previous and previous[0] != "failed":
                raise Conflict("该订单已有申请记录，先核对平台结果，不能重复进入申请")
            db.execute("INSERT INTO applications VALUES(?,'uncertain') ON CONFLICT(order_id) DO UPDATE SET status='uncertain'", (oid,))
        return order

    def fingerprint(self, oid):
        data = {"order": self.get(oid), "profile": self.profile().model_dump()}
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def prepare(self, oid, invoice_type, workspace):
        if self.application(oid) not in (None, "failed"):
            raise Conflict("该订单已有提交记录，请先到官方订单页核对，不能重复申请")
        ident = secrets.token_urlsafe(24)
        expires = time.time() + 600
        with self.connect() as db:
            db.execute("INSERT INTO previews(id,order_id,fingerprint,invoice_type,expires) VALUES(?,?,?,?,?)",
                       (ident, oid, self.fingerprint(oid), invoice_type, expires))
        order, profile = self.get(oid), self.profile()
        return dict(id=ident, order_id=oid, invoice_type=invoice_type, company_name=profile.company_name,
                    tax_id=profile.tax_id, amount_fen=order["amount_fen"], workspace=workspace,
                    platform=order["platform"], order_no=order["order_no"], product=order["product"], shop=order["shop"],
                    address=profile.address, phone=profile.phone, bank_name=profile.bank_name,
                    bank_account=profile.bank_account, email=profile.email,
                    expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                    message="请核对订单、票种、抬头和税号；确认后提交一次" if workspace == "live" else "示例操作：仅更新示例数据，不联系平台")

    def consume(self, ident):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT order_id,fingerprint,invoice_type,expires,used FROM previews WHERE id=?", (ident,)).fetchone()
            if not row:
                raise KeyError("预览不存在")
            oid, fingerprint, invoice_type, expires, used = row
            if used:
                raise Conflict("此预览已使用，请勿重复提交")
            if expires < time.time():
                raise Conflict("预览已过期，请重新核对")
            order = self.unpack(db.execute("SELECT payload FROM orders WHERE id=?", (oid,)).fetchone()[0])
            profile_row = db.execute("SELECT payload FROM meta WHERE key='profile'").fetchone()
            profile = self.unpack(profile_row[0]) if profile_row else Profile().model_dump()
            current_fingerprint = hashlib.sha256(json.dumps({"order": order, "profile": profile}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if fingerprint != current_fingerprint:
                raise Conflict("订单或企业资料已变更，请重新生成预览")
            application = db.execute("SELECT status FROM applications WHERE order_id=?", (oid,)).fetchone()
            if application and application[0] != "failed":
                raise Conflict("该订单已有提交记录，请先核对平台状态")
            db.execute("UPDATE previews SET used=1 WHERE id=?", (ident,))
            db.execute("INSERT INTO applications VALUES(?,'submitting') ON CONFLICT(order_id) DO UPDATE SET status='submitting'", (oid,))
        return {"order": order, "profile": profile, "invoice_type": invoice_type}

    def finish_application(self, oid, status):
        with self.connect() as db:
            db.execute("UPDATE applications SET status=? WHERE order_id=?", (status, oid))

    def event(self, kind, message, order_id=None):
        data = dict(at=now(), kind=kind, message=message, order_id=order_id)
        with self.connect() as db:
            db.execute("INSERT INTO events(payload) VALUES(?)", (self.pack(data),))

    def events(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,payload FROM events ORDER BY id DESC LIMIT 100").fetchall()
        return [self.unpack(r[1]) | {"id": r[0]} for r in rows]

    def job(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise KeyError("任务不存在")
        return self.unpack(row[0])

    def save_job(self, job):
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (job["id"], self.pack(job)))

    def jobs(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM jobs ORDER BY rowid DESC LIMIT 30").fetchall()
        return [self.unpack(r[0]) for r in rows]

    def invoices(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM invoices ORDER BY rowid DESC").fetchall()
        return [self.unpack(r[0]) for r in rows]

    def add_invoice(self, invoice):
        with self.connect() as db:
            existing = db.execute("SELECT payload FROM invoices WHERE sha256=?", (invoice["sha256"],)).fetchone()
            if existing:
                old = self.unpack(existing[0])
                if old["order_id"] != invoice["order_id"]:
                    raise Conflict("发现同一文件已关联其他订单，请人工核对合票或重复文件")
                return old
            db.execute("INSERT INTO invoices VALUES(?,?,?)", (invoice["id"], invoice["sha256"], self.pack(invoice)))
        return invoice
