"""Read-only inspection of one already identified buyer order.

Only observed official detail navigation and explicit list pagination are used.
This module deliberately never opens an invoice application, merchant chat,
payment control, or a download. It does not persist order/application records.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date
from urllib.parse import urljoin

from .browser import BrowserAttention
from .parsers import (
    CONTAINERS,
    ORDER_RE,
    PLATFORM_URLS,
    buyer_observation,
    clean,
    invoice_observation,
    invoice_detail_dom_observation,
    official_url,
    trade_observation,
    transaction_observation,
    transaction_dom_observation,
)


# Restrict the returned labels to invoice UI vocabulary rather than arbitrary
# links which happen to contain "发票" (such as recommended product names).
_INVOICE_LABEL = re.compile(
    r"^(?=.{1,80}$)(?:申请|查看|下载|补开|重开|换开|开具|编辑|修改|确认|提交|取消|我要|"
    r"电子|增值税|数电|普通|专用|普票|专票|PDF|OFD|XML|发票|开票|票据|"
    r"信息|详情|管理|须知|设置|抬头|资格|资质|类型|内容|记录|服务|说明|常见问题|[（）()\s:：/·-])+$",
    re.I,
)
_CONTROLS = ("a,button,[role='link'],[role='button'],input[type='button'],input[type='submit'],"
             "div[class*='button'],span[class*='button'],div[class*='Button'],span[class*='Button']")


async def _diagnostic(manager, page, platform):
    if page is None:
        return ""
    try:
        result = await manager._diagnostic(page, platform)
    except Exception:
        return ""
    if not isinstance(result, str):
        return ""
    # BrowserManager returns a human-readable message. The API result exposes
    # only its local path, not HTML or any page text.
    if result.startswith("脱敏 DOM 诊断："):
        return result.removeprefix("脱敏 DOM 诊断：").split("（超过", 1)[0]
    return result if result.startswith("/") else ""


async def _card_ids(card):
    text = await card.inner_text()
    numbers = set(ORDER_RE.findall(text))
    identifier = await card.get_attribute("id") or ""
    match = re.fullmatch(r"shopOrderContainer_(\d{6,40})", identifier)
    if match:
        numbers.add(match.group(1))
    for key in ("data-orderid", "data-order-id"):
        value = await card.get_attribute(key) or ""
        if re.fullmatch(r"\d{6,40}", value):
            numbers.add(value)
    # Legacy JD can put the number into an explicit .tr-th .number span.
    identifiers = card.locator(".tr-th .number, .tr-th .orderid, [data-orderid]")
    for index in range(await identifiers.count()):
        item = identifiers.nth(index)
        if not await item.is_visible():
            continue
        value = (await item.get_attribute("data-orderid") or await item.inner_text()).strip()
        if re.fullmatch(r"\d{6,40}", value):
            numbers.add(value)
    return numbers


async def _detail_from_list(manager, page, platform, number, year):
    await page.goto(PLATFORM_URLS[platform], wait_until="domcontentloaded")
    await manager._wait_orders_ready(page, platform)
    await manager._check(page, platform)
    # Do not silently reuse JD's default recent-three-month range. The target
    # order date determines which complete calendar year must be searched.
    scope = await manager._prepare_scan_scope(page, platform, date(year, 1, 1), date(year, 12, 31))
    if not scope.get("verified"):
        raise BrowserAttention("只读检查无法确认所选订单历史范围，已停止。")
    await manager._wait_orders_ready(page, platform)
    if not await manager._verify_first_page(page, platform):
        raise BrowserAttention("只读检查未确认从订单第一页开始，已停止。")

    seen_pages = set()
    expected_total = None
    for index in range(100):
        await manager._wait_orders_ready(page, platform)
        await manager._check(page, platform)
        locator = page.locator(CONTAINERS[platform])
        cards, identities, matching = [], [], []
        for position in range(await locator.count()):
            card = locator.nth(position)
            if not await card.is_visible():
                continue
            values = await _card_ids(card)
            if len(values) != 1:
                raise BrowserAttention("订单列表有缺失或不唯一的订单号，未猜测目标详情。")
            ident = next(iter(values))
            cards.append(card)
            identities.append(ident)
            if ident == number:
                matching.append(card)
        if not cards:
            raise BrowserAttention("当前订单列表没有可识别的订单，未找到目标订单详情。")
        if len(matching) > 1:
            raise BrowserAttention("目标订单在同页重复出现，无法唯一确认详情入口。")
        identity = hashlib.sha256("\n".join(sorted(identities)).encode()).hexdigest()
        if identity in seen_pages:
            raise BrowserAttention("分页后订单身份仍重复，已停止只读查找。")
        seen_pages.add(identity)
        if matching:
            # A current JD detail entry has no href and is implemented as a
            # visible text control. Its exact label is the only permitted entry.
            controls = matching[0].get_by_text("订单详情", exact=True)
            choices = []
            for position in range(await controls.count()):
                control = controls.nth(position)
                if await control.is_visible():
                    choices.append(control)
            if len(choices) != 1:
                raise BrowserAttention("目标订单缺少唯一的“订单详情”入口，未操作其他入口。")
            control = choices[0]
            if await manager._control_disabled(control):
                raise BrowserAttention("目标订单的详情入口当前不可用，已停止。")
            href = await control.get_attribute("href") or ""
            if href and not href.startswith(("#", "javascript:")) and not official_url(urljoin(page.url, href), platform):
                raise BrowserAttention("订单详情入口不是对应平台官方地址，未点击。")
            return await manager._activate(page, control)

        state = await manager._next_page_control(page, platform)
        current, total = state.get("current_page"), state.get("total_pages")
        if current is not None and current != index + 1:
            raise BrowserAttention("只读查找的页码不连续，已停止。")
        if index == 0:
            expected_total = total
        elif expected_total is not None and total is not None and total != expected_total:
            raise BrowserAttention("查找期间订单总页数发生变化，已停止。")
        if not state.get("recognized"):
            raise BrowserAttention("订单分页结构未明确识别，未盲目翻页。")
        if state.get("disabled"):
            raise BrowserAttention("已到所选历史订单末页，未找到目标订单。")
        next_control = state.get("next")
        if next_control is None or (total is not None and current is not None and current >= total):
            raise BrowserAttention("缺少明确可用的下一页，未继续查找。")
        if index == 99:
            raise BrowserAttention("只读查找已达到 100 页上限，未找到目标订单。")
        href = await next_control.get_attribute("href") or ""
        if href and not href.startswith(("#", "javascript:")) and not official_url(urljoin(page.url, href), platform):
            raise BrowserAttention("下一页入口离开对应平台官方地址，未点击。")
        previous = await page.locator(CONTAINERS[platform]).all_inner_texts()
        await next_control.click()
        try:
            await page.wait_for_function(
                "state => JSON.stringify(Array.from(document.querySelectorAll(state.selector)).map(el => el.innerText)) !== JSON.stringify(state.previous)",
                arg={"selector": CONTAINERS[platform], "previous": previous}, timeout=10_000,
            )
        except Exception as exc:
            raise BrowserAttention("翻页后未确认订单列表变化，未重试或继续点击。") from exc
    raise BrowserAttention("只读查找达到页数上限。")


async def _wait_exact_detail(manager, page, platform, number):
    await manager._check(page, platform)
    try:
        await page.wait_for_function(
            r"number => { const text=document.body?.innerText || ''; const ids=[...text.matchAll(/(?:订单号|订单编号|订单号码)\s*[:：]?\s*(\d{6,40})(?!\d)/g)].map(m=>m[1]); return ids.length>0 && ids.every(id=>id===number); }",
            arg=number, timeout=10_000,
        )
    except Exception as exc:
        raise BrowserAttention("订单详情未明确显示唯一的目标订单号标签，已停止。") from exc
    await manager._check(page, platform)
    if not official_url(page.url, platform):
        raise BrowserAttention("详情跳转离开对应平台官方地址，已停止。")
    body = page.locator("body")
    text = await body.inner_text()
    if set(ORDER_RE.findall(text)) != {number}:
        raise BrowserAttention("订单详情目标编号缺失或存在其他订单，已停止。")
    return body, text


async def _invoice_controls(manager, page, body, platform):
    found = []
    controls = body.locator(_CONTROLS)
    for index in range(await controls.count()):
        item = controls.nth(index)
        if not await item.is_visible():
            continue
        label = clean(await item.inner_text())
        if not label:
            label = clean(await item.get_attribute("aria-label") or await item.get_attribute("value") or "")
        if not _INVOICE_LABEL.fullmatch(label) or not re.search(r"发票|开票|票据|普票|专票|PDF|OFD|XML", label, re.I):
            continue
        if len(found) >= 80:
            raise BrowserAttention("详情出现过多发票控件，结构需人工核对。")
        role = await item.evaluate("el => el.getAttribute('role') || ({A:'link', BUTTON:'button', INPUT:'button'}[el.tagName] || el.tagName.toLowerCase())")
        href = await item.get_attribute("href") or ""
        absolute = urljoin(page.url, href) if href and not href.startswith(("#", "javascript:")) else ""
        found.append({"label": label, "role": role, "disabled": bool(await manager._control_disabled(item)),
                      "href": absolute if absolute and official_url(absolute, platform) else ""})
    return found


async def inspect_order(manager, order: dict, profile: dict, *, invoice_details=False) -> dict:
    """Inspect one order under the manager lock, returning structured fields.

    Callers must not already hold ``manager.lock`` (it is not reentrant). Profile
    data is accepted for API compatibility but is neither filled nor transmitted.
    Missing invoice fields remain explicit unknowns; no database state is changed.
    """
    platform, number = order.get("platform"), str(order.get("order_no", ""))
    if platform not in PLATFORM_URLS or not re.fullmatch(r"\d{6,40}", number):
        raise BrowserAttention("目标平台或订单编号无效，未开始只读检查。")
    url = order.get("order_url") or ""
    if url and not official_url(url, platform):
        raise BrowserAttention("已有订单详情链接不是对应平台官方地址，未导航。")
    page = None
    async with manager.lock:
        try:
            page = await manager._page(platform)
            if url:
                await page.goto(url, wait_until="domcontentloaded")
            else:
                try:
                    year = date.fromisoformat(str(order.get("paid_at", ""))).year
                except (TypeError, ValueError) as exc:
                    raise BrowserAttention("目标订单缺少有效日期，无法确认历史查找年份。") from exc
                page = await _detail_from_list(manager, page, platform, number, year)
            body, text = await _wait_exact_detail(manager, page, platform, number)
            observed_order_url = page.url
            observations = {
                "invoice": invoice_observation(text),
                "trade": trade_observation(text),
                "transaction": transaction_dom_observation(await body.inner_html(), platform),
                "buyer": buyer_observation(text),
            }
            controls = await _invoice_controls(manager, page, body, platform)
            order_diagnostic = await _diagnostic(manager, page, platform)
            invoice_detail = None
            if invoice_details:
                # Follow only a read-only invoice link observed inside the exact
                # target order. Application, reissue and download controls stay
                # outside this path, including JS-only view buttons.
                links = {item["href"] for item in controls if item["label"] in (
                    "查看发票详情", "发票详情", "查看电子发票", "查看发票") and item["href"] and not item["disabled"]}
                if len(links) == 1:
                    invoice_url = next(iter(links))
                    try:
                        await page.goto(invoice_url, wait_until="domcontentloaded")
                        invoice_body, invoice_text = await _wait_exact_detail(manager, page, platform, number)
                        details = invoice_detail_dom_observation(await invoice_body.inner_html(), platform)
                        # The invoice page contains FAQ text about multiple
                        # types. Only the labelled information table is evidence.
                        observations["invoice"] = details["invoice"]
                        buyer = details["buyer"]
                        if buyer["invoice_buyer"]:
                            observations["buyer"] = buyer
                        invoice_detail = {"observed_url": page.url, "read_only": True, "verified_order": True,
                                          "diagnostic_path": await _diagnostic(manager, page, platform),
                                          "controls": await _invoice_controls(manager, page, invoice_body, platform)}
                    except BrowserAttention as exc:
                        invoice_detail = {"read_only": True, "verified_order": False, "error": str(exc),
                                          "diagnostic_path": await _diagnostic(manager, page, platform)}
                else:
                    invoice_detail = {"read_only": True, "verified_order": False,
                                      "error": "未观察到唯一可读取的官方发票详情链接，未操作其他控件"}
            missing = []
            if invoice_details and not invoice_detail.get("verified_order"):
                missing.append("invoice_detail.verified_order")
            for key in ("trade_status", "refund_status"):
                if observations["transaction"][key] == "unknown":
                    missing.append("transaction." + key)
            if observations["transaction"]["amount_fen"] is None:
                missing.append("transaction.amount_fen")
            if observations["invoice"]["invoice_status"] == "unknown":
                missing.append("invoice.invoice_status")
            if observations["invoice"]["invoice_status"] == "issued":
                if observations["invoice"]["invoice_type"] == "unknown":
                    missing.append("invoice.invoice_type")
                for key in ("invoice_buyer", "invoice_tax_id"):
                    if not observations["buyer"][key]:
                        missing.append("buyer." + key)
            elif observations["invoice"]["special_status"] == "unknown":
                missing.append("invoice.special_status")
            return {"observed_order_url": observed_order_url, "observations": observations,
                    "invoice_controls": controls, "diagnostic_path": order_diagnostic,
                    "invoice_detail": invoice_detail,
                    "missing_fields": missing, "complete": not missing}
        except BrowserAttention as exc:
            diagnostic_path = await _diagnostic(manager, page, platform)
            if diagnostic_path:
                raise BrowserAttention(f"{exc} 脱敏 DOM 诊断：{diagnostic_path}") from exc
            raise
        except Exception as exc:
            diagnostic_path = await _diagnostic(manager, page, platform)
            suffix = f" 脱敏 DOM 诊断：{diagnostic_path}" if diagnostic_path else ""
            raise BrowserAttention("只读详情检查未完成；请核对页面状态，未操作发票申请或其他写入口。" + suffix) from exc
