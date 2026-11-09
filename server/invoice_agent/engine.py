from .models import Profile


def decide(order: dict, profile: Profile, application: str | None = None) -> dict:
    def result(queue, action, label, reason):
        return dict(queue=queue, action=action, label=label, reason=reason)

    trade, inv = order["trade_status"], order["invoice_status"]
    if application in ("submitting", "uncertain", "interrupted"):
        return result("MANUAL", "manual", "核对提交结果", "申请可能已提交；请到平台核对，禁止重复提交")
    if order.get("scope_excluded"):
        return result("DONE", "skip", "已排除本次范围", order.get("scope_reason") or "该订单未纳入本次公司开票范围")
    if not order["company_purchase"]:
        return result("MANUAL", "manual", "未纳入公司采购", "仅导入了订单；未确认属于公司采购，不会申请公司发票")
    if trade in ("refunded", "partial_refund"):
        return result("MANUAL", "manual", "退款待复核", "退款会影响开票金额或红冲，请先核对商家及财务处理结果")
    if trade in ("unpaid", "closed"):
        return result("DONE", "skip", "无需申请", "未付款或已关闭的订单不申请发票")
    if order["amount_fen"] == 0:
        return result("MANUAL", "manual", "核对零金额订单", "页面订单金额为零，请核对优惠、支付方式及实际可开票金额后处理")
    if trade == "unknown":
        return result("MANUAL", "manual", "核对交易状态", "尚未确认付款、退款及交易状态")
    if inv == "downloaded":
        return result("DONE", "complete", "已归档", "已保存原始文件；票面与税务真实性仍需独立核验")
    if inv == "issued":
        if (not profile.configured or order.get("invoice_buyer") != profile.company_name
                or order.get("invoice_tax_id", "").upper() != profile.tax_id):
            return result("MANUAL", "manual", "核对已有发票", "需确认已有发票抬头及税号与当前企业一致，避免重复开票")
        return result("P0", "download", "待下载", "已有公司抬头发票，优先下载归档")
    if inv == "pending" or application == "submitted":
        return result("WAIT", "wait", "等待开票", "已申请；可生成催票草稿，等待平台明确开具结果")
    if inv in ("unknown", "rejected"):
        return result("MANUAL", "manual", "核对发票状态", "请在订单详情核对是否已有发票、申请或拒绝原因")
    special = order["special_status"]
    if special == "unknown":
        return result("P2", "ask_special", "询问专票", "尚无该订单支持专票的明确证据，不能自动降为普票")
    if not order.get("evidence", "").strip():
        return result("MANUAL", "manual", "补充票种依据", "请记录当前页面或商家对该订单的明确答复")
    if special == "yes":
        return result("P1", "apply_special", "可申请专票", "已确认公司采购、专票支持依据及未申请状态")
    if order["ordinary_supported"] and profile.allow_ordinary:
        return result("P3", "apply_ordinary", "可申请普票", "已确认不支持专票，企业设置允许普票备选")
    return result("MANUAL", "manual", "确认普票处理", "已知不支持专票；需确认普通票支持情况并在设置启用普票备选")


def merchant_draft(order: dict, profile: Profile, kind: str) -> str:
    prefix = f"您好，订单 {order['order_no']}（{order['product']}，实付 ¥{order['amount_fen'] / 100:.2f}）为公司采购。"
    if kind == "remind":
        return prefix + "此前已申请发票，烦请告知开票进度及预计完成时间；如已开具，请提供官方发票下载入口。谢谢。"
    if kind == "ask" or (kind == "request" and order["special_status"] == "unknown"):
        return prefix + "请问该订单是否支持开具公司抬头的增值税专用发票？如不支持，请明确可开票种及申请方式。谢谢。"
    if order["special_status"] == "no" and not (order["ordinary_supported"] and profile.allow_ordinary and order["evidence"].strip()):
        raise ValueError("尚未确认普票支持情况或启用普票备选，请先核对后生成申请消息")
    inv = "增值税专用发票" if order["special_status"] == "yes" else "增值税普通发票"
    return (prefix + f"请协助申请{inv}。\n购买方：{profile.company_name or '【请填写公司全称】'}"
            f"\n纳税人识别号：{profile.tax_id or '【请填写税号】'}\n如需其他资质，请通过平台官方申请入口告知。谢谢。")
