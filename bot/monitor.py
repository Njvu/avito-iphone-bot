import asyncio
import logging
import random
import re
from dataclasses import replace
import time
from collections import Counter, deque
from datetime import datetime

import httpx
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputMediaPhoto

from bot.ai import analyze
from bot.captcha import NEW_IMAGE, CaptchaRelay, captcha_caption
from bot.catalog import MODEL_IDS, catalog_path, parse_catalog
from bot.avito import Avito, Blocked, ProxyDown, direct_ip_info, download_photos, exit_ip, search_url
from bot.config import REGIONS, ROOT, settings
from bot.damage import BAD, DEFAULT_ALLOWED, blocked, flag_confirmed, inspect_damage, market_dirt
from bot.db import Store
from bot.evaluation import EVAL_CONDITIONS, NEIGHBORS, AvitoEvaluation, eval_condition, eval_price
from bot.models import detect_phone, reject_reason
from bot.parser import Card, count_seller_phones, is_closed, parse_item, parse_search
from bot.prices import base_price, load_prices
from bot.proxies import ProxyPool, mask
from bot.remote import RemoteControl
from bot.text import normalize

log = logging.getLogger(__name__)

BASE = "наша цена продажи"
AVITO = "оценка Авито"
EVAL = "калькулятор Авито"
SHOP_SELLER = "продавец-магазин"
UNCLEAR = "состояние не подтверждено"

SHOP = re.compile(
    r"(?<!в )(?<!из )(?<!с )(?<!не )магазин(?!е\b|а\b|ам\b|ами\b|ы\b|н)|наш\w* магазин|"
    r"(даем|даём|предоставля\w*|выда\w*|наша|от нас|от магазина|от продавца)\s+гаранти|"
    r"гаранти\w* (от нас|от магазина|от продавца|\d+\s*(дн|мес|год))|"
    r"(принима\w*|принимаю|возьм\w*)\s.{0,20}(trade.?in|трейд.?ин|в зач[её]т)|"
    r"рассрочк|в кредит|кредит от|"
    r"сервисн\w* центр|наш\w* (салон|сервис)|ассортимент|доставка по (всей )?росси"
)


def money(n: int | float | None) -> str:
    return f"{int(n):,} ₽".replace(",", " ") if n is not None else "—"


def fmt_storage(gb: int) -> str:
    if not gb:
        return "память ?"
    return f"{gb // 1024} ТБ" if gb >= 1024 else f"{gb} ГБ"


def is_night(now: datetime | None = None) -> bool:
    lo, hi = settings.night_hours
    return lo <= (now or datetime.now()).hour < hi


POSTED_TODAY = re.compile(r"сегодня в (\d{1,2}):(\d{2})", re.I)


def posted_lag(posted: str, seen: datetime) -> float | None:
    m = POSTED_TODAY.search(posted or "")
    if not m:
        return None
    published = seen.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    lag = (seen - published).total_seconds()
    return lag if 0 <= lag < 3 * 3600 else None


def night_start(now: datetime | None = None) -> datetime:
    return (now or datetime.now()).replace(hour=settings.night_hours[0], minute=0, second=0, microsecond=0)


ALLOWED_DAMAGE = re.compile(r"экран|стекл|крышк|корпус|задн|разбит|трещин|скол|царап")


def ai_rejects(ai: dict, text: str = "") -> bool:
    if ai.get("faceid") == "broken" and flag_confirmed("face id", text):
        return True
    if ai.get("icloud") == "locked" and flag_confirmed("icloud", text):
        return True
    return bool(ai_confirmed_flags(ai, text))


def ai_confirmed_flags(ai: dict, text: str) -> list[str]:
    damage = [str(x) for x in ai.get("damage") or [] if not ALLOWED_DAMAGE.search(normalize(str(x)))]
    flags = [str(x) for x in ai.get("red_flags") or []]
    items = [x for x in damage + flags if normalize(x).strip() not in ("", "нет", "none", "-")]
    return [x for x in items if not _allowed_only(x) and flag_confirmed(x, text)]


def _allowed_only(item: str) -> bool:
    s = normalize(item)
    return bool(ALLOWED_DAMAGE.search(s)) and not any(r != "__icloud__" and re.search(p, s) for p, r in BAD)


REFRESH_IDLE_TRIES, REFRESH_RETRY_SEC = 15, 120
ROTATE_TIMEOUT_SEC, ROTATE_RETRY_SEC = 60, 30
ERROR_RETRY_SEC = 30
SWEEP_PAUSE_SEC = 300
LOCATION_RETRY_SEC = 600
PROXY_DOWN_PAUSE = (30, 300)


def freshness(card: Card) -> float:
    age = card.age_hours
    return 0.0 if age is None else age


GENERIC_SELLERS = {"", "пользователь", "частное лицо"}


def seller_key(url: str, name: str) -> str:
    if url:
        return url
    name = (name or "").strip()
    return "" if name.lower() in GENERIC_SELLERS else name


def is_shop_text(text: str) -> bool:
    return bool(SHOP.search(normalize(text)))


def market_exclusion(card: Card) -> str:
    text = f"{card.title}\n{card.snippet}"
    reject = reject_reason(card.title, card.snippet)
    if reject:
        return reject
    shop = SHOP.search(normalize(text))
    if shop:
        return f"магазин «{shop.group(0).strip()}»"
    if any("выше рыночн" in b.lower() for b in card.badges):
        return "выше рыночной (Авито)"
    return market_dirt(text, card.condition)


class Monitor:
    def __init__(self, bot: Bot, store: Store, avito: Avito, remote: RemoteControl | None = None):
        self.bot, self.store, self.avito, self.remote = bot, store, avito, remote
        self.base_prices = load_prices(ROOT / "prices.txt")
        self.evaluation = AvitoEvaluation(avito, store, settings.avito_eval_city)
        store.keep_evals(EVAL_CONDITIONS)
        self.pending: set[asyncio.Task] = set()
        self.refreshing = False
        self.rotations: list[float] = []
        self.proxy_ip: str | None = None
        self.claimed: dict[tuple, float] = {}
        self.background: set[asyncio.Task] = set()
        self.eval_task: asyncio.Task | None = None
        self.ai_slots = asyncio.Semaphore(2)
        self.pool = ProxyPool()
        self.captcha = CaptchaRelay(bot)
        self.last_block = float("-inf")
        self.next_deep = float("-inf")
        self.sweep = 0
        self.sweep_paused_until = 0.0
        self.location: int | None = None
        self.location_retry_at = 0.0
        self.unverified: dict[str, tuple] = {}
        self.events: dict[str, deque] = {k: deque() for k in ("check", "block", "post", "rotation", "removed", "html")}
        self.lags: deque = deque(maxlen=50)
        self.rotation_wanted: asyncio.Future | None = None
        self.proxy_down = 0
        self.started = time.monotonic()
        self.status = {"last_check": None, "last_error": None, "blocks": 0, "last_block": None,
                       "found": 0, "pause_until": None, "ai_fail": 0, "proxy": None}

    def eval_in_background(self):
        if self.eval_task and not self.eval_task.done():
            return
        self.evaluation.fresh_since = night_start() if is_night() else None
        self.eval_task = asyncio.create_task(self.evaluation.step(self.store.tracked(), settings.avito_eval_per_step))

    async def refresh_prices(self, force: bool = True) -> int | None:
        if self.refreshing:
            return None
        self.refreshing = True
        if self.eval_task and not self.eval_task.done():
            await self.eval_task
        self.evaluation.refresh_since = datetime.now() if force else None
        total = 0
        idle = 0
        try:
            while idle < REFRESH_IDLE_TRIES:
                models = self.store.tracked()
                n = await self.evaluation.step(models, settings.avito_eval_per_step, patient=True)
                total += n
                if n:
                    idle = 0
                    continue
                if not self.evaluation.pending(models):
                    break
                idle += 1
                await asyncio.sleep(REFRESH_RETRY_SEC)
            else:
                log.warning("Калькулятор Авито: нет прогресса %s мин, обновление остановлено",
                            REFRESH_IDLE_TRIES * REFRESH_RETRY_SEC // 60)
            return total
        finally:
            self.evaluation.refresh_since = None
            self.refreshing = False

    def note(self, kind: str):
        now = time.monotonic()
        events = self.events[kind]
        events.append(now)
        while events and now - events[0] > 3600:
            events.popleft()

    def count(self, kind: str, seconds: float = 3600) -> int:
        now = time.monotonic()
        return sum(1 for t in self.events[kind] if now - t <= seconds)

    def request_rotation(self) -> asyncio.Future:
        if not self.rotation_wanted or self.rotation_wanted.done():
            self.rotation_wanted = asyncio.get_running_loop().create_future()
        return self.rotation_wanted

    async def serve_rotation(self):
        wanted = self.rotation_wanted
        if not wanted or wanted.done():
            return
        try:
            rotated = await self.rotate_ip()
            if rotated:
                self.note("rotation")
            wanted.set_result(rotated)
        except Exception as e:
            wanted.set_exception(e)

    async def notify_admins(self, text: str):
        for uid in self.store.admin_ids(settings.admin_ids):
            try:
                await self.bot.send_message(uid, text)
            except Exception as e:
                log.warning("Не удалось написать админу %s: %s", uid, e)

    async def run(self):
        await asyncio.sleep(3)
        ip = await direct_ip_info(self.avito.proxy)
        log.info("Авито видит IP: %s", ip)
        if settings.proxy_url:
            self.proxy_ip = await exit_ip(settings.proxy_url)
        refresh = self.store.get_state("eval_refresh", "0")
        if refresh in ("1", "fill"):
            self.store.set_state("eval_refresh", "0")
            task = asyncio.create_task(self.refresh_prices(force=refresh == "1"))
            self.background.add(task)
            task.add_done_callback(self.background.discard)
            log.info("Полное обновление цен калькулятора запущено в фоне")
        if not ip.startswith("RU"):
            await self.notify_admins(f"⚠️ Авито видит IP не из России: {ip}. Возможна блокировка. "
                                     "Проверьте, что VPN не работает в режиме TUN / exit node.")
        account = None
        while True:
            if self.store.get_state("paused") == "1":
                await asyncio.sleep(30)
                continue
            logged = await self.avito.logged_in()
            if logged != account:
                if account and not logged:
                    await self.notify_admins("⚠️ Бот вышел из аккаунта Авито. Войдите снова в окне браузера бота — "
                                             "без входа новые объявления могут приходить позже.")
                elif account is False and logged:
                    await self.notify_admins("✅ Вход в аккаунт Авито восстановлен.")
                account = self.status["account"] = logged
                log.info("Авито: вход в аккаунт — %s (куки: %s)", "есть" if logged else "нет",
                         ", ".join(sorted(await self.avito.cookie_names())))
            try:
                await self.serve_rotation()
                if time.monotonic() >= self.next_deep:
                    self.next_deep = time.monotonic() + self.interval() * 60
                    await self.cycle(light=settings.fast_json)
                    self.status["last_error"] = None
                await self.wait_next()
            except Blocked as e:
                try:
                    await self.on_blocked(e)
                except Exception:
                    log.exception("Ошибка обработки блокировки")
                    await asyncio.sleep(60)
            except Exception as e:
                log.exception("Ошибка цикла")
                self.status["last_error"] = f"{datetime.now():%H:%M} {e!r}"[:300]
                await asyncio.sleep(ERROR_RETRY_SEC)

    async def wait_next(self):
        if self.next_deep <= time.monotonic():
            self.next_deep = time.monotonic() + self.interval() * 60
        while (left := self.next_deep - time.monotonic()) > 0:
            if self.store.get_state("paused") == "1":
                return
            fast = self.fast_delay()
            if not fast or left < fast:
                await asyncio.sleep(left)
                return
            await asyncio.sleep(fast)
            await self.serve_rotation()
            await self.cycle(pages=[1], light=True)

    def fast_base(self) -> float:
        base = settings.fast_json_sec if settings.fast_json and settings.fast_json_sec else settings.fast_interval_sec
        calm = time.monotonic() - max(self.last_block, self.started)
        if settings.fast_turbo_sec and calm >= settings.turbo_after_min * 60:
            base = min(base, settings.fast_turbo_sec)
        return base

    def slowed_until(self) -> float:
        return self.last_block + settings.block_slowdown_min * 60

    def fast_delay(self) -> float:
        if not settings.fast_interval_sec:
            return 0
        if is_night():
            return settings.night_fast_sec
        base = self.fast_base()
        jitter = min(settings.fast_jitter_sec, base * 0.25)
        delay = random.uniform(base - jitter, base + jitter)
        if time.monotonic() < self.slowed_until():
            delay *= settings.block_slowdown_factor
        return delay

    def rhythm(self) -> str:
        if not settings.fast_interval_sec:
            return f"полный проход раз в {settings.check_interval_min}–{settings.check_interval_max} мин"
        if is_night():
            fast = f"ночь: 1-я стр. раз в {settings.night_fast_sec} с"
        else:
            base = self.fast_base()
            how = "JSON" if settings.fast_json else "страница"
            fast = f"1-я стр. раз в ~{base:.0f} с ({how})"
            left = self.slowed_until() - time.monotonic()
            if left > 0:
                fast += f", после блока ×{settings.block_slowdown_factor:g} ещё {left / 60:.0f} мин"
        models = len(self.store.tracked())
        if settings.fast_json and settings.model_sweep > 0 and models:
            fast += f" + по 1 модели (круг ~{models / settings.model_sweep * self.fast_base() / 60:.0f} мин)"
        deep = max(0.0, self.next_deep - time.monotonic())
        return f"{fast}; полный проход 5 стр. раз в {settings.check_interval_min} мин (через {deep / 60:.0f} мин)"

    def interval(self) -> float:
        lo = settings.check_interval_min
        return random.uniform(lo, max(lo, settings.check_interval_max))

    async def on_blocked(self, e: Exception):
        self.status["blocks"] += 1
        self.note("block")
        self.status["last_block"] = datetime.now()
        down = isinstance(e, ProxyDown)
        self.proxy_down = self.proxy_down + 1 if down else 0
        if not down:
            self.last_block = time.monotonic()
        log.warning("Блокировка Авито (%s) через %s", e, mask(self.avito.proxy))
        if self.proxy_down > 1:
            pause = min(PROXY_DOWN_PAUSE[1], PROXY_DOWN_PAUSE[0] * 2 ** (self.proxy_down - 2))
            log.info("Прокси не отвечает %s раз подряд — пауза %s с и смена IP", self.proxy_down, pause)
            await asyncio.sleep(pause)
        link_lost = self.proxy_down == 1
        if await self.rotate_ip(link_lost):
            return
        if self._rotating():
            log.info("Смена IP не удалась, повтор через %s с", ROTATE_RETRY_SEC)
            await asyncio.sleep(ROTATE_RETRY_SEC)
            if await self.rotate_ip(link_lost):
                return
        if await self._ask_admin() and await self.solve_by_admin():
            return
        if self._rotating():
            log.info("Смена IP не удалась, повтор через %s с", ROTATE_RETRY_SEC)
            await asyncio.sleep(ROTATE_RETRY_SEC)
            return
        proxy = await self.pool.next()
        if proxy:
            log.info("Переключаюсь на прокси %s", mask(proxy))
            self.status["proxy"] = mask(proxy)
            await self.avito.set_proxy(proxy)
            return
        mins = random.randint(settings.block_pause_min, max(settings.block_pause_min, settings.block_pause_max))
        self.status["pause_until"] = datetime.now().timestamp() + mins * 60
        log.warning("Все прокси заблокированы, пауза %s мин", mins)
        await self.notify_admins(f"⛔ Авито заблокировал все доступные IP ({e}).\n"
                                 f"Бот подождёт {mins} мин и попробует снова. "
                                 "Свои прокси можно добавить в PROXY_LIST (.env).")
        for _ in range(mins * 3):
            await asyncio.sleep(20)
            if not settings.headless and not await self.avito.page_blocked():
                break
        self.status["pause_until"] = None
        self.status["proxy"] = None
        self.pool.reset()
        await self.avito.set_proxy(self.pool.current)

    async def rotate_ip(self, link_lost: bool = False) -> bool:
        if not settings.proxy_url or self.avito.proxy != settings.proxy_url:
            return False
        if not settings.proxy_rotate_url and settings.proxy_rotate_every <= 0:
            return False
        now = time.monotonic()
        self.rotations = [t for t in self.rotations if now - t < 600] + [now]
        if settings.proxy_rotate_url:
            rotated = link_lost or await self._rotate_by_link()
            if rotated:
                await self._reconnect()
        else:
            rotated = await self._wait_rotation(link_lost)
        if rotated and not link_lost:
            try:
                await self.avito.new_session()
            except Exception as e:
                log.warning("Не удалось сбросить сессию Авито: %s", e)
        return rotated

    def _rotating(self) -> bool:
        rotates = settings.proxy_rotate_every > 0 or bool(settings.proxy_rotate_url)
        return rotates and bool(settings.proxy_url) and self.avito.proxy == settings.proxy_url

    def _captcha_muted(self) -> bool:
        return time.time() < float(self.store.get_state("captcha_muted_until", "0"))

    async def _ask_admin(self) -> bool:
        if not settings.captcha_relay or self._captcha_muted():
            return False
        return not self._rotating() or await self._captcha_shown()

    async def _captcha_shown(self) -> bool:
        try:
            return await self.avito.captcha_kind() is not None
        except Exception:
            return False

    async def _reconnect(self):
        try:
            await self.avito.restart()
        except Exception as e:
            log.warning("Не удалось перезапустить браузер после смены IP: %s", e)

    async def _rotate_by_link(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=ROTATE_TIMEOUT_SEC, trust_env=False) as c:
                r = await c.get(settings.proxy_rotate_url)
        except httpx.HTTPError as err:
            log.warning("Смена IP мобильного прокси не удалась: %s", err)
            return False
        try:
            status = r.json().get("status", "ok")
        except ValueError:
            status = "ok"
        if r.status_code != 200 or status != "ok":
            log.warning("Смена IP мобильного прокси не удалась: HTTP %s %s", r.status_code, r.text[:200])
            return False
        log.info("Смена IP мобильного прокси: HTTP %s", r.status_code)
        await asyncio.sleep(settings.proxy_rotate_wait)
        return True

    async def _wait_rotation(self, link_lost: bool) -> bool:
        old = await exit_ip(settings.proxy_url)
        if link_lost and old and self.proxy_ip and old != self.proxy_ip:
            log.info("IP мобильного прокси уже сменился: %s → %s", self.proxy_ip, old)
            self.proxy_ip = old
            return True
        log.info("Жду смену IP мобильного прокси (раз в %s с), сейчас %s", settings.proxy_rotate_every, old or "?")
        deadline = time.monotonic() + settings.proxy_rotate_every + 30
        while time.monotonic() < deadline:
            await asyncio.sleep(10)
            new = await exit_ip(settings.proxy_url)
            if new and new != old:
                log.info("IP мобильного прокси сменился: %s → %s", old or "?", new)
                self.proxy_ip = new
                return True
        log.warning("IP мобильного прокси не сменился за %s с", settings.proxy_rotate_every + 30)
        return False

    async def solve_by_admin(self) -> bool:
        try:
            kind = await self.avito.captcha_kind()
        except Exception as e:
            log.warning("Не удалось определить капчу: %s", e)
            kind = None
        link = self.remote.open() if self.remote else []
        if not link and kind != "text":
            return False
        self.status["captcha_wait"] = True
        await self.avito.show_media(True)
        log.info("Жду решения капчи админом (до %s мин), тип: %s", settings.captcha_wait_min, kind or "страница")
        admins = self.store.admin_ids(settings.admin_ids)
        try:
            image = await self.avito.captcha_image() if kind == "text" else await self.avito.page_screenshot()
            await self.captcha.ask(image, admins, captcha_caption(kind, link), with_new=kind == "text")
            deadline = time.monotonic() + settings.captcha_wait_min * 60
            while time.monotonic() < deadline:
                text = await self.captcha.wait(3 / 60)
                if text == NEW_IMAGE:
                    await self.avito.refresh_captcha()
                    await self.captcha.ask(await self.avito.captcha_image(), admins, captcha_caption(kind, link))
                elif text:
                    if await self.avito.submit_captcha(text):
                        log.info("Капча принята")
                        await self.captcha.notify(admins, "✅ Капча принята, продолжаю мониторинг.")
                        return True
                    await self.captcha.ask(await self.avito.captcha_image(), admins,
                                           "❌ Не подошло.\n" + captcha_caption(kind, link))
                elif not await self.avito.page_blocked():
                    log.info("Капча решена, доступ к Авито восстановлен")
                    await self.captcha.notify(admins, "✅ Капча решена, продолжаю мониторинг.")
                    return True
            self.store.set_state("captcha_muted_until", time.time() + settings.captcha_cooldown_min * 60)
            log.info("Капчу не решили за %s мин — следующая не раньше чем через %s мин, жду новые IP",
                     settings.captcha_wait_min, settings.captcha_cooldown_min)
            await self.captcha.notify(admins, f"⌛ Капчу не решили — продолжаю менять IP, "
                                              f"новую капчу пришлю не раньше чем через {settings.captcha_cooldown_min} мин.")
            return False
        except Exception as e:
            log.warning("Ошибка при решении капчи админом: %s", e)
            self.store.set_state("captcha_muted_until", time.time() + settings.captcha_cooldown_min * 60)
            return False
        finally:
            self.status["captcha_wait"] = False
            await self.avito.show_media(False)
            self.captcha.close()
            if self.remote:
                self.remote.close()

    async def cycle(self, pages: list[int] | None = None, light: bool = False):
        bootstrap = self.store.get_state("bootstrapped") != "1" and pages is None
        if pages is None:
            pages = range(1, max(1, settings.bootstrap_pages if bootstrap else 1 + settings.deep_pages) + 1)
        max_age = settings.max_age_hours
        reasons: Counter[str] = Counter()
        marks: Counter[str] = Counter()
        outcome: Counter[str] = Counter()
        seen: set[str] = set()
        try:
            for region, rname in REGIONS.items():
                sources = [(p, None) for p in pages] + [(1, m) for m in self.sweep_models(region, light)]
                for p, model in sources:
                    cards = await self.model_cards(region, rname, model) if model else \
                        await self.page_cards(region, rname, p, light)
                    if not cards and not model:
                        break
                    fresh_on_page = 0
                    candidates = []
                    for card in cards:
                        fresh = self.fresh_enough(card, p, max_age)
                        fresh_on_page += fresh
                        reasons[self.record(card, rname, fresh)] += 1
                        marks[card.avito_mark or "нет"] += 1
                        if f"avito.ru/{region}/" in card.url and fresh and card.id not in seen:
                            seen.add(card.id)
                            candidates.append(card)
                    candidates.sort(key=freshness)
                    for card in candidates:
                        outcome[await self.consider(card, rname)] += 1
                    if not fresh_on_page and not bootstrap and not model:
                        break
        finally:
            log.info("Метки Авито за цикл: %s", ", ".join(f"{k} {v}" for k, v in marks.most_common()))
            log.info("Рынок за цикл: %s", ", ".join(f"{k} {v}" for k, v in reasons.most_common(10)))
            log.info("Кандидаты за цикл: %s", ", ".join(f"{k} {v}" for k, v in outcome.most_common()) or "нет")
        self.proxy_down = 0
        await self.recheck()
        if bootstrap:
            self.store.set_state("bootstrapped", "1")
        if settings.avito_eval and not self.refreshing and (is_night() or self.store.evals_stale(self.store.tracked())):
            self.eval_in_background()
        self.status["last_check"] = datetime.now()
        self.note("check")

    def sweep_models(self, region: str, light: bool) -> list[str]:
        if not (light and settings.fast_json and settings.model_sweep > 0):
            return []
        if time.monotonic() < self.sweep_paused_until:
            return []
        models = [m for m in self.store.tracked() if m in MODEL_IDS]
        if not models:
            return []
        picked = [models[(self.sweep + i) % len(models)] for i in range(min(settings.model_sweep, len(models)))]
        self.sweep = (self.sweep + len(picked)) % len(models)
        return picked

    async def location_id(self) -> int | None:
        if self.location is None and time.monotonic() >= self.location_retry_at:
            try:
                self.location = await self.evaluation.location_id()
                log.info("ID города %s на Авито: %s", settings.avito_eval_city, self.location)
            except Exception as e:
                self.location_retry_at = time.monotonic() + LOCATION_RETRY_SEC
                log.warning("Не удалось узнать ID города %s (%s) — пока обычные страницы, повтор через %s мин",
                            settings.avito_eval_city, e, LOCATION_RETRY_SEC // 60)
        return self.location

    async def model_cards(self, region: str, rname: str, model: str) -> list[Card]:
        location = await self.location_id()
        if not location:
            return []
        data = await self.avito.catalog(catalog_path(location, 1, model))
        if data is None:
            self.sweep_paused_until = time.monotonic() + SWEEP_PAUSE_SEC
            log.info("Перебор по моделям: Авито не отдаёт список iPhone %s — пауза %s мин", model, SWEEP_PAUSE_SEC // 60)
            return []
        cards = parse_catalog(data)
        log.info("%s iPhone %s (JSON): %s объявлений", rname, model, len(cards))
        return cards

    async def page_cards(self, region: str, rname: str, page: int, light: bool) -> list[Card]:
        if light and settings.fast_json and (location := await self.location_id()):
            cards = parse_catalog(await self.avito.catalog(catalog_path(location, page)) or {})
            if cards:
                log.info("%s стр.%s (JSON): %s объявлений", rname, page, len(cards))
                return cards
        if light:
            self.note("html")
        return await self.search_page(region, rname, page)

    async def search_page(self, region: str, rname: str, page: int) -> list[Card]:
        html = await self.avito.fetch(search_url(region, page))
        cards = parse_search(html)
        if not cards:
            (ROOT / "data" / f"empty_{region}_p{page}.html").write_text(html, encoding="utf-8")
            log.info("%s стр.%s пустая, повторяю", rname, page)
            cards = parse_search(await self.avito.fetch(search_url(region, page)))
        log.info("%s стр.%s: %s объявлений", rname, page, len(cards))
        return cards

    @staticmethod
    def fresh_enough(card: Card, page: int, max_age_hours: float) -> bool:
        age = card.age_hours
        return page == 1 if age is None else age <= max_age_hours

    def record(self, card: Card, region: str, fresh: bool = True) -> str:
        phone = detect_phone(card.title, card.snippet)
        if not phone:
            return "не iPhone/модель"
        if not phone.storage_gb or not card.price:
            return "нет памяти/цены"
        shop_id = card.seller_url
        reason = market_exclusion(card)
        if shop_id and reason.startswith("магазин"):
            self.store.add_shop(shop_id, reason)
        if not reason and self.store.is_shop(shop_id):
            reason = SHOP_SELLER
        reason = reason or ("" if fresh else "старше суток")
        self.store.save_listing(card.id, card.url, phone.model, phone.storage_gb, card.price, region,
                                not reason, card.seller, card.avito_mark)
        return reason or "в рынок"

    async def consider(self, card: Card, region: str) -> str:
        phone = detect_phone(card.title, card.snippet)
        if not phone or not card.price:
            return "не iPhone/без цены"
        sent = self.store.get_sent(card.id)
        if sent:
            try:
                await self.report_price_drop(card, *sent)
            except Exception as e:
                log.warning("Не удалось сообщить о снижении цены %s: %s", card.url, e)
                self.store.update_sent_price(card.id, card.price)
            return "уже в группе"
        if phone.model not in self.store.tracked():
            return "модель не отслеживается"
        if self.store.is_shop(card.seller_url):
            return SHOP_SELLER
        if self.store.was_checked(card.id, card.price):
            return "проверено ранее"
        quick = f"{card.title}\n{card.snippet}"
        allowed = self.allowed()
        quick_dmg = inspect_damage(quick, card.condition, "warn", allowed)
        if (blocked(reject_reason(card.title, card.snippet), allowed) or is_shop_text(quick)
                or not quick_dmg.accepted and quick_dmg.reason != "состояние не подтверждено"):
            self.store.mark_checked(card.id, card.price)
            return "отсев по тексту"
        market = self.reference(phone, region, card.id, eval_condition(card.condition))
        if card.avito_mark == "above":
            return "выше рыночной (Авито)"
        if card.below_market and settings.market_source != "eval":
            if not market or card.price > market["median"] - settings.min_profit:
                market = {"price": None, "median": None, "samples": 0, "source": AVITO}
        elif not market:
            return "нет цены рынка"
        elif card.price > self.market_limit(market) - settings.min_profit:
            return "нет выгоды"
        early = self.post_early(card, region, phone, market, quick)
        await self.deep_check(card, region, phone, market, early)
        return "мгновенно" if early else "открыто"

    async def explain(self, url: str) -> list[str]:
        html = await self.avito.fetch(url)
        if is_closed(html):
            return ["❌ Объявление снято с публикации"]
        item = parse_item(html)
        params_text = "\n".join(f"{k}: {v}" for k, v in item.params.items())
        full = f"{item.title}\n{item.description}\n{params_text}"
        condition = item.params.get("Состояние", "")
        lines = [f"📄 {item.title or '?'}", f"💰 {money(item.price)} · состояние «{condition or 'не указано'}»",
                 f"🕒 {item.posted or '?'} · 👤 {item.seller or '?'} ({item.seller_label or 'метка не видна'})"]
        problems = []
        phone = detect_phone(item.title, full)
        if not phone:
            problems.append("модель не распознана или это не iPhone X–17")
        else:
            lines.append(f"📱 iPhone {phone.model}, {fmt_storage(phone.storage_gb)}")
            if phone.model not in self.store.tracked():
                problems.append(f"модель {phone.model} выключена в /list")
        seller_url = item.seller_url
        if item.seller_label and "частн" not in item.seller_label.lower():
            problems.append(f"продавец не частное лицо («{item.seller_label}»)")
        if seller_url and self.store.is_shop(seller_url):
            problems.append("продавец в чёрном списке магазинов (/unblock ссылка)")
        if is_shop_text(full):
            problems.append("текст похож на магазин")
        reject = reject_reason(item.title, full)
        if blocked(reject, self.allowed()):
            problems.append(f"отсев: {reject}")
        dmg, _ = self.assess(full, condition)
        if not dmg.accepted:
            problems.append(f"состояние/поломка: {dmg.reason}")
        elif dmg.warnings:
            lines.append("⚠️ " + "; ".join(dmg.warnings))
        if phone:
            market = self.eval_market(phone, eval_condition(condition))
            if not market:
                problems.append("нет вилки калькулятора для этой модели/памяти")
            elif item.price:
                need = settings.min_profit_broken if dmg.damage else settings.min_profit
                limit = market["max"] - need
                lines.append(f"📊 вилка «{market['condition']}»: {market['min']:,}–{market['max']:,} ₽, порог {limit:,} ₽"
                             .replace(",", " "))
                if item.price > limit:
                    problems.append(f"дороже порога на {money(item.price - limit)}")
                else:
                    lines.append("🔥 НИЖЕ РЫНКА" if item.price < market["min"] else "✅ В РЫНКЕ")
        lines.append("❌ Не прошло бы: " + "; ".join(problems) if problems else "✅ Прошло бы — бот опубликует")
        return lines

    def eval_market(self, phone, condition: str) -> dict | None:
        storages = [phone.storage_gb] if phone.storage_gb else self.store.storages_seen(phone.model)
        for used in (condition, *NEIGHBORS.get(condition, ())):
            for storage in storages:
                ev = self.store.get_eval(phone.model, storage, used)
                if ev:
                    avg = eval_price(*ev, "avg")
                    return {"price": avg, "median": avg, "samples": 0, "source": EVAL, "min": ev[0], "max": ev[1],
                            "condition": used, "wanted": condition}
        return None

    @staticmethod
    def market_limit(market: dict) -> int:
        return market["max"] if market["source"] == EVAL else market["median"]

    def reference(self, phone, region: str, exclude_id: str | None = None,
                  condition: str = "Хорошее") -> dict | None:
        if settings.market_source == "eval":
            return self.eval_market(phone, condition)
        market = self.store.market(phone.model, phone.storage_gb, region, settings.market_window_hours,
                                   settings.market_min_samples, settings.market_discount, exclude_id)
        if market["source"] == "ручная цена":
            return market
        evaluated = self.eval_market(phone, condition)
        if evaluated:
            return evaluated
        base = base_price(self.base_prices, phone.model, phone.storage_gb)
        base_market = {"price": base, "median": base, "samples": 0, "source": BASE} if base else None
        avito = self.store.avito_market(phone.model, phone.storage_gb, settings.max_age_hours,
                                        settings.market_discount, exclude_id)
        if avito:
            return avito
        if base_market and settings.base_prices_first:
            return base_market
        if market["price"] and self.store.model_samples(
                phone.model, settings.market_window_hours, exclude_id) >= settings.market_publish_samples:
            return market
        return base_market

    async def report_price_drop(self, card: Card, old_price: int, posts: list[tuple[int, int]]):
        if card.price >= old_price:
            return
        text = f"📉 Цена снижена: {money(old_price)} → {money(card.price)}"
        if not posts:
            await self.notify_admins(f"{text}\n{card.url}")
        for chat_id, mid in posts:
            try:
                await self.bot.send_message(chat_id, text, reply_to_message_id=mid)
            except Exception as e:
                log.warning("Не удалось сообщить о снижении цены в %s: %s", chat_id, e)
        self.store.update_sent_price(card.id, card.price)

    def post_early(self, card, region, phone, market, text):
        if not (settings.instant_below_min and settings.ai_provider.lower() == "none" and market["source"] == EVAL
                and (settings.instant_all or card.price < market["min"])):
            return None
        dmg, _ = self.assess(text, card.condition)
        key = seller_key(card.seller_url, card.seller)
        if not dmg.accepted or not self.claim(key, phone, card.price):
            return None
        caption = self.caption(card, region, phone, market, dmg, None, card.seller, card.snippet)
        task = asyncio.create_task(self._post_early(card, phone, caption, key or card.seller))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        log.info("Мгновенный пост: %s", card.url)
        return task

    async def _post_early(self, card, phone, caption, seller):
        photos = await download_photos(card.photos, settings.max_photos)
        return await self.publish(card, phone, photos, caption, seller), bool(photos)

    async def deep_check(self, card, region, phone, market, early=None):
        try:
            checked = await self.verify(card, region, phone, market, claimed=early is not None)
        except Exception:
            if early:
                self.unverified[card.id] = (card, region, phone, market, early)
                log.warning("Мгновенный пост не перепроверен из-за ошибки, повторю позже: %s", card.url)
            raise
        if early:
            self.spawn(self.settle(early, card, region, checked))
        elif checked:
            self.spawn(self.finish(card, region, *checked))

    async def recheck(self):
        if not self.unverified:
            return
        card, region, phone, market, early = self.unverified.pop(next(iter(self.unverified)))
        log.info("Перепроверяю мгновенный пост: %s", card.url)
        await self.deep_check(card, region, phone, market, early)

    async def settle(self, early, card, region, checked):
        sent, with_photos = await early
        if not checked:
            await self._delete(card, sent)
            self.store.unmark_sent(card.id)
            self.status["found"] -= 1
            self.note("removed")
            log.info("Мгновенный пост снят после проверки: %s", card.url)
            return
        phone, market, dmg, item, _, _, key = checked
        seller = item.seller or card.seller
        caption = self.caption(card, region, phone, market, dmg, None, seller, item.description or card.snippet,
                               item.posted)
        photos = await download_photos(item.photos, settings.max_photos) if item.photos else []
        if photos and not with_photos:
            await self.publish(card, phone, photos, caption, key or seller)
            self.status["found"] -= 1
            await self._delete(card, sent)
            log.info("Мгновенный пост без фото заменён постом с фото: %s", card.url)
            return
        for chat, ids in sent:
            if photos and with_photos:
                await self._edit_album(chat, ids, photos, caption)
            else:
                await self._edit_caption(chat, ids[0], caption)

    async def _edit_album(self, chat: int, ids: list[int], photos: list[bytes], caption: str):
        for i, (message_id, photo) in enumerate(zip(ids, photos)):
            media = InputMediaPhoto(media=BufferedInputFile(photo, f"{i}.jpg"), caption=caption if i == 0 else None)
            try:
                await self.bot.edit_message_media(media=media, chat_id=chat, message_id=message_id)
            except Exception as e:
                log.warning("Не удалось заменить фото поста в %s: %s", chat, e)
                if i == 0:
                    await self._edit_caption(chat, message_id, caption)
                return

    async def _delete(self, card, sent):
        for chat, ids in sent:
            try:
                await self.bot.delete_messages(chat, ids)
            except Exception as e:
                log.warning("Не удалось удалить пост %s в %s: %s", card.url, chat, e)

    async def _edit_caption(self, chat: int, message_id: int, caption: str):
        try:
            await self.bot.edit_message_caption(chat_id=chat, message_id=message_id, caption=caption)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            try:
                await self.bot.edit_message_text(caption, chat_id=chat, message_id=message_id)
            except TelegramBadRequest as e2:
                if "not modified" not in str(e2):
                    log.warning("Не удалось обновить пост в %s: %s", chat, e2)
        except Exception as e:
            log.warning("Не удалось обновить пост в %s: %s", chat, e)

    def assess(self, text: str, condition: str):
        allowed = self.allowed()
        dmg = inspect_damage(text, condition, settings.unclear_mode, allowed)
        ai_on = settings.ai_provider.lower() != "none"
        unclear = not dmg.accepted and dmg.reason == UNCLEAR and "unclear" in allowed
        if unclear:
            note = "проверено ИИ" if ai_on else "уточнить у продавца"
            dmg = replace(dmg, accepted=True, warnings=[f"состояние в объявлении не описано — {note}",
                                                        "уточнить у продавца: Face ID, iCloud"])
        return dmg, unclear and ai_on

    def allowed(self) -> frozenset[str]:
        return self.store.allowed_defects(DEFAULT_ALLOWED)

    def claim(self, key: str, phone, price: int) -> bool:
        dup = (key, phone.model, phone.storage_gb, price)
        now = time.monotonic()
        self.claimed = {k: t for k, t in self.claimed.items() if now - t < 86400}
        if key and (dup in self.claimed or self.store.find_duplicate(key, *dup[1:], hours=24)):
            return False
        if key:
            self.claimed[dup] = now
        return True

    async def verify(self, card, region, phone, market, claimed=False):
        html = await self.avito.fetch(card.url)
        self.store.mark_checked(card.id, card.price)
        if is_closed(html):
            log.info("Снято с публикации: %s", card.url)
            return None
        item = parse_item(html)
        seen = self.store.seen_at(card.id)
        lag = posted_lag(item.posted, datetime.fromisoformat(seen)) if seen else None
        if lag is not None:
            self.lags.append(lag)
        log.info("Свежесть: опубликовано «%s», бот увидел %s, проверено %s — %s", item.posted or "?",
                 seen[11:19] if seen else "?", f"{datetime.now():%H:%M:%S}", card.url)
        params_text = "\n".join(f"{k}: {v}" for k, v in item.params.items())
        full = f"{card.title}\n{item.description or card.snippet}\n{params_text}"
        key = seller_key(item.seller_url or card.seller_url, item.seller or card.seller)
        if item.seller_label and "частн" not in item.seller_label.lower():
            self.store.add_shop(item.seller_url or card.seller_url, f"метка продавца «{item.seller_label}»")
            log.info("Отсев %s: продавец «%s»", card.url, item.seller_label)
            return None
        if is_shop_text(full):
            self.store.add_shop(item.seller_url or card.seller_url, "магазин по тексту объявления")
            log.info("Отсев %s: магазин по тексту", card.url)
            return None
        reject = reject_reason(card.title, full)
        if blocked(reject, self.allowed()):
            log.info("Отсев %s: %s", card.url, reject)
            return None
        if not phone.storage_gb:
            phone = detect_phone(card.title, full) or phone
        condition = item.params.get("Состояние", card.condition)
        dmg, unclear = self.assess(full, condition)
        if not dmg.accepted:
            log.info("Отсев %s: %s", card.url, dmg.reason)
            return None
        if market["source"] == EVAL:
            market = self.eval_market(phone, eval_condition(condition))
            if not market:
                log.info("Отсев %s: нет оценки калькулятора для «%s»", card.url, condition)
                return None
        need = settings.min_profit_broken if dmg.damage else settings.min_profit
        if not card.below_market and card.price > self.market_limit(market) - need:
            log.info("Мала выгода %s: %s при пороге %s", card.url, card.price, self.market_limit(market))
            return None
        seller_url = item.seller_url or card.seller_url
        if not claimed and not self.claim(key, phone, card.price):
            log.info("Дубль продавца %s: %s", key, card.url)
            return None
        if seller_url and settings.reseller_max_phones > 0:
            phones = count_seller_phones(await self.avito.fetch(seller_url))
            if phones >= settings.reseller_max_phones:
                log.info("Перекуп (%s телефонов): %s", phones, card.url)
                return None
        return phone, market, dmg, item, full, unclear, key

    def spawn(self, job):
        task = asyncio.create_task(self._limited(job))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _limited(self, job):
        async with self.ai_slots:
            try:
                await job
            except Exception:
                log.exception("Ошибка проверки объявления")

    async def drain(self):
        while self.pending:
            await asyncio.gather(*list(self.pending))

    async def finish(self, card, region, phone, market, dmg, item, full, need_ai_ok=False, key=""):
        photos = await download_photos(item.photos or card.photos, max(settings.max_photos, settings.ai_max_photos))
        ai = None
        if settings.ai_provider.lower() != "none":
            ai = await analyze(full, photos[:settings.ai_max_photos])
            if ai is None:
                self.status["ai_fail"] += 1
            elif ai_rejects(ai, full):
                log.info("ИИ отсеял %s: %s", card.url, ai_confirmed_flags(ai, full) or ai)
                return
        if need_ai_ok and not (ai and ai.get("suitable") is True):
            log.info("Отсев %s: %s, ИИ не подтвердил", card.url, UNCLEAR)
            return
        seller = item.seller or card.seller
        caption = self.caption(card, region, phone, market, dmg, ai, seller, item.description or card.snippet,
                               item.posted)
        await self.publish(card, phone, photos[:settings.max_photos], caption, key or seller)

    def caption(self, card, region, phone, market, dmg, ai, seller, description="", posted=""):
        deal = card.below_market or bool(market["price"]) and card.price <= market["price"] * settings.deal_threshold
        header = "🔥 ВЫГОДНО"
        if market["source"] == EVAL:
            deal = True
            header = "🔥 НИЖЕ РЫНКА" if card.price < market["min"] else "✅ В РЫНКЕ"
            used = f", по «{market['condition']}»" if market["condition"] != market["wanted"] else ""
            low = f"{market['min']:,}".replace(",", " ")
            price_line = (f"💰 {money(card.price)} (рынок Авито {low}–{money(market['max'])}, "
                          f"средняя {money(market['median'])}{used})")
        elif market["source"] == AVITO:
            price_line = f"💰 {money(card.price)} (ниже рыночной — оценка Авито)"
        elif market["source"] == BASE:
            price_line = f"💰 {money(card.price)} (продаём ≈ {money(market['price'])}, выгода {money(market['price'] - card.price)})"
        else:
            mk = f"по {market['samples']} объявлениям" if market["samples"] else market["source"]
            price_line = f"💰 {money(card.price)} (рынок ≈ {money(market['price'])} {mk})"
        lines = [header] if deal else []
        lines += [f"📱 iPhone {phone.model}, {fmt_storage(phone.storage_gb)}", price_line]
        for w in dmg.warnings:
            lines.append(f"⚠️ {w}")
        known = [x for x, ok in (("Face ID", dmg.faceid_ok), ("iCloud чистый", dmg.icloud_ok)) if ok]
        if known:
            lines.append("✅ " + ", ".join(known))
        if dmg.battery:
            lines.append(f"🔋 АКБ {dmg.battery}%")
        if ai:
            if ai.get("suitable") is True and ai.get("verdict"):
                lines.append(f"🤖 {ai['verdict']}"[:300])
            else:
                lines.append("🤖 ИИ: других поломок не найдено")
        elif settings.ai_provider.lower() != "none":
            lines.append("🤖 ИИ недоступен — проверено по ключевым словам")
        quote = " ".join(description.split())
        if quote:
            lines.append("📝 «" + (quote[:220] + "…" if len(quote) > 220 else quote) + "»")
        if posted or card.date:
            lines.append(f"🕒 {posted or card.date}")
        place = region if not card.location or region.lower() in card.location.lower() else f"{region}, {card.location}"
        lines.append(f"📍 {place} · частное лицо" + (f" ({seller})" if seller else ""))
        lines.append(f"🔗 {card.url}")
        return "\n".join(lines)[:1024]

    async def publish(self, card, phone, photos, caption, seller):
        chats = self.store.channels(settings.group_chat_id)
        results = await asyncio.gather(*(self._post(chat, photos, caption) for chat in chats))
        sent = [(chat, ids) for chat, (ids, _) in zip(chats, results) if ids]
        posts = [(chat, ids[0]) for chat, ids in sent]
        errors = [f"{chat}: {error}" for chat, (ids, error) in zip(chats, results) if not ids]
        for error in errors:
            log.warning("Не удалось отправить в группу %s", error)
        if not posts:
            reason = "; ".join(errors) or "не задано ни одной группы"
            await self.notify_admins(f"⚠️ Группы недоступны ({reason}), находка:\n\n{caption}")
        self.store.mark_sent(card.id, phone.model, phone.storage_gb, card.price, seller, posts)
        self.status["found"] += 1
        self.note("post")
        return sent

    async def _post(self, chat: int, photos: list[bytes], caption: str) -> tuple[list[int], Exception | None]:
        error = None
        attempt = 0
        while attempt < 3:
            wait = 10
            try:
                if photos and attempt < 2:
                    media = [InputMediaPhoto(media=BufferedInputFile(p, f"{i}.jpg"), caption=caption if i == 0 else None)
                             for i, p in enumerate(photos)]
                    return [m.message_id for m in await self.bot.send_media_group(chat, media)], None
                return [(await self.bot.send_message(chat, caption)).message_id], None
            except TelegramForbiddenError as e:
                return [], e
            except TelegramRetryAfter as e:
                error, wait = e, e.retry_after
            except TelegramBadRequest as e:
                if not photos or attempt >= 2:
                    return [], e
                error, wait, attempt = e, 0, 1
            except Exception as e:
                error = e
            attempt += 1
            if attempt < 3 and wait:
                await asyncio.sleep(wait)
        return [], error
