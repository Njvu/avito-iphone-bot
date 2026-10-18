import asyncio
import time
from types import SimpleNamespace

import bot.avito as av


class FakePage:
    url = "https://www.avito.ru/krasnodar"

    def __init__(self):
        self.mouse = SimpleNamespace(move=self._noop, wheel=self._noop)
        self.gotos = []

    async def _noop(self, *a, **k):
        pass

    async def goto(self, url, **k):
        self.gotos.append(url)

    async def wait_for_selector(self, *a, **k):
        pass

    async def wait_for_timeout(self, ms):
        pass

    async def content(self):
        return "<html><body><div data-marker='item'></div></body></html>"

    async def title(self):
        return "iPhone"


def test_fetch_returns_at_once_and_pauses_before_next_request(monkeypatch):
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(av.asyncio, "sleep", fake_sleep)
    page = FakePage()
    a = av.Avito()
    a._ctx = SimpleNamespace(pages=[page])
    asyncio.run(a.fetch("https://www.avito.ru/krasnodar/x_1"))
    assert slept == [0.0] and a._next_request > time.monotonic() + 1
    asyncio.run(a.fetch("https://www.avito.ru/krasnodar/x_2"))
    assert slept[1] > 1 and page.gotos == ["https://www.avito.ru/krasnodar/x_1", "https://www.avito.ru/krasnodar/x_2"]


def test_fetch_restarts_missing_browser(monkeypatch):
    page = FakePage()
    a = av.Avito()
    restarts = []

    async def restart():
        restarts.append(1)
        a._ctx = SimpleNamespace(pages=[page])

    monkeypatch.setattr(a, "restart", restart)
    asyncio.run(a.fetch("https://www.avito.ru/krasnodar/x_1"))
    assert restarts == [1] and page.gotos


def test_fetch_restarts_after_page_crash():
    crashed, page = FakePage(), FakePage()

    async def crash(url, **k):
        raise Exception("Page.goto: Page crashed")

    crashed.goto = crash
    a = av.Avito()
    a.proxy = "http://u:p@127.0.0.1:1"
    a._ctx = SimpleNamespace(pages=[crashed])
    a._next_request = 0.0
    restarts = []

    async def restart():
        restarts.append(1)
        a._ctx = SimpleNamespace(pages=[page])

    a.restart = restart
    html = asyncio.run(a.fetch("https://www.avito.ru/krasnodar/x_1"))
    assert restarts == [1] and page.gotos == ["https://www.avito.ru/krasnodar/x_1"] and "item" in html


def test_new_session_clears_local_storage_after_browser_restart():
    calls = []

    class Cdp:
        async def send(self, method, params):
            calls.append((method, params))

        async def detach(self):
            calls.append("detach")

    async def clear_cookies():
        calls.append("cookies")

    async def new_cdp_session(page):
        return Cdp()

    async def cookies(url=None):
        return []

    a = av.Avito()
    a._ctx = SimpleNamespace(pages=[SimpleNamespace(url="about:blank")], clear_cookies=clear_cookies,
                             new_cdp_session=new_cdp_session, cookies=cookies)
    asyncio.run(a.new_session())
    assert calls == ["cookies", ("Storage.clearDataForOrigin", {"origin": av.BASE, "storageTypes": "local_storage"}),
                     "detach"]


def test_api_pauses_outside_lock_and_hung_fetch_releases_lock(monkeypatch):
    a = av.Avito()
    locked = []
    real_sleep = asyncio.sleep

    async def fake_sleep(sec):
        locked.append(a._lock.locked())
        await real_sleep(0)

    page = FakePage()

    async def hang(*args, **kwargs):
        await real_sleep(60)

    page.evaluate = hang
    a._ctx = SimpleNamespace(pages=[page])
    monkeypatch.setattr(av.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(av, "PAGE_TIMEOUT", 50)

    async def run():
        try:
            await a.api("/web/1/x", {})
        except TimeoutError:
            return not a._lock.locked()

    assert asyncio.run(run()) is True and locked == [False]


def test_new_session_keeps_only_login_cookies():
    import asyncio
    from bot.avito import BASE, Avito

    class Page:
        url = BASE + "/krasnodar"

        async def evaluate(self, js):
            self.storage_cleared = True

    class Ctx:
        def __init__(self, names):
            self.names, self.pages = list(names), [Page()]

        async def cookies(self, url=None):
            return [{"name": n, "value": "1", "domain": ".avito.ru", "path": "/"} for n in self.names]

        async def clear_cookies(self):
            self.names = []

        async def add_cookies(self, cookies):
            self.names += [c["name"] for c in cookies]

    a = Avito()
    a._ctx = Ctx(["u", "ft", "sessid", "auth", "_avisc"])
    asyncio.run(a.new_session())
    assert sorted(a._ctx.names) == ["auth", "sessid"] and a._ctx.pages[0].storage_cleared
    assert asyncio.run(a.logged_in())
    a._ctx = Ctx(["u"])
    assert not asyncio.run(a.logged_in())
