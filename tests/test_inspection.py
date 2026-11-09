"""Fictional, offline DOM harness; these tests never open a shopping account."""
import asyncio
import re

import pytest
from bs4 import BeautifulSoup, Tag

from invoice_agent.browser import BrowserAttention, BrowserManager
from invoice_agent.inspection import inspect_order
from invoice_agent.parsers import CONTAINERS, ORDER_RE, PLATFORM_URLS, official_url


NUMBER = "123456789012"
OTHER = "223456789012"
DETAIL_URL = f"https://order.jd.com/order/detail.action?orderId={NUMBER}"
FICTIONAL_PROFILE = {"company_name": "虚构测试企业", "tax_id": "TEST00000000000000"}
DETAIL = f"""<html><body>
<h1>订单详情</h1><p>订单号：{NUMBER}</p><p>交易成功 实付款：￥88.00 无退款</p>
<p>已开票 增值税普通发票 不支持专票</p>
<p>购买方名称：虚构测试企业 购买方税号：TEST00000000000000</p>
<p>收件人：虚构收件人 收货地址：虚构星球假想街道123号 私人电话：13900000000</p>
<aside><a href="https://item.jd.com/111111.html">推荐商品：发票打印纸测试款</a></aside>
<button data-action="apply">申请开票</button>
<button disabled data-action="reissue">换开发票</button>
<a href="https://myivc.jd.com/invoice/detail.action?orderId={NUMBER}">查看发票</a>
<a href="https://invalid.example/invoice">查看电子发票</a>
<button hidden data-action="download">下载发票</button>
<button data-action="payment">立即付款</button>
<button data-action="message">发送给商家</button>
</body></html>"""


def test_optional_invoice_view_only_follows_observed_read_only_link(tmp_path):
    url = f"https://myivc.jd.com/invoice/detail.action?orderId={NUMBER}"
    manager = _Manager(tmp_path, details={DETAIL_URL: DETAIL, url: DETAIL})
    result = asyncio.run(inspect_order(manager, {"platform": "jd", "order_no": NUMBER,
        "paid_at": "2026-05-01", "order_url": DETAIL_URL}, FICTIONAL_PROFILE, invoice_details=True))
    assert result["observed_order_url"] == DETAIL_URL
    assert result["invoice_detail"]["verified_order"] is True
    assert result["invoice_detail"]["observed_url"] == url
    assert manager.navigations == [DETAIL_URL, url] and manager.clicks == []


def test_invoice_view_cannot_merge_another_orders_buyer(tmp_path):
    url = f"https://myivc.jd.com/invoice/detail.action?orderId={NUMBER}"
    manager = _Manager(tmp_path, details={DETAIL_URL: DETAIL, url: DETAIL.replace(NUMBER, OTHER)})
    result = asyncio.run(inspect_order(manager, {"platform": "jd", "order_no": NUMBER,
        "paid_at": "2026-05-01", "order_url": DETAIL_URL}, FICTIONAL_PROFILE, invoice_details=True))
    assert result["invoice_detail"]["verified_order"] is False
    assert "invoice_detail.verified_order" in result["missing_fields"]
    assert manager.clicks == []


def list_html(*numbers, duplicate_details=False, detail_url=DETAIL_URL):
    rows = []
    for number in numbers:
        rows.append(f"""<div class="orderCard-fictional" id="shopOrderContainer_{number}">
          <p>订单号：{number}</p><span>2026-05-01 实付款：￥88.00</span>
          <button data-action="detail" data-url="{detail_url}">订单详情</button>
          {'<button data-action="detail">订单详情</button>' if duplicate_details else ''}
          <button data-action="apply">申请开票</button>
        </div>""")
    return "<html><body>" + "".join(rows) + "</body></html>"


def _visible(node):
    for item in [node, *node.parents]:
        if isinstance(item, Tag) and (item.has_attr("hidden") or item.get("aria-hidden") == "true"
                                     or "display:none" in item.get("style", "").replace(" ", "")):
            return False
    return True


def _text(node):
    clone = BeautifulSoup(str(node), "html.parser")
    for item in list(clone.select("[hidden],[aria-hidden='true']")):
        item.decompose()
    return clone.get_text(" ", strip=True)


class _Locator:
    def __init__(self, page, nodes):
        self.page, self.nodes = page, list(nodes)

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return _Locator(self.page, [self.nodes[index]])

    def locator(self, selector):
        return _Locator(self.page, [child for node in self.nodes for child in node.select(selector)])

    def get_by_text(self, label, exact=False):
        nodes = []
        for scope in self.nodes:
            for node in scope.find_all(True):
                if _text(node) == label and not any(_text(child) == label for child in node.find_all(True)):
                    nodes.append(node)
        return _Locator(self.page, nodes)

    async def inner_text(self):
        assert len(self.nodes) == 1
        return _text(self.nodes[0])

    async def inner_html(self):
        assert len(self.nodes) == 1
        return self.nodes[0].decode_contents()

    async def all_inner_texts(self):
        return [_text(node) for node in self.nodes]

    async def get_attribute(self, key):
        assert len(self.nodes) == 1
        value = self.nodes[0].get(key)
        return " ".join(value) if isinstance(value, list) else value

    async def is_visible(self):
        return _visible(self.nodes[0])

    async def is_enabled(self):
        return not self.nodes[0].has_attr("disabled") and self.nodes[0].get("aria-disabled") != "true"

    async def evaluate(self, expression):
        node = self.nodes[0]
        return node.get("role") or {"a": "link", "button": "button", "input": "button"}.get(node.name, node.name)

    async def click(self):
        assert self.page.manager.lock.locked(), "Entire inspection must hold the shared browser lock"
        node = self.nodes[0]
        action = node.get("data-action")
        self.page.manager.clicks.append((action, _text(node)))
        assert action in ("detail", "next"), "Inspection must never click a write or invoice control"
        if action == "detail":
            url = node.get("data-url") or DETAIL_URL
            self.page.url = url
            self.page.html = self.page.manager.details.get(url, DETAIL)
        else:
            if not self.page.manager.repeat_page:
                self.page.index += 1
            self.page.html = self.page.manager.listings[self.page.index]


class _Page:
    def __init__(self, manager):
        self.manager = manager
        self.url = PLATFORM_URLS["jd"]
        self.html = "<html><body>尚未读取</body></html>"
        self.index = 0

    def locator(self, selector):
        soup = BeautifulSoup(self.html, "html.parser")
        return _Locator(self, soup.select(selector))

    async def goto(self, url, **kwargs):
        assert self.manager.lock.locked()
        self.manager.navigations.append(url)
        self.url = url
        if url in PLATFORM_URLS.values():
            self.index = 0
            self.html = self.manager.listings[0]
        else:
            self.html = self.manager.details.get(url, DETAIL)

    async def wait_for_function(self, expression, *, arg, **kwargs):
        assert self.manager.lock.locked()
        if isinstance(arg, str):
            ids = ORDER_RE.findall(await self.locator("body").inner_text())
            if not ids or any(ident != arg for ident in ids):
                raise TimeoutError("Fictional page has no uniquely labelled target")
        elif isinstance(arg, dict):
            current = await self.locator(arg["selector"]).all_inner_texts()
            if current == arg["previous"]:
                raise TimeoutError("Fictional page did not advance")


class _Manager(BrowserManager):
    def __init__(self, root, *, listings=None, details=None, scope_verified=True, first_verified=True,
                 recognized=True, repeat_page=False, repeated_ids=False):
        super().__init__(root)
        self.listings = listings or [list_html(NUMBER)]
        self.details = details or {DETAIL_URL: DETAIL}
        self.page = _Page(self)
        self.scope_verified, self.first_verified = scope_verified, first_verified
        self.recognized, self.repeat_page, self.repeated_ids = recognized, repeat_page, repeated_ids
        self.clicks, self.navigations, self.scope_calls = [], [], []
        self.diagnostics = 0

    async def _page(self, platform):
        assert self.lock.locked()
        return self.page

    async def _check(self, page, platform):
        assert self.lock.locked()
        if not official_url(page.url, platform):
            raise BrowserAttention("页面离开对应平台官方域名")
        text = await page.locator("body").inner_text()
        if "安全验证" in text:
            raise BrowserAttention("页面需要人工安全验证")
        return text

    async def _wait_orders_ready(self, page, platform, **kwargs):
        assert self.lock.locked()
        return True

    async def _prepare_scan_scope(self, page, platform, start, end):
        assert self.lock.locked()
        self.scope_calls.append((platform, start, end))
        return {"verified": self.scope_verified, "kind": "year", "year": start.year}

    async def _verify_first_page(self, page, platform):
        assert self.lock.locked()
        return self.first_verified

    async def _next_page_control(self, page, platform):
        assert self.lock.locked()
        root = BeautifulSoup('<button data-action="next">下一页</button>', "html.parser")
        return {"recognized": self.recognized, "current_page": page.index + 1,
                "total_pages": len(self.listings), "disabled": page.index == len(self.listings) - 1,
                "next": _Locator(page, [root.button])}

    async def _activate(self, page, locator):
        assert self.lock.locked()
        await locator.click()
        return page

    async def _diagnostic(self, page, platform):
        assert self.lock.locked()
        self.diagnostics += 1
        path = self.data_dir / "fictional-structure.html"
        path.write_text("<p>Fictional private diagnostic structure only</p>")
        return "脱敏 DOM 诊断：" + str(path)


def _order(**patch):
    return {"platform": "jd", "order_no": NUMBER, "paid_at": "2026-05-01", **patch}


def test_official_detail_is_read_only_and_structured(tmp_path):
    manager = _Manager(tmp_path)
    result = asyncio.run(inspect_order(manager, _order(order_url=DETAIL_URL), FICTIONAL_PROFILE))
    assert result["observed_order_url"] == DETAIL_URL
    assert result["observations"]["transaction"] == {"trade_status": "completed", "refund_status": "none", "amount_fen": 8800}
    assert result["observations"]["invoice"]["invoice_status"] == "issued"
    assert result["observations"]["buyer"]["invoice_buyer"] == "虚构测试企业"
    assert manager.navigations == [DETAIL_URL] and manager.clicks == []
    assert result["diagnostic_path"].endswith("fictional-structure.html")
    labels = [item["label"] for item in result["invoice_controls"]]
    assert "申请开票" in labels and "换开发票" in labels and "下载发票" not in labels
    assert next(item for item in result["invoice_controls"] if item["label"] == "换开发票")["disabled"] is True
    assert next(item for item in result["invoice_controls"] if item["label"] == "查看电子发票")["href"] == ""
    assert next(item for item in result["invoice_controls"] if item["label"] == "查看发票")["role"] == "link"
    for private_value in ("虚构收件人", "假想街道", "13900000000", "推荐商品", "打印纸测试款"):
        assert private_value not in str(result)
    assert not manager.lock.locked()


def test_no_href_uses_verified_2026_range_and_exact_detail_only(tmp_path):
    manager = _Manager(tmp_path, listings=[list_html(OTHER), list_html(NUMBER)])
    result = asyncio.run(inspect_order(manager, _order(), FICTIONAL_PROFILE))
    assert result["observed_order_url"] == DETAIL_URL
    assert manager.navigations == [PLATFORM_URLS["jd"]]
    assert manager.scope_calls[0][1].year == manager.scope_calls[0][2].year == 2026
    assert manager.clicks == [("next", "下一页"), ("detail", "订单详情")]


@pytest.mark.parametrize("settings,message", [
    ({"listings": [list_html(NUMBER, NUMBER)]}, "重复"),
    ({"listings": [list_html(NUMBER, duplicate_details=True)]}, "唯一"),
    ({"listings": [list_html(OTHER)]}, "未找到"),
    ({"scope_verified": False}, "历史范围"),
    ({"first_verified": False}, "第一页"),
    ({"listings": [list_html(OTHER), list_html(NUMBER)], "recognized": False}, "分页"),
])
def test_missing_ambiguous_or_unverified_list_stops(tmp_path, settings, message):
    manager = _Manager(tmp_path, **settings)
    with pytest.raises(BrowserAttention, match=message):
        asyncio.run(inspect_order(manager, _order(), FICTIONAL_PROFILE))
    assert manager.clicks == []
    assert manager.diagnostics == 1
    assert not manager.lock.locked()


def test_repeated_order_identity_across_pages_stops(tmp_path):
    manager = _Manager(tmp_path, listings=[list_html(OTHER), list_html(OTHER).replace("订单详情", "订单详情</button><small>分页视图2</small><button hidden>"), list_html(NUMBER)])
    with pytest.raises(BrowserAttention, match="身份仍重复"):
        asyncio.run(inspect_order(manager, _order(), FICTIONAL_PROFILE))
    assert manager.clicks == [("next", "下一页")]


def test_pagination_does_not_retry_if_page_did_not_advance(tmp_path):
    manager = _Manager(tmp_path, listings=[list_html(OTHER), list_html(NUMBER)], repeat_page=True)
    with pytest.raises(BrowserAttention, match="未确认订单列表变化"):
        asyncio.run(inspect_order(manager, _order(), FICTIONAL_PROFILE))
    assert manager.clicks == [("next", "下一页")]


@pytest.mark.parametrize("text", [
    f"推荐商品编号 {NUMBER} 交易成功 实付款：￥88.00",
    f"订单号：{OTHER} 交易成功 实付款：￥88.00",
    f"订单号：{NUMBER} 订单编号：{OTHER} 交易成功 实付款：￥88.00",
])
def test_detail_requires_exact_labelled_target(tmp_path, text):
    manager = _Manager(tmp_path, details={DETAIL_URL: f"<html><body>{text}</body></html>"})
    with pytest.raises(BrowserAttention, match="订单号标签"):
        asyncio.run(inspect_order(manager, _order(order_url=DETAIL_URL), FICTIONAL_PROFILE))
    assert manager.clicks == []


def test_detail_redirect_outside_official_domain_is_stopped(tmp_path):
    external = "https://invalid.example/detail"
    manager = _Manager(tmp_path, listings=[list_html(NUMBER, detail_url=external)])
    with pytest.raises(BrowserAttention, match="离开对应平台官方"):
        asyncio.run(inspect_order(manager, _order(), FICTIONAL_PROFILE))
    assert manager.clicks == [("detail", "订单详情")]


@pytest.mark.parametrize("patch", [
    {"order_url": "https://jd.com.invalid.example/detail"},
    {"platform": "unknown"}, {"order_no": "not-a-number"},
])
def test_invalid_target_never_navigates(tmp_path, patch):
    manager = _Manager(tmp_path)
    with pytest.raises(BrowserAttention):
        asyncio.run(inspect_order(manager, _order(**patch), FICTIONAL_PROFILE))
    assert manager.navigations == manager.clicks == []


def test_hidden_duplicate_card_does_not_create_a_second_target(tmp_path):
    html = list_html(NUMBER).replace("</body>", f'<div hidden class="orderCard-hidden">订单号：{NUMBER}<button>订单详情</button></div></body>')
    manager = _Manager(tmp_path, listings=[html])
    result = asyncio.run(inspect_order(manager, _order(), FICTIONAL_PROFILE))
    assert result["observed_order_url"] == DETAIL_URL
    assert manager.clicks == [("detail", "订单详情")]


def test_missing_fields_return_partial_observations_and_visible_custom_controls(tmp_path):
    html = f"""<html><body><h1>订单号：{NUMBER}</h1>
      <div class="trade-button">发票信息</div>
      <div class="button-fictional" aria-disabled="true">申请开票</div>
      <span class="button-fictional" hidden>下载发票</span>
      <p>无实付金额或交易状态字段</p></body></html>"""
    manager = _Manager(tmp_path, details={DETAIL_URL: html})
    result = asyncio.run(inspect_order(manager, _order(order_url=DETAIL_URL), FICTIONAL_PROFILE))
    assert result["complete"] is False
    assert result["observations"]["transaction"]["amount_fen"] is None
    assert result["observations"]["transaction"]["trade_status"] == "unknown"
    assert "transaction.amount_fen" in result["missing_fields"]
    assert "transaction.trade_status" in result["missing_fields"]
    assert [item["label"] for item in result["invoice_controls"]] == ["发票信息", "申请开票"]
    assert result["invoice_controls"][1]["disabled"] is True
    assert result["diagnostic_path"] and manager.clicks == []


@pytest.mark.parametrize("platform,url", [
    ("jd", DETAIL_URL),
    ("taobao", f"https://trade.taobao.com/trade/detail/trade_order_detail.htm?bizOrderId={NUMBER}"),
])
def test_both_platforms_use_the_order_year_when_no_detail_href(tmp_path, platform, url):
    manager = _Manager(tmp_path, listings=[list_html(NUMBER, detail_url=url)], details={url: DETAIL})
    result = asyncio.run(inspect_order(manager, _order(platform=platform, paid_at="2025-12-01"), FICTIONAL_PROFILE))
    assert result["observed_order_url"] == url
    assert manager.scope_calls[0][1].isoformat() == "2025-01-01"
    assert manager.scope_calls[0][2].isoformat() == "2025-12-31"
    assert manager.navigations == [PLATFORM_URLS[platform]]


def test_external_href_on_detail_control_is_never_clicked(tmp_path):
    html = list_html(NUMBER).replace(
        f'<button data-action="detail" data-url="{DETAIL_URL}">订单详情</button>',
        '<a href="https://invalid.example/detail" data-action="detail">订单详情</a>',
    )
    manager = _Manager(tmp_path, listings=[html])
    with pytest.raises(BrowserAttention, match="不是对应平台官方"):
        asyncio.run(inspect_order(manager, _order(), FICTIONAL_PROFILE))
    assert manager.clicks == []


def test_missing_detail_entry_does_not_fall_back_to_invoice_entry(tmp_path):
    manager = _Manager(tmp_path, listings=[list_html(NUMBER).replace("订单详情", "申请开票")])
    with pytest.raises(BrowserAttention, match="唯一的“订单详情”"):
        asyncio.run(inspect_order(manager, _order(), FICTIONAL_PROFILE))
    assert manager.clicks == []


def test_history_search_stops_at_100_pages(tmp_path):
    listings = [list_html(str(330000000000 + i)) for i in range(100)] + [list_html(NUMBER)]
    manager = _Manager(tmp_path, listings=listings)
    with pytest.raises(BrowserAttention, match="100 页上限"):
        asyncio.run(inspect_order(manager, _order(), FICTIONAL_PROFILE))
    assert len(manager.clicks) == 99
    assert all(kind == "next" for kind, _ in manager.clicks)


def test_concurrent_inspections_keep_the_shared_browser_serialized(tmp_path):
    manager = _Manager(tmp_path)

    async def together():
        return await asyncio.gather(
            inspect_order(manager, _order(order_url=DETAIL_URL), FICTIONAL_PROFILE),
            inspect_order(manager, _order(order_url=DETAIL_URL), FICTIONAL_PROFILE),
        )

    results = asyncio.run(together())
    assert len(results) == 2 and manager.diagnostics == 2
    assert not manager.lock.locked()
