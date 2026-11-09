"""Fictional offline seller-contact UI. Never opens a real shopping account."""
import asyncio
import json
import re

import pytest
from bs4 import BeautifulSoup, Tag

from invoice_agent.browser import BrowserAttention, BrowserManager
from invoice_agent.merchant import (MerchantChatPageReference, inspect_merchant_chat,
                                    open_merchant_chat, send_merchant_message)
from invoice_agent.parsers import ORDER_RE, official_url


NUMBER = "123456789012"
SHOP = "虚构音频配件店"
ORDER_URL = f"https://trade.taobao.com/trade/detail/trade_order_detail.htm?bizOrderId={NUMBER}"
CHAT_URL = "https://im.taobao.com/fictional-chat"
OBSERVED_CHAT_URL = "https://market.m.taobao.com/app/im/chat/index.html?uid=fictional-seller-001&type=fictional&spm=fictional"
DETAIL = f"""<html><body><p>订单号：{NUMBER}</p>
<div class="shopInfo--fake trade-order-detail-shop-info"><div class="shopInfoLeft--fake">
<img class="shopInfoIcon--fake"><span><a class="shopInfoName--fake" href="https://shop123456.taobao.com/category.htm">{SHOP}</a></span>
<span class="shopInfoWW--fake"><div class="ww--fake" data-action="contact"></div></span>
</div><button data-action="snapshot">查看交易快照</button></div>
<footer><a href="https://wangwang.taobao.com/" data-action="footer">阿里旺旺</a></footer></body></html>"""
CHAT = f"""<html><body><h2>{SHOP}</h2><textarea id="fictional-editor" class="fictional-input" placeholder="输入消息">既有私人草稿不得返回</textarea>
<button id="fictional-send" data-action="send">发送</button><aside>其他会话：私人聊天全文绝不可返回</aside>
<input type="password" value="绝不返回密码" hidden><input name="csrf" value="绝不返回秘密" hidden>
<button data-action="pay">付款</button><button data-action="follow">关注</button></body></html>"""


def visible(node):
    return not any(isinstance(item, Tag) and (item.has_attr("hidden") or item.get("aria-hidden") == "true"
        or "display:none" in item.get("style", "").replace(" ", "")) for item in [node, *node.parents])


def text(node):
    clone = BeautifulSoup(str(node), "html.parser")
    for item in clone.select("[hidden],[aria-hidden='true']"):
        item.decompose()
    return clone.get_text(" ", strip=True)


def descriptor(node):
    def states(item):
        cls = " ".join(item.get("class", []))
        flags = {key: item.get(key) == "true" for key in ("aria-selected", "aria-current", "data-selected", "data-current", "data-self", "data-is-self") if item.has_attr(key)}
        flags.update({"class$self": bool(re.search(r"(?:^|[^a-z])(?:self|outgoing|mine)(?:[^a-z]|$)", cls, re.I)),
                      "class$selected": bool(re.search("selected|active", cls, re.I)), "class$current": bool(re.search("current", cls, re.I))})
        return flags
    ancestors = []
    for parent in node.parents:
        if not isinstance(parent, Tag) or parent.name == "[document]":
            continue
        ancestors.append({"tag": parent.name, "class_name": " ".join(parent.get("class", [])), "role": parent.get("role", ""), "states": states(parent)})
        if len(ancestors) >= 5:
            break
    return {"tag": node.name, "id": node.get("id", ""), "class_name": " ".join(node.get("class", [])),
            "role": node.get("role", ""), "type": node.get("type", ""),
            "contenteditable": node.get("contenteditable") == "true", "placeholder": node.get("placeholder", ""),
            "ancestors": ancestors, "states": states(node)}


class Locator:
    def __init__(self, page, nodes):
        self.page, self.nodes = page, list(nodes)

    def locator(self, selector):
        return Locator(self.page, [child for node in self.nodes for child in node.select(selector)])

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return Locator(self.page, [self.nodes[index]])

    async def is_visible(self):
        return visible(self.nodes[0])

    async def is_enabled(self):
        return not self.nodes[0].has_attr("disabled") and self.nodes[0].get("aria-disabled") != "true"

    async def inner_text(self):
        return text(self.nodes[0])

    async def get_attribute(self, key):
        value = self.nodes[0].get(key)
        return " ".join(value) if isinstance(value, list) else value

    async def evaluate(self, expression):
        assert "merchant-shop-marker" in expression
        return descriptor(self.nodes[0])

    async def click(self):
        assert self.page.manager.lock.locked()
        action = self.nodes[0].get("data-action")
        self.page.manager.clicks.append(action)
        assert action == "contact", "Only the bound seller icon may be clicked in this first-stage adapter"

    async def fill(self, *_):
        raise AssertionError("No editor has been adapted; never fill one")


class Page:
    def __init__(self, manager, url, html):
        self.manager, self.url, self.html = manager, url, html
        self.closed = False
        self.main_frame = object()
        self.frames = [self.main_frame]
        self.dom_reads = 0
        self.wait_timeouts = []

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        assert self.manager.lock.locked()
        self.manager.navigations.append(url)
        self.url, self.html = url, self.manager.before if url == ORDER_URL else self.manager.after

    def locator(self, selector):
        self.dom_reads += 1
        return Locator(self, BeautifulSoup(self.html, "html.parser").select(selector))

    def get_by_text(self, expected, exact=False):
        soup = BeautifulSoup(self.html, "html.parser")
        return Locator(self, [node for node in soup.find_all(True) if text(node) == expected
                              and not any(text(child) == expected for child in node.find_all(True))])

    async def wait_for_function(self, expression, *, arg, **kwargs):
        assert self.manager.lock.locked()
        self.wait_timeouts.append(kwargs.get("timeout"))
        if isinstance(arg, list):
            soup = BeautifulSoup(self.html, "html.parser")
            if any(visible(node) for node in soup.select("textarea,input:not([type=hidden]):not([type=password]),[contenteditable=true]")) or any(notice in text(soup) for notice in arg):
                return
            raise TimeoutError("Fictional page has no ready controls or fixed notice")
        ids = ORDER_RE.findall(await self.locator("body").inner_text())
        if set(ids) != {arg}:
            raise TimeoutError("Wrong fictional target")

    async def wait_for_load_state(self, state, **kwargs):
        assert self.manager.lock.locked()
        self.wait_timeouts.append(kwargs.get("timeout"))

    async def evaluate(self, expression):
        soup = BeautifulSoup(self.html, "html.parser")
        if "merchant-message-structures" in expression:
            result, seen = [], set()
            for node in soup.select("[class]"):
                cls = " ".join(node.get("class", []))
                if not visible(node) or not re.search("message|msg|chat", cls, re.I):
                    continue
                item = {"tag": node.name, "class_name": cls, "parent_class": " ".join(node.parent.get("class", [])),
                        "children_tag_classes": [{"tag": child.name, "class_name": " ".join(child.get("class", []))} for child in node.find_all(recursive=False)[:12]],
                        "contains_editor": any(visible(child) for child in node.select("pre.edit[contenteditable=true]")),
                        "contains_send_button": any(visible(child) for child in node.select("button.send-btn")), "states": descriptor(node)["states"]}
                key = json.dumps(item, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    result.append(item)
                if len(result) >= 100:
                    break
            return result
        assert "merchant-control-metadata" in expression
        inputs = [descriptor(node) for node in soup.select("textarea,input:not([type=hidden]):not([type=password]),[contenteditable=true]") if visible(node)]
        labels = {"发送", "发送(S)", "发送（S）", "发送消息", "取消", "关闭"}
        buttons = [descriptor(node) | {"label": text(node)} for node in soup.select("button,[role=button],input[type=button],input[type=submit]") if visible(node) and text(node) in labels]
        return {"inputs": inputs, "buttons": buttons}


class Manager(BrowserManager):
    def __init__(self, root, *, before=DETAIL, after=CHAT, after_url=CHAT_URL, same_page=False):
        super().__init__(root)
        self.before, self.after, self.after_url = before, after, after_url
        self.page = Page(self, ORDER_URL, before)
        self.chat_page = self.page if same_page else Page(self, after_url, after)
        self.same_page = same_page
        self.clicks, self.navigations = [], []
        self.diagnostic_markup = ""
        self._context = Context(self, [self.page, self.chat_page])
        self.ensure_calls = 0

    async def _ensure(self):
        assert self.lock.locked()
        self.ensure_calls += 1
        if self._context is None:
            self._context = Context(self, [])

    async def _page(self, platform):
        assert self.lock.locked()
        return self.page

    async def _check(self, page, platform):
        assert self.lock.locked()
        if not official_url(page.url, platform):
            raise BrowserAttention("页面离开官方域名")
        return await page.locator("body").inner_text()

    async def _activate(self, page, contact):
        assert self.lock.locked()
        await contact.click()
        if self.same_page:
            self.page.url, self.page.html = self.after_url, self.after
        return self.chat_page

    async def _diagnostic(self, page, platform):
        assert self.lock.locked()
        self.diagnostic_markup = await page.evaluate("read-only private diagnostic")
        path = self.data_dir / "fictional-merchant-metadata.html"
        path.write_text(self.diagnostic_markup)
        return "脱敏 DOM 诊断：" + str(path)


class Context:
    def __init__(self, manager, pages):
        self.manager, self.pages = manager, pages

    async def new_page(self):
        page = Page(self.manager, "about:blank", "<html><body></body></html>")
        self.pages.append(page)
        return page


class Frame(Page):
    def __init__(self, owner, url, html):
        super().__init__(owner.manager, url, html)
        self.page = owner

    async def frame_element(self):
        node = BeautifulSoup('<iframe class="fictional-im-frame" id="fictional-frame"></iframe>', "html.parser").iframe
        return Locator(self.page, [node])


def order(**patch):
    return {"id": "taobao-" + NUMBER, "platform": "taobao", "order_no": NUMBER, "shop": SHOP,
            "company_purchase": True, "scope_excluded": False, "order_url": ORDER_URL, **patch}


def test_open_uses_only_the_bound_current_seller_icon(tmp_path):
    manager = Manager(tmp_path)
    result = asyncio.run(open_merchant_chat(manager, order()))
    assert manager.navigations == [ORDER_URL] and manager.clicks == ["contact"]
    assert result["status"] == "opened_unverified" and result["new_page"] and result["url_changed"]
    assert result["shop_verified"] and result["order_verified"]
    assert not result["recipient_verified"] and not result["send_attempted"]
    assert result["current_url"] == CHAT_URL and result["diagnostic_path"]
    assert result["metadata"]["inputs"][0]["id"] == "fictional-editor"
    assert result["metadata"]["buttons"][0]["label"] == "发送"
    assert result["metadata"]["shop_text_markers"][0]["label"] == SHOP
    for sensitive in ("既有私人草稿不得返回", "私人聊天全文绝不可返回", "绝不返回密码", "绝不返回秘密"):
        assert sensitive not in str(result) and sensitive not in manager.diagnostic_markup
    assert not manager.lock.locked()


def test_same_tab_navigation_still_does_not_claim_recipient_verification(tmp_path):
    manager = Manager(tmp_path, same_page=True)
    result = asyncio.run(open_merchant_chat(manager, order()))
    assert not result["new_page"] and result["url_changed"]
    assert not result["recipient_verified"] and manager.clicks == ["contact"]


@pytest.mark.parametrize("before", [
    DETAIL.replace(SHOP, "另一家虚构店铺"),
    DETAIL.replace('class="trade-order-detail-shop-info"', 'class="unknown-shop-block"').replace('class="shopInfo--fake trade-order-detail-shop-info"', 'class="unknown-shop-block"'),
    DETAIL.replace('<div class="ww--fake" data-action="contact"></div>', ''),
    DETAIL.replace('<div class="ww--fake" data-action="contact"></div>', '<div class="ww--fake" data-action="contact"></div><div class="ww--fake" data-action="contact"></div>'),
    DETAIL.replace(NUMBER, "223456789012"),
    DETAIL.replace('href="https://shop123456.taobao.com/category.htm"', 'href="https://invalid.example/shop"'),
])
def test_wrong_or_ambiguous_order_shop_or_contact_never_clicks(tmp_path, before):
    manager = Manager(tmp_path, before=before)
    with pytest.raises(BrowserAttention):
        asyncio.run(open_merchant_chat(manager, order()))
    assert manager.clicks == []


@pytest.mark.parametrize("patch", [
    {"company_purchase": False}, {"scope_excluded": True}, {"platform": "jd"},
    {"order_url": ""}, {"order_url": "https://taobao.com.invalid.example/"}, {"order_no": "invalid"},
])
def test_invalid_or_out_of_scope_order_never_navigates(tmp_path, patch):
    manager = Manager(tmp_path)
    with pytest.raises(BrowserAttention):
        asyncio.run(open_merchant_chat(manager, order(**patch)))
    assert manager.navigations == manager.clicks == []


@pytest.mark.parametrize("url", ["aliim:sendmsg?touid=fictional", "https://invalid.example/im", "http://im.taobao.com/chat"])
def test_native_or_nonofficial_chat_transport_is_not_used(tmp_path, url):
    manager = Manager(tmp_path, after_url=url)
    with pytest.raises(BrowserAttention, match="官方 HTTPS") as caught:
        asyncio.run(open_merchant_chat(manager, order()))
    assert caught.value.safe_to_retry is False
    assert manager.clicks == ["contact"]
    assert url not in str(caught.value)


def test_footer_client_download_is_never_a_fallback(tmp_path):
    manager = Manager(tmp_path, before=DETAIL.replace('<span class="shopInfoWW--fake"><div class="ww--fake" data-action="contact"></div></span>', ''))
    with pytest.raises(BrowserAttention):
        asyncio.run(open_merchant_chat(manager, order()))
    assert manager.clicks == []


def test_send_is_closed_until_actual_chat_dom_is_verified(tmp_path):
    manager = Manager(tmp_path)
    callbacks = []
    with pytest.raises(BrowserAttention, match="尚未适配"):
        asyncio.run(send_merchant_message(manager, order(), "精确虚构批准草稿", lambda: callbacks.append(True)))
    assert manager.navigations == manager.clicks == callbacks == []


def test_inspect_reuses_unique_exact_owned_chat_url_without_other_dom_reads(tmp_path):
    manager = Manager(tmp_path, after_url=OBSERVED_CHAT_URL)
    result = asyncio.run(inspect_merchant_chat(manager, order(), OBSERVED_CHAT_URL))
    assert result["status"] == "inspected_unverified" and not result["recipient_verified"]
    assert not result["opened_saved_url"] and not result["send_attempted"]
    assert manager.navigations == manager.clicks == [] and manager.page.dom_reads == 0
    assert manager.chat_page.dom_reads > 0
    assert result["waiting"]["input_or_fixed_notice_observed"]
    assert all(timeout <= 15000 for timeout in manager.chat_page.wait_timeouts)
    assert isinstance(manager._merchant_chat_pages[order()["id"]], MerchantChatPageReference)


def test_inspect_reopens_only_the_saved_url_if_no_matching_owned_page(tmp_path):
    manager = Manager(tmp_path)
    result = asyncio.run(inspect_merchant_chat(manager, order(), OBSERVED_CHAT_URL))
    assert result["opened_saved_url"] and manager.ensure_calls == 1
    assert manager.navigations == [OBSERVED_CHAT_URL] and manager.clicks == []
    assert not result["recipient_verified"] and not result["send_attempted"]
    result2 = asyncio.run(inspect_merchant_chat(manager, order(), OBSERVED_CHAT_URL))
    assert result2["cache_reused"]
    assert manager.navigations == [OBSERVED_CHAT_URL]  # no second navigation


def test_inspect_does_not_choose_between_duplicate_exact_url_pages(tmp_path):
    manager = Manager(tmp_path, after_url=OBSERVED_CHAT_URL)
    duplicate = Page(manager, OBSERVED_CHAT_URL, CHAT)
    manager._context.pages.append(duplicate)
    with pytest.raises(BrowserAttention, match="多个完整地址相同"):
        asyncio.run(inspect_merchant_chat(manager, order(), OBSERVED_CHAT_URL))
    assert manager.chat_page.dom_reads == duplicate.dom_reads == manager.page.dom_reads == 0
    assert manager.navigations == manager.clicks == []


@pytest.mark.parametrize("url", [
    "https://im.taobao.com/app/im/chat/index.html?uid=fictional",  # unobserved host
    "https://market.m.taobao.com/other/path?uid=fictional",
    "https://market.m.taobao.com/app/im/chat/index.html",
    "https://market.m.taobao.com/app/im/chat/index.html?uid=",
    "https://market.m.taobao.com/app/im/chat/index.html?uid=fictional&uid=second",
    "https://market.m.taobao.com/app/im/chat/index.html?uid=%20",
    "http://market.m.taobao.com/app/im/chat/index.html?uid=fictional",
    "https://market.m.taobao.com.invalid.example/app/im/chat/index.html?uid=fictional",
])
def test_inspect_invalid_saved_endpoint_never_reads_or_navigates(tmp_path, url):
    manager = Manager(tmp_path)
    with pytest.raises(BrowserAttention):
        asyncio.run(inspect_merchant_chat(manager, order(), url))
    assert manager.navigations == manager.clicks == []
    assert manager.page.dom_reads == manager.chat_page.dom_reads == 0


def test_inspect_reports_only_fixed_notices_and_no_surrounding_private_text(tmp_path):
    body = "<html><body><div>请先登录 扫码登录 加载失败 私人会话内容绝不可返回</div></body></html>"
    manager = Manager(tmp_path, after=body, after_url=OBSERVED_CHAT_URL)
    result = asyncio.run(inspect_merchant_chat(manager, order(), OBSERVED_CHAT_URL))
    assert set(result["metadata"]["notices"]) == {"请先登录", "扫码登录", "加载失败"}
    assert "私人会话内容绝不可返回" not in str(result) and "私人会话内容绝不可返回" not in manager.diagnostic_markup
    assert result["waiting"]["input_or_fixed_notice_observed"]
    assert not result["recipient_verified"] and manager.clicks == []


def test_inspect_reads_only_official_frames_belonging_to_the_selected_chat_page(tmp_path):
    manager = Manager(tmp_path, after="<html><body>空容器</body></html>", after_url=OBSERVED_CHAT_URL)
    owned = Frame(manager.chat_page, "https://market.m.taobao.com/fictional/frame", CHAT)
    external = Frame(manager.chat_page, "https://invalid.example/frame", "<textarea>外部私密文本不可读取</textarea>")
    other_page = Page(manager, OBSERVED_CHAT_URL + "&other=1", "<html><body>其他页</body></html>")
    unrelated = Frame(other_page, "https://market.m.taobao.com/fictional/other", "<textarea>其他页面文本不可读取</textarea>")
    manager.chat_page.frames.extend([owned, external, unrelated])
    result = asyncio.run(inspect_merchant_chat(manager, order(), OBSERVED_CHAT_URL))
    assert result["waiting"]["input_or_fixed_notice_observed"]
    assert result["frames"][0]["read"] and result["frames"][0]["host"] == "market.m.taobao.com"
    assert result["frames"][0]["metadata"]["inputs"][0]["id"] == "fictional-editor"
    assert not result["frames"][1]["read"] and not result["frames"][2]["read"]
    assert external.dom_reads == unrelated.dom_reads == 0
    for secret in ("外部私密文本不可读取", "其他页面文本不可读取", "既有私人草稿不得返回"):
        assert secret not in str(result)
    assert manager.navigations == manager.clicks == []


def test_typed_cached_popup_is_preferred_to_the_platform_order_page(tmp_path):
    manager = Manager(tmp_path, after_url=OBSERVED_CHAT_URL)
    manager._merchant_chat_pages = {order()["id"]: MerchantChatPageReference(manager.chat_page, NUMBER, SHOP, OBSERVED_CHAT_URL)}
    manager._context.pages = [manager.page]  # cached exact popup is still the known owned reference
    result = asyncio.run(inspect_merchant_chat(manager, order(), OBSERVED_CHAT_URL))
    assert result["cache_reused"] and manager.chat_page.dom_reads > 0 and manager.page.dom_reads == 0
    assert manager.navigations == manager.clicks == []


FICTIONAL_CORE = f"""<html><body><div class="chat-shell">
<aside class="conversation-list"><div class="conversation-item selected" aria-selected="true"><div class="name">{SHOP}</div></div></aside>
<section class="chat-window"><header class="chat-header"><div class="name">{SHOP}</div></header>
<section class="message-area"><div class="message-item self" data-self="true" id="私有消息ID不可返回"><span class="message-text">私有发送正文不可返回</span></div>
<div class="message-item incoming"><span class="message-text">私有收到正文不可返回</span></div>
<div class="message-item incoming"><span class="message-text">另一条私有正文不可返回</span></div></section>
<div class="goods-card"><div class="goods-shop">{SHOP}</div></div>
<section class="chat-editor"><pre class="edit" contenteditable="true">私有输入草稿不可返回</pre>
<button class="next-btn next-small next-btn-primary send-btn">发送</button></section></section></div></body></html>"""


def test_core_frame_metadata_distinguishes_header_sidebar_and_message_shapes_without_text(tmp_path):
    manager = Manager(tmp_path, after="<html><body>frame container</body></html>", after_url=OBSERVED_CHAT_URL)
    core = Frame(manager.chat_page, "https://market.m.taobao.com/app/im/chat-core/index.html", FICTIONAL_CORE)
    manager.chat_page.frames.append(core)
    result = asyncio.run(inspect_merchant_chat(manager, order(), OBSERVED_CHAT_URL))
    metadata = result["frames"][0]["metadata"]
    assert [item["class_name"] for item in metadata["shop_text_markers"]] == ["name", "name", "goods-shop"]
    ancestor_classes = [[parent["class_name"] for parent in item["ancestors"]] for item in metadata["shop_text_markers"]]
    assert ancestor_classes[0][0] == "conversation-item selected"
    assert ancestor_classes[1][0] == "chat-header"
    assert metadata["shop_text_markers"][0]["ancestors"][0]["states"]["aria-selected"] is True
    assert all(len(item["ancestors"]) <= 5 for group in ("inputs", "buttons", "shop_text_markers") for item in metadata[group])
    structures = metadata["message_structures"]
    assert len(structures) <= 100
    assert any(item["class_name"] == "chat-editor" and item["contains_editor"] and item["contains_send_button"] for item in structures)
    assert any(item["class_name"] == "message-item self" and item["states"]["class$self"] is True and item["states"]["data-self"] is True for item in structures)
    assert len([item for item in structures if item["class_name"] == "message-item incoming"]) == 1
    for forbidden in ("私有消息ID不可返回", "私有发送正文不可返回", "私有收到正文不可返回", "另一条私有正文不可返回", "私有输入草稿不可返回"):
        assert forbidden not in str(result) and forbidden not in manager.diagnostic_markup
    assert manager.navigations == manager.clicks == []
    assert not result["recipient_verified"] and not result["send_attempted"]


def test_message_structure_collection_is_limited_to_observed_official_core_path(tmp_path):
    manager = Manager(tmp_path, after_url=OBSERVED_CHAT_URL)
    frame = Frame(manager.chat_page, "https://market.m.taobao.com/other/official-frame", FICTIONAL_CORE)
    manager.chat_page.frames.append(frame)
    result = asyncio.run(inspect_merchant_chat(manager, order(), OBSERVED_CHAT_URL))
    assert "message_structures" not in result["frames"][0]["metadata"]


def test_message_structure_metadata_is_deduplicated_and_bounded(tmp_path):
    body = "<html><body>" + "".join(f'<div class="chat-shape-{i}"><span>不返回正文{i}</span></div>' for i in range(140)) + "</body></html>"
    manager = Manager(tmp_path, after_url=OBSERVED_CHAT_URL)
    manager.chat_page.frames.append(Frame(manager.chat_page, "https://market.m.taobao.com/app/im/chat-core/index.html", body))
    result = asyncio.run(inspect_merchant_chat(manager, order(), OBSERVED_CHAT_URL))
    structures = result["frames"][0]["metadata"]["message_structures"]
    assert len(structures) == 100
    assert "不返回正文" not in str(result)
