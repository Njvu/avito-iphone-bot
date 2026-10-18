import asyncio
import logging
import random
from urllib.parse import urlsplit

import httpx

from bot.config import CITY, ROOT, settings
from bot.parser import is_blocked

log = logging.getLogger(__name__)

FREE_SOURCES = [
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/countries/RU/data.txt",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&country=ru&protocol=http"
    "&proxy_format=protocolipport&format=text",
]
KNOWN = ROOT / "data" / "proxies_ok.txt"
TEST_URL = f"https://www.avito.ru/{CITY}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/129.0 Safari/537.36")


async def fetch_free() -> list[str]:
    out = []
    async with httpx.AsyncClient(timeout=20, trust_env=False) as c:
        for src in FREE_SOURCES:
            try:
                r = await c.get(src)
                for line in r.text.splitlines():
                    line = line.strip()
                    if line.startswith(("http://", "socks5://", "socks4://")) and line not in out:
                        out.append(line)
            except httpx.HTTPError as e:
                log.warning("Список прокси %s недоступен: %s", src, e)
    return [p for p in out if not p.startswith("socks4://")]


async def check(proxy: str) -> bool:
    try:
        return await asyncio.wait_for(_check(proxy), 15)
    except Exception:
        return False


async def _check(proxy: str) -> bool:
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=12, trust_env=False, follow_redirects=True,
                                     headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru"}) as c:
            r = await c.get(TEST_URL)
            return r.status_code == 200 and "avito" in r.text[:5000].lower() and not is_blocked("", r.text)
    except Exception:
        return False


async def working(candidates: list[str], need: int = 5) -> list[str]:
    sem = asyncio.Semaphore(60)
    good: list[str] = []
    done = asyncio.Event()

    async def one(p):
        async with sem:
            if not done.is_set() and await check(p):
                good.append(p)
                if len(good) >= need:
                    done.set()

    checks = asyncio.gather(*(one(p) for p in candidates))
    finished = asyncio.ensure_future(done.wait())
    await asyncio.wait([finished, checks], return_when=asyncio.FIRST_COMPLETED)
    checks.cancel()
    finished.cancel()
    await asyncio.gather(checks, finished, return_exceptions=True)
    return good[:need]


def mask(proxy: str | None) -> str:
    if not proxy:
        return "напрямую"
    u = urlsplit(proxy)
    return f"{u.scheme}://{u.hostname}:{u.port}"


def load_known() -> list[str]:
    try:
        return [x for x in KNOWN.read_text(encoding="utf-8").split() if x]
    except OSError:
        return []


def save_known(proxies: list[str]):
    merged = list(dict.fromkeys(proxies + load_known()))[:30]
    KNOWN.write_text("\n".join(merged), encoding="utf-8")


class ProxyPool:
    def __init__(self):
        self.queue: list[str] = []
        self.current: str | None = settings.proxy_url or None
        self.bad: set[str] = set()

    async def refill(self):
        own = [p for p in settings.proxy_list if p not in self.bad]
        own_ok = await working(own, len(own)) if own else []
        free_ok = []
        if settings.free_proxies:
            known = [p for p in load_known() if p not in self.bad and p not in own]
            free_ok = await working(known, 5) if known else []
            if len(free_ok) < 3:
                fresh = [p for p in await fetch_free() if p not in self.bad and p not in own and p not in known]
                random.shuffle(fresh)
                free_ok += await working(fresh[:600], 5 - len(free_ok))
            save_known(free_ok)
        self.queue = own_ok + free_ok
        log.info("Рабочих прокси: свои %s, бесплатные %s", len(own_ok), len(free_ok))

    async def next(self) -> str | None:
        if self.current:
            self.bad.add(self.current)
        if not self.queue:
            await self.refill()
        self.current = self.queue.pop(0) if self.queue else None
        return self.current

    def reset(self):
        self.bad.clear()
        self.queue = []
        self.current = settings.proxy_url or None
