"""Narrow seller-chat navigation and an explicit send boundary.

Only the currently observed order-detail seller block is supported for opening.
No native protocol, guessed chat URL, private API, footer client-download link,
or uncalibrated message editor is used as a fallback.
"""
from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlparse

from .browser import BrowserAttention
from .inspection import _wait_exact_detail
from .parsers import clean, official_url


_SHOP_BLOCK = "div.trade-order-detail-shop-info"
_SHOP_NAME = "a[class^='shopInfoName--']"
_CONTACT = "span[class^='shopInfoWW--'] > div[class^='ww--']"
_SENSITIVE = re.compile(r"token|cookie|session|secret|password|credential|authorization|csrf", re.I)
_CHAT_HOST = "market.m.taobao.com"
_CHAT_PATH = "/app/im/chat/index.html"
_CHAT_CORE_PATH = "/app/im/chat-core/index.html"
_STATE_FLAGS = {"aria-selected", "aria-current", "data-selected", "data-current", "data-self", "data-is-self",
                "class$self", "class$selected", "class$current"}
_NOTICES = (
    "请先登录", "请登录", "扫码登录", "扫描二维码", "登录淘宝", "账号密码登录", "短信登录",
    "正在加载", "加载中", "连接中", "加载失败", "连接失败", "网络异常", "网络错误",
    "页面不存在", "页面已失效", "系统繁忙", "服务异常", "暂不支持", "不支持当前浏览器",
    "请使用手机淘宝", "请在淘宝客户端打开", "下载千牛", "下载旺旺", "下载阿里旺旺", "下载客户端", "打开客户端",
)


@dataclass(frozen=True)
class MerchantChatPageReference:
    page: Any
    order_no: str
    shop: str
    observed_chat_url: str


def _chat_key(order):
    return order.get("id") or "taobao-" + str(order["order_no"])


def _validate_chat_url(value):
    if not isinstance(value, str) or not official_url(value, "taobao"):
        raise BrowserAttention("已观察的聊天地址不是淘宝/天猫官方 HTTPS 地址，未读取或重开。")
    parsed = urlparse(value)
    uid = [content for key, content in parse_qsl(parsed.query, keep_blank_values=True) if key == "uid"]
    if parsed.hostname != _CHAT_HOST or parsed.path != _CHAT_PATH or len(uid) != 1 or not uid[0].strip():
        raise BrowserAttention("聊天地址不符合已观察的官方入口路径或唯一非空 uid，未猜测或修改地址。")


def _remember_chat_page(manager, order, page, observed_url):
    _validate_chat_url(observed_url)
    cache = getattr(manager, "_merchant_chat_pages", None)
    if not isinstance(cache, dict):
        cache = {}
        manager._merchant_chat_pages = cache
    cache[_chat_key(order)] = MerchantChatPageReference(page, str(order["order_no"]), order["shop"], observed_url)


def _safe_states(value):
    return {name: flag for name, flag in (value.items() if isinstance(value, dict) else [])
            if name in _STATE_FLAGS and isinstance(flag, bool)}


def _safe_ancestors(value):
    result = []
    for ancestor in (value if isinstance(value, list) else [])[:5]:
        if not isinstance(ancestor, dict):
            continue
        item = {}
        for key in ("tag", "class_name", "role"):
            content = str(ancestor.get(key) or "")
            if len(content) <= 180 and not _SENSITIVE.search(content):
                item[key] = content
        item["states"] = _safe_states(ancestor.get("states"))
        result.append(item)
    return result


def _validate_order(order):
    number, shop = str(order.get("order_no", "")), str(order.get("shop", ""))
    if (order.get("platform") != "taobao" or not re.fullmatch(r"\d{6,40}", number)
            or not shop or shop != shop.strip() or len(shop) > 200
            or order.get("company_purchase") is not True or order.get("scope_excluded")):
        raise BrowserAttention("只能为已确认且未排除的淘宝公司采购联系精确原商家。")
    url = str(order.get("order_url") or "")
    if not url or not official_url(url, "taobao"):
        raise BrowserAttention("缺少已观察到的淘宝/天猫官方订单详情链接，未打开商家聊天。")
    return number, shop, url


async def _visible(locator):
    return [locator.nth(index) for index in range(await locator.count()) if await locator.nth(index).is_visible()]


class _DiagnosticMetadataPage:
    """Read-only facade: generic diagnostics receive metadata, never chat text."""
    def __init__(self, url, markup):
        self.url, self.markup = url, markup

    async def evaluate(self, *_args, **_kwargs):
        return self.markup


async def _metadata_diagnostic(manager, page, metadata):
    # Preserve field/selector facts useful for adapting the UI, but not arbitrary
    # conversations, recipient lists, input values, or account credentials.
    items = []
    for group in ("inputs", "buttons", "shop_text_markers"):
        for item in metadata.get(group, []):
            attributes = []
            for source, target in (("id", "id"), ("class_name", "class"), ("role", "role"),
                                   ("type", "type"), ("placeholder", "placeholder")):
                value = str(item.get(source) or "")
                if value and len(value) <= 180 and not _SENSITIVE.search(value):
                    attributes.append(f'{target}="{html.escape(value, quote=True)}"')
            tag = item.get("tag", "div").lower()
            tag = tag if tag in {"input", "textarea", "div", "span", "button", "a", "h1", "h2", "h3"} else "div"
            # Only already-approved store text and fixed UI command labels exist
            # in metadata. No other visible text is collected by _page_metadata.
            label = str(item.get("label") or "")
            items.append(f"<{tag} {' '.join(attributes)}>{html.escape(label)}</{tag}>")
    markup = "<html><body><h1>商家联系页面，仅控件元数据</h1>" + "".join(items) + "</body></html>"
    try:
        message = await manager._diagnostic(_DiagnosticMetadataPage(page.url, markup), "taobao")
        if isinstance(message, str) and message.startswith("脱敏 DOM 诊断："):
            return message.removeprefix("脱敏 DOM 诊断：").split("（超过", 1)[0]
        return message if isinstance(message, str) and message.startswith("/") else ""
    except Exception:
        return ""


async def _page_metadata(page, shop):
    metadata = await page.evaluate(r"""() => { // merchant-control-metadata
      const visible=el=>el.getClientRects().length && !el.hidden && getComputedStyle(el).visibility!=='hidden';
      const states=el=>{const result={};for(const name of ['aria-selected','aria-current','data-selected','data-current','data-self','data-is-self']) {
        if(el.hasAttribute(name)) result[name]=el.getAttribute(name)==='true';}
        const cls=typeof el.className==='string'?el.className:'';
        result['class$self']=/(?:^|[^a-z])(?:self|outgoing|mine)(?:[^a-z]|$)/i.test(cls);
        result['class$selected']=/selected|active/i.test(cls);result['class$current']=/current/i.test(cls);return result;};
      const ancestors=el=>{const result=[];let parent=el.parentElement;for(let n=0;parent && n<5;n++,parent=parent.parentElement)
        result.push({tag:parent.tagName.toLowerCase(),class_name:typeof parent.className==='string'?parent.className:'',role:parent.getAttribute('role') || '',states:states(parent)});return result;};
      const descriptor=el=>({tag:el.tagName.toLowerCase(),id:el.id || '',class_name:typeof el.className==='string'?el.className:'',
        role:el.getAttribute('role') || '',type:el.getAttribute('type') || '',contenteditable:el.isContentEditable,
        placeholder:el.getAttribute('placeholder') || '',ancestors:ancestors(el),states:states(el)});
      const inputs=Array.from(document.querySelectorAll('textarea,input:not([type=hidden]):not([type=password]),[contenteditable=true]'))
        .filter(visible).slice(0,80).map(descriptor);
      const allowed=new Set(['发送','发送(S)','发送（S）','发送消息','取消','关闭']);
      const buttons=Array.from(document.querySelectorAll('button,[role=button],input[type=button],input[type=submit]')).filter(visible)
        .filter(el=>allowed.has((el.innerText || el.getAttribute('aria-label') || el.getAttribute('value') || '').trim()))
        .slice(0,40).map(el=>({...descriptor(el),label:(el.innerText || el.getAttribute('aria-label') || el.getAttribute('value') || '').trim()}));
      return {inputs,buttons};
    }""")
    safe = {"inputs": [], "buttons": [], "shop_text_markers": []}
    for group in ("inputs", "buttons"):
        for item in metadata.get(group, []):
            # Bound static selector descriptors; omit suspicious attributes.
            entry = {}
            for key in ("tag", "id", "class_name", "role", "type", "placeholder", "label"):
                value = str(item.get(key) or "")
                if len(value) <= 180 and not _SENSITIVE.search(value):
                    entry[key] = value
            entry["contenteditable"] = bool(item.get("contenteditable"))
            entry["ancestors"] = _safe_ancestors(item.get("ancestors"))
            entry["states"] = _safe_states(item.get("states"))
            safe[group].append(entry)
    for item in await _visible(page.get_by_text(shop, exact=True)):
        descriptor = await item.evaluate(r"""el => { // merchant-shop-marker
          const states=node=>{const result={};for(const name of ['aria-selected','aria-current','data-selected','data-current','data-self','data-is-self'])
            if(node.hasAttribute(name))result[name]=node.getAttribute(name)==='true';
            const cls=typeof node.className==='string'?node.className:'';
            result['class$self']=/(?:^|[^a-z])(?:self|outgoing|mine)(?:[^a-z]|$)/i.test(cls);
            result['class$selected']=/selected|active/i.test(cls);result['class$current']=/current/i.test(cls);return result;};
          const ancestors=[];let parent=el.parentElement;for(let n=0;parent && n<5;n++,parent=parent.parentElement)
            ancestors.push({tag:parent.tagName.toLowerCase(),class_name:typeof parent.className==='string'?parent.className:'',role:parent.getAttribute('role') || '',states:states(parent)});
          return {tag:el.tagName.toLowerCase(),id:el.id || '',class_name:typeof el.className==='string'?el.className:'',
            role:el.getAttribute('role') || '',ancestors,states:states(el)};
        }""")
        marker = {key: str(descriptor.get(key) or "")[:180] for key in ("tag", "id", "class_name", "role")
                  if not _SENSITIVE.search(str(descriptor.get(key) or ""))}
        marker.update(label=shop, ancestors=_safe_ancestors(descriptor.get("ancestors")), states=_safe_states(descriptor.get("states")))
        safe["shop_text_markers"].append(marker)
    # A visible occurrence of a store name is not proof of the chat recipient.
    safe["recipient_verified"] = False
    text = await page.locator("body").inner_text()
    # Return fixed UI phrases only, never the surrounding conversation text.
    safe["notices"] = [notice for notice in _NOTICES if notice in text]
    return safe


async def _message_structures(frame):
    raw = await frame.evaluate(r"""() => { // merchant-message-structures
      const visible=el=>el.getClientRects().length && !el.hidden && getComputedStyle(el).visibility!=='hidden';
      const cls=el=>typeof el?.className==='string'?el.className:'';
      const states=el=>{const result={};for(const name of ['aria-selected','aria-current','data-selected','data-current','data-self','data-is-self'])
        if(el.hasAttribute(name))result[name]=el.getAttribute(name)==='true';
        result['class$self']=/(?:^|[^a-z])(?:self|outgoing|mine)(?:[^a-z]|$)/i.test(cls(el));
        result['class$selected']=/selected|active/i.test(cls(el));result['class$current']=/current/i.test(cls(el));return result;};
      const result=[],seen=new Set();
      for(const el of document.querySelectorAll('[class]')) {
        if(!visible(el) || !/message|msg|chat/i.test(cls(el))) continue;
        const item={tag:el.tagName.toLowerCase(),class_name:cls(el),parent_class:cls(el.parentElement),
          children_tag_classes:Array.from(el.children).slice(0,12).map(child=>({tag:child.tagName.toLowerCase(),class_name:cls(child)})),
          contains_editor:Array.from(el.querySelectorAll('pre.edit[contenteditable=true]')).some(visible),
          contains_send_button:Array.from(el.querySelectorAll('button.send-btn')).some(visible),states:states(el)};
        const signature=JSON.stringify(item);if(seen.has(signature))continue;seen.add(signature);result.push(item);if(result.length>=100)break;
      }return result;
    }""")
    result, seen = [], set()
    for item in (raw if isinstance(raw, list) else [])[:100]:
        entry = {}
        for key in ("tag", "class_name", "parent_class"):
            value = str(item.get(key) or "")
            if len(value) <= 180 and not _SENSITIVE.search(value):
                entry[key] = value
        children = []
        for child in item.get("children_tag_classes", [])[:12]:
            children.append({key: str(child.get(key) or "")[:180] for key in ("tag", "class_name")
                             if not _SENSITIVE.search(str(child.get(key) or ""))})
        entry.update(children_tag_classes=children, contains_editor=bool(item.get("contains_editor")),
                     contains_send_button=bool(item.get("contains_send_button")), states=_safe_states(item.get("states")))
        signature = repr(entry)
        if signature not in seen:
            seen.add(signature)
            result.append(entry)
    return result


async def _open_locked(manager, order):
    number, shop, url = _validate_order(order)
    page, activated = None, False
    try:
        page = await manager._page("taobao")
        await page.goto(url, wait_until="domcontentloaded")
        body, _ = await _wait_exact_detail(manager, page, "taobao", number)
        blocks = await _visible(body.locator(_SHOP_BLOCK))
        if len(blocks) != 1:
            raise BrowserAttention("订单详情没有唯一的已适配店铺信息块，未使用页脚或全页联系入口。")
        names = await _visible(blocks[0].locator(_SHOP_NAME))
        if len(names) != 1 or (await names[0].inner_text()).strip() != shop:
            raise BrowserAttention("当前订单店铺名称与已批准原商家不完全一致，未打开联系入口。")
        shop_href = await names[0].get_attribute("href") or ""
        if shop_href and not official_url(urljoin(page.url, shop_href), "taobao"):
            raise BrowserAttention("已显示的店铺链接不是淘宝/天猫官方地址，未打开联系入口。")
        contacts = await _visible(blocks[0].locator(_CONTACT))
        if len(contacts) != 1 or await manager._control_disabled(contacts[0]):
            raise BrowserAttention("原商家信息块中没有唯一可用的已观察旺旺图标，未点击其他控件。")
        href = await contacts[0].get_attribute("href") or ""
        if href and not official_url(urljoin(page.url, href), "taobao"):
            raise BrowserAttention("联系入口使用未支持的原生协议或非官方地址，未继续。")
        original_page, original_url = page, page.url
        activated = True
        page = await manager._activate(page, contacts[0])
        if not official_url(page.url, "taobao"):
            error = BrowserAttention("联系入口离开淘宝/天猫官方 HTTPS 页面，未使用原生协议或继续发送。")
            error.safe_to_retry = False
            raise error
        await manager._check(page, "taobao")
        metadata = await _page_metadata(page, shop)
        try:
            _remember_chat_page(manager, order, page, page.url)
        except BrowserAttention:
            # Opening can still yield a login/client page. It is not silently
            # cached as an adapted chat endpoint.
            pass
        return page, {"status": "opened_unverified", "current_url": page.url,
                      "new_page": page is not original_page, "url_changed": page.url != original_url,
                      "order_verified": True, "shop_verified": True, "recipient_verified": False,
                      "contact_activated": True, "send_attempted": False,
                      "metadata": metadata, "diagnostic_path": await _metadata_diagnostic(manager, page, metadata),
                      "message": "已从精确订单的原商家图标打开联系入口；聊天收件人和输入结构尚未核验，未填写或发送。"}
    except (Exception, asyncio.CancelledError) as cause:
        metadata, diagnostic = {}, ""
        if page is not None and official_url(page.url, "taobao"):
            try:
                metadata = await _page_metadata(page, shop)
                diagnostic = await _metadata_diagnostic(manager, page, metadata)
            except Exception:
                pass
        error = cause if isinstance(cause, BrowserAttention) else BrowserAttention(
            "联系入口未能确认，未填写或发送；请人工核对页面，勿盲目重试。")
        error.safe_to_retry = not activated
        error.observation = {
            "contact_activated": activated, "send_attempted": False, "recipient_verified": False,
            "current_url": page.url if page is not None and official_url(page.url, "taobao") else "",
            "diagnostic_path": diagnostic, "metadata": metadata,
        }
        if diagnostic:
            error.args = (str(error) + " 脱敏 DOM 诊断：" + diagnostic,)
        if error is cause:
            raise
        raise error from cause


async def open_merchant_chat(manager, order: dict) -> dict:
    """Open the observed seller icon once; return only coarse, private metadata."""
    _validate_order(order)
    async with manager.lock:
        try:
            _, result = await _open_locked(manager, order)
            return result
        except BrowserAttention:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            error = BrowserAttention("商家联系入口操作未能确认；未填写或发送，请人工核对已打开页面，勿盲目重试。")
            error.safe_to_retry = False
            raise error from exc


async def _official_chat_frames(page):
    result = []
    for index, frame in enumerate(getattr(page, "frames", [])):
        if frame is getattr(page, "main_frame", None):
            continue
        belongs = getattr(frame, "page", None) is page
        parsed = urlparse(str(getattr(frame, "url", "")))
        item = {"index": index, "host": parsed.hostname or "", "path": parsed.path,
                "official": bool(belongs and official_url(str(getattr(frame, "url", "")), "taobao")),
                "belongs_to_chat_page": belongs, "class_name": "", "id": ""}
        try:
            element = await frame.frame_element()
            for key, attribute in (("class_name", "class"), ("id", "id")):
                value = str(await element.get_attribute(attribute) or "")
                if len(value) <= 180 and not _SENSITIVE.search(value):
                    item[key] = value
        except Exception:
            pass
        result.append((frame, item))
    return result


async def _wait_chat_loaded(page):
    """At most 15 seconds total, using load/readiness events rather than sleeps."""
    deadline = time.monotonic() + 15
    network_idle = False
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
        network_idle = True
    except Exception:
        pass
    frames = await _official_chat_frames(page)
    targets = [page] + [frame for frame, item in frames if item["official"]]
    remaining = max(1, int((deadline - time.monotonic()) * 1000))
    tasks = {asyncio.create_task(target.wait_for_function(r"""notices => {
        const visible=el=>el.getClientRects().length && !el.hidden && getComputedStyle(el).visibility!=='hidden';
        if (Array.from(document.querySelectorAll('textarea,input:not([type=hidden]):not([type=password]),[contenteditable=true]')).some(visible)) return true;
        const text=document.body?.innerText || '';
        return notices.some(notice=>text.includes(notice));
    }""", arg=list(_NOTICES), timeout=remaining)) for target in targets}
    ready = False
    try:
        pending = tasks
        while pending and time.monotonic() < deadline:
            completed, pending = await asyncio.wait(pending, timeout=max(0, deadline - time.monotonic()), return_when=asyncio.FIRST_COMPLETED)
            if not completed:
                break
            if any(not task.cancelled() and task.exception() is None for task in completed):
                ready = True
                break
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {"network_idle_observed": network_idle, "input_or_fixed_notice_observed": ready,
            "maximum_seconds": 15}


async def inspect_merchant_chat(manager, order: dict, observed_chat_url: str) -> dict:
    """Read only a previously observed exact seller-chat URL and owned frames.

    The caller must obtain this URL from this order's saved contact observation,
    never from arbitrary client input. Zero existing exact pages permits one
    direct reopening of that same saved URL; no contact icon is clicked again.
    """
    number, shop, _ = _validate_order(order)
    _validate_chat_url(observed_chat_url)
    async with manager.lock:
        page, opened, reused = None, False, False
        cache = getattr(manager, "_merchant_chat_pages", None)
        reference = cache.get(_chat_key(order)) if isinstance(cache, dict) else None
        if (isinstance(reference, MerchantChatPageReference) and reference.order_no == number
                and reference.shop == shop and reference.observed_chat_url == observed_chat_url
                and not reference.page.is_closed()):
            page, reused = reference.page, True
        else:
            context = getattr(manager, "_context", None)
            exact = [candidate for candidate in getattr(context, "pages", [])
                     if not candidate.is_closed() and candidate.url == observed_chat_url]
            if len(exact) > 1:
                raise BrowserAttention("本工具中存在多个完整地址相同的聊天页面，未猜测选择。")
            if exact:
                page = exact[0]
            else:
                await manager._ensure()
                page = await manager._context.new_page()
                await page.goto(observed_chat_url, wait_until="domcontentloaded")
                opened = True
            _remember_chat_page(manager, order, page, observed_chat_url)
        try:
            if not official_url(page.url, "taobao"):
                raise BrowserAttention("已观察聊天页面当前离开官方 HTTPS 域名，未继续读取或发送。")
            # A typed cached reference may have redirected to an official login
            # page. Its fixed login notice is useful, but never proves recipient.
            waiting = await _wait_chat_loaded(page)
            if not official_url(page.url, "taobao"):
                raise BrowserAttention("等待期间页面离开官方 HTTPS 域名，未继续读取或发送。")
            metadata = await _page_metadata(page, shop)
            frame_results = []
            for frame, frame_info in await _official_chat_frames(page):
                item = dict(frame_info)
                if item["official"]:
                    try:
                        item["metadata"] = await _page_metadata(frame, shop)
                        if item["host"] == _CHAT_HOST and item["path"] == _CHAT_CORE_PATH:
                            item["metadata"]["message_structures"] = await _message_structures(frame)
                        item["read"] = True
                    except Exception:
                        item["read"] = False
                else:
                    item["read"] = False
                frame_results.append(item)
            return {"status": "inspected_unverified", "current_url": page.url,
                    "observed_chat_url": observed_chat_url, "opened_saved_url": opened, "cache_reused": reused,
                    "recipient_verified": False, "send_attempted": False, "metadata": metadata,
                    "frames": frame_results, "waiting": waiting,
                    "diagnostic_path": await _metadata_diagnostic(manager, page, metadata),
                    "message": "仅读取已观察的原商家聊天页面及其官方框架；未核验聊天收件人，未填写或发送。"}
        except BrowserAttention:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            raise BrowserAttention("已观察聊天页面读取未完成；未再次点击商家入口、填写或发送。") from exc


async def send_merchant_message(manager, order: dict, approved_text: str, before_send) -> dict:
    """Fail closed until actual official web-IM recipient/editor DOM is adapted.

    The current observed facts support opening the order's seller contact only.
    Root will supply the real chat recipient, editor and bubble structure before
    this function is enabled. Fictional fixture selectors are not production
    evidence and must never enable a live send.
    """
    _validate_order(order)
    if not isinstance(approved_text, str) or not approved_text or len(approved_text) > 8000 or "\x00" in approved_text:
        raise BrowserAttention("已批准消息文本无效，未填写或发送。")
    if not callable(before_send):
        raise BrowserAttention("缺少发送前持久记录回调，未填写或发送。")
    raise BrowserAttention("官方网页聊天的收件人、输入框和已发送气泡结构尚未适配，未填写或发送。")
