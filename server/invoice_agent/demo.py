"""Fictional samples, stored only in the separate demo workspace."""
from .models import OrderInput, Profile


def seed(store):
    if store.orders():
        return
    profile = Profile(company_name="示例科技有限公司（演示）", tax_id="91310000123456789X",
                      address="示例市示例路 100 号", phone="000-12345678", bank_name="示例银行",
                      bank_account="0000000000000000000", email="demo@example.com", allow_ordinary=True)
    store.save_profile(profile)
    base = dict(paid_at="2026-11-06", trade_status="completed", company_purchase=True,
                invoice_status="unrequested", source="demo")
    samples = [
        dict(platform="jd", order_no="202611060001", product="办公显示器支架", shop="示例京东自营店", amount_fen=32900,
             special_status="yes", evidence="示例：订单申请页提供增值税专用发票"),
        dict(platform="taobao", order_no="202611060002", product="铝合金结构件打样", shop="示例加工店", amount_fen=68000),
        dict(platform="jd", order_no="202611060003", product="USB 测试仪", shop="示例仪器店", amount_fen=15900,
             invoice_status="issued", invoice_type="special", invoice_buyer=profile.company_name, invoice_tax_id=profile.tax_id),
        dict(platform="taobao", order_no="202611060004", product="包装材料与标签纸", shop="示例包材店", amount_fen=8650,
             special_status="no", ordinary_supported=True, evidence="示例：商家明确该订单仅可开普票"),
        dict(platform="jd", order_no="202611060005", product="传感器测试模组", shop="示例电子店", amount_fen=42800,
             invoice_status="pending", invoice_type="special"),
        dict(platform="taobao", order_no="202611060006", product="桌面收纳配件", shop="示例生活店", amount_fen=4900,
             company_purchase=False),
        dict(platform="taobao", order_no="202611060007", product="打样加工费（部分退款）", shop="示例加工店", amount_fen=12000,
             trade_status="partial_refund"),
        dict(platform="jd", order_no="202611060008", product="无线鼠标采购", shop="示例外设店", amount_fen=19900,
             special_status="yes", evidence="示例：商家确认支持专票"),
    ]
    for item in samples:
        store.upsert(OrderInput(**(base | item)).model_dump(mode="json"))
    store.event("demo", "已载入 8 笔虚构订单；所有示例操作均不会访问平台")
