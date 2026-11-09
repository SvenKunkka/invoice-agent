"""Entirely fictional offline form pages; no live accounts or company data."""
import asyncio

import pytest
from bs4 import BeautifulSoup, Tag

from invoice_agent.browser import BrowserAttention, BrowserManager
from invoice_agent.form_inspection import (
    InvoiceFormPageReference,
    inspect_invoice_form,
    read_current_invoice_form,
)
from invoice_agent.parsers import ORDER_RE, official_url


NUMBER = "123456789012"
URL = f"https://trade.taobao.com/trade/detail/trade_order_detail.htm?bizOrderId={NUMBER}"
FORM_URL = f"https://trade.taobao.com/invoice/form?bizOrderId={NUMBER}"
DETAIL = f"""<html><body><h1>订单详情</h1><p>订单号：{NUMBER}</p>
  <p>交易成功 实付款：￥88.00</p><p>未申请发票</p>
  <div class="trade-button" data-action="entry">申请开票</div>
  <button data-action="pay">立即付款</button><button data-action="message">发送给商家</button>
</body></html>"""
FORM = f"""<html><body><h1>申请开票</h1><p>订单号：{NUMBER}</p>
  <p>交易成功 实付款：￥88.00</p>
  <form><label><input type="radio" name="invoiceType">增值税专用发票</label>
  <label><input type="radio" name="invoiceType" checked>电子普通发票</label>
  <label for="title">发票抬头</label><input id="title" name="invoiceTitle" value="已有公司值绝不可返回" required>
  <label for="tax">纳税人识别号</label><input id="tax" name="taxId" value="旧税号绝不可返回" aria-required="true">
  <label for="bank">开户银行</label><input id="bank" name="bankName" value="旧银行值绝不可返回">
  <input hidden name="csrf" value="秘密值绝不可返回"><input type="password" name="password" value="密码绝不可返回">
  <textarea name="misc">私人备注绝不可返回</textarea>
  <button data-action="submit">提交申请</button></form>
  <button data-action="download">下载附件</button><button data-action="message">发送给商家</button>
</body></html>"""
PROFILE = {"company_name": "虚构待填公司绝不可返回", "tax_id": "TEST00000000000000",
           "bank_account": "虚构银行账号绝不可传输"}


def visible(node):
    return not any(isinstance(item, Tag) and (
        item.has_attr("hidden") or item.get("aria-hidden") == "true"
        or "display:none" in item.get("style", "").replace(" ", "")) for item in [node, *node.parents])


def text(node):
    clone = BeautifulSoup(str(node), "html.parser")
    for child in clone.select("[hidden],[aria-hidden='true'],input,textarea,select"):
        child.decompose()
    return clone.get_text(" ", strip=True)


def labels(node):
    values = []
    parent = node.find_parent("label")
    if parent:
        values.append(text(parent))
    if node.get("id"):
        root = node
        while root.parent is not None:
            root = root.parent
        values.extend(text(item) for item in root.find_all("label", attrs={"for": node["id"]}))
    return values


class Locator:
    def __init__(self, page, nodes):
        self.page, self.nodes = page, list(nodes)

    def locator(self, selector):
        return Locator(self.page, [child for node in self.nodes for child in node.select(selector)])

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return Locator(self.page, [self.nodes[index]])

    async def inner_text(self):
        return text(self.nodes[0])

    async def inner_html(self):
        return self.nodes[0].decode_contents()

    async def get_attribute(self, name):
        value = self.nodes[0].get(name)
        return " ".join(value) if isinstance(value, list) else value

    async def is_visible(self):
        return visible(self.nodes[0])

    async def is_enabled(self):
        return not any(isinstance(item, Tag) and (item.has_attr("disabled") or item.get("aria-disabled") == "true")
                       for item in [self.nodes[0], *self.nodes[0].parents])

    async def evaluate(self, expression):
        node = self.nodes[0]
        if "form-choice-metadata" in expression:
            label = node.get("aria-label") or " ".join(labels(node)) or text(node)
            checked = node.has_attr("checked") if node.name == "input" else (
                True if node.get("aria-checked") == "true" else False if node.get("aria-checked") == "false" else None)
            return {"label": label, "checked": checked}
        if "form-select-metadata" in expression:
            return "|".join([node.get("name", ""), node.get("id", ""), node.get("aria-label", ""), *labels(node)])
        if "form-field-metadata" in expression:
            return {"name": node.get("name", ""), "id": node.get("id", ""), "labels": labels(node),
                    "placeholder": node.get("placeholder", ""), "ariaLabel": node.get("aria-label", ""),
                    "required": node.has_attr("required") or node.get("aria-required") == "true"}
        if "el.selected" in expression:
            return node.has_attr("selected")
        raise AssertionError("Unexpected DOM evaluation: " + expression)

    async def click(self):
        assert self.page.manager.lock.locked()
        node = self.nodes[0]
        self.page.manager.clicks.append(node.get("data-action"))
        assert node.get("data-action") == "entry", "Never submit, download, pay, message, or change a choice"

    async def fill(self, *_):
        raise AssertionError("Profile fields must never be filled")

    async def check(self, *_):
        raise AssertionError("Invoice choices must never be changed")

    async def select_option(self, *_):
        raise AssertionError("Invoice choices must never be changed")


class Page:
    def __init__(self, manager, url, html):
        self.manager, self.url, self.html = manager, url, html
        self.closed = False

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        assert self.manager.lock.locked()
        self.manager.navigations.append(url)
        self.url, self.html = url, self.manager.before

    def locator(self, selector):
        return Locator(self, BeautifulSoup(self.html, "html.parser").select(selector))

    async def wait_for_function(self, expression, *, arg, **kwargs):
        assert self.manager.lock.locked()
        if isinstance(arg, str):
            ids = ORDER_RE.findall(await self.locator("body").inner_text())
            if not ids or set(ids) != {arg}:
                raise TimeoutError("Fictional target label is absent or ambiguous")
        else:
            # The production helper bounds the wait and then independently reads
            # controls; this fake never performs network or hidden state changes.
            if not any(text(item) in arg["labels"] and visible(item)
                       for item in BeautifulSoup(self.html, "html.parser").select(arg["selector"])):
                raise TimeoutError("No final control on fictional page")


class Manager(BrowserManager):
    def __init__(self, root, *, before=DETAIL, after=FORM, after_url=FORM_URL, activation_error=False):
        super().__init__(root)
        self.before, self.after, self.after_url = before, after, after_url
        self.page = Page(self, URL, before)
        self.form_page = Page(self, after_url, after)
        self.activation_error = activation_error
        self.clicks, self.navigations, self.diagnostic_count = [], [], 0

    async def _page(self, platform):
        assert self.lock.locked()
        return self.page

    async def _check(self, page, platform):
        assert self.lock.locked()
        if not official_url(page.url, platform):
            raise BrowserAttention("页面已离开官方域名")
        return await page.locator("body").inner_text()

    async def _activate(self, page, entry):
        assert self.lock.locked()
        await entry.click()
        if self.activation_error:
            raise TimeoutError("An entry can have applied before a navigation timeout")
        return self.form_page

    async def _diagnostic(self, page, platform):
        assert self.lock.locked()
        self.diagnostic_count += 1
        path = self.data_dir / "fictional-form-structure.html"
        path.write_text("<p>Fictional private structure only</p>")
        return "脱敏 DOM 诊断：" + str(path)


def order(**patch):
    return {"platform": "taobao", "order_no": NUMBER, "company_purchase": True,
            "scope_excluded": False, "invoice_status": "unrequested", "trade_status": "completed",
            "amount_fen": 8800, "order_url": URL, **patch}


def test_one_entry_reads_choices_and_metadata_without_fill_or_submission(tmp_path):
    manager = Manager(tmp_path)
    result = asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert result["form_open"] and result["target_verified"]
    assert result["entry_activated"] and result["side_effect_unknown"]
    assert result["current_url"] == FORM_URL and result["amount_fen"] == result["current_amount_fen"] == 8800
    assert result["refund_status_before_entry"] == "unknown"
    assert result["type_options"] == [
        {"label": "增值税专用发票", "enabled": True, "checked": False, "invoice_type": "special"},
        {"label": "电子普通发票", "enabled": True, "checked": True, "invoice_type": "ordinary"},
    ]
    assert result["fields"] == [
        {"label": "公司名称", "name": "invoiceTitle", "required": True, "kind": "company_name"},
        {"label": "纳税人识别号", "name": "taxId", "required": True, "kind": "tax_id"},
        {"label": "开户银行", "name": "bankName", "required": False, "kind": "bank_name"},
    ]
    assert result["submit_controls"] == [{"label": "提交申请", "enabled": True}]
    assert result["diagnostic_path"] and manager.diagnostic_count == 1
    assert manager.navigations == [URL] and manager.clicks == ["entry"]
    for value in [*PROFILE.values(), "已有公司值绝不可返回", "旧税号绝不可返回", "旧银行值绝不可返回", "秘密值绝不可返回", "私人备注绝不可返回"]:
        assert value not in str(result)
    assert not manager.lock.locked()


@pytest.mark.parametrize("patch", [
    {"company_purchase": False}, {"scope_excluded": True}, {"invoice_status": "pending"},
    {"trade_status": "unpaid"}, {"amount_fen": 0}, {"amount_fen": 88.0},
    {"order_url": ""}, {"order_url": "https://taobao.com.invalid.example/detail"}, {"order_no": "invalid"},
])
def test_ineligible_local_orders_never_navigate(tmp_path, patch):
    manager = Manager(tmp_path)
    with pytest.raises(BrowserAttention):
        asyncio.run(inspect_invoice_form(manager, order(**patch), PROFILE))
    assert manager.navigations == manager.clicks == []


@pytest.mark.parametrize("change", [
    ("实付款：￥88.00", "实付款：￥99.00"),
    ("交易成功", "交易关闭"), ("交易成功", "等待买家付款"),
    ("交易成功", "交易成功 部分退款"), ("交易成功", "交易成功 已全额退款"),
    ("未申请发票", "已申请开票"), ("未申请发票", "已开票"),
])
def test_current_transaction_and_invoice_guards_precede_entry(tmp_path, change):
    manager = Manager(tmp_path, before=DETAIL.replace(*change))
    with pytest.raises(BrowserAttention) as error:
        asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert error.value.safe_to_retry is True
    assert manager.clicks == [] and manager.diagnostic_count == 1


def test_ambiguous_entry_is_not_clicked(tmp_path):
    manager = Manager(tmp_path, before=DETAIL.replace("</body>", '<button data-action="entry">申请发票</button></body>'))
    with pytest.raises(BrowserAttention, match="唯一"):
        asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert manager.clicks == []


def test_disabled_entry_is_not_clicked(tmp_path):
    manager = Manager(tmp_path, before=DETAIL.replace('class="trade-button"', 'class="trade-button" aria-disabled="true"'))
    with pytest.raises(BrowserAttention, match="唯一可用"):
        asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert manager.clicks == []


@pytest.mark.parametrize("after", [
    f"<html><body>订单号：{NUMBER}<p>申请已提交</p></body></html>",
    FORM.replace("申请开票", "申请成功", 1),
    DETAIL,
    FORM.replace('<button data-action="submit">提交申请</button>', ''),
    FORM.replace("实付款：￥88.00", "实付款：￥77.00"),
    FORM.replace("交易成功", "部分退款"),
])
def test_uncertain_success_or_nonform_never_becomes_verified_form(tmp_path, after):
    manager = Manager(tmp_path, after=after)
    result = asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert not result["form_open"] and result["entry_activated"] and result["side_effect_unknown"]
    assert result["missing_fields"] and manager.clicks == ["entry"]


@pytest.mark.parametrize("settings", [
    {"activation_error": True},
    {"after_url": "https://invalid.example/form"},
    {"after": FORM.replace(NUMBER, "223456789012")},
])
def test_after_entry_errors_are_not_safe_to_replay(tmp_path, settings):
    manager = Manager(tmp_path, **settings)
    with pytest.raises(BrowserAttention) as error:
        asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert error.value.safe_to_retry is False
    assert "禁止自动重放" in str(error.value)
    assert manager.clicks == ["entry"] and manager.diagnostic_count == 1


def test_absent_special_radio_is_not_evidence_of_no_special_support(tmp_path):
    html = FORM.replace('<label><input type="radio" name="invoiceType">增值税专用发票</label>', '')
    manager = Manager(tmp_path, after=html)
    result = asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert result["form_open"]
    assert [option["invoice_type"] for option in result["type_options"]] == ["ordinary"]
    assert "special_status" not in result and "special_supported" not in result
    assert manager.clicks == ["entry"]


def test_company_fields_without_radio_are_partial_form_observation(tmp_path):
    soup = BeautifulSoup(FORM, "html.parser")
    for radio in soup.select("input[type=radio]"):
        radio.parent.decompose()
    manager = Manager(tmp_path, after=str(soup))
    result = asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert result["form_open"] and result["type_options"] == []
    assert "invoice_type_options" in result["missing_fields"]
    assert manager.clicks == ["entry"]


def test_disabled_option_and_final_control_remain_readable_but_untouched(tmp_path):
    manager = Manager(tmp_path, after=FORM.replace('name="invoiceType">', 'name="invoiceType" disabled>', 1).replace('data-action="submit"', 'data-action="submit" disabled'))
    result = asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert result["type_options"][0]["enabled"] is False
    assert result["submit_controls"][0]["enabled"] is False
    assert result["form_open"] and manager.clicks == ["entry"]


def test_native_select_choices_are_only_read_not_opened_or_changed(tmp_path):
    html = FORM.replace('<label><input type="radio" name="invoiceType">增值税专用发票</label>', '').replace(
        '<label><input type="radio" name="invoiceType" checked>电子普通发票</label>',
        '<label for="types">发票类型</label><select id="types" name="invoiceType"><option>增值税专用发票</option><option selected>电子普通发票</option></select>')
    manager = Manager(tmp_path, after=html)
    result = asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert result["form_open"] and len(result["type_options"]) == 2
    assert result["type_options"][1]["checked"] is True
    assert manager.clicks == ["entry"]


def test_entry_identity_failure_carries_private_observation_and_keeps_exact_popup_reference(tmp_path):
    generic = FORM.replace(f"<p>订单号：{NUMBER}</p>", "").replace("提交申请", "保存")
    manager = Manager(tmp_path, after=generic)
    with pytest.raises(BrowserAttention) as caught:
        asyncio.run(inspect_invoice_form(manager, order(id="fictional-order"), PROFILE))
    observation = caught.value.observation
    assert observation["current_url"] == FORM_URL
    assert observation["entry_activated"] is True and observation["target_verified"] is False
    assert observation["side_effect_unknown"] is True and observation["diagnostic_path"]
    assert observation["url_identity"]["parameters"] == [{"name": "bizOrderId", "exact_order_match": True}]
    assert FORM_URL not in str(caught.value)
    reference = manager._invoice_form_pages["fictional-order"]
    assert isinstance(reference, InvoiceFormPageReference) and reference.page is manager.form_page
    # The platform page remains the original order detail. The reader must use
    # the cached activation result (a popup), not that unrelated original page.
    manager._pages["taobao"] = manager.page
    result = asyncio.run(read_current_invoice_form(manager, order(id="fictional-order"), PROFILE))
    assert result["current_url"] == FORM_URL and result["cached_from_entry"]
    assert result["target_verified"] is False and result["form_open"] is False
    assert result["submit_controls"] == []  # "保存" is not treated as submitting an order.
    assert result["url_identity"]["has_exact_order_parameter"] is True
    assert len(result["type_options"]) == 2 and len(result["fields"]) == 3
    assert manager.navigations == [URL] and manager.clicks == ["entry"]
    for value in PROFILE.values():
        assert value not in str(result)


def test_current_reader_does_not_search_or_navigate_if_typed_cache_is_absent(tmp_path):
    manager = Manager(tmp_path)
    manager._pages["taobao"] = manager.form_page
    with pytest.raises(BrowserAttention, match="没有此订单已记录"):
        asyncio.run(read_current_invoice_form(manager, order(), PROFILE))
    assert manager.navigations == manager.clicks == [] and manager.diagnostic_count == 0


@pytest.mark.parametrize("reference", [None, "page", object()])
def test_current_reader_rejects_untyped_cache_values(tmp_path, reference):
    manager = Manager(tmp_path)
    manager._invoice_form_pages = {"taobao-" + NUMBER: reference}
    with pytest.raises(BrowserAttention, match="没有此订单已记录"):
        asyncio.run(read_current_invoice_form(manager, order(), PROFILE))
    assert manager.navigations == manager.clicks == []


def test_current_reader_does_not_accept_wrong_order_reference(tmp_path):
    manager = Manager(tmp_path)
    manager._invoice_form_pages = {"taobao-" + NUMBER: InvoiceFormPageReference(manager.form_page, "taobao", "223456789012")}
    with pytest.raises(BrowserAttention, match="没有此订单已记录"):
        asyncio.run(read_current_invoice_form(manager, order(), PROFILE))
    assert manager.navigations == manager.clicks == []


def test_url_parameter_match_never_replaces_visible_target_verification(tmp_path):
    manager = Manager(tmp_path, after=FORM.replace(f"<p>订单号：{NUMBER}</p>", ""))
    manager._invoice_form_pages = {"taobao-" + NUMBER: InvoiceFormPageReference(manager.form_page, "taobao", NUMBER)}
    result = asyncio.run(read_current_invoice_form(manager, order(), PROFILE))
    assert result["url_identity"]["has_exact_order_parameter"]
    assert not result["target_verified"] and not result["form_open"]
    assert "visible_exact_order_number" in result["missing_fields"]
    assert manager.navigations == manager.clicks == []


def test_global_profile_url_has_no_exact_order_identity(tmp_path):
    manager = Manager(tmp_path, after_url="https://invoice.taobao.com/profile/settings?source=orders")
    manager._invoice_form_pages = {"taobao-" + NUMBER: InvoiceFormPageReference(manager.form_page, "taobao", NUMBER)}
    result = asyncio.run(read_current_invoice_form(manager, order(), PROFILE))
    assert result["url_identity"]["has_exact_order_parameter"] is False
    assert result["url_identity"]["parameters"] == [{"name": "source", "exact_order_match": False}]
    assert manager.navigations == manager.clicks == []


def test_current_reader_rejects_closed_page_without_substitution(tmp_path):
    manager = Manager(tmp_path)
    manager.form_page.closed = True
    manager._invoice_form_pages = {"taobao-" + NUMBER: InvoiceFormPageReference(manager.form_page, "taobao", NUMBER)}
    with pytest.raises(BrowserAttention, match="已经关闭") as caught:
        asyncio.run(read_current_invoice_form(manager, order(), PROFILE))
    assert caught.value.safe_to_retry is False and caught.value.observation["entry_activated"]
    assert manager.navigations == manager.clicks == []


def test_nonofficial_redirect_url_is_not_exposed_in_failure_observation(tmp_path):
    manager = Manager(tmp_path, after_url="https://invalid.example/form?secret=not-returned")
    with pytest.raises(BrowserAttention) as caught:
        asyncio.run(inspect_invoice_form(manager, order(), PROFILE))
    assert caught.value.observation["current_url"] == ""
    assert caught.value.observation["url_identity"]["official"] is False
    assert "invalid.example" not in str(caught.value.observation)


def test_real_chrome_offline_fictional_form_has_no_fill_or_final_click(tmp_path):
    """Exercise actual Playwright metadata/JS against locally fulfilled routes.

    Every request is intercepted; no Taobao/JD request reaches the network and
    no saved browser profile or shopping account is opened.
    """
    from playwright.async_api import async_playwright

    async def exercise():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            requests = []
            before = DETAIL.replace('data-action="entry"', f'data-action="entry" onclick="location.href=\'{FORM_URL}\'"')
            after = FORM.replace('type="password"', 'type="password" hidden').replace(
                'data-action="submit"', 'data-action="submit" onclick="window.finalClicked=true"')

            async def route(request_route):
                url = request_route.request.url
                requests.append(url)
                if url == URL:
                    await request_route.fulfill(status=200, content_type="text/html; charset=utf-8", body=before)
                elif url == FORM_URL:
                    await request_route.fulfill(status=200, content_type="text/html; charset=utf-8", body=after)
                else:
                    await request_route.abort()

            await context.route("**/*", route)

            class OfflineManager(BrowserManager):
                async def _page(self, platform):
                    assert self.lock.locked()
                    return page

            manager = OfflineManager(tmp_path)
            manager._context = context
            try:
                result = await inspect_invoice_form(manager, order(), PROFILE)
                assert result["form_open"] and result["target_verified"]
                assert len(result["type_options"]) == 2
                assert result["type_options"][0]["checked"] is False
                assert result["type_options"][1]["checked"] is True
                assert {field["kind"] for field in result["fields"]} == {"company_name", "tax_id", "bank_name"}
                assert await page.locator("#title").input_value() == "已有公司值绝不可返回"
                assert await page.locator("#tax").input_value() == "旧税号绝不可返回"
                assert not await page.evaluate("Boolean(window.finalClicked)")
                assert requests == [URL, FORM_URL]
                for value in PROFILE.values():
                    assert value not in str(result)
                assert result["diagnostic_path"]
            finally:
                await context.close()
                await browser.close()

    asyncio.run(exercise())
