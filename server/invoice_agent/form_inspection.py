"""Inspect an invoice form after one explicitly journalled entry activation.

Opening an application entry can itself have side effects. The caller must
write an uncertain application journal before calling this function. This
helper never clears that journal, fills a field, changes a choice, or submits.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlparse

from .browser import BrowserAttention, ORDINARY_OPTION, SPECIAL_OPTION
from .inspection import _diagnostic, _wait_exact_detail
from .parsers import (
    PLATFORM_URLS,
    ORDER_RE,
    clean,
    invoice_observation,
    official_url,
    transaction_dom_observation,
)


_ENTRY_LABELS = {"申请开票", "申请发票", "补开发票"}
_SUBMIT_LABELS = {"提交申请", "确认申请", "确认提交", "提交开票申请", "向商家发送申请", "确认开票", "提交开票", "提交"}
_CONTROLS = "button,a,[role='button'],[role='link'],div.trade-button,span.trade-button,input[type='submit'],input[type='button']"
_BUSY_OR_SUCCESS = re.compile(r"已申请(?:开票|发票)?|开票中|待开票|审核中|申请已提交|申请成功|开票成功|已开票|发票已开具|提交成功|开票申请已提交")
_FIELD_KINDS = (
    ("company_name", r"企业名称|公司名称|单位名称|发票抬头|invoice[_-]?title|buyer[_-]?name", "公司名称"),
    ("tax_id", r"纳税人识别号|纳税人识别码|税号|统一社会信用代码|tax[_-]?(?:id|no)|taxpayer", "纳税人识别号"),
    ("address", r"注册地址|企业地址|单位地址|公司地址|register[_-]?address", "注册地址"),
    ("phone", r"注册电话|企业电话|单位电话|公司电话|register[_-]?phone", "注册电话"),
    ("bank_name", r"开户银行|开户行名称|bank[_-]?name", "开户银行"),
    ("bank_account", r"银行账号|银行账户|开户账号|bank[_-]?account", "银行账号"),
    ("email", r"接收邮箱|收票邮箱|电子邮箱|邮箱地址|email|e-mail", "收票邮箱"),
)


@dataclass(frozen=True)
class InvoiceFormPageReference:
    """Only produced after this helper receives an activated entry's page."""
    page: Any
    platform: str
    order_no: str


def _cache_key(order):
    return order.get("id") or f"{order['platform']}-{order['order_no']}"


def _url_identity(url, platform, number):
    if not official_url(url, platform):
        return {"official": False, "parameters": [], "exact_order_parameter_names": [], "has_exact_order_parameter": False}
    parsed = urlparse(url)
    parameters = [{"name": key[:100], "exact_order_match": value == number}
                  for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    matching = [item["name"] for item in parameters if item["exact_order_match"]]
    return {"official": True, "host": parsed.hostname, "path": parsed.path,
            "parameters": parameters, "exact_order_parameter_names": matching,
            "has_exact_order_parameter": bool(matching)}


def _failure_observation(page, platform, number, activated, diagnostic):
    url = page.url if page is not None and official_url(page.url, platform) else ""
    return {"current_url": url, "entry_activated": activated, "target_verified": False,
            "diagnostic_path": diagnostic, "side_effect_unknown": activated,
            "url_identity": _url_identity(url, platform, number)}


async def _label(item):
    label = clean(await item.inner_text())
    if not label:
        label = clean(await item.get_attribute("aria-label") or await item.get_attribute("value") or "")
    return label


async def _matching_controls(manager, body, labels):
    result = []
    controls = body.locator(_CONTROLS)
    for index in range(await controls.count()):
        item = controls.nth(index)
        if await item.is_visible() and await _label(item) in labels:
            result.append(item)
    return result


async def _type_options(manager, body):
    result = []
    choices = body.locator("input[type='radio'],[role='radio']")
    for index in range(await choices.count()):
        item = choices.nth(index)
        if not await item.is_visible():
            continue
        metadata = await item.evaluate(r"""el => { // form-choice-metadata
            const label = el.getAttribute('aria-label') ||
              (el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim() ||
              Array.from(el.labels || []).map(node => node.innerText || '').join(' ').trim() || el.innerText || '';
            return {label, checked: el.matches('input[type=radio]') ? el.checked :
              (el.getAttribute('aria-checked') === 'true' ? true : el.getAttribute('aria-checked') === 'false' ? false : null)};
        }""")
        label = clean(str(metadata.get("label") or ""))
        if len(label) > 120 or not re.search(r"发票|专票|普票|数电", label):
            continue
        typ = "special" if re.fullmatch(SPECIAL_OPTION, label) else "ordinary" if re.fullmatch(ORDINARY_OPTION, label) else "unknown"
        result.append({"label": label, "enabled": not await manager._control_disabled(item),
                       "checked": metadata.get("checked"), "invoice_type": typ})
    # Native select options are readable without opening or changing the select.
    selects = body.locator("select")
    for index in range(await selects.count()):
        select = selects.nth(index)
        if not await select.is_visible():
            continue
        descriptor = await select.evaluate("""el => // form-select-metadata
          [el.name, el.id, el.getAttribute('aria-label'), ...Array.from(el.labels || []).map(node => node.innerText || '')].filter(Boolean).join('|')""")
        if not re.search(r"发票类型|发票种类|票种|invoice[_-]?(?:type|kind)", str(descriptor), re.I):
            continue
        select_disabled = await manager._control_disabled(select)
        options = select.locator("option")
        for position in range(await options.count()):
            option = options.nth(position)
            label = clean(await option.inner_text())
            if len(label) > 120 or not re.search(r"发票|专票|普票|数电", label):
                continue
            typ = "special" if re.fullmatch(SPECIAL_OPTION, label) else "ordinary" if re.fullmatch(ORDINARY_OPTION, label) else "unknown"
            result.append({"label": label, "enabled": not select_disabled and not await manager._control_disabled(option),
                           "checked": bool(await option.evaluate("el => el.selected")), "invoice_type": typ})
    return result


async def _fields(body):
    result = []
    inputs = body.locator("input:not([type='hidden']):not([type='password']):not([type='radio']):not([type='checkbox']):not([type='submit']):not([type='button']):not([type='reset']),textarea")
    for index in range(await inputs.count()):
        item = inputs.nth(index)
        if not await item.is_visible():
            continue
        metadata = await item.evaluate("""el => { // form-field-metadata
          // Read captions and field metadata only. Never read .value, textContent
          // of a textarea, selected values, or any enterprise/profile contents.
          const labels=Array.from(el.labels || []).map(node => {const clone=node.cloneNode(true);
            clone.querySelectorAll('input,textarea,select').forEach(child => child.remove());return clone.innerText || clone.textContent || '';});
          return {name:el.name || '', id:el.id || '', labels, placeholder:el.getAttribute('placeholder') || '',
            ariaLabel:el.getAttribute('aria-label') || '', required:el.required || el.getAttribute('aria-required') === 'true'};
        }""")
        descriptor = "|".join(str(value) for value in (
            metadata.get("name", ""), metadata.get("id", ""), metadata.get("placeholder", ""),
            metadata.get("ariaLabel", ""), *metadata.get("labels", [])))
        for kind, pattern, label in _FIELD_KINDS:
            if re.search(pattern, descriptor, re.I):
                # Return a stable caption, not incidental text adjacent to an
                # existing value inside a composite label.
                result.append({"label": label, "required": bool(metadata.get("required")),
                               "name": str(metadata.get("name") or "")[:100], "kind": kind})
                break
    return result


async def inspect_invoice_form(manager, order: dict, profile: dict) -> dict:
    """Activate one exact application entry and read its form without filling.

    The helper owns ``manager.lock``. Callers must not hold it. A returned form
    is an observation, never proof that the first click had no side effect.
    """
    platform, number = order.get("platform"), str(order.get("order_no", ""))
    amount = order.get("amount_fen")
    if (platform not in PLATFORM_URLS or not re.fullmatch(r"\d{6,40}", number)
            or order.get("company_purchase") is not True or order.get("scope_excluded")
            or order.get("invoice_status") != "unrequested"
            or order.get("trade_status") not in ("paid", "completed")
            or type(amount) is not int or amount <= 0):
        raise BrowserAttention("仅能为已确认公司采购、未申请且已付款/完成的有效订单检查开票表单。")
    url = order.get("order_url") or ""
    if not url or not official_url(url, platform):
        raise BrowserAttention("缺少已观察到的官方订单详情链接，未打开开票入口。")
    page, activated = None, False
    async with manager.lock:
        try:
            page = await manager._page(platform)
            await page.goto(url, wait_until="domcontentloaded")
            body, text = await _wait_exact_detail(manager, page, platform, number)
            transaction = transaction_dom_observation(await body.inner_html(), platform)
            if transaction["amount_fen"] is None or transaction["amount_fen"] != amount:
                raise BrowserAttention("当前订单实付金额缺失、存在歧义或与已核对订单不一致，未点击开票入口。")
            if transaction["trade_status"] not in ("paid", "completed"):
                raise BrowserAttention("当前交易状态未知、未付款、关闭或存在退款，未点击开票入口。")
            if transaction["refund_status"] in ("partial_or_pending", "refunded"):
                raise BrowserAttention("当前订单存在退款或售后处理，未点击开票入口。")
            current = invoice_observation(text)
            if _BUSY_OR_SUCCESS.search(text) or current["invoice_status"] != "unrequested":
                raise BrowserAttention("当前页面未明确处于未申请状态，或已有申请/发票，未点击开票入口。")
            entries = await _matching_controls(manager, body, _ENTRY_LABELS)
            if len(entries) != 1 or await manager._control_disabled(entries[0]):
                raise BrowserAttention("当前订单没有唯一可用的精确开票申请入口，未点击其他控件。")
            entry = entries[0]
            href = await entry.get_attribute("href") or ""
            if href and not href.startswith(("#", "javascript:")) and not official_url(urljoin(page.url, href), platform):
                raise BrowserAttention("开票入口离开对应平台官方地址，未点击。")
            # An entry click itself may create an application. From this point,
            # all exceptions and results must preserve the caller's journal.
            activated = True
            page = await manager._activate(page, entry)
            cache = getattr(manager, "_invoice_form_pages", None)
            if not isinstance(cache, dict):
                cache = {}
                manager._invoice_form_pages = cache
            cache[_cache_key(order)] = InvoiceFormPageReference(page, platform, number)
            await _wait_exact_detail(manager, page, platform, number)
            try:
                await page.wait_for_function("""state => {
                  const text=document.body?.innerText || '';
                  if (/已申请(?:开票|发票)?|开票中|待开票|审核中|申请已提交|申请成功|开票成功|已开票|提交成功/.test(text)) return true;
                  return Array.from(document.querySelectorAll(state.selector)).some(el => el.getClientRects().length &&
                    state.labels.includes((el.innerText || el.getAttribute('aria-label') || el.getAttribute('value') || '').trim()));
                }""", arg={"selector": _CONTROLS, "labels": sorted(_SUBMIT_LABELS)}, timeout=10_000)
            except Exception:
                # Keep a partial observation; no replay or second entry click.
                pass
            body, text = await _wait_exact_detail(manager, page, platform, number)
            options, fields = await _type_options(manager, body), await _fields(body)
            submissions = await _matching_controls(manager, body, _SUBMIT_LABELS)
            submit_controls = [{"label": await _label(item), "enabled": not await manager._control_disabled(item)} for item in submissions]
            invoice = invoice_observation(text)
            after = transaction_dom_observation(await body.inner_html(), platform)
            missing = []
            if len(submit_controls) != 1:
                missing.append("unique_submit_control")
            if not options:
                missing.append("invoice_type_options")
            company_fields = [field for field in fields if field["kind"] in ("company_name", "tax_id")]
            if not company_fields:
                missing.append("company_fields")
            blocked = bool(_BUSY_OR_SUCCESS.search(text) or invoice["invoice_status"] in ("pending", "issued"))
            if blocked:
                missing.append("unrequested_invoice_state")
            if after["amount_fen"] is not None and after["amount_fen"] != amount:
                missing.append("matching_form_amount")
            if after["refund_status"] in ("partial_or_pending", "refunded") or after["trade_status"] in ("unpaid", "closed", "partial_refund", "refunded"):
                missing.append("eligible_form_transaction")
            form_open = (len(submit_controls) == 1 and bool(options or company_fields) and not blocked
                         and "matching_form_amount" not in missing and "eligible_form_transaction" not in missing)
            return {
                "current_url": page.url, "target_verified": True, "form_open": form_open,
                "url_identity": _url_identity(page.url, platform, number),
                "entry_activated": True, "side_effect_unknown": True,
                "type_options": options, "fields": fields, "submit_controls": submit_controls,
                "diagnostic_path": await _diagnostic(manager, page, platform),
                "invoice_status": invoice["invoice_status"], "amount_fen": amount,
                "current_amount_fen": after["amount_fen"], "refund_status": after["refund_status"],
                "refund_status_before_entry": transaction["refund_status"],
                "missing_fields": missing,
                "message": "仅观察已打开的页面，未填写或提交；首次入口仍可能已创建申请，必须保留结果不明记录，禁止自动重放。",
            }
        except BrowserAttention as exc:
            diagnostic = await _diagnostic(manager, page, platform)
            exc.observation = _failure_observation(page, platform, number, activated, diagnostic)
            message = str(exc)
            if diagnostic:
                message += " 脱敏 DOM 诊断：" + diagnostic
            if activated:
                message += " 开票入口已操作，可能已产生申请；结果不明，禁止自动重放。"
                exc.safe_to_retry = False
            exc.args = (message,)
            raise
        except (Exception, asyncio.CancelledError) as exc:
            diagnostic = await _diagnostic(manager, page, platform)
            message = ("开票入口已操作，可能已产生申请；表单检查未完成，禁止自动重放。" if activated
                       else "开票表单检查未完成，未点击申请入口。")
            if diagnostic:
                message += " 脱敏 DOM 诊断：" + diagnostic
            error = BrowserAttention(message)
            error.safe_to_retry = not activated
            error.observation = _failure_observation(page, platform, number, activated, diagnostic)
            raise error from exc


async def read_current_invoice_form(manager, order: dict, profile: dict) -> dict:
    """Read only the typed page reference saved after a prior entry activation.

    No navigation, context-page discovery, input, selection, or click occurs.
    A URL parameter match is metadata only; it never replaces the visible exact
    order-number requirement and never makes a generic profile page a form.
    """
    platform, number = order.get("platform"), str(order.get("order_no", ""))
    if platform not in PLATFORM_URLS or not re.fullmatch(r"\d{6,40}", number):
        raise BrowserAttention("目标平台或订单编号无效，未读取当前表单。")
    async with manager.lock:
        cache = getattr(manager, "_invoice_form_pages", None)
        reference = cache.get(_cache_key(order)) if isinstance(cache, dict) else None
        if (not isinstance(reference, InvoiceFormPageReference)
                or reference.platform != platform or reference.order_no != number):
            raise BrowserAttention("没有此订单已记录的开票入口返回页面，未猜测、遍历或导航其他页面。")
        page = reference.page
        try:
            if callable(getattr(page, "is_closed", None)) and page.is_closed():
                raise BrowserAttention("此订单已记录的表单页面已经关闭，未打开或查找替代页面。")
            await manager._check(page, platform)
            body = page.locator("body")
            text = await body.inner_text()
            target_verified = set(ORDER_RE.findall(text)) == {number}
            options, fields = await _type_options(manager, body), await _fields(body)
            submissions = await _matching_controls(manager, body, _SUBMIT_LABELS)
            submit_controls = [{"label": await _label(item), "enabled": not await manager._control_disabled(item)} for item in submissions]
            invoice = invoice_observation(text)
            transaction = transaction_dom_observation(await body.inner_html(), platform)
            company_fields = [field for field in fields if field["kind"] in ("company_name", "tax_id")]
            blocked = bool(_BUSY_OR_SUCCESS.search(text) or invoice["invoice_status"] in ("pending", "issued"))
            missing = []
            if not target_verified:
                missing.append("visible_exact_order_number")
            if len(submit_controls) != 1:
                missing.append("unique_submit_control")
            if not options:
                missing.append("invoice_type_options")
            if not company_fields:
                missing.append("company_fields")
            if blocked:
                missing.append("unrequested_invoice_state")
            return {
                "current_url": page.url, "url_identity": _url_identity(page.url, platform, number),
                "entry_activated": True, "cached_from_entry": True, "side_effect_unknown": True,
                "target_verified": target_verified,
                "form_open": bool(target_verified and len(submit_controls) == 1 and (options or company_fields) and not blocked),
                "type_options": options, "fields": fields, "submit_controls": submit_controls,
                "invoice_status": invoice["invoice_status"], "amount_fen": transaction["amount_fen"],
                "refund_status": transaction["refund_status"], "missing_fields": missing,
                "diagnostic_path": await _diagnostic(manager, page, platform),
                "message": "仅读取已记录的当前页面，未导航、点击或填写；URL身份信息不能替代可见订单号核验，也不证明已提交申请。",
            }
        except BrowserAttention as exc:
            diagnostic = await _diagnostic(manager, page, platform)
            exc.observation = _failure_observation(page, platform, number, True, diagnostic)
            exc.safe_to_retry = False
            raise
        except (Exception, asyncio.CancelledError) as exc:
            diagnostic = await _diagnostic(manager, page, platform)
            error = BrowserAttention("当前表单只读检查未完成，未导航、点击或填写；保留原有结果不明记录。")
            error.observation = _failure_observation(page, platform, number, True, diagnostic)
            error.safe_to_retry = False
            raise error from exc
