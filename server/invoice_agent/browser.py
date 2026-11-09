"""Visible, serialized browser workflows using official rendered pages only.

Selectors are deliberately conservative. A layout this adapter cannot identify is
an attention state, never permission to guess or call an undocumented endpoint.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import time
import uuid
import zipfile
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .parsers import (CONTAINERS, PLATFORM_URLS, invoice_detail_dom_observation, invoice_observation,
                      official_url, parse_orders, sanitize_diagnostic_html, transaction_dom_observation)


class BrowserAttention(Exception):
    # apply changes this to False once any application-entry click has begun.
    # Its final-submit block returns uncertain instead of throwing exceptions.
    safe_to_retry = True


SPECIAL_OPTION = r"^(?:增值税(?:电子)?专用发票(?:[（(]电子[）)])?|电子专票|数电专票|(?:数电|电子)发票[（(]增值税专用发票[）)])$"
ORDINARY_OPTION = r"^(?:增值税(?:电子)?普通发票|电子普通发票|普通发票[（(]电子[）)]|数电发票[（(]普通发票[）)])$"
INVOICE_MAX_BYTES = 50 * 1024 * 1024
BROWSER_DOWNLOAD_TIMEOUT_SECONDS = 30.0
JD_INVOICE_HOST = re.compile(r"eicore-invoice-\d{2}\.s3\.cn-north-1\.jdcloud-oss\.com")
DOWNLOAD_ACTION_PATTERN = r"^(?:下载电子发票|下载发票|下载PDF|下载 PDF|下载OFD|下载 OFD|下载XML|下载 XML)$"
VIEW_ACTION_PATTERN = r"^(?:查看发票|查看电子发票|发票详情)$"


def observed_jd_invoice_url(url: str, platform: str) -> bool:
    """Download-only gate. This does not extend platform/form navigation trust.

    JDCloud documents its OSS endpoint/virtual-hosted bucket form in its official
    SDK. The eicore bucket is additionally pinned to the observed invoice-view
    navigation, never accepted from user input or a guessed storage path.
    Sources:
    https://github.com/jd-opensource/velero-plugin-for-jdcloud/blob/main/README.md
    https://github.com/jdcloud-api/jdcloud-sdk-go/blob/master/services/jdaip/models/ModelSpec.go
    """
    try:
        parsed = urlparse(url)
        return bool(platform == "jd" and parsed.scheme == "https" and not parsed.username
                    and not parsed.password and parsed.port in (None, 443) and not parsed.fragment
                    and JD_INVOICE_HOST.fullmatch(parsed.hostname or "")
                    and parsed.path not in ("", "/"))
    except ValueError:
        return False


def invoice_response_extension(headers: dict, status: int) -> str:
    if status != 200:
        raise BrowserAttention("发票原件响应不是成功200（重定向也不会跟随），未归档。")
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    content_type = lowered.get("content-type", "").split(";", 1)[0].strip().lower()
    extensions = {"application/pdf": ".pdf", "application/ofd": ".ofd", "application/vnd.ofd": ".ofd",
                  "application/xml": ".xml", "text/xml": ".xml"}
    if content_type not in extensions:
        raise BrowserAttention("响应内容类型未明确为 PDF/OFD/XML，未读取为发票原件。")
    if "content-length" in lowered:
        try:
            length = int(lowered["content-length"])
        except ValueError as exc:
            raise BrowserAttention("发票响应体积声明无效，未归档。") from exc
        if not 16 <= length <= INVOICE_MAX_BYTES:
            raise BrowserAttention("发票响应为空、过小或超过 50 MiB，未归档。")
    return extensions[content_type]


def save_invoice_bytes(body: bytes, extension: str, order: dict, dest_dir: Path) -> dict:
    if extension not in (".pdf", ".ofd", ".xml") or not 16 <= len(body) <= INVOICE_MAX_BYTES:
        raise BrowserAttention("原件格式或体积无效，未归档。")
    if not re.fullmatch(r"\d{6,40}", str(order.get("order_no", ""))):
        raise BrowserAttention("目标订单号无效，未归档。")
    if extension == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise BrowserAttention("PDF结构校验依赖未就绪，未归档。") from None
        try:
            reader = PdfReader(io.BytesIO(body), strict=True)
            if reader.is_encrypted or len(reader.pages) < 1:
                raise ValueError("Encrypted or page-less document")
        except Exception:
            raise BrowserAttention("PDF结构不完整、损坏或加密，未归档。") from None
    directory = Path(dest_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / f"{order['platform']}-{order['order_no']}-{uuid.uuid4().hex[:12]}{extension}"
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(body)
        validate_invoice_file(path)
        return {"path": str(path)}
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def validate_invoice_file(path: Path) -> None:
    size = path.stat().st_size
    if not 16 <= size <= 50 * 1024 * 1024:
        raise BrowserAttention("下载文件为空、过小或超过 50 MiB，请在官方页面人工检查。")
    with path.open("rb") as stream:
        head = stream.read(1024)
    suffix = path.suffix.lower()
    if suffix == ".pdf" and head.startswith(b"%PDF-"):
        return
    if suffix == ".ofd" and head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("OFD.xml")
                if info.file_size > 1024 * 1024:
                    raise BrowserAttention("OFD 目录信息异常。")
                root = ElementTree.fromstring(archive.read(info))
                if root.tag.split("}")[-1] != "OFD":
                    raise BrowserAttention("下载的 OFD 文件缺少有效目录。")
            return
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
            raise BrowserAttention("下载文件不是有效 OFD。") from exc
    if suffix == ".xml" and head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"<"):
        # No DTD/entity expansion and no browser HTML saved as a fake XML invoice.
        data = path.read_bytes()
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise BrowserAttention("XML 包含外部文档声明，请人工检查。")
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            raise BrowserAttention("下载文件不是有效 XML。") from exc
        name = root.tag.split("}")[-1].lower()
        if name not in {"html", "script"} and re.search(r"invoice|fapiao|发票", name):
            return
    raise BrowserAttention("文件格式未通过 PDF/OFD/发票 XML 检查，已停止归档。")


class BrowserManager:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.lock = asyncio.Lock()
        self._playwright = None
        self._context = None
        self._context_closed = False
        self._pages = {}
        self.last_scan_report = {"pages": 0, "complete": False, "reason": "尚未扫描"}

    async def _ensure(self):
        if self._context is not None:
            try:
                browser = self._context.browser
                closed = self._context_closed or (browser is not None and not browser.is_connected())
            except Exception:
                closed = True
            if not closed:
                return
            self._context = None
            self._pages.clear()
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            profile = self.data_dir / "browser-profile"
            profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            profile.chmod(0o700)
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(profile), channel="chrome", headless=False, accept_downloads=True,
                viewport={"width": 1440, "height": 1000}, locale="zh-CN", timezone_id="Asia/Shanghai",
            )
            self._context_closed = False
            owned_context = self._context

            def context_closed(*_):
                if self._context is owned_context:
                    self._context_closed = True
                    self._pages.clear()

            owned_context.on("close", context_closed)
            self._context.set_default_timeout(10_000)
        except Exception as exc:
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            self._context = None
            raise BrowserAttention("无法打开独立 Chrome。请确认本机已安装 Google Chrome，且此工具的浏览器资料目录未被另一实例占用。") from exc

    async def _page(self, platform):
        if platform not in PLATFORM_URLS:
            raise BrowserAttention("不支持的平台。")
        await self._ensure()
        page = self._pages.get(platform)
        if page is None or page.is_closed():
            page = await self._context.new_page()
            self._pages[platform] = page
        return page

    async def open(self, platform) -> str:
        async with self.lock:
            page = await self._page(platform)
            await page.goto(PLATFORM_URLS[platform], wait_until="domcontentloaded")
            await page.bring_to_front()
            return "已打开独立浏览器，请本人完成登录和验证，再返回扫描。"

    async def check(self, platform) -> dict:
        """Observe an existing page only; never disrupt a QR/login interaction."""
        async with self.lock:
            if platform not in PLATFORM_URLS:
                return {"status": "blocked", "message": "不支持的平台。"}
            page = self._pages.get(platform)
            if page is None or page.is_closed():
                return {"status": "disconnected", "message": "尚未打开此平台的独立浏览器页面。"}
            try:
                text = await self._check(page, platform)
                parsed = parse_orders(await page.content(), platform, page.url)
                if parsed.orders or parsed.empty:
                    return {"status": "ready", "message": "当前页面已显示可识别的订单列表，可以开始扫描。"}
                if parsed.container_count:
                    return {"status": "open", "message": "订单列表已出现，但字段仍在加载或需要适配，尚未确认可扫描。"}
                # The observation deliberately avoids input values, cookies, and
                # account identifiers. A QR/login page may still be transitioning.
                if re.search(r"扫码登录|扫描二维码登录|请先登录|账号密码登录|短信登录", text):
                    return {"status": "needs_login", "message": "当前仍是登录页面，请本人继续完成登录。"}
                return {"status": "open", "message": "页面已打开，尚未确认进入订单列表；请在浏览器完成登录或打开订单页面。"}
            except BrowserAttention as exc:
                return {"status": "needs_login" if "登录" in str(exc) else "blocked", "message": str(exc)}
            except Exception:
                return {"status": "open", "message": "当前页面暂时无法读取，请等待页面完成加载后再检查。"}

    async def _check(self, page, platform):
        if not official_url(page.url, platform):
            try:
                parsed_host = urlparse(page.url).hostname
                host = "无主机名"
                if parsed_host:
                    host = parsed_host.encode("idna").decode("ascii")
                    if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
                        host = "无法识别主机名"
            except (ValueError, UnicodeError):
                host = "无法识别主机名"
            raise BrowserAttention(f"页面离开了对应平台官方 HTTPS 域名，已停止。（hostname：{host}）")
        text = await page.locator("body").inner_text()
        if re.search(r"滑动验证|请完成验证|拖动滑块|访问受限|访问过于频繁|安全验证|拼图验证", text):
            raise BrowserAttention("平台要求人工安全验证，请在独立浏览器完成后继续。")
        if re.search(r"passport\.jd\.com|login\.taobao\.com|/login(?:[./?]|$)", page.url, re.I):
            raise BrowserAttention("请在独立浏览器由本人登录平台，完成后再继续。")
        if await page.locator("input[type='password']:visible").count():
            raise BrowserAttention("平台登录尚未完成，请本人在浏览器操作。")
        return text

    async def _wait_orders_ready(self, page, platform, timeout_ms=15_000):
        """One bounded render wait, without navigation or a platform retry."""
        try:
            await page.wait_for_function("""selector => {
                const text = document.body?.innerText || '';
                if (/passport\\.jd\\.com|login\\.taobao\\.com|\\/login(?:[./?]|$)/i.test(location.href)) return true;
                if (/扫码登录|请先登录|账号密码登录|滑动验证|请完成验证|拖动滑块|访问受限|访问过于频繁|安全验证|拼图验证/.test(text)) return true;
                if (/暂无订单|没有找到符合条件的订单|没有符合条件的订单|您还没有订单|未找到相关订单/.test(text)) return true;
                return Array.from(document.querySelectorAll(selector)).some(el => {
                    const t = el.innerText || '';
                    const id = el.id || '';
                    return (/shopOrderContainer_\\d{6,40}/.test(id) || /(?:订单号|订单编号|订单号码)\\s*[:：]?\\s*\\d{6,40}/.test(t))
                        && /20\\d{2}[-年/.]\\d{1,2}[-月/.]\\d{1,2}/.test(t)
                        && /[¥￥]\\s*[\\d,]+|\\d+\\.\\d{2}/.test(t);
                });
            }""", arg=CONTAINERS[platform], timeout=min(timeout_ms, 15_000))
            return True
        except Exception:
            return False

    async def _diagnostic(self, page, platform):
        """Persist only a sanitized, size-bounded snapshot; never raw page HTML."""
        try:
            # Remove nodes hidden by the actual computed stylesheet from a
            # detached clone; this does not mutate the user's platform page.
            html = await page.evaluate("""() => {
                const root = document.documentElement.cloneNode(true);
                const actual = [document.documentElement, ...document.documentElement.querySelectorAll('*')];
                const copied = [root, ...root.querySelectorAll('*')];
                for (let i = actual.length - 1; i > 0; i--) {
                    const style = getComputedStyle(actual[i]);
                    if (actual[i].hidden || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') copied[i]?.remove();
                }
                return root.outerHTML;
            }""")
            sanitized, truncated = sanitize_diagnostic_html(html, platform, page.url)
            folder = self.data_dir / "diagnostics"
            if folder.is_symlink():
                raise ValueError("Diagnostic directory must not be a symlink")
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.chmod(0o700)
            path = folder / f"orders-{platform}-{uuid.uuid4().hex}.html"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(sanitized)
            return f"脱敏 DOM 诊断：{path}" + ("（超过 1 MiB，已截断）" if truncated else "")
        except Exception:
            return "本地脱敏 DOM 诊断未能保存"

    async def _visible(self, locator):
        return [locator.nth(i) for i in range(await locator.count()) if await locator.nth(i).is_visible()]

    async def _control_disabled(self, button, parent=None):
        if not await button.is_enabled() or await button.get_attribute("aria-disabled") == "true":
            return True
        for item in (button, parent):
            if item is not None and (await item.get_attribute("aria-disabled") == "true"
                                     or re.search(r"(?:^|[-_\s])disabled(?:$|[-_\s])", await item.get_attribute("class") or "", re.I)):
                return True
        return False

    async def _settle_order_page(self, page, platform, timeout_ms=15_000):
        """Bounded network-idle and stable-DOM readback after a data selection."""
        deadline = time.monotonic() + min(timeout_ms, 15_000) / 1000
        try:
            await page.wait_for_load_state("networkidle", timeout=max(1, min(8000, int((deadline - time.monotonic()) * 1000))))
        except Exception:
            pass
        last, stable_since = None, time.monotonic()
        script = """selector => JSON.stringify({
            orders:Array.from(document.querySelectorAll(selector)).map(el=>el.innerText),
            pagination:Array.from(document.querySelectorAll(\"ul.ant-pagination,div[class^='pagination-']\")).map(el=>el.innerText),
            filters:Array.from(document.querySelectorAll(\"div[class^='selectContainer-']\")).map(el=>el.innerText)
        })"""
        while time.monotonic() < deadline:
            try:
                current = await page.evaluate(script, CONTAINERS[platform])
                if current != last:
                    last, stable_since = current, time.monotonic()
                elif time.monotonic() - stable_since >= 0.6:
                    remaining = int((deadline - time.monotonic()) * 1000)
                    if remaining <= 0:
                        break
                    await page.wait_for_load_state("networkidle", timeout=remaining)
                    after = await page.evaluate(script, CONTAINERS[platform])
                    if after == current:
                        return {"settled": True, "network_idle": True,
                                "dom_hash": hashlib.sha256(after.encode()).hexdigest()}
                    last, stable_since = after, time.monotonic()
                await asyncio.sleep(min(0.15, max(0, deadline - time.monotonic())))
            except Exception:
                break
        return {"settled": False, "network_idle": False}

    async def _next_page_control(self, page, platform):
        """Prefer the single observed bottom paginator; accessible names vary."""
        root_selector = "ul.ant-pagination" if platform == "taobao" else "div[class^='pagination-']"
        roots = await self._visible(page.locator(root_selector))
        if len(roots) > 1:
            raise BrowserAttention("找到多个底部分页区域，未猜测点击对象。")
        result = {"next": None, "root": roots[0] if roots else None, "disabled": False,
                  "recognized": False, "first_page": False, "current_page": None, "total_pages": None}
        if roots:
            root = roots[0]
            selector = "li.ant-pagination-next > button.ant-pagination-item-link" if platform == "taobao" else "button[class^='nextBtn-']"
            buttons = await self._visible(root.locator(selector))
            if len(buttons) > 1:
                raise BrowserAttention("底部分页有多个下一页按钮，未点击。")
            if buttons:
                button = buttons[0]
                parent = button.locator("xpath=..") if platform == "taobao" else None
                result.update(next=button, disabled=await self._control_disabled(button, parent), recognized=True)
                metadata = await root.evaluate("""(root, platform) => {
                    const number = el => { const t=(el?.getAttribute('title') || el?.innerText || '').trim();return /^\\d+$/.test(t)?Number(t):null; };
                    const active = platform==='taobao' ? root.querySelector('li.ant-pagination-item-active')
                        : Array.from(root.querySelectorAll(\"button,a,div[class^='pageItem-']\")).find(el => /^\\d+$/.test((el.innerText||'').trim())
                          && (el.getAttribute('aria-current')==='page' || /active|selected|current/i.test(el.className)));
                    const explicit = (root.innerText||'').match(/共\\s*(\\d+)\\s*页/);
                    const items = platform==='taobao' ? Array.from(root.querySelectorAll('li.ant-pagination-item')).map(number).filter(n=>n!==null) : [];
                    return {current_page:number(active),total_pages:explicit?Number(explicit[1]):(items.length?Math.max(...items):null)};
                }""", platform)
                result.update(metadata)
                previous_selector = "li.ant-pagination-prev > button" if platform == "taobao" else "button[class^='prevBtn-']"
                previous = await self._visible(root.locator(previous_selector))
                previous_disabled = False
                if len(previous) == 1:
                    prev_parent = previous[0].locator("xpath=..") if platform == "taobao" else None
                    previous_disabled = await self._control_disabled(previous[0], prev_parent)
                result["first_page"] = result["current_page"] == 1 or (result["current_page"] is None and previous_disabled)
                return result
        # Older accessible controls remain usable, but absence of a recognized
        # paginator never establishes the end of the historical order range.
        fallback = await self._named(page, r"^下一页\s*(?:[›»>]*)$", roles=("link", "button"))
        if len(fallback) > 1:
            raise BrowserAttention("下一页入口存在歧义，未点击。")
        if fallback:
            result.update(next=fallback[0], disabled=await self._control_disabled(fallback[0]))
        return result

    async def _prepare_scan_scope(self, page, platform, start, end):
        if platform == "taobao":
            tabs = await self._visible(page.locator("span[class*='tabItem--']").filter(has_text=re.compile(r"^所有订单$")))
            if len(tabs) == 1:
                if "tabItemHighlight--" not in (await tabs[0].get_attribute("class") or ""):
                    await tabs[0].click()
                    try:
                        await page.wait_for_function("() => Array.from(document.querySelectorAll(\"span[class*='tabItem--'][class*='tabItemHighlight--']\")).some(el => (el.innerText||'').trim()==='所有订单')", timeout=8000)
                    except Exception:
                        pass
                selected = "tabItemHighlight--" in (await tabs[0].get_attribute("class") or "")
                return {"verified": selected, "kind": "all_history", "label": "所有订单",
                        "reason": "已回读所有订单选中标记" if selected else "所有订单选中状态尚未确认"}
            return {"verified": False, "kind": "all_history", "label": "", "reason": "未唯一识别所有订单选中标记"}
        if start.year != end.year:
            raise BrowserAttention("京东历史采集一次只支持同年范围，请按年份分别扫描。" + await self._diagnostic(page, platform))
        year = start.year
        aliases = [rf"{year}(?:年(?:全部|全年)?订单|年|年内订单)?"]
        if year == datetime.now(ZoneInfo("Asia/Shanghai")).year:
            aliases.append(r"(?:今年|本年)(?:内)?(?:全部|全年)?(?:订单)?")
        year_pattern = "^(?:" + "|".join(aliases) + ")$"
        date_pattern = re.compile(r"^(?:近[3三]个月(?:内)?订单|最近[3三]个月(?:内)?订单|\d{4}(?:年(?:全部|全年)?订单|年|年内订单)?|(?:今年|本年)(?:内)?(?:全部|全年)?(?:订单)?)$")
        candidates = []
        for control in await self._visible(page.locator("div[class^='selectContainer-']")):
            label = (await control.inner_text()).strip()
            if date_pattern.fullmatch(label):
                candidates.append(control)
        if len(candidates) != 1:
            raise BrowserAttention("无法唯一识别京东订单时间筛选，未沿用默认近3个月范围。" + await self._diagnostic(page, platform))
        control = candidates[0]
        if not re.fullmatch(year_pattern, (await control.inner_text()).strip()):
            await control.click()
            try:
                await page.wait_for_function("pattern => Array.from(document.querySelectorAll('[role=option],li,div,span,button')).some(el => el.getClientRects().length && new RegExp(pattern).test((el.innerText||'').trim()))", arg=year_pattern, timeout=5000)
            except Exception:
                pass
            options = await self._visible(page.get_by_role("option", name=re.compile(year_pattern)))
            if not options:
                options = await self._visible(page.get_by_text(re.compile(year_pattern), exact=True))
            if len(options) != 1:
                raise BrowserAttention(f"京东未显示唯一覆盖 {year} 全年的选项，已停止。" + await self._diagnostic(page, platform))
            await options[0].click()
            try:
                await page.wait_for_function("pattern => Array.from(document.querySelectorAll(\"div[class^='selectContainer-']\")).some(el => el.getClientRects().length && new RegExp(pattern).test((el.innerText||'').trim()))", arg=year_pattern, timeout=8000)
            except Exception:
                pass
        selected = (await control.inner_text()).strip()
        if not re.fullmatch(year_pattern, selected):
            raise BrowserAttention("京东年度筛选选择后未能回读确认，已停止。" + await self._diagnostic(page, platform))
        settled = await self._settle_order_page(page, platform)
        if not settled["settled"] or not re.fullmatch(year_pattern, (await control.inner_text()).strip()):
            raise BrowserAttention("京东年度选择后数据请求或页面尚未稳定，未使用旧近3个月页面继续采集。" + await self._diagnostic(page, platform))
        return {"verified": True, "kind": "year", "label": selected, "year": year,
                "start_date": f"{year}-01-01", "end_date": f"{year}-12-31", "reason": "已回读覆盖全年选项及稳定数据页面",
                "settle_evidence": settled}

    async def _verify_first_page(self, page, platform):
        state = await self._next_page_control(page, platform)
        if state["first_page"]:
            return True
        if state["root"] is not None:
            if platform == "taobao":
                first = await self._visible(state["root"].locator("li.ant-pagination-item[title='1'] a"))
            else:
                first = await self._visible(state["root"].locator("div[class^='pageItem-']").filter(has_text=re.compile(r"^1$")))
                if not first:
                    first = await self._named(state["root"], r"^1$", roles=("button", "link"))
            if len(first) == 1:
                previous = await page.locator(CONTAINERS[platform]).all_inner_texts()
                await first[0].click()
                try:
                    await page.wait_for_function("state => JSON.stringify(Array.from(document.querySelectorAll(state.selector)).map(e => e.innerText)) !== JSON.stringify(state.previous)",
                                                 arg={"selector": CONTAINERS[platform], "previous": previous}, timeout=8000)
                except Exception:
                    return False
                await self._wait_orders_ready(page, platform)
                if platform == "jd" and not (await self._settle_order_page(page, platform))["settled"]:
                    return False
                state = await self._next_page_control(page, platform)
        return state["first_page"]

    async def scan(self, platform, start_date, end_date, max_pages):
        start = date.fromisoformat(str(start_date))
        end = date.fromisoformat(str(end_date))
        if platform not in PLATFORM_URLS or start > end or not 1 <= max_pages <= 100:
            raise BrowserAttention("扫描平台、日期或页数无效。")
        async with self.lock:
            report = {"platform": platform, "start_date": start.isoformat(), "end_date": end.isoformat(),
                      "pages": 0, "complete": False, "reason": "扫描未完成", "page_report": [],
                      "range_filter": {"verified": False}, "first_page_verified": False,
                      "terminal_reached": False, "observed_oldest_date": None, "observed_newest_date": None,
                      "unresolved_records": []}
            self.last_scan_report = report
            page = await self._page(platform)
            await page.goto(PLATFORM_URLS[platform], wait_until="domcontentloaded")
            await self._wait_orders_ready(page, platform)
            await self._check(page, platform)
            try:
                scope = await self._prepare_scan_scope(page, platform, start, end)
                report["range_filter"] = scope
                await self._wait_orders_ready(page, platform)
                report["first_page_verified"] = await self._verify_first_page(page, platform)
            except BrowserAttention as exc:
                report["reason"] = str(exc)
                raise
            orders, seen_pages, seen_ids = {}, set(), set()
            all_dates, issues = [], []
            if not scope["verified"]:
                issues.append(scope.get("reason", "所选订单范围尚未核实"))
            expected_total = None
            explicit_empty = False
            for count in range(1, max_pages + 1):
                try:
                    await self._wait_orders_ready(page, platform)
                    await self._check(page, platform)
                    html = await page.content()
                except BrowserAttention as exc:
                    if not seen_ids:
                        report["reason"] = str(exc)
                        raise
                    issues.append(str(exc))
                    break
                except Exception as exc:
                    if seen_ids:
                        issues.append("后续订单页面暂时无法读取，已保留先前订单")
                        break
                    report["reason"] = "订单页面暂时无法读取"
                    raise BrowserAttention("订单页面暂时无法读取，请确认独立浏览器仍然打开。") from exc
                parsed = parse_orders(html, platform, page.url)
                ids = tuple(o["order_no"] for o in parsed.orders)
                dates = [o["paid_at"] for o in parsed.orders]
                report["page_report"].append({"page": count, "first_date": dates[0] if dates else None,
                    "last_date": dates[-1] if dates else None, "parsed": len(parsed.orders),
                    "rejected": len(parsed.rejected), "ids_hash": hashlib.sha256("\n".join(ids).encode()).hexdigest()})
                report["pages"] = count
                report["page_report"][-1]["total_pages"] = None
                report["unresolved_records"].extend(record | {"page": count} for record in parsed.unresolved_records)
                if not parsed.recognized or (parsed.container_count and not parsed.orders):
                    diagnostic = await self._diagnostic(page, platform)
                    message = ("订单页等待加载后仍无法识别" if not parsed.recognized else "订单字段无法唯一识别") + "；" + diagnostic
                    report["reason"] = message
                    if seen_ids:
                        issues.append(message)
                        break
                    raise BrowserAttention(message)
                if ids in seen_pages and not parsed.empty:
                    issues.append("分页后仍是同一批订单，已停止，未把重复页面计作完整覆盖")
                    break
                seen_pages.add(ids)
                issues.extend(parsed.rejected)
                if parsed.rejected:
                    issues.append(await self._diagnostic(page, platform))
                overlapping = seen_ids.intersection(ids)
                if overlapping:
                    issues.append(f"第 {count} 页有 {len(overlapping)} 条跨页重复订单，覆盖需要核对")
                for order in parsed.orders:
                    if order["order_no"] in seen_ids:
                        continue
                    seen_ids.add(order["order_no"])
                    all_dates.append(order["paid_at"])
                    if scope.get("kind") == "year" and not order["paid_at"].startswith(str(scope["year"]) + "-"):
                        issues.append(f"第 {count} 页出现所选年份之外的订单，时间筛选覆盖未获确认")
                    if start.isoformat() <= order["paid_at"] <= end.isoformat():
                        orders[order["order_no"]] = order
                if parsed.empty:
                    explicit_empty = True
                    report["terminal_reached"] = True
                    if count == 1 and scope["verified"]:
                        report["first_page_verified"] = True
                    break
                try:
                    control = await self._next_page_control(page, platform)
                except BrowserAttention as exc:
                    issues.append(str(exc) + "；" + await self._diagnostic(page, platform))
                    break
                except Exception:
                    issues.append("分页区域暂时无法读取，已保留先前订单；" + await self._diagnostic(page, platform))
                    break
                current = control["current_page"]
                report["page_report"][-1]["total_pages"] = control["total_pages"]
                if current is not None:
                    report["page_report"][-1]["platform_page"] = current
                    for record in report["unresolved_records"]:
                        if record["page"] == count:
                            record["platform_page"] = current
                    if current != count:
                        issues.append(f"平台页码 {current} 与连续采集页号 {count} 不符")
                if count == 1:
                    expected_total = control["total_pages"]
                    report["expected_total_pages"] = expected_total
                if control["disabled"] and control["recognized"]:
                    report["terminal_reached"] = True
                    break
                if control["next"] is None or control["disabled"]:
                    issues.append("未识别可靠的下一页/末页证据，未声称历史范围完整")
                    break
                if count == max_pages:
                    issues.append(f"达到 {max_pages} 页上限，后续订单尚未扫描")
                    break
                try:
                    href = await control["next"].get_attribute("href")
                except Exception:
                    issues.append("分页链接暂时无法读取，已保留先前订单")
                    break
                if href and not href.startswith(("#", "javascript:")):
                    from urllib.parse import urljoin
                    if not official_url(urljoin(page.url, href), platform):
                        issues.append("分页链接不是官方平台地址")
                        break
                try:
                    previous = await page.locator(CONTAINERS[platform]).all_inner_texts()
                    await control["next"].click()
                    await page.wait_for_function("state => JSON.stringify(Array.from(document.querySelectorAll(state.selector)).map(e => e.innerText)) !== JSON.stringify(state.previous)",
                                                 arg={"selector": CONTAINERS[platform], "previous": previous}, timeout=8000)
                    await page.wait_for_load_state("domcontentloaded")
                    if platform == "jd" and not (await self._settle_order_page(page, platform))["settled"]:
                        issues.append("京东翻页后数据请求或页面尚未稳定，已保留此前结果")
                        break
                except Exception:
                    issues.append("分页未能确认完成，已保留先前读取的订单")
                    break
            if all_dates:
                report["observed_oldest_date"], report["observed_newest_date"] = min(all_dates), max(all_dates)
            totals = {row["total_pages"] for row in report["page_report"] if row["total_pages"] is not None}
            report["observed_total_pages"] = sorted(totals)
            if len(totals) > 1:
                issues.append("采集期间各页显示的总页数不一致，未把旧筛选结果当作全年完整结果")
            if (platform == "jd" and report["terminal_reached"] and report["first_page_verified"]
                    and report["page_report"] and not explicit_empty):
                try:
                    first_ok = await self._verify_first_page(page, platform)
                    await self._check(page, platform)
                    first = parse_orders(await page.content(), platform, page.url)
                    first_control = await self._next_page_control(page, platform)
                    first_hash = hashlib.sha256("\n".join(o["order_no"] for o in first.orders).encode()).hexdigest()
                    labels = [(await item.inner_text()).strip() for item in await self._visible(page.locator("div[class^='selectContainer-']"))]
                    scope_label_verified = labels.count(scope["label"]) == 1
                    verified = bool(first_ok and scope_label_verified and not first.rejected
                                    and first_hash == report["page_report"][0]["ids_hash"]
                                    and (expected_total is None or first_control["total_pages"] == expected_total))
                    report["first_page_recheck"] = {"verified": verified, "ids_hash": first_hash,
                        "current_page": first_control["current_page"], "total_pages": first_control["total_pages"],
                        "scope_label_verified": scope_label_verified}
                    if not verified:
                        issues.append("末页后回读第一页的订单哈希、年度标签或总页数不一致，首屏年度归属仍需核对")
                except Exception:
                    report["first_page_recheck"] = {"verified": False}
                    issues.append("末页后未能回读第一页核对所选年度首屏，未确认覆盖完整")
            if not report["first_page_verified"]:
                issues.append("未取得从第一页开始的明确证据")
            if report["terminal_reached"] and expected_total is not None and not explicit_empty and report["pages"] != expected_total:
                issues.append(f"平台显示 {expected_total} 页，但实际采集 {report['pages']} 页，需核对覆盖")
            scope_covers_request = bool(scope["verified"] and (
                scope.get("kind") == "all_history"
                or (scope.get("kind") == "year" and scope.get("year") == start.year == end.year)))
            # A new account may have no orders near the requested lower bound.
            # Verified scope plus a complete first-to-terminal traversal proves
            # coverage; actual order dates remain evidence, not a prerequisite.
            range_verified = bool(scope_covers_request and report["first_page_verified"] and report["terminal_reached"])
            report["range_dates_verified"] = range_verified
            report["range_coverage_verified"] = range_verified
            if not scope_covers_request:
                issues.append("尚未核验订单筛选覆盖所选日期范围")
            report["issues"] = issues
            report["complete"] = bool(range_verified and not issues)
            if report["complete"]:
                report["reason"] = (f"已核对{scope['label']}，从第一页连续采集到末页，共 {report['pages']} 页且无解析遗漏；"
                                    f"实际最早/最新日期为 {report['observed_oldest_date'] or '无订单'}/{report['observed_newest_date'] or '无订单'}，已按所选日期本地过滤。")
            else:
                report["reason"] = "；".join(issues[:8]) or "采集未完成"
                if len(issues) > 8:
                    report["reason"] += f"；另有 {len(issues) - 8} 项见逐页覆盖记录"
            return list(orders.values())

    async def _named(self, scope, pattern, roles=("button", "link")):
        matches = []
        for role in roles:
            locator = scope.get_by_role(role, name=re.compile(pattern))
            for index in range(await locator.count()):
                item = locator.nth(index)
                if await item.is_visible():
                    matches.append(item)
        return matches

    async def _scope(self, page, order):
        number = str(order["order_no"])
        exact_number = re.compile(rf"(?<!\d){re.escape(number)}(?!\d)")
        containers = page.locator(CONTAINERS[order["platform"]])
        matched = []
        for index in range(await containers.count()):
            item = containers.nth(index)
            text = await item.inner_text()
            identifier = await item.get_attribute("id") or ""
            if exact_number.search(text) or identifier == "shopOrderContainer_" + number:
                matched.append(item)
        if len(matched) == 1:
            return matched[0]
        if matched:
            raise BrowserAttention("目标订单重复出现，无法唯一定位。")
        body = page.locator("body")
        if not exact_number.search(await body.inner_text()):
            raise BrowserAttention("当前页面没有确切显示目标订单号，已停止。")
        # Detail/form pages may use entirely different layouts. Require a labelled
        # target number; an incidental number in a recommendation is insufficient.
        text = await body.inner_text()
        if not re.search(rf"(?:订单号|订单编号|订单号码)\s*[:：]?\s*{re.escape(number)}(?!\d)", text):
            raise BrowserAttention("订单详情无法确认订单号标签，需人工处理。")
        labelled_ids = set(re.findall(r"(?:订单号|订单编号|订单号码)\s*[:：]?\s*(\d{6,40})(?!\d)", text))
        if labelled_ids != {number}:
            raise BrowserAttention("页面包含多个订单，无法唯一确认申请对象。")
        return body

    async def _navigate(self, order, invoice=False):
        platform = order["platform"]
        url = order.get("invoice_url") if invoice else order.get("order_url")
        url = url or order.get("order_url")
        if not url or not official_url(url, platform):
            raise BrowserAttention("此订单没有已观察到的官方详情链接，请先重新扫描或在平台核对。")
        page = await self._page(platform)
        await page.goto(url, wait_until="domcontentloaded")
        await self._check(page, platform)
        return page, await self._scope(page, order)

    async def _activate(self, page, locator):
        # A platform can open a new tab or navigate in place. Observe both.
        old_pages = set(self._context.pages)
        await locator.click()
        await page.wait_for_timeout(500)
        new_pages = [p for p in self._context.pages if p not in old_pages]
        if len(new_pages) > 1:
            raise BrowserAttention("平台打开了多个页面，请人工核对。")
        target = new_pages[0] if new_pages else page
        await target.wait_for_load_state("domcontentloaded")
        return target

    async def _field(self, scope, names, required=True):
        pattern = re.compile(names, re.I)
        matched = []
        for item in await scope.locator("input:not([type='hidden']):not([type='password']):not([type='radio']):not([type='checkbox']), textarea").element_handles():
            if not await item.is_visible():
                continue
            descriptor = await item.evaluate("el => [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), ...Array.from(el.labels || []).map(l => l.innerText)].filter(Boolean).join('|')")
            if pattern.search(descriptor):
                matched.append(item)
        if len(matched) == 1:
            return matched[0]
        if matched or required:
            raise BrowserAttention("开票表单字段缺失或存在多个匹配，已停止自动填写。")
        return None

    async def _choose_type(self, scope, invoice_type):
        pattern = SPECIAL_OPTION if invoice_type == "special" else ORDINARY_OPTION
        radios = await self._named(scope, pattern, roles=("radio",))
        if len(radios) != 1:
            raise BrowserAttention("当前页面无法唯一确认目标发票类型。未自动降为普通发票，请人工核对。")
        radio = radios[0]
        if not await radio.is_enabled():
            raise BrowserAttention("目标发票类型当前不可用，请人工核对资质或联系商家。")
        await radio.check()
        if not await radio.is_checked():
            raise BrowserAttention("未能回读确认目标发票类型，已停止。")
        return radio

    async def _no_available_special(self, scope):
        options = await self._named(scope, SPECIAL_OPTION, roles=("radio",))
        for option in options:
            if await option.is_enabled() and await option.get_attribute("aria-disabled") != "true":
                raise BrowserAttention("当前页面已有可用专票选项，已停止普通票申请；请按专票优先重新核对。")

    async def _verify_transaction(self, scope, order):
        current = transaction_dom_observation(await scope.inner_html(), order["platform"])
        if current["refund_status"] != "none":
            raise BrowserAttention("当前页面未明确确认无退款，或已出现退款/售后处理，未提交。")
        if current["trade_status"] not in ("paid", "completed"):
            raise BrowserAttention("当前交易状态未知、未付款或已关闭，未提交。")
        if current["trade_status"] != order.get("trade_status"):
            raise BrowserAttention("当前交易状态与已批准的申请快照不一致，请重新核对。")
        if current["amount_fen"] is None or current["amount_fen"] != order.get("amount_fen"):
            raise BrowserAttention("当前页面订单实付金额缺失、有歧义或与已批准快照不一致，未提交。")

    async def _verify_buyer(self, scope, order):
        expected_name = str(order.get("invoice_buyer") or "").strip()
        expected_tax = str(order.get("invoice_tax_id") or "").strip().upper()
        if not expected_name or not expected_tax:
            raise BrowserAttention("缺少已核对的购买方名称与税号，请先核对发票归属。")
        current = invoice_detail_dom_observation(await scope.inner_html(), order["platform"])["buyer"]
        if current["invoice_buyer"] != expected_name or current["invoice_tax_id"] != expected_tax:
            raise BrowserAttention("当前发票详情的购买方名称/税号缺失、歧义或与已核对资料不一致，未下载。")

    async def apply(self, order, profile, invoice_type):
        if invoice_type not in ("special", "ordinary"):
            raise BrowserAttention("申请票种无效。")
        async with self.lock:
            page, scope = await self._navigate(order)
            await self._verify_transaction(scope, order)
            observation = invoice_observation(await scope.inner_text())
            if observation["invoice_status"] in ("pending", "issued", "downloaded"):
                raise BrowserAttention("平台当前显示已申请或已有发票，已停止重复提交，请先刷新核对。")
            if observation["invoice_status"] not in ("unrequested", "rejected"):
                raise BrowserAttention("当前开票状态不明，需人工确认后重新扫描。")
            entries = await self._named(scope, r"^(?:申请开票|申请发票|补开发票)$")
            if len(entries) != 1:
                raise BrowserAttention("无法唯一定位申请入口，已停止。")
            # The entry itself may create an application on some layouts.
            # From the moment its click starts, no error is safe to replay.
            try:
                page = await self._activate(page, entries[0])
                return await self._complete_application(page, order, profile, invoice_type)
            except BrowserAttention as exc:
                exc.safe_to_retry = False
                exc.args = (str(exc) + " 申请入口已操作，尚不能排除已生成申请；请先在官方页面核对，禁止直接重试。",)
                raise
            except (Exception, asyncio.CancelledError):
                return {"status": "uncertain", "message": "申请入口操作后未能确认结果；入口可能已创建申请。请先在官方页面核对，禁止直接重试。"}

    async def _complete_application(self, page, order, profile, invoice_type):
        await self._check(page, order["platform"])
        scope = await self._scope(page, order)
        current = invoice_observation(await scope.inner_text())
        if current["invoice_status"] in ("pending", "issued"):
            return {"status": "uncertain", "message": "申请入口操作后页面显示开票处理中或已有发票；入口可能已创建申请，未再次提交。请先在官方页面核对。"}
        if invoice_type == "ordinary":
            await self._no_available_special(scope)
        selected = await self._choose_type(scope, invoice_type)
        fields = [
            ("company_name", r"企业名称|公司名称|单位名称|发票抬头|invoice[_-]?title|buyer[_-]?name", True),
            ("tax_id", r"纳税人识别号|纳税人识别码|税号|统一社会信用代码|tax[_-]?(?:id|no)|taxpayer", True),
            ("address", r"注册地址|企业地址|单位地址|公司地址|register[_-]?address", False),
            ("phone", r"注册电话|企业电话|单位电话|公司电话|register[_-]?phone", False),
            ("bank_name", r"开户银行|开户行名称|bank[_-]?name", False),
            ("bank_account", r"银行账号|银行账户|开户账号|bank[_-]?account", False),
            ("email", r"接收邮箱|收票邮箱|电子邮箱|邮箱地址|email|e-mail", False),
        ]
        filled = []
        for key, names, required in fields:
            item = await self._field(scope, names, required)
            if item is None:
                continue
            value = str(profile.get(key) or "").strip()
            browser_required = await item.get_attribute("required") is not None or await item.get_attribute("aria-required") == "true"
            special_required = invoice_type == "special" and key in ("address", "phone", "bank_name", "bank_account")
            if not value:
                if required or browser_required or special_required:
                    raise BrowserAttention(f"企业资料缺少表单要求的 {key}，未提交。")
                continue
            await item.fill(value)
            if (await item.input_value()).strip() != value:
                raise BrowserAttention("填写后回读不一致，未提交开票申请。")
            filled.append((item, value))
        await self._check(page, order["platform"])
        final_scope = await self._scope(page, order)
        await self._verify_transaction(final_scope, order)
        if invoice_type == "ordinary":
            await self._no_available_special(final_scope)
        if not await selected.is_checked():
            raise BrowserAttention("发票类型在填写过程中发生变化，未提交。")
        for item, value in filled:
            if (await item.input_value()).strip() != value:
                raise BrowserAttention("表单资料已变化，未提交。")
        submit = await self._named(scope, r"^(?:提交申请|确认申请|确认提交|提交开票申请|向商家发送申请)$", roles=("button",))
        if len(submit) != 1:
            raise BrowserAttention("无法唯一确认最终申请按钮，未提交。")
        # Once click starts, every error is uncertain. The caller must never
        # automatically replay this operation on timeout or browser loss.
        try:
            await submit[0].click()
            await page.wait_for_timeout(800)
            text = await self._check(page, order["platform"])
            target = re.search(rf"(?<!\d){re.escape(str(order['order_no']))}(?!\d)", text)
            success = re.search(r"申请成功|开票申请已提交|申请已提交|提交成功|已申请开票", text)
            if target and success:
                return {"status": "submitted", "message": "平台显示目标订单申请已提交，待商家开票；尚未获得发票。"}
            return {"status": "uncertain", "message": "已点击提交，但未取得目标订单的明确成功结果。请在官方页面核对，禁止直接重试。"}
        except Exception:
            return {"status": "uncertain", "message": "提交后页面未能确认结果。申请可能已生效，请在官方页面核对，禁止直接重试。"}
        except asyncio.CancelledError:
            return {"status": "uncertain", "message": "提交后的任务被取消，结果尚不明确。请在官方页面核对，禁止直接重试。"}

    async def _get_observed_invoice_document(self, url, order, dest_dir):
        """GET a click-observed, pinned JD invoice object; never navigate it."""
        if not observed_jd_invoice_url(url, order["platform"]):
            raise BrowserAttention("观察到的原件地址不属于已限定的京东发票bucket，未读取。")
        response = None
        try:
            response = await self._context.request.get(url, max_redirects=0, max_retries=0,
                ignore_https_errors=False, fail_on_status_code=False, timeout=20_000)
            extension = invoice_response_extension(response.headers, response.status)
            body = await response.body()
            return save_invoice_bytes(body, extension, order, dest_dir)
        except BrowserAttention:
            raise
        except Exception:
            # Request errors can contain a signed URL. Expose no exception text.
            raise BrowserAttention("已观察的发票原件读取失败，未归档；未跟随重定向或重试。") from None
        finally:
            if response is not None:
                try:
                    await response.dispose()
                except Exception:
                    pass

    async def _download_action(self, page, order, dest_dir, action_kind):
        """One verified view/download click; prefer a native original download.

        Non-platform document navigation is paused before it can render scripts.
        Only the exact pinned bucket URL observed from that click may then be
        retrieved as bytes. No caller-provided storage URL enters this branch.
        """
        # Every click is rebound immediately to the current page/order. A body
        # or Locator retained from an earlier inspection is not an identity.
        await self._check(page, order["platform"])
        scope = await self._scope(page, order)
        await self._verify_buyer(scope, order)
        if invoice_detail_dom_observation(await scope.inner_html(), order["platform"])["invoice"]["invoice_status"] != "issued":
            raise BrowserAttention("点击前发票状态未明确为已开票，未读取原件。")
        patterns = {"download": DOWNLOAD_ACTION_PATTERN, "view": VIEW_ACTION_PATTERN}
        if action_kind not in patterns:
            raise BrowserAttention("原件操作类型无法确认，未点击。")
        matches = await self._named(scope, patterns[action_kind])
        if len(matches) != 1:
            raise BrowserAttention("当前目标订单的原件入口缺失或不唯一，未点击。")
        locator = matches[0]
        context = self._context
        if context is None:
            raise BrowserAttention("独立浏览器上下文已关闭，未读取原件。")
        downloads, response_meta, listeners = [], {}, []
        observed, blocked = set(), set()
        signal = asyncio.Event()
        initial_pages = set(context.pages)
        source = urlparse(page.url)
        source_origin = (source.scheme, source.hostname, source.port)
        href = await locator.get_attribute("href")
        declared_document = href if href and observed_jd_invoice_url(href, order["platform"]) else None
        click_started = False
        deadline = time.monotonic() + BROWSER_DOWNLOAD_TIMEOUT_SECONDS
        blocked_provenance = False

        def attach(target):
            def downloaded(item):
                downloads.append((item, target))
                signal.set()
            target.on("download", downloaded)
            listeners.append((target, downloaded))

        def response_seen(response):
            try:
                if official_url(response.url, order["platform"]) or observed_jd_invoice_url(response.url, order["platform"]):
                    headers = response.headers
                    response_meta[response.url] = (response.status, {key: value for key, value in headers.items()
                        if key.lower() in ("content-type", "content-length")})
            except Exception:
                pass

        async def source_opener(target):
            try:
                return await target.opener() is page
            except Exception:
                return False

        async def related(target):
            return target is page or (target not in initial_pages and await source_opener(target))

        async def document_route(route, request):
            nonlocal blocked_provenance
            try:
                if not request.is_navigation_request() or not click_started:
                    await route.fallback()
                    return
                # Chromium's first target=_blank navigation can precede frame
                # creation. Bind that observable request to this one click by
                # its official referrer origin; never invent a target URL.
                referrer = request.headers.get("referer", "")
                reference = urlparse(referrer)
                same_origin = official_url(referrer, order["platform"]) and (
                    reference.scheme, reference.hostname, reference.port) == source_origin
                exact_link_target = declared_document is not None and request.url == declared_document
                try:
                    frame = request.frame
                except Exception:
                    frame = None
                if frame is None:
                    belongs = bool(same_origin or exact_link_target)
                    if not belongs and not official_url(request.url, order["platform"]):
                        blocked.add(request.url)
                        signal.set()
                        await route.abort("blockedbyclient")
                        return
                else:
                    target = frame.page
                    has_source_opener = await source_opener(target) if target is not page else False
                    current_candidate = target is page or has_source_opener or same_origin or exact_link_target
                    old_popup = target in initial_pages and target is not page
                    unknown_new_opener = target not in initial_pages and not has_source_opener
                    if current_candidate and (frame is not target.main_frame or old_popup or unknown_new_opener):
                        blocked_provenance = True
                        blocked.add(request.url)
                        signal.set()
                        await route.abort("blockedbyclient")
                        return
                    belongs = frame is target.main_frame and (target is page or (target not in initial_pages and has_source_opener))
                if not belongs:
                    await route.fallback()
                    return
                if official_url(request.url, order["platform"]):
                    await route.fallback()
                    return
                # Read no response document in the browser. A strict, top-level
                # GET observed here is the sole source of the byte-read URL.
                if request.method == "GET" and observed_jd_invoice_url(request.url, order["platform"]):
                    observed.add(request.url)
                else:
                    blocked.add(request.url)
                signal.set()
                await route.abort("blockedbyclient")
            except Exception:
                try:
                    await route.abort("blockedbyclient")
                except Exception:
                    pass

        target = None
        attach(page)
        context.on("page", attach)
        context.on("response", response_seen)
        await context.route("**/*", document_route)
        try:
            try:
                click_started = True
                target = await asyncio.wait_for(self._activate(page, locator),
                    timeout=max(0.001, min(15, deadline - time.monotonic())))
            except Exception:
                if not downloads and not observed and not blocked:
                    raise BrowserAttention("查看/下载入口操作未确认，未取得可验证的原件地址。") from None
            if not downloads and not observed and not blocked:
                try:
                    await asyncio.wait_for(signal.wait(), timeout=max(0.001, min(2, deadline - time.monotonic())))
                except TimeoutError:
                    pass
        finally:
            await context.unroute("**/*", document_route)
            context.remove_listener("page", attach)
            context.remove_listener("response", response_seen)
            for item, callback in listeners:
                item.remove_listener("download", callback)

        if blocked:
            if blocked_provenance:
                raise BrowserAttention("原件导航来自子框架或此前已存在的弹窗，未读取或归档。")
            # Reuse the strict platform guard for hostname-only diagnostics.
            class ObservedTarget:
                url = next(iter(blocked))
            await self._check(ObservedTarget(), order["platform"])
            raise BrowserAttention("未能确认目标原件的顶层导航，未归档。")
        eligible_downloads = [item for item, origin in downloads if await related(origin)]
        if len(eligible_downloads) > 1 or len(observed) > 1:
            raise BrowserAttention("一次操作出现多个原件候选，未选择或归档。")
        if eligible_downloads:
            download = eligible_downloads[0]
            url = download.url
            if not (official_url(url, order["platform"]) or observed_jd_invoice_url(url, order["platform"])):
                raise BrowserAttention("浏览器下载来源不受信任，未归档。")
            if observed and observed != {url}:
                raise BrowserAttention("浏览器下载与观察到的文档不一致，未归档。")
            metadata = response_meta.get(url)
            if metadata is not None:
                extension = invoice_response_extension(metadata[1], metadata[0])
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    path = await asyncio.wait_for(download.path(), timeout=remaining)
                    if not 16 <= path.stat().st_size <= INVOICE_MAX_BYTES:
                        raise BrowserAttention("浏览器下载体积无效或超过 50 MiB，未归档。")
                    return save_invoice_bytes(path.read_bytes(), extension, order, dest_dir)
                except BrowserAttention:
                    raise
                except TimeoutError:
                    try:
                        await asyncio.wait_for(download.cancel(), timeout=2)
                    except Exception:
                        pass
                    raise BrowserAttention("浏览器原始下载超过操作时限，已请求取消，未保存或归档。") from None
                except Exception:
                    raise BrowserAttention("浏览器原始下载未能验证，未归档。") from None
            if observed_jd_invoice_url(url, order["platform"]):
                return await self._get_observed_invoice_document(url, order, dest_dir)
            raise BrowserAttention("浏览器下载缺少明确原件响应类型，未归档。")
        if observed:
            return await self._get_observed_invoice_document(next(iter(observed)), order, dest_dir)
        if target is None:
            raise BrowserAttention("未观察到原件下载或官方详情页面，未归档。")
        await self._check(target, order["platform"])
        return {"page": target}

    async def download(self, order, dest_dir: Path):
        async with self.lock:
            page, scope = await self._navigate(order, invoice=True)
            # Verify the populated invoice-information table before opening a
            # viewer which might navigate to an as-yet-untrusted PDF/CDN host.
            await self._verify_buyer(scope, order)
            observation = invoice_detail_dom_observation(await scope.inner_html(), order["platform"])["invoice"]
            if observation["invoice_status"] != "issued":
                raise BrowserAttention("当前订单页面未明确显示已开票或可下载，无法自动归档。")
            pattern = DOWNLOAD_ACTION_PATTERN
            buttons = await self._named(scope, pattern)
            if len(buttons) == 1:
                result = await self._download_action(page, order, dest_dir, "download")
                if "path" in result:
                    return result
                raise BrowserAttention("点击下载后未获得原始文件，未归档。")
            if not buttons:
                views = await self._named(scope, VIEW_ACTION_PATTERN)
                if len(views) != 1:
                    raise BrowserAttention("无法唯一定位目标订单发票详情，请在官方页面人工核对。")
                result = await self._download_action(page, order, dest_dir, "view")
                if "path" in result:
                    return result
                page = result["page"]
                scope = await self._scope(page, order)
                buttons = await self._named(scope, pattern)
            if len(buttons) != 1:
                raise BrowserAttention("下载入口缺失或有多个文件选项，需人工选择原始发票文件。")
            result = await self._download_action(page, order, dest_dir, "download")
            if "path" in result:
                return result
            raise BrowserAttention("未获得原始发票下载，未归档。")

    async def close(self):
        async with self.lock:
            owned_context, owned_playwright = self._context, self._playwright
            try:
                if owned_context:
                    try:
                        await owned_context.close()
                    except Exception:
                        pass  # It may already have been closed by the user.
            finally:
                try:
                    if owned_playwright:
                        try:
                            await owned_playwright.stop()
                        except Exception:
                            pass
                finally:
                    self._context = None
                    self._playwright = None
                    self._context_closed = True
                    self._pages.clear()
