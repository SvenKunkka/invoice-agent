from pathlib import Path
import asyncio
import zipfile

import pytest
from bs4 import BeautifulSoup

from invoice_agent.browser import (BrowserAttention, BrowserManager, INVOICE_MAX_BYTES,
    invoice_response_extension, observed_jd_invoice_url, save_invoice_bytes, validate_invoice_file)
from invoice_agent.parsers import (buyer_observation, invoice_detail_dom_observation, invoice_observation, money_fen,
                                   official_url, parse_orders, sanitize_diagnostic_html,
                                   transaction_dom_observation, transaction_observation)


FIXTURES = Path(__file__).parent / "fixtures"


def _approved_order():
    return {"platform": "jd", "order_no": "123456789012", "trade_status": "completed", "amount_fen": 12345}


def test_taobao_visible_order_details_are_conservative():
    result = parse_orders((FIXTURES / "taobao-orders.html").read_text(), "taobao")
    assert result.recognized and not result.empty and not result.rejected
    assert len(result.orders) == 2
    first, second = result.orders
    assert first["order_no"] == "1234567890123456789"
    assert first["amount_fen"] == 12345
    assert first["paid_at"] == "2026-11-01"
    assert first["special_status"] == "yes"
    assert first["invoice_type"] == "unknown"
    assert first["company_purchase"] is False
    assert first["source"] == "browser"
    assert first["invoice_status"] == "unrequested"
    assert first["order_url"].endswith("bizOrderId=1234567890123456789")
    assert second["amount_fen"] == 125000
    assert second["trade_status"] == "partial_refund"
    assert second["special_status"] == "no" and second["ordinary_supported"] is True


def test_jd_invoice_exists_does_not_mean_downloaded_by_tool():
    result = parse_orders((FIXTURES / "jd-orders.html").read_text(), "jd")
    assert result.recognized and not result.rejected
    assert len(result.orders) == 2
    first, second = result.orders
    assert first["amount_fen"] == 234560
    assert first["invoice_status"] == "issued"
    assert first["invoice_type"] == "special"
    assert first["special_status"] == "unknown"
    assert first["invoice_url"].startswith("https://myivc.jd.com/")
    assert second["trade_status"] == "unpaid"


@pytest.mark.parametrize("html", [
    "<html><body>登录后查看订单</body></html>",
    "<html><body>页面正在加载，请稍后</body></html>",
    "<html><body>系统繁忙</body></html>",
])
def test_unknown_page_never_becomes_empty_success(html):
    result = parse_orders(html, "jd")
    assert not result.recognized and not result.empty and not result.orders


def test_explicit_empty_list_is_recognized():
    result = parse_orders("<h1>我的订单</h1><p>暂无订单</p>", "jd")
    assert result.recognized and result.empty and not result.orders


def test_ambiguous_amounts_and_dates_are_rejected():
    html = (FIXTURES / "jd-orders.html").read_text()
    html = html.replace("￥2,345.60", "￥2,345.60</span><strong>￥99.00</strong><span>")
    result = parse_orders(html, "jd")
    assert len(result.orders) == 1 and result.rejected
    assert "唯一金额" in result.rejected[0]
    html = (FIXTURES / "jd-orders.html").read_text().replace("2026-11-03", "2026-02-30")
    result = parse_orders(html, "jd")
    assert len(result.orders) == 1 and "日期" in result.rejected[0]


def test_conflicting_order_numbers_are_rejected():
    html = (FIXTURES / "taobao-orders.html").read_text().replace(
        "订单号：1234567890123456789", "订单号：1111111111111111111")
    result = parse_orders(html, "taobao")
    assert len(result.orders) == 1 and "订单号" in result.rejected[0]


def test_unlabelled_item_prices_are_not_treated_as_paid_amount():
    html = (FIXTURES / "taobao-orders.html").read_text().replace('class="realPay--m"', 'class="product-price"')
    result = parse_orders(html, "taobao")
    assert not result.orders and len(result.rejected) == 2


def test_support_is_not_inferred_from_generic_invoice_entry():
    info = invoice_observation("交易成功 申请开票 电子发票")
    assert info == {"invoice_status": "unrequested", "invoice_type": "unknown",
                    "special_status": "unknown", "ordinary_supported": False}
    assert invoice_observation("不支持增值税专用发票 电子普通发票")["special_status"] == "no"
    assert invoice_observation("已申请开票 申请发票")["invoice_status"] == "pending"
    assert invoice_observation("发票已开具 已下载")["invoice_status"] == "issued"


@pytest.mark.parametrize("text,status", [
    ("交易成功 查看发票详情", "unknown"),
    ("交易成功 查看发票 详情", "unknown"),
    ("已开票 查看发票详情", "issued"),
    ("开票中 查看发票详情", "pending"),
    ("已申请 查看发票详情", "pending"),
    ("查看发票", "issued"),
])
def test_generic_invoice_detail_navigation_is_not_evidence_of_issuance(text, status):
    assert invoice_observation(text)["invoice_status"] == status


@pytest.mark.parametrize("url,platform", [
    ("https://jd.com.attacker.example/", "jd"),
    ("https://attacker.example/?next=https://jd.com", "jd"),
    ("http://order.jd.com/", "jd"),
    ("https://user:password@order.jd.com/", "jd"),
    ("https://order.jd.com:444/", "jd"),
    ("https://order.jd.com/", "taobao"),
])
def test_nonplatform_urls_are_rejected(url, platform):
    assert not official_url(url, platform)


def test_exact_money_and_official_https():
    assert money_fen("￥1,234.56") == 123456
    assert money_fen("0.1") == 10
    assert money_fen("3.456") is None
    assert money_fen("￥5.00 运费￥3.00") is None
    assert official_url("https://order.jd.com/center/list.action", "jd")
    assert official_url("https://trade.tmall.com/order", "taobao")


def test_download_magic_rejects_html_and_wrong_format(tmp_path):
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.7\nexample invoice bytes")
    validate_invoice_file(pdf)
    pdf.write_bytes(b"<html><body>login page instead of invoice</body></html>")
    with pytest.raises(BrowserAttention):
        validate_invoice_file(pdf)
    xml = tmp_path / "invoice.xml"
    xml.write_text("<?xml version='1.0'?><Invoice><Number>123456</Number></Invoice>")
    validate_invoice_file(xml)
    xml.write_text("<!DOCTYPE Invoice [<!ENTITY x 'value'>]><Invoice>&x;</Invoice>")
    with pytest.raises(BrowserAttention):
        validate_invoice_file(xml)


def test_ofd_requires_ofd_directory(tmp_path):
    path = tmp_path / "invoice.ofd"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("OFD.xml", "<?xml version='1.0'?><OFD><DocBody/></OFD>")
    validate_invoice_file(path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("some-other-file.txt", "not an invoice")
    with pytest.raises(BrowserAttention):
        validate_invoice_file(path)


class _FakeField:
    def __init__(self):
        self.value = ""

    async def get_attribute(self, name):
        return None

    async def fill(self, value):
        self.value = value

    async def input_value(self):
        return self.value


class _FakeScope:
    def __init__(self, text):
        self.text = text

    async def inner_text(self):
        return self.text

    async def inner_html(self):
        from html import escape
        return "<div>" + escape(self.text) + "</div>"


class _FakePage:
    async def wait_for_timeout(self, delay):
        pass


class _FakeRadio:
    async def is_checked(self):
        return True


class _FakeSubmit:
    def __init__(self, fail=False):
        self.count = 0
        self.fail = fail

    async def click(self):
        self.count += 1
        if self.fail:
            raise TimeoutError("Timeout after a possibly successful click")


class _ApplyHarness(BrowserManager):
    """Deterministic DOM-independent checks of the irreversible boundary."""
    def __init__(self, path, *, status="unrequested", fail=False, success=False,
                 transaction="交易成功 实付款：￥123.45 无退款", final_transaction=None, entry_fail=False):
        super().__init__(path)
        self.scope = _FakeScope("订单号：123456789012 " + transaction + " " + ("查看发票" if status == "issued" else "申请开票"))
        self.page = _FakePage()
        self.submit = _FakeSubmit(fail)
        self.success = success
        self.fields = {}
        self.final_transaction = final_transaction
        self.entry_count = 0
        self.entry_fail = entry_fail

    async def _navigate(self, order, invoice=False):
        return self.page, self.scope

    async def _activate(self, page, locator):
        self.entry_count += 1
        if self.entry_fail:
            raise TimeoutError("Entry may already have created an application")
        if self.final_transaction is not None:
            self.scope.text = "订单号：123456789012 申请开票 " + self.final_transaction
        return self.page

    async def _check(self, page, platform):
        if self.submit.count:
            if self.success:
                return "订单号：123456789012 申请已提交"
            raise BrowserAttention("提交后出现安全验证")
        return self.scope.text

    async def _scope(self, page, order):
        return self.scope

    async def _choose_type(self, scope, invoice_type):
        return _FakeRadio()

    async def _field(self, scope, names, required=True):
        if not required:
            return None
        return self.fields.setdefault(names, _FakeField())

    async def _named(self, scope, pattern, roles=("button", "link")):
        return [self.submit] if roles == ("button",) else [object()]


def test_after_submit_timeout_is_uncertain_and_never_replayed(tmp_path):
    browser = _ApplyHarness(tmp_path, fail=True)
    result = asyncio.run(browser.apply(_approved_order(),
                                       {"company_name": "测试企业", "tax_id": "TEST123"}, "special"))
    assert result["status"] == "uncertain"
    assert browser.submit.count == 1


def test_after_submit_attention_is_also_uncertain(tmp_path):
    browser = _ApplyHarness(tmp_path)
    result = asyncio.run(browser.apply(_approved_order(),
                                       {"company_name": "测试企业", "tax_id": "TEST123"}, "special"))
    assert result["status"] == "uncertain"
    assert browser.submit.count == 1


def test_existing_invoice_stops_before_click(tmp_path):
    browser = _ApplyHarness(tmp_path, status="issued")
    with pytest.raises(BrowserAttention) as error:
        asyncio.run(browser.apply(_approved_order(),
                                  {"company_name": "测试企业", "tax_id": "TEST123"}, "special"))
    assert error.value.safe_to_retry is True
    assert browser.submit.count == 0


def test_submitted_requires_order_number_in_positive_result(tmp_path):
    browser = _ApplyHarness(tmp_path, success=True)
    result = asyncio.run(browser.apply(_approved_order(),
                                       {"company_name": "测试企业", "tax_id": "TEST123"}, "special"))
    assert result["status"] == "submitted"
    assert browser.submit.count == 1


def test_labelled_transaction_and_buyer_from_fixed_html():
    text = BeautifulSoup((FIXTURES / "transaction-invoice.html").read_text(), "html.parser").get_text(" ", strip=True)
    assert transaction_observation(text) == {"trade_status": "completed", "refund_status": "none", "amount_fen": 12345}
    assert buyer_observation(text) == {"invoice_buyer": "测试采购企业", "invoice_tax_id": "91310000123456789A"}
    assert buyer_observation("销售方信息 名称：测试销售企业 纳税人识别号：91440000987654321B") == {"invoice_buyer": "", "invoice_tax_id": ""}


@pytest.mark.parametrize("text", [
    "交易成功 实付款：￥123.45 部分退款",
    "交易成功 实付款：￥123.45 退款成功",
    "待付款 实付款：￥123.45 无退款",
    "交易关闭 实付款：￥123.45 无退款",
    "交易成功 实付款：￥100.00 无退款",
    "交易成功 无退款",
    "实付款：￥123.45 无退款",
    "交易成功 实付款：￥123.45",
    "交易成功 实付款：￥123.45 退款金额：￥1.00",
    "交易成功 实付款：￥123.45 订单金额：￥100.00 无退款",
    "已付款 交易成功 实付款：￥123.45 无退款",
])
@pytest.mark.parametrize("when", ["initial", "final"])
def test_transaction_changes_stop_before_irreversible_click(tmp_path, text, when):
    kwargs = {"transaction" if when == "initial" else "final_transaction": text}
    browser = _ApplyHarness(tmp_path, **kwargs)
    with pytest.raises(BrowserAttention) as error:
        asyncio.run(browser.apply(_approved_order(), {"company_name": "测试企业", "tax_id": "TEST123"}, "special"))
    assert error.value.safe_to_retry is (when == "initial")
    assert browser.submit.count == 0


def test_zero_refund_is_explicit_and_nonzero_refund_is_not_safe():
    assert transaction_observation("交易成功 实付款：￥123.45 退款金额：￥0.00")["refund_status"] == "none"
    assert transaction_observation("交易成功 实付款：￥123.45 退款金额：￥1.00")["refund_status"] == "partial_or_pending"
    assert transaction_observation("交易成功 商品价格：￥123.45 无退款")["amount_fen"] is None
    assert transaction_observation("交易成功 实付款：￥123.456 无退款")["amount_fen"] is None
    assert transaction_observation("交易成功 实付款：￥123.45 订单金额：￥123.456 无退款")["amount_fen"] is None
    assert transaction_observation("交易成功 实付款：￥123.45 无退款 退款金额：￥1.000")["refund_status"] == "unknown"


def test_shipping_date_is_not_used_as_order_date():
    html = (FIXTURES / "jd-orders.html").read_text().replace('class="dealtime"', 'class="shipping-deadline"')
    result = parse_orders(html, "jd")
    assert not result.orders
    assert all("日期" in message for message in result.rejected)


def test_current_buyer_must_match_confirmed_order_identity(tmp_path):
    browser = BrowserManager(tmp_path)

    class HtmlScope:
        async def inner_html(self):
            return (FIXTURES / "jd-invoice-detail-fictional.html").read_text()

        async def inner_text(self):
            raise AssertionError("Buyer verification must use the bound invoice table, not whole-body text")

    scope = HtmlScope()
    expected = {"platform": "jd", "invoice_buyer": "示例采购企业", "invoice_tax_id": "91310000123456789A"}
    asyncio.run(browser._verify_buyer(scope, expected))
    for wrong in ({"invoice_buyer": "测试销售企业", "invoice_tax_id": "91440000987654321B"},
                  {"invoice_buyer": "示例采购企业", "invoice_tax_id": "913100001234567890"},
                  {"invoice_buyer": "", "invoice_tax_id": ""}):
        with pytest.raises(BrowserAttention):
            asyncio.run(browser._verify_buyer(scope, expected | wrong))
    with pytest.raises(BrowserAttention):
        asyncio.run(browser._verify_buyer(_FakeScope("名称：测试采购企业 纳税人识别号：91310000123456789A"), expected))


@pytest.mark.parametrize("url,host", [
    ("https://user:privateCredential@invoice-cdn.example/private-document.pdf?token=privateQuery#privateFragment", "invoice-cdn.example"),
    ("https://invoice-cdn.example:8443/another-secret?auth=privateQuery", "invoice-cdn.example"),
    ("blob:https://invoice-cdn.example/privateBlob", "无主机名"),
])
def test_platform_domain_error_exposes_hostname_only_and_still_blocks(tmp_path, url, host):
    class External:
        def __init__(self):
            self.url = url

    manager = BrowserManager(tmp_path)
    with pytest.raises(BrowserAttention) as error:
        asyncio.run(manager._check(External(), "jd"))
    message = str(error.value)
    assert host in message and "hostname" in message
    for forbidden in ("privateCredential", "privateQuery", "privateFragment", "private-document", "another-secret", "privateBlob", "https://", "8443"):
        assert forbidden not in message
    assert not official_url(url, "jd")


@pytest.mark.parametrize("matching_buyer", [True, False])
def test_download_verifies_bound_buyer_before_viewer_navigation_and_blocks_unknown_host(tmp_path, matching_buyer):
    api = pytest.importorskip("playwright.async_api")

    class ExternalPage:
        url = "https://invoice-cdn.example/private-document?token=privateQuery"

    class ViewerBrowser(BrowserManager):
        async def _navigate(self, order, invoice=False):
            assert invoice is True
            return self.fixture_page, self.fixture_page.locator("body")

        async def _activate(self, page, locator):
            await locator.click()
            return ExternalPage()

        async def _check(self, page, platform):
            if page is self.fixture_page:
                return await page.locator("body").inner_text()
            return await super()._check(page, platform)

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                await page.set_content((FIXTURES / "jd-invoice-detail-fictional.html").read_text())
                await page.locator("#invoiceInformation").evaluate("el => el.insertAdjacentHTML('beforeend', '<tr><td colspan=2><button id=viewInvoice onclick=\"window.viewClicks=(window.viewClicks||0)+1\">查看发票</button></td></tr>')")
                manager = ViewerBrowser(tmp_path)
                manager.fixture_page = page
                manager._context = page.context
                order = {"platform": "jd", "order_no": "202605080061", "invoice_tax_id": "91310000123456789A",
                         "invoice_buyer": "示例采购企业" if matching_buyer else "其他企业"}
                destination = tmp_path / "invoices"
                with pytest.raises(BrowserAttention) as error:
                    await manager.download(order, destination)
                assert await page.evaluate("window.viewClicks || 0") == (1 if matching_buyer else 0)
                if matching_buyer:
                    assert "invoice-cdn.example" in str(error.value) and "privateQuery" not in str(error.value)
                else:
                    assert "购买方" in str(error.value)
                assert not destination.exists()
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_real_offline_browser_dom_scope_fields_and_special_priority(tmp_path):
    api = pytest.importorskip("playwright.async_api")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                await page.set_content((FIXTURES / "invoice-form.html").read_text())
                manager = BrowserManager(tmp_path)
                scope = await manager._scope(page, _approved_order())
                await manager._verify_transaction(scope, _approved_order())
                with pytest.raises(BrowserAttention, match="可用专票"):
                    await manager._no_available_special(scope)
                special = await manager._choose_type(scope, "special")
                assert await special.is_checked()
                company = await manager._field(scope, r"发票抬头|invoice[_-]?title")
                tax = await manager._field(scope, r"纳税人识别号|tax[_-]?id")
                await company.fill("离线测试企业")
                await tax.fill("91310000123456789A")
                assert await company.input_value() == "离线测试企业"
                assert await tax.input_value() == "91310000123456789A"
                # A disabled special option permits ordinary consideration; a
                # later qualification refresh re-enabling it must block again.
                await page.locator("input[value='special']").evaluate("el => el.disabled = true")
                await manager._no_available_special(scope)
                ordinary = await manager._choose_type(scope, "ordinary")
                assert await ordinary.is_checked()
                await page.locator("input[value='special']").evaluate("el => el.disabled = false")
                with pytest.raises(BrowserAttention, match="可用专票"):
                    await manager._no_available_special(scope)
                # Genuine DOM ambiguity must stop rather than fill the first.
                await page.locator("form").evaluate("el => el.insertAdjacentHTML('beforeend', '<label>发票抬头<input name=invoiceTitleDuplicate></label>')")
                with pytest.raises(BrowserAttention):
                    await manager._field(scope, r"发票抬头|invoice[_-]?title")
                await page.set_content("<p>订单号：999999999999</p>")
                with pytest.raises(BrowserAttention):
                    await manager._scope(page, _approved_order())
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_real_offline_apply_guards_run_at_both_checkpoints(tmp_path):
    api = pytest.importorskip("playwright.async_api")

    class OfflineBrowser(BrowserManager):
        """Use only synthetic about:blank DOM, retaining actual apply helpers."""
        async def _navigate(self, order, invoice=False):
            return self.fixture_page, await self._scope(self.fixture_page, order)

        async def _check(self, page, platform):
            assert page.url == "about:blank"
            return await page.locator("body").inner_text()

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()
            try:
                cases = [
                    ("special_initially_available", "ordinary", "", False),
                    ("special_becomes_available", "ordinary", "document.querySelector('input[value=special]').disabled=false", False),
                    ("amount_changes", "special", "document.querySelector('#paid').textContent='实付款：￥1.00'", False),
                    ("stable_special", "special", "", True),
                ]
                for name, typ, on_input, submitted in cases:
                    page = await context.new_page()
                    await page.set_content((FIXTURES / "invoice-form.html").read_text())
                    if name == "special_becomes_available":
                        await page.locator("input[value=special]").evaluate("el => el.disabled=true")
                    if on_input:
                        await page.locator("#taxId").evaluate("(el, source) => el.addEventListener('input', () => Function(source)())", on_input)
                    manager = OfflineBrowser(tmp_path)
                    manager.fixture_page = page
                    manager._context = context
                    profile = {"company_name": "离线测试企业", "tax_id": "91310000123456789A", "bank_name": "测试开户银行"}
                    if submitted:
                        result = await manager.apply(_approved_order(), profile, typ)
                        assert result["status"] == "submitted"
                        assert await page.evaluate("window.submitted") == 1
                    else:
                        with pytest.raises(BrowserAttention) as error:
                            await manager.apply(_approved_order(), profile, typ)
                        assert error.value.safe_to_retry is False
                        assert await page.evaluate("window.submitted || 0") == 0
                    await page.close()
            finally:
                await context.close()
                await browser.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("state", ["开票中", "已开票"])
def test_application_entry_can_itself_create_request_and_is_not_replayable(tmp_path, state):
    browser = _ApplyHarness(tmp_path, final_transaction="交易成功 实付款：￥123.45 无退款 " + state)
    result = asyncio.run(browser.apply(_approved_order(), {"company_name": "测试企业", "tax_id": "TEST123"}, "special"))
    assert result["status"] == "uncertain"
    assert browser.entry_count == 1 and browser.submit.count == 0


def test_application_entry_timeout_is_uncertain_and_not_replayed(tmp_path):
    browser = _ApplyHarness(tmp_path, entry_fail=True)
    result = asyncio.run(browser.apply(_approved_order(), {"company_name": "测试企业", "tax_id": "TEST123"}, "special"))
    assert result["status"] == "uncertain"
    assert browser.entry_count == 1 and browser.submit.count == 0


def test_unknown_form_after_entry_attention_is_not_safe_to_retry(tmp_path):
    class UnknownForm(_ApplyHarness):
        async def _choose_type(self, scope, invoice_type):
            raise BrowserAttention("无法确认申请表单")

    browser = UnknownForm(tmp_path)
    with pytest.raises(BrowserAttention) as error:
        asyncio.run(browser.apply(_approved_order(), {"company_name": "测试企业", "tax_id": "TEST123"}, "special"))
    assert error.value.safe_to_retry is False
    assert browser.entry_count == 1 and browser.submit.count == 0


def test_check_only_observes_existing_page_without_navigation(tmp_path):
    class ExistingPage:
        url = "https://order.jd.com/center/list.action"

        def __init__(self, html):
            self.html = html

        def is_closed(self):
            return False

        async def content(self):
            return self.html

    class ReadOnlyCheck(BrowserManager):
        error = None

        async def _page(self, platform):
            raise AssertionError("check must not create a page")

        async def _ensure(self):
            raise AssertionError("check must not launch a browser")

        async def _check(self, page, platform):
            if self.error:
                raise self.error
            return BeautifulSoup(page.html, "html.parser").get_text(" ", strip=True)

    async def exercise():
        manager = ReadOnlyCheck(tmp_path)
        assert (await manager.check("jd"))["status"] == "disconnected"
        page = ExistingPage((FIXTURES / "jd-orders.html").read_text())
        manager._pages["jd"] = page
        assert (await manager.check("jd"))["status"] == "ready"
        page.html = "<p>暂无订单</p>"
        assert (await manager.check("jd"))["status"] == "ready"
        page.html = "<table class='order-tb'><tbody><tr><td>订单正在加载</td></tr></tbody></table>"
        assert (await manager.check("jd"))["status"] == "open"
        page.html = "<p>扫码登录</p><input value='must-not-be-returned'>"
        observed = await manager.check("jd")
        assert observed["status"] == "needs_login" and "must-not-be-returned" not in str(observed)
        page.html = "<p>页面加载中</p>"
        assert (await manager.check("jd"))["status"] == "open"
        manager.error = BrowserAttention("请本人登录")
        assert (await manager.check("jd"))["status"] == "needs_login"
        manager.error = BrowserAttention("请人工安全验证")
        assert (await manager.check("jd"))["status"] == "blocked"
        assert manager._pages["jd"] is page

    asyncio.run(exercise())


def test_real_offline_entry_direct_submission_and_timeout_are_not_replayed(tmp_path):
    api = pytest.importorskip("playwright.async_api")

    class OfflineEntry(BrowserManager):
        async def _navigate(self, order, invoice=False):
            return self.fixture_page, await self._scope(self.fixture_page, order)

        async def _check(self, page, platform):
            assert page.url == "about:blank"
            return await page.locator("body").inner_text()

        async def _activate(self, page, locator):
            activated = await super()._activate(page, locator)
            if self.entry_timeout:
                raise TimeoutError("Navigation timeout after the actual entry click")
            return activated

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()
            try:
                for state, timeout in (("开票中", False), ("已开票", False), ("开票中", True)):
                    page = await context.new_page()
                    await page.set_content((FIXTURES / "invoice-form.html").read_text())
                    await page.locator("#openForm").evaluate("(el, state) => el.addEventListener('click', () => { window.entryClicks=(window.entryClicks||0)+1;document.querySelector('h1').textContent=state; })", state)
                    manager = OfflineEntry(tmp_path)
                    manager.fixture_page = page
                    manager._context = context
                    manager.entry_timeout = timeout
                    result = await manager.apply(_approved_order(), {"company_name": "离线测试企业", "tax_id": "91310000123456789A"}, "special")
                    assert result["status"] == "uncertain"
                    assert await page.evaluate("window.entryClicks") == 1
                    assert await page.evaluate("window.submitted || 0") == 0
                    await page.close()
            finally:
                await context.close()
                await browser.close()

    asyncio.run(exercise())


def test_diagnostic_removes_executable_hidden_values_and_sensitive_urls():
    raw = """<!doctype html><html><head><script>rawScriptCredential</script>
    <base href='https://evil.example/'><meta http-equiv='refresh' content='0; url=https://evil.example/'></head>
    <body onload='rawEventCredential'><style>.hidden {display:none}</style>
    <iframe src='https://evil.example/frame'>rawFrameCredential</iframe>
    <object data='https://evil.example/object'>rawObjectCredential</object><embed src='https://evil.example/embed'>
    <input name='company_name' placeholder='企业名称' value='rawInputCredential' data-key='rawDataCredential'>
    <input type='hidden' value='rawHiddenCredential'><textarea>rawTextareaCredential</textarea>
    <select><option selected>rawSelectCredential</option></select>
    <div style='display:none'>rawDisplayCredential</div><div aria-hidden='true'>rawAriaCredential</div>
    <div id='accessToken'>rawTokenCredential</div>
    <form action='https://evil.example/submit' method='post'><button onclick='rawButtonCredential'>申请开票</button></form>
    <img src='https://evil.example/?token=rawImageCredential'>
    <a href='https://details.jd.com/normal/item.action?orderId=123456789012&amp;token=rawQueryCredential&amp;auth=rawAuthCredential#rawFragmentCredential'>订单详情</a>
    <a href='https://evil.example/?orderId=123456789012'>外部链接</a>
    <p>Authorization: Bearer rawBearerCredential</p></body></html>"""
    sanitized, truncated = sanitize_diagnostic_html(raw, "jd", "https://order.jd.com/center/list.action")
    assert not truncated
    assert "Credential" not in sanitized
    assert "orderId=123456789012" in sanitized
    assert "class" not in BeautifulSoup(sanitized, "html.parser").find("input").attrs
    soup = BeautifulSoup(sanitized, "html.parser")
    assert not soup.select("script,style,iframe,object,embed,base,meta,link")
    assert soup.find("input")["placeholder"] == "企业名称"
    assert not soup.find("input").has_attr("value")
    assert not soup.find("img").has_attr("src")
    assert not soup.find("form").has_attr("action")
    assert not soup.find_all("a")[1].has_attr("href")


def test_diagnostic_is_size_bounded_and_truncation_is_explicit():
    sanitized, truncated = sanitize_diagnostic_html("<main>" + "订单字段" * 100_000 + "</main>", "jd", "https://order.jd.com/")
    assert truncated and "已截断" in sanitized
    assert len(sanitized.encode("utf-8")) <= 1024 * 1024


def test_delayed_dom_wait_and_private_diagnostic_real_offline_browser(tmp_path):
    api = pytest.importorskip("playwright.async_api")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                manager = BrowserManager(tmp_path)
                await page.set_content((FIXTURES / "delayed-orders-shell.html").read_text())
                orders = (FIXTURES / "jd-orders.html").read_text()
                await page.evaluate("html => setTimeout(() => document.body.innerHTML=html, 100)", orders)
                assert await manager._wait_orders_ready(page, "jd", timeout_ms=1000)
                assert len(parse_orders(await page.content(), "jd").orders) == 2
                assert page.url == "about:blank"
                await page.set_content((FIXTURES / "delayed-orders-shell.html").read_text())
                assert not await manager._wait_orders_ready(page, "jd", timeout_ms=100)
                await page.evaluate("() => setTimeout(() => document.body.innerHTML='<p>暂无订单</p>', 60)")
                assert await manager._wait_orders_ready(page, "jd", timeout_ms=1000)
                await page.set_content("<style>.invisible{display:none}</style><p>可见结构</p><div class='invisible'>rawComputedHiddenCredential</div><input value='rawInputCredential'>")
                result = await manager._diagnostic(page, "jd")
                paths = list((tmp_path / "diagnostics").glob("*.html"))
                assert len(paths) == 1 and str(paths[0]) in result
                assert paths[0].stat().st_mode & 0o777 == 0o600
                assert paths[0].parent.stat().st_mode & 0o777 == 0o700
                assert "Credential" not in paths[0].read_text()
                assert "可见结构" in paths[0].read_text()
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_manual_browser_close_can_reopen_owned_profile_and_close_clears_handles(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import playwright.async_api as api

    class FakeContext:
        def __init__(self, connected=True):
            self.browser = SimpleNamespace(is_connected=lambda: connected)
            self.callbacks = {}

        def on(self, event, callback):
            self.callbacks[event] = callback

        def set_default_timeout(self, timeout):
            pass

        async def close(self):
            raise RuntimeError("Already closed")

    class FakePlaywright:
        def __init__(self):
            self.stops = 0
            self.launches = 0
            self.chromium = SimpleNamespace(launch_persistent_context=self.launch)

        async def launch(self, profile, **options):
            self.launches += 1
            assert profile == str(tmp_path / "browser-profile")
            assert options["channel"] == "chrome" and options["headless"] is False
            assert options["timezone_id"] == "Asia/Shanghai"
            return FakeContext()

        async def stop(self):
            self.stops += 1

    created = []

    class Starter:
        async def start(self):
            instance = FakePlaywright()
            created.append(instance)
            return instance

    monkeypatch.setattr(api, "async_playwright", lambda: Starter())

    async def exercise():
        manager = BrowserManager(tmp_path)
        old = FakePlaywright()
        manager._playwright = old
        manager._context = FakeContext(connected=False)
        manager._pages["jd"] = object()
        await manager._ensure()
        assert old.stops == 1 and len(created) == 1 and not manager._pages
        first = manager._context
        await manager._ensure()
        assert manager._context is first and len(created) == 1
        first.callbacks["close"]()
        await manager._ensure()
        assert manager._context is not first and len(created) == 2
        manager._pages["jd"] = object()
        await manager.close()
        assert manager._context is None and manager._playwright is None and not manager._pages
        assert created[-1].stops == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["click", "control_read"])
def test_next_page_click_failure_preserves_already_parsed_orders(tmp_path, failure):
    class Page:
        url = "https://order.jd.com/center/list.action"

        async def goto(self, *_args, **_kwargs):
            pass

        async def content(self):
            return (FIXTURES / "jd-orders.html").read_text()

        def locator(self, _selector):
            class Locator:
                async def all_inner_texts(self):
                    return ["previous page"]
            return Locator()

    class Next:
        async def is_enabled(self):
            return True

        async def get_attribute(self, _name):
            return None

        async def click(self):
            raise TimeoutError("Click failed")

    class Scan(BrowserManager):
        async def _page(self, platform):
            return Page()

        async def _wait_orders_ready(self, *_args, **_kwargs):
            return True

        async def _check(self, *_args):
            return "订单列表"

        async def _named(self, *_args, **_kwargs):
            return [Next()]

        async def _prepare_scan_scope(self, *_args):
            return {"verified": True, "kind": "year", "label": "2026年订单", "year": 2026}

        async def _verify_first_page(self, *_args):
            return True

        async def _next_page_control(self, *_args):
            if failure == "control_read":
                raise RuntimeError("The page closed while reading pagination")
            return {"next": Next(), "root": None, "recognized": True, "disabled": False,
                    "current_page": 1, "first_page": True, "total_pages": None}

        async def _diagnostic(self, *_args):
            return "虚构诊断"

    manager = Scan(tmp_path)
    orders = asyncio.run(manager.scan("jd", "2026-11-01", "2026-11-04", 10))
    assert len(orders) == 2
    assert manager.last_scan_report["complete"] is False
    assert ("分页未能确认完成" if failure == "click" else "分页区域暂时无法读取") in manager.last_scan_report["reason"]


def test_current_taobao_layout_uses_only_unique_split_total_and_shop():
    html = (FIXTURES / "taobao-current-layout-fictional.html").read_text()
    result = parse_orders(html, "taobao")
    assert not result.rejected and len(result.orders) == 1
    order = result.orders[0]
    assert order["order_no"] == "202611060011"
    assert order["shop"] == "示例店铺" and order["product"] == "示例商品"
    assert order["amount_fen"] == 12345 and order["paid_at"] == "2026-11-06"
    assert order["order_url"] == "https://trade.taobao.com/trade/detail/trade_item_detail.htm?bizOrderId=202611060011"
    assert order["company_purchase"] is False and order["invoice_type"] == "unknown"
    # Make product, shipping, and recommendation prices visibly different.
    soup = BeautifulSoup(html, "html.parser")
    for integer in soup.select(".trade-price-integer"):
        if not integer.find_parent(class_=lambda value: value and "priceReal--" in value):
            integer.string = "999"
    for label in soup.select(".trade-price-label"):
        if label.get_text(strip=True) != "合计":
            label.string = "含运费:"
    assert parse_orders(str(soup), "taobao").orders[0]["amount_fen"] == 12345


@pytest.mark.parametrize("change", ["duplicate_total", "shipping_label", "missing_total", "duplicate_integer", "invalid_decimal", "conflicting_shop"])
def test_current_taobao_total_ambiguity_and_shipping_never_become_order_total(change):
    from copy import deepcopy
    soup = BeautifulSoup((FIXTURES / "taobao-current-layout-fictional.html").read_text(), "html.parser")
    total = soup.select_one(".trade-bought-col-order-payment [class*='priceReal--']")
    if change == "duplicate_total":
        total.insert_after(deepcopy(total))
    elif change == "shipping_label":
        total.select_one(".trade-price-label").string = "含运费:"
    elif change == "missing_total":
        total.decompose()
    elif change == "duplicate_integer":
        integer = total.select_one(".trade-price-integer")
        integer.insert_after(deepcopy(integer))
    elif change == "invalid_decimal":
        total.select_one(".trade-price-decimal").string = "456"
    elif change == "conflicting_shop":
        shop = soup.select_one("a[class^='shopInfoName--']")
        another = deepcopy(shop)
        another.string = "另一个示例店铺"
        shop.insert_after(another)
    result = parse_orders(str(soup), "taobao")
    assert not result.orders and result.rejected


def test_current_jd_layout_split_price_metadata_and_nonlink_details():
    html = (FIXTURES / "jd-current-layout-fictional.html").read_text()
    result = parse_orders(html, "jd")
    assert not result.rejected and len(result.orders) == 1
    order = result.orders[0]
    assert order["order_no"] == "202611060012" and order["paid_at"] == "2026-11-06"
    assert order["shop"] == "示例京东店铺" and order["product"] == "示例测试商品"
    assert order["amount_fen"] == 45678 and order["trade_status"] == "completed"
    assert order["order_url"] == "" and "未推断详情地址" in order["evidence"]
    assert order["company_purchase"] is False and order["special_status"] == "unknown"
    assert order["invoice_status"] == "unknown"
    soup = BeautifulSoup(html, "html.parser")
    soup.select_one("[class^='productCount-']").string = "数量9999 单价￥999.99"
    soup.select_one("[class^='paymentTypeName-']").string = "在线支付 888"
    assert parse_orders(str(soup), "jd").orders[0]["amount_fen"] == 45678
    soup.select_one("[class^='priceDecimal-']").string = "78"
    assert parse_orders(str(soup), "jd").orders[0]["amount_fen"] == 45678


@pytest.mark.parametrize("change", ["split_order_ids", "unlabelled_order_id", "duplicate_total", "duplicate_integer", "invalid_decimal", "missing_total", "conflicting_shop"])
def test_current_jd_conflicts_split_orders_and_arbitrary_numbers_are_rejected(change):
    from copy import deepcopy
    soup = BeautifulSoup((FIXTURES / "jd-current-layout-fictional.html").read_text(), "html.parser")
    if change == "split_order_ids":
        meta = soup.select_one("[class^='orderMetas-']")
        second = deepcopy(meta)
        second.select_one("[class^='metaValue-']").string = "202611060013"
        meta.insert_after(second)
    elif change == "unlabelled_order_id":
        soup.select_one("[class^='orderMetas-'] [class^='metaLabel-']").string = "物流编号"
    elif change == "duplicate_total":
        row = soup.select_one("[class^='priceColumn-'] [class^='priceRow-']")
        row.insert_after(deepcopy(row))
    elif change == "duplicate_integer":
        integer = soup.select_one("[class^='priceInteger-']")
        integer.insert_after(deepcopy(integer))
    elif change == "invalid_decimal":
        soup.select_one("[class^='priceDecimal-']").string = ".789"
    elif change == "missing_total":
        soup.select_one("[class^='priceColumn-']").decompose()
        soup.select_one("[class^='productCount-']").string = "数量 456.78"
    elif change == "conflicting_shop":
        shop = soup.select_one("span[class^='shopNames-']")
        other = deepcopy(shop)
        other.string = "另一个示例京东店铺"
        shop.insert_after(other)
    result = parse_orders(str(soup), "jd")
    assert not result.orders and result.rejected


def test_current_jd_overlapping_containers_are_not_double_imported():
    soup = BeautifulSoup((FIXTURES / "jd-current-layout-fictional.html").read_text(), "html.parser")
    card = soup.select_one("[class^='orderCard-']")
    wrapper = soup.new_tag("div", attrs={"class": "orderCard-wrapper"})
    card.wrap(wrapper)
    result = parse_orders(str(soup), "jd")
    assert len(result.orders) == 1 and not result.rejected


def test_current_jd_container_is_used_by_bounded_render_wait(tmp_path):
    api = pytest.importorskip("playwright.async_api")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                await page.set_content((FIXTURES / "delayed-orders-shell.html").read_text())
                html = (FIXTURES / "jd-current-layout-fictional.html").read_text()
                await page.evaluate("html => setTimeout(() => document.body.innerHTML=html, 100)", html)
                manager = BrowserManager(tmp_path)
                assert await manager._wait_orders_ready(page, "jd", timeout_ms=1000)
                result = parse_orders(await page.content(), "jd")
                assert len(result.orders) == 1 and result.orders[0]["amount_fen"] == 45678
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_current_taobao_explicit_actual_payment_label_is_supported():
    soup = BeautifulSoup((FIXTURES / "taobao-current-layout-fictional.html").read_text(), "html.parser")
    total = soup.select_one(".trade-bought-col-order-payment [class*='priceReal--']")
    total.select_one(".trade-price-label").string = "实付款"
    result = parse_orders(str(soup), "taobao")
    assert len(result.orders) == 1 and not result.rejected
    assert result.orders[0]["amount_fen"] == 12345
    total.select_one(".trade-price-label").string = "待确认金额"
    assert not parse_orders(str(soup), "taobao").orders


@pytest.mark.parametrize("platform", ["taobao", "jd"])
def test_explicit_zero_amount_is_preserved_for_ledger_manual_review(platform):
    filename = "taobao-current-layout-fictional.html" if platform == "taobao" else "jd-current-layout-fictional.html"
    soup = BeautifulSoup((FIXTURES / filename).read_text(), "html.parser")
    if platform == "taobao":
        total = soup.select_one(".trade-bought-col-order-payment [class*='priceReal--']")
        total.select_one(".trade-price-integer").string = "0"
        total.select_one(".trade-price-decimal").string = "00"
    else:
        total = soup.select_one("[class^='priceColumn-'] [class^='priceRow-']")
        total.select_one("[class^='priceInteger-']").string = "0"
        total.select_one("[class^='priceDecimal-']").string = ".00"
    result = parse_orders(str(soup), platform)
    assert len(result.orders) == 1 and not result.rejected
    assert result.orders[0]["amount_fen"] == 0
    assert result.orders[0]["trade_status"] == "completed"


def test_history_controls_locate_structural_next_and_parent_disabled(tmp_path):
    api = pytest.importorskip("playwright.async_api")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                manager = BrowserManager(tmp_path)
                await page.set_content((FIXTURES / "taobao-history-fictional.html").read_text())
                control = await manager._next_page_control(page, "taobao")
                assert control["recognized"] and not control["disabled"]
                assert await control["next"].get_attribute("id") == "bottomNext"
                assert control["current_page"] == 1 and control["first_page"] is True
                await page.locator("li.ant-pagination-next").evaluate("el => el.classList.add('ant-pagination-disabled')")
                control = await manager._next_page_control(page, "taobao")
                assert control["disabled"] is True
                await page.set_content((FIXTURES / "jd-history-fictional.html").read_text())
                control = await manager._next_page_control(page, "jd")
                assert control["recognized"] and not control["disabled"]
                assert await control["next"].get_attribute("id") == "next"
                assert control["first_page"] is True
                await page.locator("#next").evaluate("el => el.disabled=true")
                assert (await manager._next_page_control(page, "jd"))["disabled"] is True
            finally:
                await browser.close()

    asyncio.run(exercise())


async def _install_history_fixture(page, platform, dates, repeat=False):
    await page.set_content((FIXTURES / f"{platform}-history-fictional.html").read_text())
    template = (FIXTURES / f"{platform}-current-layout-fictional.html").read_text()
    original_id = "202611060011" if platform == "taobao" else "202611060012"
    cards = [template.replace(original_id, str(202604160001 + i)).replace("2026-11-06", day)
             for i, day in enumerate(dates)]
    if repeat and len(cards) > 1:
        repeated = BeautifulSoup(cards[0], "html.parser")
        card = repeated.select_one("[id^='shopOrderContainer_']" if platform == "taobao" else "[class^='orderCard-']")
        marker = repeated.new_tag("span")
        marker.string = "页面已刷新"
        card.append(marker)
        cards[1] = str(repeated)
    await page.evaluate("""state => {
        window.historyFixture = {...state,index:0,nextClicks:0,topClicks:0,yearSelections:0};
        const f=window.historyFixture;
        const render = index => {
            f.index=index; document.querySelector('#orders').innerHTML=f.cards[index];
            if(f.platform==='taobao') {
                document.querySelectorAll('li.ant-pagination-item').forEach(el => el.classList.toggle('ant-pagination-item-active',el.title===String(index+1)));
                document.querySelector('li.ant-pagination-prev').classList.toggle('ant-pagination-disabled',index===0);
                document.querySelector('li.ant-pagination-next').classList.toggle('ant-pagination-disabled',index===f.cards.length-1);
            } else {
                document.querySelectorAll('button.pageBtn-fictional').forEach(el => {const active=el.innerText===String(index+1);el.classList.toggle('active',active);if(active)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
                document.querySelector('#previous').disabled=index===0;
                document.querySelector('#next').disabled=index===f.cards.length-1;
            }
        };
        const next = () => {f.nextClicks++;if(f.index<f.cards.length-1)render(f.index+1);};
        f.render=render;
        if(f.platform==='taobao') {
            document.querySelector('#bottomNext').addEventListener('click',next);
            document.querySelector('#topNext').addEventListener('click',()=>f.topClicks++);
            document.querySelectorAll('li.ant-pagination-item a').forEach(el=>el.addEventListener('click',()=>render(Number(el.innerText)-1)));
        } else {
            document.querySelector('#next').addEventListener('click',next);
            document.querySelectorAll('button.pageBtn-fictional').forEach(el=>el.addEventListener('click',()=>render(Number(el.innerText)-1)));
            document.querySelector('#dateFilter').addEventListener('click',()=>document.querySelector('#yearOptions').hidden=false);
            document.querySelectorAll('[role=option]').forEach(el=>el.addEventListener('click',()=>{document.querySelector('#dateSelection').innerText=el.innerText;document.querySelector('#yearOptions').hidden=true;f.yearSelections++;render(0);}));
        }
        render(0);
    }""", {"platform": platform, "cards": cards})


class _OfflineHistoryPage:
    def __init__(self, page, platform, dates, repeat=False):
        self.page, self.platform, self.dates, self.repeat = page, platform, dates, repeat

    async def goto(self, *_args, **_kwargs):
        await _install_history_fixture(self.page, self.platform, self.dates, self.repeat)

    def __getattr__(self, name):
        return getattr(self.page, name)


class _OfflineHistoryManager(BrowserManager):
    async def _page(self, platform):
        return self.fixture_page

    async def _check(self, page, platform):
        assert page.url == "about:blank"
        return await page.locator("body").inner_text()


@pytest.mark.parametrize("platform", ["taobao", "jd"])
def test_complete_historical_scan_visits_every_page_and_filters_locally(tmp_path, platform):
    api = pytest.importorskip("playwright.async_api")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                manager = _OfflineHistoryManager(tmp_path)
                # Page two is already older than the requested date. Page three
                # is newer again: ordering must never cause an early stop.
                manager.fixture_page = _OfflineHistoryPage(page, platform, ["2026-11-06", "2026-04-15", "2026-04-20"])
                result = await manager.scan(platform, "2026-04-16", "2026-11-06", 100)
                assert len(result) == 2
                report = manager.last_scan_report
                assert report["complete"] is True and report["terminal_reached"] is True
                assert report["pages"] == 3 and report["first_page_verified"] is True
                assert report["range_filter"]["verified"] is True
                assert report["observed_oldest_date"] == "2026-04-15"
                rows = report["page_report"]
                assert [row["page"] for row in rows] == [1, 2, 3]
                assert all(row["parsed"] == 1 and row["rejected"] == 0 for row in rows)
                assert len({row["ids_hash"] for row in rows}) == 3
                assert all(len(row["ids_hash"]) == 64 for row in rows)
                assert await page.evaluate("window.historyFixture.nextClicks") == 2
                assert await page.evaluate("window.historyFixture.topClicks") == 0
                if platform == "jd":
                    assert await page.evaluate("window.historyFixture.yearSelections") == 1
                    assert report["range_filter"]["label"] == "2026年订单"
            finally:
                await browser.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("scenario", ["page_limit", "repeated_page", "dates_not_reached"])
def test_historical_scan_coverage_is_explicit(tmp_path, scenario):
    api = pytest.importorskip("playwright.async_api")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                manager = _OfflineHistoryManager(tmp_path)
                dates = ["2026-11-06", "2026-04-15", "2026-04-20"]
                if scenario == "dates_not_reached":
                    dates = ["2026-11-06", "2026-10-01", "2026-09-01"]
                manager.fixture_page = _OfflineHistoryPage(page, "taobao", dates, repeat=scenario == "repeated_page")
                result = await manager.scan("taobao", "2026-04-16", "2026-11-06", 2 if scenario == "page_limit" else 100)
                assert result
                report = manager.last_scan_report
                if scenario == "page_limit":
                    assert report["complete"] is False
                    assert "页上限" in report["reason"] and report["pages"] == 2
                elif scenario == "repeated_page":
                    assert report["complete"] is False
                    assert "同一批订单" in report["reason"] and report["pages"] == 2
                else:
                    assert report["terminal_reached"] is True and report["complete"] is True
                    assert report["observed_oldest_date"] == "2026-09-01"
            finally:
                await browser.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["missing", "duplicate"])
def test_jd_missing_annual_option_stops_and_saves_sanitized_diagnostic(tmp_path, failure):
    api = pytest.importorskip("playwright.async_api")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                await _install_history_fixture(page, "jd", ["2026-11-06"])
                await page.locator("#yearOptions [role=option]").filter(has_text="2026年订单").evaluate("(el, failure)=>failure==='missing'?el.remove():el.after(el.cloneNode(true))", failure)
                manager = BrowserManager(tmp_path)
                with pytest.raises(BrowserAttention, match="全年") as error:
                    await manager._prepare_scan_scope(page, "jd", __import__("datetime").date(2026, 4, 16), __import__("datetime").date(2026, 11, 6))
                assert "脱敏 DOM 诊断" in str(error.value)
                assert list((tmp_path / "diagnostics").glob("*.html"))
                assert await page.evaluate("window.historyFixture.nextClicks") == 0
            finally:
                await browser.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("platform", ["taobao", "jd"])
def test_historical_scope_can_return_to_proven_first_page(tmp_path, platform):
    api = pytest.importorskip("playwright.async_api")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                await _install_history_fixture(page, platform, ["2026-11-06", "2026-04-15", "2026-04-20"])
                if platform == "taobao":
                    await page.locator("li.ant-pagination-item[title='2'] a").click()
                else:
                    await page.get_by_role("button", name="2", exact=True).click()
                manager = BrowserManager(tmp_path)
                assert (await manager._next_page_control(page, platform))["first_page"] is False
                assert await manager._verify_first_page(page, platform) is True
                assert await page.evaluate("window.historyFixture.index") == 0
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_verified_jd_year_with_new_account_september_orders_is_complete_at_two_page_end(tmp_path):
    api = pytest.importorskip("playwright.async_api")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                manager = _OfflineHistoryManager(tmp_path)
                manager.fixture_page = _OfflineHistoryPage(page, "jd", ["2026-11-06", "2026-09-01"])
                result = await manager.scan("jd", "2026-04-16", "2026-11-06", 100)
                report = manager.last_scan_report
                assert len(result) == 2 and report["pages"] == 2
                assert report["range_filter"]["label"] == "2026年订单"
                assert report["first_page_verified"] and report["terminal_reached"]
                assert report["observed_oldest_date"] == "2026-09-01"
                assert report["range_coverage_verified"] is True and report["complete"] is True
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_missing_jd_amount_exposes_only_unresolved_cards_without_inventing_zero():
    from copy import deepcopy
    template = BeautifulSoup((FIXTURES / "jd-current-layout-fictional.html").read_text(), "html.parser")
    card = template.select_one("[class^='orderCard-']")
    incomplete = []
    for number, product in (("202605080041", "示例键盘"), ("202605080042", "示例办公用品")):
        item = BeautifulSoup(str(card).replace("202611060012", number).replace("2026-11-06", "2026-05-08"), "html.parser")
        item.select_one("[class^='priceColumn-']").decompose()
        item.select_one("[class^='productTitle-']").string = product
        incomplete.append(str(item))
    result = parse_orders(str(card) + "".join(incomplete), "jd")
    assert len(result.orders) == 1 and len(result.unresolved_records) == 2
    assert {row["order_no"] for row in result.unresolved_records} == {"202605080041", "202605080042"}
    for row in result.unresolved_records:
        assert row["paid_at"] == "2026-05-08" and row["shop"] == "示例京东店铺"
        assert row["product"] and row["observed_url"] == ""
        assert row["amount_fen"] is None and row["missing_fields"] == ["amount_fen"]


def test_jd_waits_for_delayed_year_data_and_rechecks_div_pageitem_first_hash(tmp_path):
    api = pytest.importorskip("playwright.async_api")

    class DelayedYear(_OfflineHistoryPage):
        async def goto(self, *_args, **_kwargs):
            await super().goto(*_args, **_kwargs)

            async def year_response(route):
                await asyncio.sleep(0.35)
                await route.fulfill(status=200, content_type="application/json", body="{}",
                                    headers={"Access-Control-Allow-Origin": "*"})

            await self.page.route("https://history-fixture.invalid/year", year_response)
            await self.page.evaluate("""() => {
                const f=window.historyFixture, originalRender=f.render;
                f.yearCards=[...f.cards]; f.cards=f.cards.slice(0,2);
                f.cards[0]=f.cards[0].replaceAll('202604160001','999999900001');
                const total=document.createElement('span');total.id='pageTotal';
                document.querySelector('div.pagination-fictional').append(total);
                document.querySelectorAll('button.pageBtn-fictional').forEach(old => {
                    const item=document.createElement('div'); item.className='pageItem-fictional';item.innerText=old.innerText;
                    item.addEventListener('click',()=>f.render(Number(item.innerText)-1));old.replaceWith(item);
                });
                f.render = index => {
                    originalRender(index);total.innerText=`共 ${f.cards.length} 页`;
                    document.querySelectorAll('div.pageItem-fictional').forEach(el=>el.classList.toggle('activePage-fictional',el.innerText===String(index+1)));
                };
                const oldNext=document.querySelector('#next'), next=oldNext.cloneNode(true);oldNext.replaceWith(next);
                next.addEventListener('click',()=>{f.nextClicks++;if(f.index<f.cards.length-1)f.render(f.index+1);});
                document.querySelectorAll('#yearOptions [role=option]').forEach(old => {
                    const option=old.cloneNode(true);old.replaceWith(option);
                    option.addEventListener('click',async()=>{
                        document.querySelector('#dateSelection').innerText=option.innerText;
                        document.querySelector('#yearOptions').hidden=true; f.yearSelections++;
                        await fetch('https://history-fixture.invalid/year');
                        f.cards=f.yearCards;f.render(0);
                    });
                });
                f.render(0);
            }""")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                manager = _OfflineHistoryManager(tmp_path)
                manager.fixture_page = DelayedYear(page, "jd", ["2026-11-06", "2026-09-01", "2026-05-08"])
                orders = await manager.scan("jd", "2026-04-16", "2026-11-06", 100)
                report = manager.last_scan_report
                assert report["complete"] is True and len(orders) == 3
                assert "999999900001" not in {row["order_no"] for row in orders}
                assert report["expected_total_pages"] == 3
                assert [row["total_pages"] for row in report["page_report"]] == [3, 3, 3]
                assert [row["platform_page"] for row in report["page_report"]] == [1, 2, 3]
                assert report["observed_total_pages"] == [3]
                assert report["first_page_recheck"]["verified"] is True
                assert report["first_page_recheck"]["ids_hash"] == report["page_report"][0]["ids_hash"]
                assert report["first_page_recheck"]["current_page"] == 1
                assert await page.evaluate("window.historyFixture.yearSelections") == 1
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_historical_report_preserves_unresolved_records_with_page_numbers(tmp_path):
    api = pytest.importorskip("playwright.async_api")

    class PartiallyUnknown(_OfflineHistoryPage):
        async def goto(self, *_args, **_kwargs):
            await super().goto(*_args, **_kwargs)
            await self.page.evaluate("""() => {
                const f=window.historyFixture, parser=new DOMParser();
                const missing=parser.parseFromString(f.cards[1].replaceAll('202604160002','202605089999'),'text/html');
                missing.querySelector('[class^=priceColumn-]').remove();
                f.cards[1]+=missing.body.innerHTML;
            }""")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                manager = _OfflineHistoryManager(tmp_path)
                manager.fixture_page = PartiallyUnknown(page, "jd", ["2026-11-06", "2026-05-08", "2026-04-20"])
                orders = await manager.scan("jd", "2026-04-16", "2026-11-06", 100)
                report = manager.last_scan_report
                assert len(orders) == 3 and report["terminal_reached"] is True
                assert report["complete"] is False and report["page_report"][1]["rejected"] == 1
                assert len(report["unresolved_records"]) == 1
                record = report["unresolved_records"][0]
                assert record["order_no"] == "202605089999" and record["amount_fen"] is None
                assert record["page"] == 2 and record["platform_page"] == 2
                assert record["missing_fields"] == ["amount_fen"]
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_conflicting_per_page_totals_never_become_complete_after_successful_first_recheck(tmp_path):
    api = pytest.importorskip("playwright.async_api")

    class ConflictingTotal(_OfflineHistoryManager):
        async def _next_page_control(self, page, platform):
            control = await super()._next_page_control(page, platform)
            control["total_pages"] = 4 if control["current_page"] == 2 else 3
            return control

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = await browser.new_page()
                manager = ConflictingTotal(tmp_path)
                manager.fixture_page = _OfflineHistoryPage(page, "jd", ["2026-11-06", "2026-09-01", "2026-05-08"])
                await manager.scan("jd", "2026-04-16", "2026-11-06", 100)
                report = manager.last_scan_report
                assert report["terminal_reached"] is True
                assert report["first_page_recheck"]["verified"] is True
                assert report["observed_total_pages"] == [3, 4]
                assert report["complete"] is False and "总页数不一致" in report["reason"]
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_taobao_payment_dom_supplements_amount_without_inventing_refund_state():
    html = (FIXTURES / "taobao-settlement-detail-fictional.html").read_text()
    observation = transaction_dom_observation(html, "taobao")
    assert observation == {"trade_status": "completed", "refund_status": "unknown", "amount_fen": 8850}
    assert transaction_dom_observation(html, "jd")["amount_fen"] is None
    assert transaction_dom_observation(html.replace("88</span>", "0</span>").replace("50</span>", "00</span>"), "taobao")["amount_fen"] == 0


@pytest.mark.parametrize("change", ["two_totals", "two_sections", "text_conflict", "ambiguous_text", "wrong_title", "missing_point", "invalid_decimal", "duplicate_component"])
def test_taobao_settlement_ambiguity_never_overrides_unknown_or_conflicting_amount(change):
    from copy import deepcopy
    soup = BeautifulSoup((FIXTURES / "taobao-settlement-detail-fictional.html").read_text(), "html.parser")
    section = soup.select_one("div.trade-order-detail-settlement-info")
    last = section.select_one("div[class*='lastDetailInfoContent--']")
    if change == "two_totals":
        last.insert_after(deepcopy(last))
    elif change == "two_sections":
        section.insert_after(deepcopy(section))
    elif change == "text_conflict":
        soup.body.append(BeautifulSoup("<p>实付款：￥89.00</p>", "html.parser"))
    elif change == "ambiguous_text":
        soup.body.append(BeautifulSoup("<p>实付款：￥88.50 订单金额：￥89.00</p>", "html.parser"))
    elif change == "wrong_title":
        section.select_one("div[class^='infoTitle--']").string = "推荐商品价格"
    elif change == "missing_point":
        last.select_one(".trade-price-point").decompose()
    elif change == "invalid_decimal":
        last.select_one(".trade-price-decimal").string = "500"
    elif change == "duplicate_component":
        integer = last.select_one(".trade-price-integer")
        integer.insert_after(deepcopy(integer))
    observation = transaction_dom_observation(str(soup), "taobao")
    assert observation["amount_fen"] is None
    assert observation["refund_status"] == "unknown"


def test_matching_text_and_settlement_amount_agree_but_bare_totals_do_not_create_payment():
    html = (FIXTURES / "taobao-settlement-detail-fictional.html").read_text()
    assert transaction_dom_observation(html + "<p>实付款：￥88.50</p>", "taobao")["amount_fen"] == 8850
    soup = BeautifulSoup(html, "html.parser")
    soup.select_one("div.trade-order-detail-settlement-info").decompose()
    assert transaction_dom_observation(str(soup), "taobao")["amount_fen"] is None


def test_browser_transaction_guard_uses_settlement_html_and_keeps_refund_guard(tmp_path):
    html = (FIXTURES / "taobao-settlement-detail-fictional.html").read_text()

    class Scope:
        current = html

        async def inner_html(self):
            return self.current

        async def inner_text(self):
            raise AssertionError("The guard must consume bound HTML, not a bare body total")

    scope = Scope()
    manager = BrowserManager(tmp_path)
    order = {"platform": "taobao", "order_no": "202605080051", "trade_status": "completed", "amount_fen": 8850}
    with pytest.raises(BrowserAttention, match="退款"):
        asyncio.run(manager._verify_transaction(scope, order))
    scope.current = html + "<p>退款状态：无</p>"
    asyncio.run(manager._verify_transaction(scope, order))
    with pytest.raises(BrowserAttention, match="金额"):
        asyncio.run(manager._verify_transaction(scope, order | {"amount_fen": 8800}))


def test_invoice_detail_uses_exact_rows_not_faq_capabilities_or_seller_identity():
    html = (FIXTURES / "jd-invoice-detail-fictional.html").read_text()
    result = invoice_detail_dom_observation(html, "jd")
    assert result["invoice"] == {"invoice_status": "issued", "invoice_type": "ordinary",
                                  "special_status": "unknown", "ordinary_supported": False}
    assert result["buyer"] == {"invoice_buyer": "示例采购企业", "invoice_tax_id": "91310000123456789A"}
    assert result["complete"] is True
    assert invoice_detail_dom_observation(html.replace("电子普通发票</span>", "数电专票</span>"), "jd")["invoice"]["invoice_type"] == "special"


@pytest.mark.parametrize("table_class", ["tb-void", "independent-type"])
def test_invoice_detail_independent_exact_type_row_and_conflicts_are_conservative(table_class):
    from copy import deepcopy
    soup = BeautifulSoup((FIXTURES / "jd-invoice-detail-fictional.html").read_text(), "html.parser")
    information = soup.select_one("#invoiceInformation")
    type_row = information.find("td", class_="label").find_parent("tr")
    separate = soup.new_tag("table", attrs={"class": table_class})
    separate.append(type_row.extract())
    information.insert_before(separate)
    assert invoice_detail_dom_observation(str(soup), "jd")["invoice"]["invoice_type"] == "ordinary"
    conflicting = deepcopy(type_row)
    conflicting.find_all("td")[1].select_one("span.ftx06").string = "数电专票"
    separate.append(conflicting)
    result = invoice_detail_dom_observation(str(soup), "jd")
    assert result["invoice"]["invoice_type"] == "unknown" and result["complete"] is False


@pytest.mark.parametrize("value,expected", [("已开具", "issued"), ("已开票", "issued"), ("已申请", "pending"),
    ("开票中", "pending"), ("未申请", "unrequested"), ("未开具", "unknown"), ("未开票", "unknown"),
    ("资料审核通过", "unknown"), ("提示：已开票后可查看", "unknown")])
def test_invoice_status_requires_exact_invoice_status_row_value(value, expected):
    soup = BeautifulSoup((FIXTURES / "jd-invoice-detail-fictional.html").read_text(), "html.parser")
    soup.select_one("#state").find_all("td")[1].string = value
    assert invoice_detail_dom_observation(str(soup), "jd")["invoice"]["invoice_status"] == expected


@pytest.mark.parametrize("action,expected", [
    ("<a href='https://myivc.jd.com/fictional/invoice'>查看发票</a>", "issued"),
    ("<button>查看 XML</button>", "issued"),
    ("<a href='#'>查看发票</a>", "unknown"),
    ("<span>查看XML</span>", "unknown"),
    ("<button hidden>查看发票</button>", "unknown"),
    ("<a href='https://evil.example/invoice'>查看发票</a>", "unknown"),
])
def test_invoice_action_fallback_is_bound_to_populated_information_table(action, expected):
    soup = BeautifulSoup((FIXTURES / "jd-invoice-detail-fictional.html").read_text(), "html.parser")
    soup.select_one("#state").decompose()
    table = soup.select_one("#invoiceInformation")
    table.append(BeautifulSoup("<tr><td colspan='2'>" + action + "</td></tr>", "html.parser"))
    assert invoice_detail_dom_observation(str(soup), "jd")["invoice"]["invoice_status"] == expected


def test_invoice_missing_rows_and_multiple_information_tables_never_use_faq():
    from copy import deepcopy
    soup = BeautifulSoup((FIXTURES / "jd-invoice-detail-fictional.html").read_text(), "html.parser")
    soup.select_one("#state").decompose()
    result = invoice_detail_dom_observation(str(soup), "jd")
    assert result["invoice"]["invoice_status"] == "unknown" and result["complete"] is False
    table = soup.select_one("#invoiceInformation")
    table.insert_after(deepcopy(table))
    result = invoice_detail_dom_observation(str(soup), "jd")
    assert result["invoice"]["invoice_type"] == "unknown" and not result["buyer"]["invoice_buyer"]
    for item in soup.select("#invoiceInformation"):
        item.decompose()
    result = invoice_detail_dom_observation(str(soup), "jd")
    assert result["invoice"]["invoice_type"] == "unknown" and result["complete"] is False


def _original_pdf_bytes():
    import io
    from pypdf import PdfWriter
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": "Fictional invoice test original"})
    writer.write(stream)
    return stream.getvalue()


class _DocumentResponse:
    def __init__(self, body, *, status=200, content_type="application/pdf", length=None):
        self.payload, self.status = body, status
        self.headers = {"content-type": content_type}
        if length is not None:
            self.headers["content-length"] = str(length)
        self.body_reads, self.disposed = 0, False

    async def body(self):
        self.body_reads += 1
        return self.payload

    async def dispose(self):
        self.disposed = True


class _DocumentRequest:
    def __init__(self, response):
        self.response, self.calls = response, []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.mark.parametrize("url,allowed", [
    ("https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/fictional.pdf?signature=unchanged", True),
    ("https://eicore-invoice-01.s3.cn-north-1.jdcloud-oss.com/fictional.xml", True),
    ("https://another-bucket.s3.cn-north-1.jdcloud-oss.com/fictional.pdf", False),
    ("https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com.evil.example/fictional.pdf", False),
    ("https://eicore-invoice-2.s3.cn-north-1.jdcloud-oss.com/fictional.pdf", False),
    ("http://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/fictional.pdf", False),
    ("https://user:password@eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/fictional.pdf", False),
    ("https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com:8443/fictional.pdf", False),
    ("https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/", False),
])
def test_invoice_bucket_gate_is_download_only_and_exact(url, allowed):
    assert observed_jd_invoice_url(url, "jd") is allowed
    assert not observed_jd_invoice_url(url, "taobao")
    assert not official_url(url, "jd")


@pytest.mark.parametrize("status,headers", [
    (302, {"content-type": "application/pdf", "location": "https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/another.pdf"}),
    (206, {"content-type": "application/pdf"}),
    (200, {"content-type": "text/html"}),
    (200, {"content-type": "application/octet-stream"}),
    (200, {"content-type": "application/pdf", "content-length": str(INVOICE_MAX_BYTES + 1)}),
    (200, {"content-type": "application/pdf", "content-length": "invalid"}),
])
def test_raw_document_response_requires_success_explicit_type_and_bounded_size(status, headers):
    with pytest.raises(BrowserAttention):
        invoice_response_extension(headers, status)


def test_raw_document_get_is_exact_no_redirects_tls_verified_and_original_bytes(tmp_path):
    from types import SimpleNamespace
    original = _original_pdf_bytes()
    response = _DocumentResponse(original, length=len(original))
    request = _DocumentRequest(response)
    manager = BrowserManager(tmp_path)
    manager._context = SimpleNamespace(request=request)
    url = "https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/fictional/raw.pdf?signature=a%2Bb&part=one"
    order = {"platform": "jd", "order_no": "202605080061"}
    result = asyncio.run(manager._get_observed_invoice_document(url, order, tmp_path / "invoices"))
    archived = Path(result["path"])
    assert archived.read_bytes() == original and archived.stat().st_mode & 0o777 == 0o600
    assert len(request.calls) == 1 and request.calls[0][0] == url
    options = request.calls[0][1]
    assert options["max_redirects"] == 0 and options["max_retries"] == 0
    assert options["ignore_https_errors"] is False and options["fail_on_status_code"] is False
    assert not set(options).intersection({"data", "form", "multipart", "headers", "params"})
    assert response.disposed


@pytest.mark.parametrize("scenario", ["redirect", "html_type", "html_body", "malformed_pdf", "oversized_header", "oversized_body"])
def test_raw_document_failures_never_archive_a_file(tmp_path, monkeypatch, scenario):
    from types import SimpleNamespace
    import invoice_agent.browser as browser_module
    body = _original_pdf_bytes()
    response = _DocumentResponse(body)
    if scenario == "redirect":
        response.status = 302
        response.headers["location"] = "https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/redirected.pdf"
    elif scenario == "html_type":
        response.headers["content-type"] = "text/html"
    elif scenario == "html_body":
        response.payload = b"<html><body>fake invoice HTML</body></html>"
    elif scenario == "malformed_pdf":
        response.payload = b"%PDF-1.7\nnot a valid PDF structure\n%%EOF"
    elif scenario == "oversized_header":
        response.headers["content-length"] = str(INVOICE_MAX_BYTES + 1)
    elif scenario == "oversized_body":
        monkeypatch.setattr(browser_module, "INVOICE_MAX_BYTES", 32)
    request = _DocumentRequest(response)
    manager = BrowserManager(tmp_path)
    manager._context = SimpleNamespace(request=request)
    with pytest.raises(BrowserAttention):
        asyncio.run(manager._get_observed_invoice_document(
            "https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/fictional.pdf?signature=fictional",
            {"platform": "jd", "order_no": "202605080061"}, tmp_path / "invoices"))
    assert len(request.calls) == 1 and response.disposed
    assert not list((tmp_path / "invoices").glob("*"))
    if scenario in {"redirect", "html_type", "oversized_header"}:
        assert response.body_reads == 0


def test_atomic_archive_does_not_overwrite_or_delete_an_existing_file(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import invoice_agent.browser as browser_module
    monkeypatch.setattr(browser_module.uuid, "uuid4", lambda: SimpleNamespace(hex="fixedfilename"))
    original = tmp_path / "jd-202605080061-fixedfilenam.xml"
    original.write_bytes(b"existing user original")
    with pytest.raises(FileExistsError):
        save_invoice_bytes(b"<?xml version='1.0'?><Invoice><Id>1</Id></Invoice>", ".xml",
                           {"platform": "jd", "order_no": "202605080061"}, tmp_path)
    assert original.read_bytes() == b"existing user original"


@pytest.mark.parametrize("scenario", ["success", "success_noreferrer", "wrong_buyer", "other_bucket", "other_host", "redirect", "html", "malformed", "oversized"])
def test_real_offline_chrome_observes_one_pinned_document_and_never_executes_it(tmp_path, scenario):
    api = pytest.importorskip("playwright.async_api")
    original = _original_pdf_bytes()
    url = "https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/fictional/invoice.pdf?signature=keep%2Bexact"
    if scenario == "other_bucket":
        url = url.replace("eicore-invoice-26", "another-bucket")
    elif scenario == "other_host":
        url = "https://untrusted.example/fictional.pdf?signature=fictional"
    response = _DocumentResponse(original, length=len(original))
    if scenario == "redirect":
        response.status = 302
        response.headers["location"] = url.replace("invoice.pdf", "another.pdf")
    elif scenario == "html":
        response.headers["content-type"] = "text/html"
        response.payload = b"<html><script>window.documentScriptRan=true</script></html>"
    elif scenario == "malformed":
        response.payload = b"%PDF-1.7\nmalformed original bytes\n%%EOF"
    elif scenario == "oversized":
        response.headers["content-length"] = str(INVOICE_MAX_BYTES + 1)
    request = _DocumentRequest(response)

    class ContextProxy:
        def __init__(self, context):
            self.actual, self.request = context, request

        def __getattr__(self, name):
            return getattr(self.actual, name)

    class InvoiceBrowser(BrowserManager):
        async def _navigate(self, order, invoice=False):
            return self.fixture_page, self.fixture_page.locator("body")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()
            network_fallbacks = []
            html = (FIXTURES / "jd-invoice-detail-fictional.html").read_text()
            soup = BeautifulSoup(html, "html.parser")
            anchor = soup.new_tag("a", href=url, target="_blank", id="viewOriginal")
            if scenario == "success_noreferrer":
                anchor["rel"] = "noreferrer"
            anchor.string = "查看发票"
            anchor["onclick"] = "window.viewClicks=(window.viewClicks||0)+1"
            soup.select_one("#invoiceInformation").append(anchor)
            source_url = "https://myivc.jd.com/fictional/invoice-detail"

            async def offline_network(route):
                network_fallbacks.append(route.request.url)
                if route.request.url == source_url:
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=str(soup))
                else:
                    # This would execute if a document ever reached the browser.
                    await route.fulfill(status=200, content_type="text/html", body="<script>window.documentScriptRan=true</script>")

            await context.route("**/*", offline_network)
            try:
                page = await context.new_page()
                await page.goto(source_url)
                manager = InvoiceBrowser(tmp_path)
                manager.fixture_page, manager._context = page, ContextProxy(context)
                order = {"platform": "jd", "order_no": "202605080061", "invoice_tax_id": "91310000123456789A",
                         "invoice_buyer": "其他企业" if scenario == "wrong_buyer" else "示例采购企业"}
                destination = tmp_path / "invoices"
                if scenario in {"success", "success_noreferrer"}:
                    result = await manager.download(order, destination)
                    archived = Path(result["path"])
                    assert archived.read_bytes() == original and archived.stat().st_mode & 0o777 == 0o600
                    assert len(request.calls) == 1 and request.calls[0][0] == url
                else:
                    with pytest.raises(BrowserAttention):
                        await manager.download(order, destination)
                    assert not list(destination.glob("*"))
                assert await page.evaluate("window.viewClicks || 0") == (0 if scenario == "wrong_buyer" else 1)
                assert len(request.calls) == (0 if scenario in {"wrong_buyer", "other_bucket", "other_host"} else 1)
                assert network_fallbacks == [source_url]
                assert await page.evaluate("window.documentScriptRan || false") is False
                if request.calls:
                    assert request.calls[0][1]["max_redirects"] == 0
                    assert request.calls[0][1]["ignore_https_errors"] is False
                assert not official_url(url, "jd")
            finally:
                await context.close()
                await browser.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["xml", "ofd"])
def test_observed_get_preserves_explicit_xml_and_ofd_originals(tmp_path, kind):
    from types import SimpleNamespace
    import io
    if kind == "xml":
        body = b"<?xml version='1.0' encoding='utf-8'?><Invoice><Number>fictional</Number></Invoice>"
        content_type = "application/xml; charset=utf-8"
    else:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("OFD.xml", "<?xml version='1.0'?><OFD><DocBody/></OFD>")
        body, content_type = stream.getvalue(), "application/ofd"
    request = _DocumentRequest(_DocumentResponse(body, content_type=content_type, length=len(body)))
    manager = BrowserManager(tmp_path)
    manager._context = SimpleNamespace(request=request)
    result = asyncio.run(manager._get_observed_invoice_document(
        "https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/fictional-original." + kind,
        {"platform": "jd", "order_no": "202605080061"}, tmp_path / "invoices"))
    archived = Path(result["path"])
    assert archived.suffix == "." + kind and archived.read_bytes() == body
    assert archived.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("stalled", [False, True])
def test_real_offline_native_browser_download_is_preferred_without_api_get(tmp_path, monkeypatch, stalled):
    api = pytest.importorskip("playwright.async_api")
    import invoice_agent.browser as browser_module
    path_calls, cancel_calls = [], []
    if stalled:
        original_cancel = api.Download.cancel

        async def hanging_path(download):
            path_calls.append(download)
            await asyncio.Event().wait()

        async def tracked_cancel(download):
            cancel_calls.append(download)
            return await original_cancel(download)

        monkeypatch.setattr(browser_module, "BROWSER_DOWNLOAD_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(api.Download, "path", hanging_path)
        monkeypatch.setattr(api.Download, "cancel", tracked_cancel)
    original = _original_pdf_bytes()
    source = "https://myivc.jd.com/fictional/invoice-source"
    document = "https://myivc.jd.com/fictional/original-download.pdf"

    class NoGet:
        calls = 0

        async def get(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("Native browser download must not trigger an API GET")

    no_get = NoGet()

    class Proxy:
        def __init__(self, actual):
            self.actual, self.request = actual, no_get

        def __getattr__(self, name):
            return getattr(self.actual, name)

    class NativeBrowser(BrowserManager):
        async def _navigate(self, order, invoice=False):
            return self.fixture_page, self.fixture_page.locator("body")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context(accept_downloads=True)
            soup = BeautifulSoup((FIXTURES / "jd-invoice-detail-fictional.html").read_text(), "html.parser")
            anchor = soup.new_tag("a", href=document)
            anchor.string = "下载发票"
            soup.select_one("#invoiceInformation").append(anchor)

            async def offline(route):
                if route.request.url == document:
                    await route.fulfill(status=200, headers={"content-type": "application/pdf",
                        "content-length": str(len(original)), "content-disposition": "attachment; filename=fictional-original.pdf"}, body=original)
                else:
                    await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=str(soup))

            await context.route("**/*", offline)
            try:
                page = await context.new_page()
                await page.goto(source)
                manager = NativeBrowser(tmp_path)
                manager.fixture_page, manager._context = page, Proxy(context)
                order = {"platform": "jd", "order_no": "202605080061",
                    "invoice_buyer": "示例采购企业", "invoice_tax_id": "91310000123456789A"}
                if stalled:
                    with pytest.raises(BrowserAttention, match="时限"):
                        await manager.download(order, tmp_path / "invoices")
                    assert len(path_calls) == 1 and len(cancel_calls) == 1
                    assert not list((tmp_path / "invoices").glob("*"))
                    assert not manager.lock.locked()
                else:
                    result = await manager.download(order, tmp_path / "invoices")
                    assert Path(result["path"]).read_bytes() == original
                assert no_get.calls == 0
            finally:
                await context.close()
                await browser.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["different_order", "duplicate_button", "pending_status"])
def test_download_rebinds_order_identity_and_unique_action_immediately_before_click(tmp_path, change):
    api = pytest.importorskip("playwright.async_api")
    from invoice_agent.browser import VIEW_ACTION_PATTERN
    request = _DocumentRequest(_DocumentResponse(_original_pdf_bytes()))

    class Proxy:
        def __init__(self, actual):
            self.actual, self.request = actual, request

        def __getattr__(self, name):
            return getattr(self.actual, name)

    class ChangedInvoice(BrowserManager):
        changed = False

        async def _navigate(self, order, invoice=False):
            return self.fixture_page, self.fixture_page.locator("body")

        async def _named(self, scope, pattern, roles=("button", "link")):
            matched = await super()._named(scope, pattern, roles)
            if pattern == VIEW_ACTION_PATTERN and not self.changed:
                self.changed = True
                await self.fixture_page.evaluate("""change => {
                    if(change==='different_order')document.querySelector('p').innerText='订单号：202605080062';
                    if(change==='duplicate_button')document.querySelector('#view').after(document.querySelector('#view').cloneNode(true));
                    if(change==='pending_status')document.querySelector('#state td:last-child').innerText='已申请';
                }""", change)
            return matched

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()
            soup = BeautifulSoup((FIXTURES / "jd-invoice-detail-fictional.html").read_text(), "html.parser")
            button = soup.new_tag("button", id="view")
            button.string = "查看发票"
            button["onclick"] = "window.viewClicks=(window.viewClicks||0)+1"
            soup.body.append(button)
            await context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=str(soup)))
            try:
                page = await context.new_page()
                await page.goto("https://myivc.jd.com/fictional/source")
                manager = ChangedInvoice(tmp_path)
                manager.fixture_page, manager._context = page, Proxy(context)
                with pytest.raises(BrowserAttention):
                    await manager.download({"platform": "jd", "order_no": "202605080061",
                        "invoice_buyer": "示例采购企业", "invoice_tax_id": "91310000123456789A"}, tmp_path / "invoices")
                assert await page.evaluate("window.viewClicks||0") == 0
                assert not request.calls and not list((tmp_path / "invoices").glob("*"))
            finally:
                await context.close()
                await browser.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("source_kind", ["iframe", "old_popup"])
def test_known_child_frame_and_existing_popup_never_supply_invoice_original(tmp_path, source_kind):
    api = pytest.importorskip("playwright.async_api")
    request = _DocumentRequest(_DocumentResponse(_original_pdf_bytes()))
    source_url = "https://myivc.jd.com/fictional/source"
    document = "https://eicore-invoice-26.s3.cn-north-1.jdcloud-oss.com/fictional.pdf?signature=fictional"

    class Proxy:
        def __init__(self, actual):
            self.actual, self.request = actual, request

        def __getattr__(self, name):
            return getattr(self.actual, name)

    class ScopedBrowser(BrowserManager):
        async def _navigate(self, order, invoice=False):
            return self.fixture_page, self.fixture_page.locator("body")

    async def exercise():
        async with api.async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()
            network = []
            soup = BeautifulSoup((FIXTURES / "jd-invoice-detail-fictional.html").read_text(), "html.parser")
            button = soup.new_tag("button", id="view")
            button.string = "查看发票"
            soup.body.append(button)

            async def offline(route):
                network.append(route.request.url)
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=str(soup) if route.request.url == source_url else "<script>window.badDocument=true</script>")

            await context.route("**/*", offline)
            try:
                page = await context.new_page()
                await page.goto(source_url)
                if source_kind == "iframe":
                    await page.locator("body").evaluate("el=>el.insertAdjacentHTML('beforeend','<iframe id=invoiceFrame></iframe>')")
                    await page.wait_for_function("() => document.querySelector('#invoiceFrame').contentDocument?.readyState==='complete'")
                else:
                    async with page.expect_popup() as pending:
                        await page.evaluate("() => {window.previousInvoicePopup=window.open('about:blank','oldInvoicePopup');}")
                    old = await pending.value
                    assert await old.opener() is page
                await page.locator("#view").evaluate("""(el,state) => el.addEventListener('click',()=>{
                    window.viewClicks=(window.viewClicks||0)+1;
                    if(state.kind==='iframe')document.querySelector('#invoiceFrame').src=state.url;
                    else window.previousInvoicePopup.location.href=state.url;
                })""", {"kind": source_kind, "url": document})
                manager = ScopedBrowser(tmp_path)
                manager.fixture_page, manager._context = page, Proxy(context)
                with pytest.raises(BrowserAttention, match="子框架|既有|此前"):
                    await manager.download({"platform": "jd", "order_no": "202605080061",
                        "invoice_buyer": "示例采购企业", "invoice_tax_id": "91310000123456789A"}, tmp_path / "invoices")
                assert await page.evaluate("window.viewClicks||0") == 1
                assert not request.calls and network == [source_url]
                assert not list((tmp_path / "invoices").glob("*"))
            finally:
                await context.close()
                await browser.close()

    asyncio.run(exercise())
