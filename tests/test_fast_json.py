import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import bot.monitor as mon
from bot.catalog import catalog_path, parse_catalog
from bot.config import settings
from bot.db import Store
from bot.parser import Card

FIX = Path(__file__).parent / "fixtures"


def _store(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.set_state("eval_location:Краснодар", 633540)
    return store


def _catalog():
    return json.loads((FIX / "catalog_krasnodar.json").read_text(encoding="utf-8"))


def test_parse_catalog_maps_search_fields():
    cards = parse_catalog(_catalog())
    assert [c.id for c in cards] == ["8369128922", "8340298156", "8368811566"]
    first, second, _ = cards
    assert first.title == "iPhone 11, 128 ГБ" and first.price == 10000 and first.condition == "Хорошее"
    assert first.url == "https://www.avito.ru/krasnodar/telefony/iphone_11_128_gb_8369128922"
    assert first.seller == "Продавец 2" and first.seller_url == "https://www.avito.ru/brands/f43bfdc8811879bff12c5dda239c2fd0"
    assert first.date == "1 час назад" and first.delivery and "636x636" not in first.photos[0]
    assert second.below_market and second.location == "р-н Центральный"
    assert "треснутая задняя" in second.snippet
    assert parse_catalog({}) == [] and parse_catalog(None) == []


def test_catalog_path_has_region_and_page():
    path = catalog_path(633540, 2)
    assert path.startswith("/web/1/js/items?") and "locationId=633540" in path and "&p=2&" in path
    assert "s=104" in path and "user=1" in path


class Avito:
    def __init__(self, data=None, item_html=""):
        self.data, self.item_html, self.events = data, item_html, []

    async def catalog(self, path):
        self.events.append(("catalog", path))
        return self.data

    async def fetch(self, url):
        await asyncio.sleep(0.05)
        self.events.append(("fetch", url))
        return self.item_html


def test_light_cycle_uses_json_and_falls_back_to_html(tmp_path, monkeypatch):
    html_pages = []

    async def search_page(self, region, rname, page):
        html_pages.append(page)
        return []

    monkeypatch.setattr(mon.Monitor, "search_page", search_page)
    monkeypatch.setattr(settings, "fast_json", True)
    m = mon.Monitor(None, _store(tmp_path), Avito(_catalog()))
    cards = asyncio.run(m.page_cards("krasnodar", "Краснодар", 1, light=True))
    assert len(cards) == 3 and html_pages == []
    m.avito = Avito(None)
    asyncio.run(m.page_cards("krasnodar", "Краснодар", 1, light=True))
    asyncio.run(m.page_cards("krasnodar", "Краснодар", 1, light=False))
    assert html_pages == [1, 1] and len(m.avito.events) == 1


def test_fast_delay_uses_json_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "fast_json", True)
    monkeypatch.setattr(settings, "fast_json_sec", 15)
    monkeypatch.setattr(settings, "fast_jitter_sec", 10)
    monkeypatch.setattr(settings, "fast_turbo_sec", 30)
    monkeypatch.setattr(mon, "is_night", lambda: False)
    m = mon.Monitor(None, Store(tmp_path / "t.sqlite"), None)
    assert all(11.25 <= m.fast_delay() <= 18.75 for _ in range(50))


class Bot:
    def __init__(self, events):
        self.events, self.deleted, self.edited, self.next_id = events, [], [], 10

    async def send_message(self, chat, text, **kw):
        self.events.append(("post", text))
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)

    async def send_media_group(self, chat, media):
        self.events.append(("post", media[0].caption))
        return [SimpleNamespace(message_id=i) for i in (1, 2)]

    async def delete_messages(self, chat, ids):
        self.deleted.append((chat, ids))

    async def edit_message_caption(self, chat_id, message_id, caption):
        self.edited.append((chat_id, message_id, caption))

    async def edit_message_media(self, media, chat_id, message_id):
        self.edited.append((chat_id, message_id, media.caption))


def _below_market(tmp_path, monkeypatch, item_html, price=30000):
    async def no_photos(urls, limit):
        return []

    monkeypatch.setattr(mon, "download_photos", no_photos)
    monkeypatch.setattr(settings, "market_source", "eval")
    monkeypatch.setattr(settings, "ai_provider", "none")
    monkeypatch.setattr(settings, "instant_below_min", True)
    monkeypatch.setattr(settings, "reseller_max_phones", 0)
    monkeypatch.setattr(settings, "group_chat_id", -5)
    store = Store(tmp_path / "t.sqlite")
    for condition in ("Отличное", "Хорошее"):
        store.save_eval("14 Pro", 128, condition, 40000, 45000)
    avito = Avito(item_html=item_html)
    bot = Bot(avito.events)
    m = mon.Monitor(bot, store, avito)
    card = Card(id="77", title="iPhone 14 Pro, 128 ГБ", price=price, url="https://www.avito.ru/krasnodar/x_77",
                snippet="Целый, всё работает, Face ID работает", condition="Хорошее",
                seller="Иван", seller_url="https://www.avito.ru/brands/ivan")

    async def run():
        outcome = await m.consider(card, "Краснодар")
        await m.drain()
        return outcome

    return m, bot, avito, store, asyncio.run(run())


def test_below_min_posts_before_item_check_and_updates_caption(tmp_path, monkeypatch):
    item = (FIX / "item_14pro.html").read_text(encoding="utf-8")
    m, bot, avito, store, outcome = _below_market(tmp_path, monkeypatch, item)
    assert outcome == "мгновенно"
    assert [e[0] for e in avito.events] == ["post", "fetch"]
    assert "🔥 НИЖЕ РЫНКА" in avito.events[0][1]
    assert bot.deleted == [] and store.get_sent("77") is not None
    assert len(bot.edited) == 1 and bot.edited[0][:2] == (-5, 11)


def test_below_min_post_is_removed_when_item_check_rejects(tmp_path, monkeypatch):
    m, bot, avito, store, outcome = _below_market(tmp_path, monkeypatch, "<main>Снято с публикации</main>")
    assert outcome == "мгновенно"
    assert bot.deleted == [(-5, [11])] and store.get_sent("77") is None
    assert store.was_checked("77", 30000) and m.status["found"] == 0


def test_in_range_price_is_not_posted_early_unless_instant_all(tmp_path, monkeypatch):
    item = (FIX / "item_14pro.html").read_text(encoding="utf-8")
    monkeypatch.setattr(settings, "instant_all", True)
    assert _below_market(tmp_path / "a", monkeypatch, item, price=41000)[4] == "мгновенно"
    monkeypatch.setattr(settings, "instant_all", False)
    m, bot, avito, store, outcome = _below_market(tmp_path / "b", monkeypatch, item, price=41000)
    assert outcome == "открыто" and avito.events[0][0] == "fetch" and bot.edited == []


def test_lower_conditions_are_priced_as_good_and_old_ranges_dropped(tmp_path, monkeypatch):
    from bot.evaluation import eval_condition
    from bot.models import PhoneModel
    monkeypatch.setattr(settings, "market_source", "eval")
    store = Store(tmp_path / "t.sqlite")
    store.save_eval("12 Pro Max", 256, "Хорошее", 16262, 18338)
    store.save_eval("12 Pro Max", 256, "Удовлетворительное", 12765, 14394)
    m = mon.Monitor(None, store, None)
    assert store.get_eval("12 Pro Max", 256, "Удовлетворительное") is None
    for condition in ("Удовлетворительное", "Требуется ремонт", ""):
        market = m.reference(PhoneModel("12 Pro Max", 256), "Краснодар", None, eval_condition(condition))
        assert market["condition"] == "Хорошее" and market["min"] == 16262
        assert 15000 <= m.market_limit(market) - settings.min_profit


def test_catalog_path_filters_one_model():
    assert "params%5B110617%5D%5B%5D=1642358" in catalog_path(633540, 1, "13")
    assert "110617" not in catalog_path(633540, 1)


def test_light_cycle_sweeps_models_round_robin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "fast_json", True)
    monkeypatch.setattr(settings, "model_sweep", 2)
    store = _store(tmp_path)
    monkeypatch.setattr(store, "tracked", lambda: ["13", "14", "15 Pro"])
    avito = Avito(_catalog())
    m = mon.Monitor(None, store, avito)

    async def consider(card, region):
        return "нет выгоды"

    monkeypatch.setattr(m, "consider", consider)
    asyncio.run(m.cycle(pages=[1], light=True))
    asyncio.run(m.cycle(pages=[1], light=True))
    paths = [p for kind, p in avito.events if kind == "catalog"]
    swept = [p.rsplit("=", 1)[1] for p in paths if "110617" in p]
    assert len(paths) == 6 and swept == ["1642358", "17605542", "18720713", "1642358"]
    assert m.sweep_models("krasnodar", light=False) == []


def test_text_only_early_post_is_replaced_by_album_after_check(tmp_path, monkeypatch):
    item = (FIX / "item_14pro.html").read_text(encoding="utf-8")
    item_photos = []

    async def photos(urls, limit):
        item_photos.append(list(urls))
        return [b"jpg"] if len(item_photos) > 1 else []

    m, bot, avito, store, outcome = _below_market(tmp_path, monkeypatch, item)
    monkeypatch.setattr(mon, "download_photos", photos)
    card = Card(id="78", title="iPhone 14 Pro, 128 ГБ", price=30000, url="https://www.avito.ru/krasnodar/x_78",
                snippet="Целый, всё работает", condition="Хорошее", seller="Пётр",
                seller_url="https://www.avito.ru/brands/petr")
    avito.events.clear()

    async def run():
        outcome = await m.consider(card, "Краснодар")
        await m.drain()
        return outcome

    assert asyncio.run(run()) == "мгновенно"
    assert [e[0] for e in avito.events] == ["post", "fetch", "post"]
    assert bot.deleted and bot.deleted[-1][0] == -5 and store.get_sent("78")[1] == [(-5, 1)]


def test_early_album_photos_are_upgraded_after_check(tmp_path, monkeypatch):
    item = (FIX / "item_14pro.html").read_text(encoding="utf-8")
    m, bot, avito, store, _ = _below_market(tmp_path, monkeypatch, item)

    async def photos(urls, limit):
        return [b"a", b"b"]

    monkeypatch.setattr(mon, "download_photos", photos)
    card = Card(id="79", title="iPhone 14 Pro, 128 ГБ", price=30000, url="https://www.avito.ru/krasnodar/x_79",
                snippet="Целый, всё работает", condition="Хорошее", seller="Олег",
                seller_url="https://www.avito.ru/brands/oleg", photos=["https://img/1", "https://img/2"])
    bot.edited.clear()

    async def run():
        await m.consider(card, "Краснодар")
        await m.drain()

    asyncio.run(run())
    assert [(c, mid) for c, mid, _ in bot.edited] == [(-5, 1), (-5, 2)]
    assert "🔥 НИЖЕ РЫНКА" in bot.edited[0][2] and bot.edited[1][2] is None and not bot.deleted


def test_unverified_instant_post_is_rechecked_next_cycle(tmp_path, monkeypatch):
    from bot.avito import Blocked
    m, bot, avito, store, _ = _below_market(tmp_path, monkeypatch, "<main>Снято с публикации</main>")
    calls = []
    real_fetch = avito.fetch

    async def flaky_fetch(url):
        calls.append(url)
        if len(calls) == 1:
            raise Blocked("Доступ ограничен")
        return await real_fetch(url)

    avito.fetch = flaky_fetch
    card = Card(id="80", title="iPhone 14 Pro, 128 ГБ", price=30000, url="https://www.avito.ru/krasnodar/x_80",
                snippet="Целый, всё работает", condition="Хорошее", seller="Ира",
                seller_url="https://www.avito.ru/brands/ira")

    async def first():
        try:
            await m.consider(card, "Краснодар")
        except Blocked:
            pass
        await m.drain()

    asyncio.run(first())
    assert "80" in m.unverified and store.get_sent("80") is not None

    async def second():
        await m.recheck()
        await m.drain()

    asyncio.run(second())
    assert not m.unverified and store.get_sent("80") is None and bot.deleted


def test_model_sweep_pauses_when_avito_refuses_model_list(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "fast_json", True)
    monkeypatch.setattr(settings, "model_sweep", 2)
    store = _store(tmp_path)
    m = mon.Monitor(None, store, Avito(None))
    assert asyncio.run(m.model_cards("krasnodar", "Краснодар", "13")) == []
    assert m.sweep_models("krasnodar", light=True) == []


def test_night_refresh_treats_ranges_from_before_this_night_as_stale(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    monkeypatch.setattr(mon, "is_night", lambda now=None: True)
    store = Store(tmp_path / "t.sqlite")
    store.save_eval("13", 128, "Хорошее", 40000, 45000)
    m = mon.Monitor(None, store, None)
    assert m.evaluation._is_fresh("13", 128, "Хорошее")
    m.evaluation.fresh_since = datetime.now() + timedelta(minutes=1)
    assert not m.evaluation._is_fresh("13", 128, "Хорошее")
    assert mon.night_start(datetime(2026, 10, 18, 3, 30)) == datetime(2026, 10, 18, settings.night_hours[0])


def test_unknown_city_id_falls_back_to_pages_and_retries_later(tmp_path, monkeypatch):
    html_pages, lookups = [], []

    async def search_page(self, region, rname, page):
        html_pages.append(page)
        return []

    async def failing_lookup():
        lookups.append(1)
        raise RuntimeError("429")

    monkeypatch.setattr(mon.Monitor, "search_page", search_page)
    monkeypatch.setattr(settings, "fast_json", True)
    m = mon.Monitor(None, Store(tmp_path / "t.sqlite"), Avito(_catalog()))
    monkeypatch.setattr(m.evaluation, "location_id", failing_lookup)
    asyncio.run(m.page_cards("krasnodar", "Краснодар", 1, light=True))
    asyncio.run(m.page_cards("krasnodar", "Краснодар", 1, light=True))
    assert html_pages == [1, 1] and len(lookups) == 1 and not m.avito.events
