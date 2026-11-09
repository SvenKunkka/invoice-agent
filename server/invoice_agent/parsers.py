"""Conservative parsing of rendered order pages; no platform private APIs."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Comment, Doctype, Tag


PLATFORM_URLS = {
    "taobao": "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm",
    "jd": "https://order.jd.com/center/list.action",
}
CONTAINERS = {
    "taobao": "[id^='shopOrderContainer_'], .bought-table-mod__table___3u4gN, table.bought-table",
    "jd": ".order-tb tbody, [class^='orderCard-']",
}
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})(?:日)?(?!\d)")
ORDER_RE = re.compile(r"(?:订单号|订单编号|订单号码)\s*[:：]?\s*(\d{6,40})(?!\d)")


@dataclass
class ParseResult:
    orders: list[dict] = field(default_factory=list)
    recognized: bool = False
    empty: bool = False
    rejected: list[str] = field(default_factory=list)
    container_count: int = 0
    unresolved_records: list[dict] = field(default_factory=list)


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def official_url(value: str, platform: str) -> bool:
    """Platform-local HTTPS only, including subdomains; reject credentials/ports."""
    try:
        parsed = urlparse(value)
        roots = ("taobao.com", "tmall.com") if platform == "taobao" else ("jd.com",)
        host = (parsed.hostname or "").lower()
        return (platform in PLATFORM_URLS and parsed.scheme == "https" and not parsed.username
                and not parsed.password and parsed.port in (None, 443)
                and any(host == root or host.endswith("." + root) for root in roots))
    except ValueError:
        return False


def money_fen(value: str) -> int | None:
    text = clean(value).replace(",", "").replace("，", "")
    match = re.fullmatch(r"(?:[¥￥]\s*)?(\d{1,10})(?:\.(\d{1,2}))?(?:\s*元)?", text)
    if not match:
        return None
    try:
        return int(Decimal(match.group(1) + "." + (match.group(2) or "0")) * 100)
    except InvalidOperation:
        return None


def _one(values: list[str]) -> str | None:
    unique = list(dict.fromkeys(clean(v) for v in values if clean(v)))
    return unique[0] if len(unique) == 1 else None


def _texts(node: Tag, selectors: str) -> list[str]:
    return [clean(item.get_text(" ", strip=True)) for item in node.select(selectors)]


def _order_no(node: Tag, platform: str) -> str | None:
    values = ORDER_RE.findall(node.get_text(" ", strip=True))
    if platform == "taobao":
        match = re.fullmatch(r"shopOrderContainer_(\d{6,40})", node.get("id", ""))
        if match:
            values.append(match.group(1))
        for text in _texts(node, "[class^='shopInfoOrderId--'], [class*='order-info-mod__order-number']"):
            match = re.search(r"(?<!\d)(\d{6,40})(?!\d)", text)
            if match:
                values.append(match.group(1))
    else:
        modern_metas = node.select("[class^='orderMetas-']")
        if modern_metas:
            modern_values = []
            for meta in modern_metas:
                for label in meta.select("[class^='metaLabel-']"):
                    if clean(label.get_text(" ", strip=True)).rstrip("：:") != "订单号":
                        continue
                    sibling = label.find_next_sibling()
                    if sibling is None or not any(str(c).startswith("metaValue-") for c in sibling.get("class", [])):
                        return None
                    value = clean(sibling.get_text(" ", strip=True))
                    if not re.fullmatch(r"\d{6,40}", value):
                        return None
                    modern_values.append(value)
            if not modern_values:
                return None
            values.extend(modern_values)
        for item in node.select(".tr-th .number, .tr-th .orderid, [data-orderid]"):
            text = clean(item.get("data-orderid", "") or item.get_text(" ", strip=True))
            if re.fullmatch(r"\d{6,40}", text):
                values.append(text)
    return _one(values)


def _order_date(node: Tag) -> str | None:
    candidates = []
    for item in node.select(".dealtime, [class^='orderTime-'] [class^='metaValue-'], [class^='shopInfoOrderTime--'], [class*='order-info-mod__create-time'], .order-time"):
        candidates.extend([item.get("title", ""), item.get_text(" ", strip=True)])
    if not candidates:
        candidates = re.findall(r"(?:下单时间|订单时间|创建时间|成交时间)\s*[:：]?\s*(20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}日?)", node.get_text(" ", strip=True))
    dates = []
    for candidate in candidates:
        for match in DATE_RE.finditer(candidate):
            try:
                dates.append(date(*map(int, match.groups())).isoformat())
            except ValueError:
                pass
    return _one(dates)


def _amount(node: Tag, platform: str) -> int | None:
    selectors = ("[class^='realPay--'], [class^='actualPayment--'], .real-pay, [class*='price-mod__price'] strong, .amount strong"
                 if platform == "taobao" else ".amount > span:first-child, .amount .ftx-13, .amount strong, .amount .price, .order-amount")
    values = []
    if platform == "taobao":
        # Current order-list totals have split numeric spans. Scope these to
        # the order-payment column and its explicit "合计" total, never product
        # prices, recommendations, or the separate "含运费" price container.
        totals = node.select(".trade-bought-col-order-payment [class*='priceReal--']")
        if totals:
            if len(totals) != 1:
                return None
            total = totals[0]
            components = {name: total.select(".trade-price-" + name)
                          for name in ("label", "symbol", "integer", "point", "decimal")}
            if any(len(items) != 1 for items in components.values()):
                return None
            parts = {name: clean(items[0].get_text("", strip=True)) for name, items in components.items()}
            if (parts["label"] not in ("合计", "实付款") or parts["symbol"] not in ("¥", "￥")
                    or parts["point"] != "." or not re.fullmatch(r"\d{2}", parts["decimal"])
                    or not re.fullmatch(r"\d{1,10}|\d{1,3}(?:,\d{3}){1,3}", parts["integer"])):
                return None
            amount = money_fen(parts["integer"] + "." + parts["decimal"])
            if amount is None:
                return None
            values.append(amount)
    if platform == "jd":
        totals = node.select("[class^='priceColumn-'] [class^='priceRow-']")
        if totals:
            if len(totals) != 1:
                return None
            total = totals[0]
            components = {name: total.select("[class^='price" + name + "-']")
                          for name in ("Symbol", "Integer", "Decimal")}
            if any(len(items) != 1 for items in components.values()):
                return None
            parts = {name: clean(items[0].get_text("", strip=True)) for name, items in components.items()}
            if (parts["Symbol"] not in ("¥", "￥")
                    or not re.fullmatch(r"\.?\d{2}", parts["Decimal"])
                    or not re.fullmatch(r"\d{1,10}|\d{1,3}(?:,\d{3}){1,3}", parts["Integer"])):
                return None
            amount = money_fen(parts["Integer"] + "." + parts["Decimal"].lstrip("."))
            if amount is None:
                return None
            values.append(amount)
    for item in node.select(selectors):
        value = money_fen(item.get_text(" ", strip=True))
        if value is not None:
            values.append(value)
    # Labelled totals are accepted; bare product prices and shipping are not.
    for match in re.finditer(r"(?:实付款|实付金额|实际支付|订单金额|应付总额|总额)\s*[:：]?\s*([¥￥]?\s*[\d,]+(?:\.\d{1,2})?(?:\s*元)?)(?![\d.,]|\s*[.,])", node.get_text(" ", strip=True)):
        value = money_fen(match.group(1))
        if value is not None:
            values.append(value)
    unique = set(values)
    return next(iter(unique)) if len(unique) == 1 else None


def invoice_observation(text: str) -> dict:
    text = clean(text)
    special_no = bool(re.search(r"(?:不支持|不提供|无法|不能|不可).{0,6}(?:专票|专用发票)|(?:仅支持|只能开|仅开具).{0,5}(?:普通发票|普票)", text))
    special_yes = bool(re.search(r"(?:支持|可开|可以开具|可申请).{0,6}(?:专票|专用发票)", text))
    special = "no" if special_no else "yes" if special_yes else "unknown"
    ordinary = bool(re.search(r"(?:支持|可开|可申请).{0,6}(?:普通发票|普票)|电子普通发票|增值税普通发票", text))
    if re.search(r"(?:不支持|无法|不能).{0,6}(?:普通发票|普票)", text):
        ordinary = False
    if re.search(r"已下载", text):
        # A platform label alone cannot prove that this tool archived a file.
        status = "issued"
    elif re.search(r"已开票|开票成功|发票已开具|查看发票(?!\s*详情)|下载(?:电子)?发票", text):
        status = "issued"
    elif re.search(r"开票中|待开票|已申请(?:开票|发票)?|申请已提交|审核中", text):
        status = "pending"
    elif re.search(r"申请驳回|开票失败|申请被拒|已拒绝", text):
        status = "rejected"
    elif re.search(r"申请开票|申请发票|未申请|未开票|补开发票", text):
        status = "unrequested"
    else:
        status = "unknown"
    typ = "unknown"
    if status == "issued":
        if re.search(r"增值税专用发票|电子专票|数电专票", text):
            typ = "special"
        elif re.search(r"增值税普通发票|电子普通发票", text):
            typ = "ordinary"
    return {"invoice_status": status, "invoice_type": typ, "special_status": special,
            "ordinary_supported": ordinary}


def invoice_detail_dom_observation(html: str, platform: str) -> dict:
    """Read a bound invoice-detail table; prose/FAQ never supplies capabilities.

    `complete` describes unambiguous observed fields, not tax validation or an
    issued invoice. The caller must bind this visible page to its target order.
    """
    if platform not in PLATFORM_URLS:
        raise ValueError("不支持的平台")
    invoice = {"invoice_status": "unknown", "invoice_type": "unknown",
               "special_status": "unknown", "ordinary_supported": False}
    buyer = {"invoice_buyer": "", "invoice_tax_id": ""}
    soup = BeautifulSoup(html, "html.parser")
    for node in list(soup.select("script,style,template,noscript")):
        node.decompose()
    for node in list(soup.find_all(True)):
        if node.attrs is None:
            continue
        style = re.sub(r"\s+", "", str(node.get("style", ""))).lower()
        if (node.has_attr("hidden") or str(node.get("aria-hidden", "")).lower() == "true"
                or "display:none" in style or "visibility:hidden" in style
                or node.get("type", "").lower() == "hidden"
                or set(node.get("class", [])) & {"hide", "hidden", "dn", "display-none"}):
            node.decompose()

    def label_text(value):
        return re.sub(r"\s*[:：]\s*$", "", clean(value)).strip()

    def value_text(value):
        return re.sub(r"^\s*[:：]\s*", "", clean(value)).strip()

    def own_headers(table):
        return [label_text(cell.get_text(" ", strip=True)) for cell in table.find_all("th")
                if cell.find_parent("table") is table]

    tables = [table for table in soup.find_all("table") if "发票信息" in own_headers(table)]
    if len(tables) != 1:
        return {"invoice": invoice, "buyer": buyer, "complete": False}
    information = tables[0]

    def exact_rows(table):
        pairs = []
        for row in table.find_all("tr"):
            if row.find_parent("table") is not table:
                continue
            cells = row.find_all("td", recursive=False)
            if len(cells) != 2 or "label" not in cells[0].get("class", []):
                continue
            pairs.append((label_text(cells[0].get_text(" ", strip=True)), value_text(cells[1].get_text(" ", strip=True))))
        return pairs

    rows = exact_rows(information)
    title = _one([value for label, value in rows if label == "发票抬头"])
    tax = _one([value.upper() for label, value in rows if label == "公司税号"])
    buyer["invoice_buyer"] = title or ""
    buyer["invoice_tax_id"] = tax if tax and re.fullmatch(r"(?:[A-Z0-9]{15}|[A-Z0-9]{18}|[A-Z0-9]{20})", tax) else ""

    # Some versions place the exact type row in a separate tb-void table. Such
    # rows are acceptable only outside explicit help/FAQ/seller areas. Conflicts
    # remain unknown; finding one familiar word never wins over another value.
    type_values = []
    for table in soup.find_all("table"):
        headers = own_headers(table)
        if any(re.search(r"FAQ|常见问题|帮助|说明|销售方|销方", header, re.I) for header in headers):
            continue
        excluded = False
        for ancestor in [table, *table.parents]:
            if not isinstance(ancestor, Tag):
                continue
            marker = " ".join([str(ancestor.get("id", "")), *ancestor.get("class", [])])
            if ancestor.name in ("aside", "details") or re.search(r"(?:^|[-_\s])(?:faq|help|question|explanation)(?:$|[-_\s])", marker, re.I):
                excluded = True
                break
        if not excluded:
            if table is information:
                type_values.extend(value for label, value in exact_rows(table) if label == "发票类型")
            else:
                # A separate type section may use a different table class. Its
                # observed exact label/value span structure is required, so an
                # unstructured help table cannot provide a type by mentioning it.
                for row in table.find_all("tr"):
                    if row.find_parent("table") is not table:
                        continue
                    cells = row.find_all("td", recursive=False)
                    if (len(cells) == 2 and "label" in cells[0].get("class", [])
                            and cells[0].select_one("span.ftx06") and cells[1].select_one("span.ftx06")
                            and label_text(cells[0].get_text(" ", strip=True)) == "发票类型"):
                        type_values.append(value_text(cells[1].get_text(" ", strip=True)))
    typ = _one(type_values)
    ordinary = {"电子普通发票", "增值税电子普通发票", "增值税普通发票", "数电普票", "数电普通发票",
                "数电发票（普通发票）", "数电发票(普通发票)", "普通发票（电子）", "普通发票(电子)"}
    special = {"数电专票", "电子专票", "增值税专用发票", "增值税电子专用发票",
               "数电发票（增值税专用发票）", "数电发票(增值税专用发票)", "电子发票（增值税专用发票）"}
    if typ in ordinary:
        invoice["invoice_type"] = "ordinary"
    elif typ in special:
        invoice["invoice_type"] = "special"

    states = [value for label, value in rows if label in ("发票状态", "开票状态") and value]
    state = _one(states)
    if state in {"已开具", "已开票", "开票成功"}:
        invoice["invoice_status"] = "issued"
    elif state in {"已申请", "开票中", "待开具", "待开票", "申请中", "审核中", "待审核"}:
        invoice["invoice_status"] = "pending"
    elif state == "未申请":
        invoice["invoice_status"] = "unrequested"
    elif state in {"申请驳回", "开票失败", "已拒绝"}:
        invoice["invoice_status"] = "rejected"
    elif not states and buyer["invoice_buyer"] and buyer["invoice_tax_id"] and invoice["invoice_type"] != "unknown":
        # A visible action is useful only inside the actual, populated invoice
        # table. FAQ links, placeholder hrefs, or standalone text are insufficient.
        for action in information.select("a[href],button,[role='button']"):
            if re.sub(r"\s+", "", action.get_text(" ", strip=True)) not in {"查看发票", "查看XML"}:
                continue
            if action.has_attr("disabled") or action.get("aria-disabled") == "true":
                continue
            href = str(action.get("href", ""))
            if action.name == "a" and not (official_url(href, platform) or (href.startswith("/") and not href.startswith("//"))):
                continue
            invoice["invoice_status"] = "issued"
            break
    complete = bool(invoice["invoice_status"] != "unknown" and invoice["invoice_type"] != "unknown"
                    and buyer["invoice_buyer"] and buyer["invoice_tax_id"])
    return {"invoice": invoice, "buyer": buyer, "complete": complete}


def trade_observation(text: str) -> str:
    if re.search(r"部分退款|部分退货|退款中|退货中|售后处理中", text):
        return "partial_refund"
    if re.search(r"退款成功|已全额退款|全额退款|已退款", text):
        return "refunded"
    if re.search(r"交易关闭|已取消|订单取消", text):
        return "closed"
    if re.search(r"等待买家付款|等待付款|待付款|未付款", text):
        return "unpaid"
    if re.search(r"交易成功|已完成|订单完成", text):
        return "completed"
    if re.search(r"等待卖家发货|卖家已发货|待发货|待收货|等待收货|已付款", text):
        return "paid"
    return "unknown"


def transaction_observation(text: str) -> dict:
    """Only labelled current totals and explicit refund observations are trusted.

    This is intentionally stricter than listing import: absent refund information
    is unknown, and a product price or invoice total is not the order paid total.
    """
    text = clean(text)
    statuses = set()
    groups = {
        "unpaid": r"等待买家付款|等待付款|待付款|未付款",
        "closed": r"交易关闭|已取消|订单取消",
        "completed": r"交易成功|已完成|订单完成",
        "paid": r"等待卖家发货|卖家已发货|待发货|待收货|等待收货|已付款",
    }
    for status, pattern in groups.items():
        if re.search(pattern, text):
            statuses.add(status)
    totals = set()
    total_matches = list(re.finditer(r"(?:实付款|实付金额|实际支付|订单金额|订单实付|应付总额)\s*[:：]?\s*([¥￥]?\s*[\d,]+(?:\.\d{1,2})?(?:\s*元)?)(?![\d.,]|\s*[.,])", text))
    malformed_total = len(total_matches) != len(re.findall(r"实付款|实付金额|实际支付|订单金额|订单实付|应付总额", text))
    for match in total_matches:
        amount = money_fen(match.group(1))
        if amount is not None:
            totals.add(amount)
        else:
            malformed_total = True
    refund = "unknown"
    if re.search(r"部分退款|部分退货|退款中|退货中|售后处理中", text):
        refund = "partial_or_pending"
    elif re.search(r"退款成功|已全额退款|全额退款|已退款", text):
        refund = "refunded"
    else:
        amounts = []
        refund_matches = list(re.finditer(r"退款金额\s*[:：]?\s*([¥￥]?\s*[\d,]+(?:\.\d{1,2})?(?:\s*元)?)(?![\d.,]|\s*[.,])", text))
        malformed_refund = len(refund_matches) != text.count("退款金额")
        for match in refund_matches:
            amount = money_fen(match.group(1))
            if amount is not None:
                amounts.append(amount)
            else:
                malformed_refund = True
        if any(amount > 0 for amount in amounts):
            refund = "partial_or_pending"
        elif not malformed_refund and (re.search(r"无退款|未发生退款|未退款|退款状态\s*[:：]\s*(?:无|未申请)|售后状态\s*[:：]\s*无", text) or (amounts and set(amounts) == {0})):
            refund = "none"
    status = next(iter(statuses)) if len(statuses) == 1 else "unknown"
    if refund == "refunded":
        status = "refunded"
    elif refund == "partial_or_pending":
        status = "partial_refund"
    return {"trade_status": status, "refund_status": refund,
            "amount_fen": next(iter(totals)) if len(totals) == 1 and not malformed_total else None}


def transaction_dom_observation(html: str, platform: str) -> dict:
    """Supplement only a unique Taobao payment-settlement total from known DOM.

    Callers must first bind this HTML to the intended order. Trade and refund
    observations retain their text rules; payment-benefit labels prove neither
    the existence nor the absence of a refund.
    """
    if platform not in PLATFORM_URLS:
        raise ValueError("不支持的平台")
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script,style,template,noscript"):
        node.decompose()
    text = clean(soup.get_text(" ", strip=True))
    observation = transaction_observation(text)
    if platform != "taobao":
        return observation
    sections = soup.select("div.trade-order-detail-settlement-info")
    if not sections:
        return observation

    def unknown_amount():
        return observation | {"amount_fen": None}

    if len(sections) != 1:
        return unknown_amount()
    section = sections[0]
    titles = section.select("div[class^='infoTitle--']")
    if len(titles) != 1 or clean(titles[0].get_text(" ", strip=True)) != "付款详情":
        return unknown_amount()
    last_rows = section.find_all(lambda tag: tag.name == "div" and any(
        value.startswith("lastDetailInfoContent--") for value in tag.get("class", [])))
    total_labels = [item for item in section.select("div[class^='detailInfoItem--']")
                    if clean(item.get_text(" ", strip=True)).rstrip("：:") == "合计"]
    if len(last_rows) != 1 or len(total_labels) != 1:
        return unknown_amount()
    row = last_rows[0]
    labels = row.select("div[class^='detailInfoItem--']")
    if len(labels) != 1 or labels[0] is not total_labels[0]:
        return unknown_amount()
    components = {name: row.select(".trade-price-" + name) for name in ("symbol", "integer", "point", "decimal")}
    if any(len(items) != 1 for items in components.values()):
        return unknown_amount()
    parts = {name: clean(items[0].get_text("", strip=True)) for name, items in components.items()}
    if (parts["symbol"] not in ("¥", "￥") or parts["point"] != "."
            or not re.fullmatch(r"\d{1,10}|\d{1,3}(?:,\d{3}){1,3}", parts["integer"])
            or not re.fullmatch(r"\d{2}", parts["decimal"])):
        return unknown_amount()
    amount = money_fen(parts["integer"] + "." + parts["decimal"])
    if amount is None:
        return unknown_amount()
    current = observation["amount_fen"]
    # An ambiguous/malformed explicit textual total is not "absent" and cannot
    # be healed by choosing whichever DOM amount happens to look plausible.
    explicit_text_total = bool(re.search(r"实付款|实付金额|实际支付|订单金额|订单实付|应付总额", text))
    if (current is not None and current != amount) or (current is None and explicit_text_total):
        return unknown_amount()
    return observation | {"amount_fen": amount}


def buyer_observation(text: str) -> dict:
    """Read explicitly labelled buyer identity, excluding seller identity blocks."""
    text = clean(text)
    name_end = r"(?=\s*(?:(?:购买方|购方|销售方|销方)?(?:纳税人识别号|税号|统一社会信用代码)|地址|电话|开户|发票|$))"
    names = re.findall(r"(?:购买方|购方)(?:名称|企业名称|单位名称)\s*[:：]\s*(.+?)" + name_end, text)
    taxes = re.findall(r"(?:购买方|购方)(?:纳税人识别号|税号|统一社会信用代码)\s*[:：]\s*([A-Za-z0-9]{15}|[A-Za-z0-9]{18}|[A-Za-z0-9]{20})(?![A-Za-z0-9])", text)
    sections = re.findall(r"(?:购买方|购方)(?:信息)?\s*[:：]?\s+(.+?)(?=(?:销售方|销方)(?:信息)?|$)", text)
    for section in sections:
        names.extend(re.findall(r"(?:企业名称|单位名称|名称)\s*[:：]\s*(.+?)" + name_end, section))
        taxes.extend(re.findall(r"(?:纳税人识别号|税号|统一社会信用代码)\s*[:：]\s*([A-Za-z0-9]{15}|[A-Za-z0-9]{18}|[A-Za-z0-9]{20})(?![A-Za-z0-9])", section))
    return {"invoice_buyer": _one(names) or "", "invoice_tax_id": (_one(taxes) or "").upper()}


def sanitize_diagnostic_html(html: str, platform: str, base_url: str, max_bytes=1024 * 1024) -> tuple[str, bool]:
    """Keep useful rendered structure without executable/hidden/form-value data."""
    soup = BeautifulSoup(html, "html.parser")
    for node in list(soup.find_all(string=lambda s: isinstance(s, (Comment, Doctype)))):
        node.extract()
    for node in list(soup.select("script,style,template,noscript,iframe,object,embed,base,svg,canvas,meta,link")):
        node.decompose()
    sensitive = re.compile(r"token|authorization|\bauth\b|cookie|session|secret|password|credential|csrf|authkey", re.I)
    for node in list(soup.find_all(True)):
        if node.attrs is None:
            continue
        style = str(node.get("style", "")).replace(" ", "").lower()
        hidden = (node.has_attr("hidden") or node.get("aria-hidden") == "true"
                  or node.get("type", "").lower() == "hidden"
                  or "display:none" in style or "visibility:hidden" in style
                  or "opacity:0;" in style or style.endswith("opacity:0"))
        identifiers = " ".join(str(node.get(key, "")) for key in ("id", "class", "name"))
        if hidden or sensitive.search(identifiers):
            node.decompose()
            continue
        if node.name in ("textarea", "select", "datalist") or node.has_attr("contenteditable"):
            node.clear()
    allowed = {"class", "id", "name", "role", "type", "placeholder", "href", "for", "aria-label",
               "aria-labelledby", "aria-describedby", "aria-disabled", "colspan", "rowspan"}
    order_keys = {"orderid", "order_id", "bizorderid", "biz_order_id", "tradeid", "trade_id",
                  "parentorderid", "mainorderid", "suborderid"}
    for node in soup.find_all(True):
        for key in list(node.attrs):
            if key not in allowed:
                del node.attrs[key]
                continue
            value = node.attrs[key]
            values = value if isinstance(value, list) else [value]
            if any(len(str(item)) > 300 or sensitive.search(str(item)) for item in values) and key != "href":
                del node.attrs[key]
        if node.has_attr("href"):
            absolute = urljoin(base_url, str(node["href"]))
            if not official_url(absolute, platform):
                del node.attrs["href"]
                continue
            parsed = urlparse(absolute)
            path = parsed.path
            if (sensitive.search(path) or re.search(r"passport|login|qrcode|/qr(?:/|$)", path, re.I)
                    or any(len(part) > 64 for part in path.split("/"))):
                path = "/"
            query = [(key, value) for key, value in parse_qsl(parsed.query)
                     if key.lower() in order_keys and re.fullmatch(r"\d{6,40}", value)]
            node["href"] = urlunparse(("https", parsed.netloc, path, "", urlencode(query), ""))
    # Cookie/token text in rendered error/debug panels is unnecessary for DOM
    # adaptation. Remove its text node rather than retaining a credential dump.
    for text in list(soup.find_all(string=True)):
        if sensitive.search(str(text)) or re.search(r"Bearer\s+\S+", str(text), re.I):
            text.replace_with("[敏感调试文本已移除]")
    encoded = str(soup).encode("utf-8")
    regular_notice = "<p>脱敏 DOM 诊断；已移除脚本、隐藏区、输入值及非白名单属性。</p>\n"
    truncated = len(encoded) + len(regular_notice.encode("utf-8")) > max_bytes
    notice = ("<p>脱敏 DOM 诊断；文件超过 1 MiB，以下已截断，不代表完整页面。</p>\n"
              if truncated else regular_notice).encode("utf-8")
    body = encoded[:max(0, max_bytes - len(notice))].decode("utf-8", errors="ignore")
    return notice.decode("utf-8") + body, truncated


def _observed_link(node: Tag, platform: str, base_url: str, order_no: str, invoice=False) -> str:
    found = []
    for link in node.select("a[href]"):
        label = clean(link.get_text(" ", strip=True))
        if not re.search(r"发票" if invoice else r"订单详情|查看订单", label):
            continue
        url = urljoin(base_url, link["href"])
        if not official_url(url, platform):
            continue
        if not invoice:
            values = [v for vs in parse_qs(urlparse(url).query).values() for v in vs]
            if order_no not in values and not re.search(rf"(?<!\d){re.escape(order_no)}(?!\d)", url):
                continue
        found.append(url)
    return _one(found) or ""


def parse_orders(html: str, platform: str, base_url: str | None = None) -> ParseResult:
    if platform not in PLATFORM_URLS:
        raise ValueError("不支持的平台")
    soup = BeautifulSoup(html, "html.parser")
    text = clean(soup.get_text(" ", strip=True))
    base_url = base_url or PLATFORM_URLS[platform]
    nodes = soup.select(CONTAINERS[platform])
    # A legacy table can contain independently identified modern containers.
    nodes = [n for n in nodes if not any(parent in nodes for parent in n.parents)]
    result = ParseResult(container_count=len(nodes))
    explicit_empty = bool(re.search(r"暂无订单|没有找到符合条件的订单|没有符合条件的订单|您还没有订单|未找到相关订单", text))
    result.recognized = bool(nodes) or explicit_empty
    result.empty = explicit_empty and not nodes
    seen: set[str] = set()
    for index, node in enumerate(nodes):
        number = _order_no(node, platform)
        day = _order_date(node)
        amount = _amount(node, platform)
        product_selector = ("[class^='titleText--'], [class*='production-mod__title'], .baobei-name, .goods-item .p-name"
                            if platform == "taobao" else "h3[class^='productTitle-'], .goods-item .p-name, .goods-item .p-name a, .p-name a")
        shop_selector = ("a[class^='shopInfoName--'], [class^='shopName--'], [class*='seller-mod__name'], .seller-name, .shop-name"
                         if platform == "taobao" else "span[class^='shopNames-'], .tr-th .shop-txt, .tr-th .shop-name, .tr-th .shop .txt, .tr-th .shop a")
        products = list(dict.fromkeys(_texts(node, product_selector)))
        shop = _one(_texts(node, shop_selector))
        if not number or not day or amount is None or not products or not shop:
            missing = [name for name, ok in (("订单号", number), ("日期", day), ("唯一金额", amount is not None), ("商品", products), ("店铺", shop)) if not ok]
            result.rejected.append(f"第 {index + 1} 条订单缺少或存在歧义：{'、'.join(missing)}")
            field_names = {"订单号": "order_no", "日期": "paid_at", "唯一金额": "amount_fen", "商品": "product", "店铺": "shop"}
            result.unresolved_records.append({"platform": platform, "order_no": number or "", "paid_at": day or "",
                "product": "；".join(products), "shop": shop or "", "amount_fen": amount,
                "observed_url": _observed_link(node, platform, base_url, number) if number else "",
                "missing_fields": [field_names[name] for name in missing], "missing_labels": missing,
                "reason": "当前卡片字段缺失或存在歧义；未知金额未按零金额导入，需只读详情核对"})
            continue
        if number in seen:
            result.rejected.append(f"订单 {number} 重复出现，未重复导入")
            continue
        seen.add(number)
        node_text = clean(node.get_text(" ", strip=True))
        order_url = _observed_link(node, platform, base_url, number)
        evidence = "平台订单页面可见信息；日期为页面订单时间，非支付流水核验。专票能力仅据当前可见文字，待核对。"
        if not order_url:
            evidence += "当前详情入口未观察到官方链接，未推断详情地址；申请和下载需人工核对可识别详情。"
        order = {
            "platform": platform, "order_no": number, "product": "；".join(products), "shop": shop,
            "amount_fen": amount, "paid_at": day, "trade_status": trade_observation(node_text),
            "company_purchase": False, "source": "browser",
            "evidence": evidence,
            "order_url": order_url,
            "invoice_url": _observed_link(node, platform, base_url, number, invoice=True),
            **invoice_observation(node_text),
        }
        result.orders.append(order)
    return result
