import asyncio
import logging
import random
import sys
import time
from urllib.parse import unquote, urlsplit

import httpx
from playwright.async_api import async_playwright

from bot.catalog import CATALOG_PATH
from bot.config import ROOT, SEARCH_PATH, settings
from bot.parser import BASE, is_blocked

log = logging.getLogger(__name__)


JPEG = bytes([0xFF, 0xD8, 0xFF])


class Blocked(Exception):
    pass


class ProxyDown(Blocked):
    pass


def search_url(region: str, page: int = 1) -> str:
    url = f"{BASE}/{region}/{SEARCH_PATH}?s=104&user=1"
    return url + (f"&p={page}" if page > 1 else "")


def _pw_proxy(url: str | None) -> dict | None:
    if not url:
        return None
    u = urlsplit(url)
    d = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        d.update(username=unquote(u.username), password=unquote(u.password or ""))
    return d


PAGE_TIMEOUT = 30000
PAGE_PAUSE = (2.0, 4.0)
RENDER_WAIT = (300, 800)
HEAVY = {"media"}
EVAL_PAGE = f"{BASE}/evaluation/mobile-phones"
SEARCH_READY = '[data-marker="item"]'
ITEM_READY = 'h1'
BLOCK_READY = "[class*='firewall'], h1:has-text('Доступ ограничен'), h2:has-text('Доступ ограничен')"
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled", "--webrtc-ip-handling-policy=disable_non_proxied_udp"]
AUTH_COOKIES = {"sessid", "auth"}
NEXT_PAGE = '[data-marker="pagination-button/nextPage"]'
CATALOG_PAUSE = (1.0, 2.0)
CATALOG_JS = """(path) => new Promise((resolve) => {
    const x = new XMLHttpRequest();
    x.open('GET', path);
    x.setRequestHeader('accept', 'application/json');
    x.setRequestHeader('x-requested-with', 'XMLHttpRequest');
    x.setRequestHeader('x-source', 'client-browser');
    x.timeout = 25000;
    x.onload = () => {
        let body = null;
        try { body = JSON.parse(x.responseText); } catch (e) {}
        if (x.status !== 200 || !body) return resolve({status: x.status, pow: !!(body && body.pow_challenge)});
        resolve({status: x.status, data: {catalog: {items: (body.catalog && body.catalog.items) || []}}});
    };
    x.onerror = () => resolve({status: 0});
    x.ontimeout = () => resolve({status: 0});
    x.send();
})"""
SYSTEMS = {"win32":"Windows NT 10.0; Win64; x64", "darwin": "Macintosh; Intel Mac OS X 10_15_7"}


def user_agent(version: str, platform: str = sys.platform) -> str:
    system = SYSTEMS.get(platform, "X11; Linux x86_64")
    return (f"Mozilla/5.0 ({system}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version.split('.')[0]}.0.0.0 Safari/537.36")


async def launch_context(pw, proxy_url: str | None, headless: bool):
    proxy = _pw_proxy(proxy_url)
    ua = None
    if headless:
        probe = await pw.chromium.launch(headless=True)
        ua = user_agent(probe.version)
        await probe.close()
    return await pw.chromium.launch_persistent_context(
        str(ROOT / "data" / "browser"), channel="chromium", headless=headless, proxy=proxy, user_agent=ua,
        args=STEALTH_ARGS + ([] if proxy else ["--no-proxy-server"]), ignore_default_args=["--enable-automation"],
        locale="ru-RU", timezone_id="Europe/Moscow", viewport={"width": 1366, "height": 900})


class Avito:
    def __init__(self):
        self._pw = self._browser = self._ctx = None
        self._lock = asyncio.Lock()
        self.proxy: str | None = settings.proxy_url or None
        self.light = settings.light_pages
        self._next_request = 0.0

    async def start(self):
        self._pw = await async_playwright().start()
        self._ctx = await launch_context(self._pw, self.proxy, settings.headless)
        if settings.light_pages:
            await self._ctx.route("**/*", self._route)

    async def _route(self, route):
        if self.light and route.request.resource_type in HEAVY:
            await route.abort()
        else:
            await route.continue_()

    async def show_media(self, on: bool):
        was_light = self.light
        self.light = settings.light_pages and not on
        if on and was_light:
            async with self._lock:
                try:
                    await (await self._page()).reload(wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                except Exception:
                    pass

    async def api(self, path: str, body: dict | None = None):
        await asyncio.sleep(random.uniform(1.0, 2.5))
        async with self._lock:
            page = await self._page()
            if not page.url.startswith(BASE):
                try:
                    await page.goto(EVAL_PAGE, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                except Exception:
                    return None
            return await asyncio.wait_for(page.evaluate("""async ({path, body}) => {
                const opts = body ? {method: 'POST', headers: {'content-type': 'application/json'},
                                     body: JSON.stringify(body)} : {};
                const r = await fetch(path, opts);
                if (!r.ok) return {__status: r.status};
                try { return await r.json(); } catch (e) { return {__status: 'not json'}; }
            }""", {"path": path, "body": body}), PAGE_TIMEOUT / 1000)

    async def catalog(self, path: str) -> dict | None:
        async with self._lock:
            if not self._ctx:
                return None
            await asyncio.sleep(max(0.0, self._next_request - time.monotonic()))
            try:
                page = await self._page()
                if not page.url.startswith(BASE):
                    return None
                res = await self._catalog_json(page, path)
                if res.get("pow") and await self._solve_pow(page):
                    res = await self._catalog_json(page, path)
            except Exception as e:
                log.info("JSON-запрос списка не удался: %s", str(e).split("\n")[0][:200])
                return None
            finally:
                self._next_request = time.monotonic() + random.uniform(*CATALOG_PAUSE)
        if res.get("status") != 200:
            log.info("JSON-запрос списка: HTTP %s%s", res.get("status"), " (PoW)" if res.get("pow") else "")
            return None
        return res.get("data")

    @staticmethod
    async def _catalog_json(page, path: str) -> dict:
        return await asyncio.wait_for(page.evaluate(CATALOG_JS, path), PAGE_TIMEOUT / 1000)

    @staticmethod
    async def _solve_pow(page) -> bool:
        if SEARCH_PATH not in page.url:
            return False
        button = page.locator(NEXT_PAGE).first
        if not await button.count():
            return False
        try:
            async with page.expect_response(lambda r: CATALOG_PATH in r.url and r.ok, timeout=20000) as info:
                await button.scroll_into_view_if_needed(timeout=5000)
                await page.mouse.move(random.randint(400, 900), random.randint(300, 700), steps=random.randint(3, 8))
                await button.click(timeout=5000)
            return (await info.value).ok
        except Exception:
            return False

    async def close(self):
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def page_blocked(self) -> bool:
        if not self._ctx or not self._ctx.pages:
            return True
        try:
            page = self._ctx.pages[0]
            return is_blocked(await page.title(), await page.content())
        except Exception:
            return True

    async def _page(self):
        return self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()

    async def captcha_kind(self) -> str | None:
        async with self._lock:
            page = await self._page()
            if await page.locator(".js-form-captcha-image").first.is_visible():
                return "text"
            if await page.locator("iframe[src*='hcaptcha'], .geetest_captcha, [class*='geetest']").count():
                return "interactive"
            return None

    async def captcha_image(self) -> bytes:
        async with self._lock:
            page = await self._page()
            img = page.locator(".js-form-captcha-image").first
            if await img.is_visible():
                return await img.screenshot()
            return await page.screenshot()

    async def show(self, url: str):
        async with self._lock:
            await (await self._page()).goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

    async def page_screenshot(self) -> bytes:
        async with self._lock:
            return await (await self._page()).screenshot()

    async def refresh_captcha(self):
        async with self._lock:
            page = await self._page()
            await page.locator(".js-form-captcha-refresh").first.click()
            await page.wait_for_timeout(1500)

    async def submit_captcha(self, text: str) -> bool:
        async with self._lock:
            page = await self._page()
            await page.locator(".js-captcha-input").first.fill(text)
            await page.locator("form.js-firewall-form button[type=submit]").first.click()
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
            return not is_blocked(await page.title(), await page.content())

    async def remote_screenshot(self) -> bytes:
        page = await self._page()
        return await page.screenshot(type="jpeg", quality=60, scale="css")

    async def remote_mouse(self, kind: str, x: float, y: float):
        page = await self._page()
        await page.mouse.move(x, y, steps=1 if kind == "move" else 3)
        if kind == "down":
            await page.mouse.down()
        elif kind == "up":
            await page.mouse.up()

    async def remote_type(self, text: str):
        await (await self._page()).keyboard.type(text, delay=60)

    async def remote_key(self, key: str):
        await (await self._page()).keyboard.press(key)

    async def cookie_names(self) -> set[str]:
        if not self._ctx:
            return set()
        try:
            return {c["name"] for c in await self._ctx.cookies(BASE)}
        except Exception:
            return set()

    async def logged_in(self) -> bool:
        return bool(await self.cookie_names() & AUTH_COOKIES)

    async def new_session(self):
        async with self._lock:
            login = [c for c in await self._ctx.cookies() if c["name"] in AUTH_COOKIES]
            await self._ctx.clear_cookies()
            if login:
                await self._ctx.add_cookies(login)
            page = await self._page()
            if page.url.startswith(BASE):
                await page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
            else:
                cdp = await self._ctx.new_cdp_session(page)
                await cdp.send("Storage.clearDataForOrigin", {"origin": BASE, "storageTypes": "local_storage"})
                await cdp.detach()

    async def set_proxy(self, proxy: str | None):
        self.proxy = proxy
        await self.restart()

    async def restart(self):
        try:
            await self.close()
        except Exception:
            pass
        self._ctx = self._pw = None
        await self.start()

    async def fetch(self, url: str) -> str:
        async with self._lock:
            if not self._ctx:
                await self.restart()
            await asyncio.sleep(max(0.0, self._next_request - time.monotonic()))
            try:
                page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
                referer = page.url if page.url.startswith(BASE) else None
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT, referer=referer)
            except Exception as e:
                if self.proxy and ("net::" in str(e) or "Timeout" in str(e)):
                    raise ProxyDown(f"прокси не отвечает: {str(e).split(' at ')[0]}") from e
                if not any(w in str(e).lower() for w in ("closed", "target", "crash")):
                    raise
                await self.restart()
                page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            await self._wait_ready(page, url)
            await self._browse(page)
            await page.wait_for_timeout(random.randint(*RENDER_WAIT))
            html, title = await self._snapshot(page)
            self._next_request = time.monotonic() + random.uniform(*PAGE_PAUSE)
        if is_blocked(title, html):
            raise Blocked(title)
        return html

    @staticmethod
    async def _browse(page):
        try:
            await page.mouse.move(random.randint(200, 1100), random.randint(150, 700), steps=random.randint(3, 8))
            await page.mouse.wheel(0, random.randint(300, 900))
        except Exception:
            pass

    @staticmethod
    async def _wait_ready(page, url: str):
        ready = SEARCH_READY if SEARCH_PATH in url else ITEM_READY
        try:
            await page.wait_for_selector(f"{ready}, {BLOCK_READY}", timeout=15000)
        except Exception:
            pass

    @staticmethod
    async def _snapshot(page) -> tuple[str, str]:
        for _ in range(3):
            try:
                return await page.content(), await page.title()
            except Exception as e:
                if "navigating" not in str(e):
                    raise
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
        return await page.content(), await page.title()


async def download_photos(urls: list[str], limit: int) -> list[bytes]:
    async with httpx.AsyncClient(timeout=20, trust_env=False,
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "image/jpeg"}) as c:
        async def one(u: str) -> bytes | None:
            try:
                r = await c.get(u)
            except httpx.HTTPError:
                return None
            return r.content if r.status_code == 200 and r.content.startswith(JPEG) else None

        results = await asyncio.gather(*(one(u) for u in urls[:limit]))
    return [r for r in results if r]


async def _ipinfo(proxy: str | None) -> dict:
    async with httpx.AsyncClient(timeout=15, trust_env=False, proxy=proxy) as c:
        return (await c.get("https://ipinfo.io/json")).json()


async def direct_ip_info(proxy: str | None = None) -> str:
    try:
        d = await _ipinfo(proxy)
        return f"{d.get('country', '?')} {d.get('org', '')} {d.get('ip', '')}".strip()
    except Exception as e:
        return f"? ({type(e).__name__})"


async def exit_ip(proxy: str | None = None) -> str | None:
    try:
        return (await _ipinfo(proxy)).get("ip")
    except Exception:
        return None
