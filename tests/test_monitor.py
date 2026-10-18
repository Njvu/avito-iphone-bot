import asyncio

import pytest
from pathlib import Path

import bot.monitor as mon
from bot.config import settings
from bot.db import Store
from bot.parser import parse_search

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _mixed_market(monkeypatch):
    monkeypatch.setattr(settings, "market_source", "mixed")
    monkeypatch.setattr(settings, "min_profit", 1000)
    monkeypatch.setattr(settings, "min_profit_broken", 2500)
    import bot.evaluation as evaluation
    monkeypatch.setattr(evaluation, "PAUSE", (0, 0))


class FakeAvito:
    def __init__(self):
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)
        name = "seller.html" if "/brands/" in url else "item_14pro.html"
        return (FIX / name).read_text(encoding="utf-8")


class Msg:
    message_id = 77


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat, text, **kw):
        self.sent.append(text)
        return Msg()

    async def send_media_group(self, chat, media):
        self.sent.append(media[0].caption)
        return [Msg()]


async def _consider(m, card, region):
    await m.consider(card, region)
    await m.drain()


async def _no_photos(urls, limit):
    return []


def test_consider_publishes_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(mon, "download_photos", _no_photos)
    monkeypatch.setattr(settings, "ai_provider", "none")
    store = Store(tmp_path / "t.sqlite")
    fb, fa = FakeBot(), FakeAvito()
    m = mon.Monitor(fb, store, fa)
    cards = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))
    store.set_manual_market("15 Pro", 128, 80000)
    store.set_manual_market("16", 128, 80000)
    store.set_manual_market("12", 128, 80000)
    for c in cards:
        m.record(c, "Ставропольский край")
        asyncio.run(_consider(m, c, "Ставропольский край"))
    assert len(fb.sent) == 1, fb.sent
    assert "iPhone 15 Pro, 128 ГБ" in fb.sent[0] and "48 000 ₽" in fb.sent[0]
    assert "Перепродажа" not in fb.sent[0] and "🔥" in fb.sent[0]
    assert store.get_sent("8382185244")[0] == 48000
    cheaper = cards[2]
    cheaper.price = 45000
    asyncio.run(_consider(m, cheaper, "Ставропольский край"))
    assert fb.sent[-1].startswith("📉") and store.get_sent("8382185244")[0] == 45000


def test_avito_badge_and_unknown_market(tmp_path, monkeypatch):
    monkeypatch.setattr(mon, "download_photos", _no_photos)
    monkeypatch.setattr(settings, "ai_provider", "none")
    monkeypatch.setattr(settings, "admin_ids", {1})
    monkeypatch.setattr(settings, "group_chat_id", -100)
    store = Store(tmp_path / "t.sqlite")
    fb = FakeBot()
    m = mon.Monitor(fb, store, FakeAvito())
    cards = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))
    assert cards[2].below_market and not cards[1].below_market
    m.base_prices = {("15 Pro", 128): 40000}
    asyncio.run(_consider(m, cards[2], "Ставропольский край"))
    assert "ниже рыночной — оценка Авито" in fb.sent[-1] and "🔥" in fb.sent[-1]
    plain = cards[2]
    plain.badges, plain.id, plain.price = [], "plain", 47000
    m.base_prices = {("15 Pro", 128): 47500}
    asyncio.run(_consider(m, plain, "Ставропольский край"))
    assert len(fb.sent) == 1
    m.base_prices = {("15 Pro", 128): 60000}
    asyncio.run(_consider(m, plain, "Ставропольский край"))
    assert "продаём ≈ 60 000 ₽, выгода 13 000 ₽" in fb.sent[-1]
    plain.id, plain.badges = "above", ["Цена выше рыночной"]
    assert asyncio.run(m.consider(plain, "Ставропольский край")) == "выше рыночной (Авито)"


def test_avito_market_from_market_badges(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.sqlite")
    for i, p in enumerate([40000, 42000, 44000]):
        store.save_listing(f"m{i}", "u", "15 Pro", 128, p, "Краснодар", True, "", "market")
    store.save_listing("b", "u", "15 Pro", 128, 30000, "Краснодар", True, "", "below")
    a = store.avito_market("15 Pro", 128, 24, 0.9)
    assert a["median"] == 42000 and a["samples"] == 3 and a["source"] == "рынок Авито"
    assert store.avito_market("15 Pro", 256, 24, 0.9) is None
    m = mon.Monitor(FakeBot(), store, FakeAvito())
    m.base_prices = {("15 Pro", 128): 47000}
    from bot.models import PhoneModel
    assert m.reference(PhoneModel("15 Pro", 128), "Краснодар")["source"] == "рынок Авито"


def test_no_negative_profit(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_prices_first", False)
    monkeypatch.setattr(mon, "download_photos", _no_photos)
    monkeypatch.setattr(settings, "ai_provider", "none")
    monkeypatch.setattr(settings, "market_min_samples", 3)
    monkeypatch.setattr(settings, "market_publish_samples", 3)
    store = Store(tmp_path / "t.sqlite")
    fb = FakeBot()
    m = mon.Monitor(fb, store, FakeAvito())
    card = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))[2]
    card.badges = []
    for i, p in enumerate([40000, 41000, 42000]):
        store.save_listing(f"m{i}", "u", "15 Pro", 128, p, "Ставропольский край", True)
    asyncio.run(_consider(m, card, "Ставропольский край"))
    assert not fb.sent
    for i in range(3, 20):
        store.save_listing(f"m{i}", "u", "15 Pro", 128, 70000, "Ставропольский край", True)
    card.id, card.price = "cheap", 48000
    asyncio.run(_consider(m, card, "Ставропольский край"))
    assert len(fb.sent) == 1 and "рынок ≈ 63 000 ₽" in fb.sent[0]


def test_fresh_enough_and_age():
    from bot.parser import Card
    card = Card(id="1", title="", price=1, url="", date="2 дня назад")
    assert mon.Monitor.fresh_enough(card, 3, 7 * 24)
    card.date = "3 недели назад"
    assert not mon.Monitor.fresh_enough(card, 1, 7 * 24)
    card.date = ""
    assert mon.Monitor.fresh_enough(card, 1, 7 * 24) and not mon.Monitor.fresh_enough(card, 2, 7 * 24)


def test_ai_rejects():
    assert not mon.ai_rejects({"suitable": False, "faceid": "ok", "red_flags": ["разбита задняя крышка"]})
    assert mon.ai_rejects({"suitable": True, "faceid": "broken"}, "face id не работает")
    assert mon.ai_rejects({"suitable": False, "red_flags": ["полосы на экране"]}, "На экране полоса")
    assert not mon.ai_rejects({"suitable": False, "red_flags": ["полосы на экране"]}, "Разбит экран")


def test_ai_hallucination_ignored():
    assert not mon.ai_rejects({"faceid": "broken", "icloud": "locked"}, "Продаю 15 про.")
    assert mon.ai_rejects({"faceid": "broken"}, "Face ID не работает")


def test_ai_copied_flags_ignored():
    flags = ["не работает Face ID", "камера", "сеть", "динамик", "полосы на дисплее"]
    assert not mon.ai_rejects({"suitable": True, "faceid": "ok", "red_flags": flags}, "Продаю")
    assert not mon.ai_rejects({"suitable": False, "red_flags": flags}, "Продаю")
    assert not mon.ai_rejects({"suitable": False, "damage": ["крышка"], "red_flags": []}, "Продаю")
    assert not mon.ai_rejects({"suitable": False, "damage": [], "red_flags": []}, "Продаю")


def test_ai_flags_need_text_evidence():
    good = "Айфон 13, разбита задняя крышка. Камера отличная, всё работает, без ремонта."
    for flag in ("не включается", "неоригинальные детали", "не работает камера", "утопленник", "заблокирован iCloud"):
        assert not mon.ai_rejects({"suitable": False, "red_flags": [flag]}, good), flag
    assert mon.ai_rejects({"red_flags": ["не включается"]}, "Телефон иногда не включается сам")
    assert mon.ai_rejects({"red_flags": ["неоригинальные детали"]}, "Экран менял, копия")
    assert mon.ai_rejects({"red_flags": ["камера"]}, "Задняя камера не фокусируется")
    assert mon.ai_rejects({"damage": ["динамик"]}, "Динамик хрипит, плохо слышно")
    assert not mon.ai_rejects({"red_flags": ["разбита крышка", "экран"]}, "Разбита крышка")


def test_ai_providers_chain(monkeypatch):
    import bot.ai as ai
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "ai_fallback", "ollama")
    assert ai.providers() == ["openai", "ollama"]
    monkeypatch.setattr(settings, "ai_fallback", "openai")
    assert ai.providers() == ["openai"]
    monkeypatch.setattr(settings, "ai_provider", "none")
    monkeypatch.setattr(settings, "ai_fallback", "none")
    assert ai.providers() == []


def test_ai_fallback_used(monkeypatch):
    import bot.ai as ai
    calls = []

    async def broken(text, images):
        calls.append("openai")
        raise ai.AIError("402 limit")

    async def local(text, images):
        calls.append("ollama")
        return {"suitable": True}

    monkeypatch.setitem(ai.PROVIDERS, "openai", broken)
    monkeypatch.setitem(ai.PROVIDERS, "ollama", local)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "ai_fallback", "ollama")
    assert asyncio.run(ai.analyze("x", [])) == {"suitable": True} and calls == ["openai", "ollama"]


def _card(snippet, title="iPhone 13, 128 ГБ", condition=""):
    from bot.parser import Card
    return Card(id="1", title=title, price=30000, url="", snippet=snippet, condition=condition)


def test_market_exclusion_reasons():
    assert mon.market_exclusion(_card("Отличное состояние, гарантия Apple до 2027, не восстановленный, не реф")) == ""
    assert mon.market_exclusion(_card("Не лоченый, не обменка. Зарядка не оригинал")) == ""
    assert mon.market_exclusion(_card("Отдаю в наличии ещё два, торг")) == ""
    assert mon.market_exclusion(_card("Наш магазин даёт гарантию 3 месяца")).startswith("магазин")
    assert mon.market_exclusion(_card("Рассрочка без переплат")).startswith("магазин")
    assert mon.market_exclusion(_card("Восстановленный, как новый")) == "восстановленный/обменка"
    assert mon.market_exclusion(_card("Разбит экран, всё работает")) == "разбит экран"
    assert mon.market_exclusion(_card("Покупал в магазине, все работает")) == ""


def test_record_counts_reason(tmp_path):
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), FakeAvito())
    assert m.record(_card("Идеальное состояние"), "Краснодар") == "в рынок"
    assert m.record(_card("x", title="Чехол для iPhone 13"), "Краснодар") == "не iPhone/модель"


def test_chunk_lines_keeps_tags_whole():
    from bot.text import chunk_lines
    lines = [f"<b>iPhone {i}</b>: <i>{'x' * 50}</i>" for i in range(200)]
    chunks = chunk_lines(lines, 1000)
    assert len(chunks) > 1 and all(len(c) <= 1000 for c in chunks)
    assert all(c.count("<b>") == c.count("</b>") for c in chunks)
    assert "\n".join(chunks) == "\n".join(lines)


def test_publish_gate_counts_all_storages(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_prices_first", False)
    monkeypatch.setattr(mon, "download_photos", _no_photos)
    monkeypatch.setattr(settings, "ai_provider", "none")
    monkeypatch.setattr(settings, "market_min_samples", 5)
    monkeypatch.setattr(settings, "market_publish_samples", 10)
    store = Store(tmp_path / "t.sqlite")
    fb = FakeBot()
    m = mon.Monitor(fb, store, FakeAvito())
    card = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))[2]
    card.badges = []
    for i in range(5):
        store.save_listing(f"a{i}", "u", "15 Pro", 128, 70000, "Ставропольский край", True)
    asyncio.run(_consider(m, card, "Ставропольский край"))
    assert not fb.sent and not store.was_checked(card.id, card.price)
    for i in range(5):
        store.save_listing(f"b{i}", "u", "15 Pro", 256, 80000, "Ставропольский край", True)
    assert store.model_samples("15 Pro") == 10
    asyncio.run(_consider(m, card, "Ставропольский край"))
    assert len(fb.sent) == 1 and "по 5 объявлениям" in fb.sent[0]


def test_price_compared_with_raw_median(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_prices_first", False)
    monkeypatch.setattr(mon, "download_photos", _no_photos)
    monkeypatch.setattr(settings, "ai_provider", "none")
    monkeypatch.setattr(settings, "market_min_samples", 5)
    monkeypatch.setattr(settings, "market_publish_samples", 5)
    store = Store(tmp_path / "t.sqlite")
    fb = FakeBot()
    m = mon.Monitor(fb, store, FakeAvito())
    card = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))[2]
    card.badges = []
    for i in range(5):
        store.save_listing(f"a{i}", "u", "15 Pro", 128, 50000, "Ставропольский край", True)
    card.price = 50001
    asyncio.run(_consider(m, card, "Ставропольский край"))
    assert not fb.sent
    card.id, card.price = "mid", 48000
    asyncio.run(_consider(m, card, "Ставропольский край"))
    assert len(fb.sent) == 1 and "рынок ≈ 45 000 ₽" in fb.sent[0] and "🔥" not in fb.sent[0]


def test_parse_prices():
    from bot.prices import base_price, load_prices, parse_prices
    p = parse_prices("# x\n13 Pro Max 256 29000\n17 Pro 256 59000 esim\n17 Pro 256 78000 sim\nAir 256 58000\n13 mini 128 15000")
    assert p == {("13 Pro Max", 256): 29000, ("17 Pro", 256): 78000, ("Air", 256): 58000, ("13 mini", 128): 15000}
    assert base_price(p, "13 Pro Max", 0) == 29000 and base_price(p, "11", 128) is None
    full = load_prices(Path(__file__).parent.parent / "prices.txt")
    assert full[("16 Pro Max", 1024)] == 69000 and len(full) == 55


def test_watchdog_stale_and_backup(tmp_path):
    from datetime import datetime, timedelta
    from bot.watchdog import backup_db, is_stale
    now = datetime.now()
    assert is_stale({"last_check": None}, now, 30)
    assert not is_stale({"last_check": now - timedelta(minutes=5)}, now, 30)
    assert is_stale({"last_check": now - timedelta(minutes=40)}, now, 30)
    assert not is_stale({"last_check": None, "captcha_wait": True}, now, 30)
    assert not is_stale({"last_check": None, "pause_until": now.timestamp() + 60}, now, 30)
    store = Store(tmp_path / "t.sqlite")
    store.set_state("x", "1")
    target = backup_db(store.db, tmp_path / "backup")
    assert target.exists() and Store(target).get_state("x") == "1"


def test_finish_jobs_run_concurrently(tmp_path):
    import time
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), FakeAvito())
    done = []

    async def job(i):
        await asyncio.sleep(0.2)
        done.append(i)

    async def boom():
        raise RuntimeError("x")

    async def run():
        t = time.monotonic()
        for i in range(3):
            m.spawn(job(i))
        m.spawn(boom())
        await m.drain()
        return time.monotonic() - t

    assert asyncio.run(run()) < 0.5 and sorted(done) == [0, 1, 2] and not m.pending


def test_failed_model_skipped_for_a_while(monkeypatch):
    import bot.ai as ai
    monkeypatch.setattr(settings, "ai_models", ["a", "b"])
    monkeypatch.setattr(ai, "_down_until", {})
    ai._mark_down("a")
    assert ai.available_models() == ["b"]
    monkeypatch.setattr(ai, "_down_until", {"a": 0})
    assert ai.available_models() == ["a", "b"]


def test_market_only_fresh_and_expires(tmp_path):
    from datetime import datetime, timedelta
    store = Store(tmp_path / "t.sqlite")
    m = mon.Monitor(FakeBot(), store, FakeAvito())
    assert m.record(_card("Идеальное состояние"), "Краснодар", fresh=False) == "старше суток"
    assert store.model_samples("13", 6) == 0
    for i in range(5):
        store.save_listing(f"f{i}", "u", "13", 128, 30000 + i, "Краснодар", True)
    assert store.market("13", 128, None, 6, 5)["median"] == 30002
    old = (datetime.now() - timedelta(hours=7)).isoformat()
    store.db.execute("UPDATE listings SET seen_at=? WHERE id='f0'", (old,))
    assert store.model_samples("13", 6) == 4 and store.market("13", 128, None, 6, 5)["price"] is None


def test_market_excludes_resellers_self_and_overpriced(tmp_path):
    store = Store(tmp_path / "t.sqlite", reseller_listings=3)
    store.save_listing("me", "u", "13", 128, 20000, "Краснодар", True, "Иван")
    store.save_listing("p1", "u", "13", 128, 30000, "Краснодар", True, "Пётр")
    for i in range(3):
        store.save_listing(f"r{i}", "u", "13", 128, 45000, "Краснодар", True, "Продавец 3")
    m = store.market("13", 128, None, 6, 1, 0.9, exclude_id="me")
    assert m["median"] == 30000 and m["samples"] == 1
    assert store.market("13", 128, None, 6, 1, 0.9)["median"] == 25000
    assert store.model_samples("13", 6, exclude_id="me") == 1
    card = _card("Отличное состояние")
    card.badges = ["Выше рыночной"]
    assert mon.market_exclusion(card) == "выше рыночной (Авито)"


def test_ai_faceid_icloud_need_problem_in_text():
    assert not mon.ai_rejects({"icloud": "locked"}, "iCloud чистый, Face ID работает")
    assert not mon.ai_rejects({"faceid": "broken"}, "Face ID работает отлично")
    assert mon.ai_rejects({"icloud": "locked"}, "Заблокирован iCloud, пароль не помню")
    assert mon.ai_rejects({"faceid": "broken"}, "Не работает Face ID")


def test_base_prices_first(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "base_prices_first", True)
    monkeypatch.setattr(settings, "market_min_samples", 1)
    monkeypatch.setattr(settings, "market_publish_samples", 1)
    store = Store(tmp_path / "t.sqlite")
    m = mon.Monitor(FakeBot(), store, FakeAvito())
    m.base_prices = {("13", 128): 16500}
    store.save_listing("x", "u", "13", 128, 30000, "Краснодар", True)
    from bot.models import PhoneModel
    assert m.reference(PhoneModel("13", 128), "Краснодар")["median"] == 16500
    assert m.reference(PhoneModel("11", 128), "Краснодар") is None
    store.save_listing("y", "u", "11", 128, 9000, "Краснодар", True)
    assert m.reference(PhoneModel("11", 128), "Краснодар")["median"] == 9000


def test_seller_duplicates(tmp_path):
    assert mon.seller_key("", "Пользователь") == "" and mon.seller_key("/user/abc", "Пользователь") == "/user/abc"
    store = Store(tmp_path / "t.sqlite")
    store.mark_sent("1", "15 Pro", 256, 43990, "/user/abc", [(-1, 1)])
    assert store.find_duplicate("/user/abc", "15 Pro", 256, 43990)
    assert not store.find_duplicate("/user/abc", "15 Pro", 256, 42990)
    assert not store.find_duplicate("/user/abc", "15 Pro", 256, 43990, hours=0)


def test_shop_seller_blacklisted(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    m = mon.Monitor(FakeBot(), store, FakeAvito())
    shop = _card("Наш магазин даёт гарантию 3 месяца")
    shop.seller_url = "/brands/seller1dfc"
    assert m.record(shop, "Краснодар").startswith("магазин") and store.is_shop("/brands/seller1dfc")
    other = _card("Идеальное состояние")
    other.id, other.seller_url = "2", "/brands/seller1dfc"
    assert m.record(other, "Краснодар") == "продавец-магазин"
    assert asyncio.run(m.consider(other, "Краснодар")) == "продавец-магазин"
    named = _card("Наш магазин, рассрочка")
    named.id, named.seller = "3", "Иван"
    m.record(named, "Краснодар")
    assert not store.is_shop("Иван") and store.shops_count() == 1


def test_fast_checks_between_cycles(tmp_path, monkeypatch):
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), FakeAvito())
    calls = []

    async def fake_cycle(pages=None, light=False):
        calls.append((pages, light))

    monkeypatch.setattr(m, "cycle", fake_cycle)
    monkeypatch.setattr(m, "interval", lambda: 0.5 / 60 * 4)
    monkeypatch.setattr(settings, "fast_json_sec", 0.3)
    monkeypatch.setattr(mon, "is_night", lambda: False)
    asyncio.run(m.wait_next())
    assert calls and all(c == ([1], True) for c in calls)
    assert "13 mini" in m.store.tracked()


class FakeEvalAvito(FakeAvito):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def api(self, path, body=None):
        self.calls.append((path, body))
        if "slocations" in path:
            return {"result": {"locations": [{"names": {"1": "Краснодарский край"}, "id": 1},
                                             {"names": {"1": "Краснодар"}, "id": 2}]}}
        if path.endswith("layout"):
            if not any(p["attrId"] == 110617 for p in body["params"]):
                return {"layout": [{"fields": [{"attrId": 110617, "options": [
                    {"label": "iPhone 13", "valueId": 13}, {"label": "iPhone Xs Max", "valueId": 99}]}]}]}
            return {"layout": [{"fields": [{"attrId": 110615, "options": [{"label": "128 ГБ", "valueId": 128},
                                                                          {"label": "1 ТБ", "valueId": 1024}]},
                                           {"attrId": 110616, "options": [{"label": "синий", "valueId": 7}]}]}]}
        assert body["locationID"] == 2
        return {"isPredicted": True, "min": 22607, "max": 25493}


def test_avito_evaluation_step_and_reference(tmp_path, monkeypatch):
    from bot.evaluation import AvitoEvaluation, eval_condition, storage_from_label
    from bot.models import PhoneModel
    monkeypatch.setattr(settings, "market_source", "eval")
    assert storage_from_label("1 ТБ") == 1024 and storage_from_label("256 ГБ") == 256
    assert eval_condition("Новое") == "Отличное" and eval_condition("Требуется ремонт") == "Хорошее"
    assert eval_condition("") == "Хорошее" and eval_condition("Удовлетворительное") == "Хорошее"
    store = Store(tmp_path / "t.sqlite")
    av = FakeEvalAvito()
    ev = AvitoEvaluation(av, store, "Краснодар")
    assert asyncio.run(ev.step(["13", "XS Max"], budget=3)) == 3
    assert asyncio.run(ev.step(["13"], budget=10)) == 1
    assert asyncio.run(ev.step(["13"], budget=10)) == 0
    assert store.get_eval("13", 128, "Хорошее") == (22607, 25493)
    m = mon.Monitor(FakeBot(), store, av)
    ref = m.reference(PhoneModel("13", 128), "Краснодар", None, "Хорошее")
    assert ref["source"] == "калькулятор Авито" and (ref["min"], ref["median"], ref["max"]) == (22607, 24050, 25493)
    assert m.market_limit(ref) == 25493 and ref["condition"] == ref["wanted"] == "Хорошее"
    assert m.reference(PhoneModel("14", 128), "Краснодар") is None
    store.db.execute("DELETE FROM avito_eval WHERE model='13' AND condition='Отличное'")
    near = m.reference(PhoneModel("13", 128), "Краснодар", None, "Отличное")
    assert near["condition"] == "Хорошее" and near["wanted"] == "Отличное"


def test_refresh_prices_forces_all(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.sqlite")
    m = mon.Monitor(FakeBot(), store, FakeEvalAvito())
    monkeypatch.setattr(settings, "avito_eval_per_step", 1000)
    monkeypatch.setattr(store, "tracked", lambda: ["13"])
    assert asyncio.run(m.refresh_prices()) == 4
    assert asyncio.run(m.refresh_prices()) == 4 and not m.refreshing and m.evaluation.refresh_since is None


def test_unclear_condition_without_ai_posts_with_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(mon, "download_photos", _no_photos)
    monkeypatch.setattr(settings, "ai_provider", "none")
    from bot.damage import DamageResult
    monkeypatch.setattr(mon, "inspect_damage", lambda *a, **k: DamageResult(False, reason=mon.UNCLEAR))
    store = Store(tmp_path / "t.sqlite")
    fb = FakeBot()
    m = mon.Monitor(fb, store, FakeAvito())
    card = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))[2]
    card.badges = []
    m.base_prices = {("15 Pro", 128): 60000}
    async def run():
        await m.deep_check(card, "Ставропольский край", mon.detect_phone(card.title, card.snippet),
                           {"price": 60000, "median": 60000, "samples": 0, "source": mon.BASE})
        await m.drain()

    asyncio.run(run())
    assert fb.sent and "состояние в объявлении не описано — уточнить у продавца" in fb.sent[-1]


def test_blocked_item_not_marked_checked(tmp_path):
    from bot.avito import Blocked

    class BlockedAvito(FakeAvito):
        async def fetch(self, url):
            raise Blocked("Доступ ограничен")

    store = Store(tmp_path / "t.sqlite")
    m = mon.Monitor(FakeBot(), store, BlockedAvito())
    card = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))[2]
    with pytest.raises(Blocked):
        asyncio.run(m.deep_check(card, "Ставропольский край", mon.detect_phone(card.title, card.snippet),
                                 {"price": 60000, "median": 60000, "samples": 0, "source": mon.BASE}))
    assert not store.was_checked(card.id, card.price)


def test_price_drop_failure_does_not_break_cycle(tmp_path):
    class FailingBot(FakeBot):
        async def send_message(self, chat, text, **kw):
            raise RuntimeError("message to reply not found")

    store = Store(tmp_path / "t.sqlite")
    m = mon.Monitor(FailingBot(), store, FakeAvito())
    card = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))[2]
    store.mark_sent(card.id, "15 Pro", 128, card.price + 5000, "x", [(-1, 5)])
    assert asyncio.run(m.consider(card, "Ставропольский край")) == "уже в группе"
    assert store.get_sent(card.id)[0] == card.price


def test_publish_falls_back_to_text(tmp_path, monkeypatch):
    class AlbumFailBot(FakeBot):
        async def send_media_group(self, chat, media):
            raise RuntimeError("bad photo")

    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    fb = AlbumFailBot()
    m = mon.Monitor(fb, Store(tmp_path / "t.sqlite"), FakeAvito())
    card = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))[2]
    asyncio.run(m.publish(card, mon.detect_phone(card.title, card.snippet), [b"x"], "caption", "s"))
    assert fb.sent == ["caption"] and m.store.get_sent(card.id)[1] == [(settings.group_chat_id, 77)]


async def _no_sleep(*a, **k):
    return None


def test_eval_429_sets_cooldown_without_waiting(tmp_path):
    from bot.evaluation import AvitoEvaluation

    class LimitedAvito(FakeEvalAvito):
        async def api(self, path, body=None):
            if path.endswith("prediction"):
                return {"__status": 429}
            return await super().api(path, body)

    store = Store(tmp_path / "t.sqlite")
    ev = AvitoEvaluation(LimitedAvito(), store, "Краснодар")
    assert asyncio.run(ev.step(["13"], budget=10)) == 0 and ev.cooldown_until > 0
    assert asyncio.run(ev.step(["13"], budget=10)) == 0


def test_rotate_ip_on_block(tmp_path, monkeypatch):
    import httpx
    calls = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)
            return httpx.Response(200)

    monkeypatch.setattr(mon.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(settings, "proxy_url", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr(settings, "proxy_rotate_url", "https://proxy.example/rotate?key=1")
    class CaptchaAvito(FakeAvito):
        kind = None

        async def captcha_kind(self):
            return self.kind

    av = CaptchaAvito()
    av.proxy = settings.proxy_url
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), av)
    assert all(asyncio.run(m.rotate_ip()) for _ in range(5))
    av.kind = "interactive"
    assert asyncio.run(m.rotate_ip()) and len(calls) == 6
    av.proxy = None
    m.rotations.clear()
    assert not asyncio.run(m.rotate_ip())


def test_wait_ip_rotation_on_block(tmp_path, monkeypatch):
    ips = iter(["1.1.1.1", "1.1.1.1", None, "2.2.2.2"])

    async def fake_exit_ip(proxy=None):
        return next(ips)

    monkeypatch.setattr(mon, "exit_ip", fake_exit_ip)
    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(settings, "proxy_url", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr(settings, "proxy_rotate_url", "")
    monkeypatch.setattr(settings, "proxy_rotate_every", 120)
    av = FakeAvito()
    av.proxy = settings.proxy_url
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), av)
    assert asyncio.run(m.rotate_ip())
    assert next(ips, "end") == "end" and m.proxy_ip == "2.2.2.2"
    ips = iter(["3.3.3.3"])
    assert asyncio.run(m.rotate_ip(link_lost=True)) and m.proxy_ip == "3.3.3.3"
    m.rotations.clear()
    ips = iter(["4.4.4.4", "5.5.5.5"])
    assert asyncio.run(m.rotate_ip()) and m.proxy_ip == "5.5.5.5"


def test_ip_block_without_captcha_keeps_rotating(tmp_path, monkeypatch):
    ips = iter(str(i) for i in range(100))

    async def fake_exit_ip(proxy=None):
        return next(ips)

    class CaptchaAvito(FakeAvito):
        kind = None

        async def captcha_kind(self):
            return self.kind

    monkeypatch.setattr(mon, "exit_ip", fake_exit_ip)
    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(settings, "proxy_url", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr(settings, "proxy_rotate_url", "")
    monkeypatch.setattr(settings, "proxy_rotate_every", 120)
    av = CaptchaAvito()
    av.proxy = settings.proxy_url
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), av)
    assert all(asyncio.run(m.rotate_ip()) for _ in range(5))
    av.kind = "interactive"
    assert asyncio.run(m.rotate_ip())


def test_wait_ip_rotation_gives_up(tmp_path, monkeypatch):
    clock = iter(range(0, 10_000, 10))

    async def same_ip(proxy=None):
        return "1.1.1.1"

    monkeypatch.setattr(mon, "exit_ip", same_ip)
    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(mon.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(settings, "proxy_url", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr(settings, "proxy_rotate_url", "")
    monkeypatch.setattr(settings, "proxy_rotate_every", 120)
    av = FakeAvito()
    av.proxy = settings.proxy_url
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), av)
    assert not asyncio.run(m.rotate_ip())
    monkeypatch.setattr(settings, "proxy_rotate_every", 0)
    assert not asyncio.run(m.rotate_ip())


def test_ip_block_with_rotating_proxy_skips_admin_captcha(tmp_path, monkeypatch):
    asked = []

    class NoCaptchaAvito(FakeAvito):
        async def captcha_kind(self):
            return None

        async def set_proxy(self, proxy):
            self.proxy = proxy

        async def page_blocked(self):
            return False

    async def no_rotation(self, link_lost=False):
        return False

    async def ask(self):
        asked.append(1)
        return True

    monkeypatch.setattr(mon.Monitor, "rotate_ip", no_rotation)
    monkeypatch.setattr(mon.Monitor, "solve_by_admin", ask)
    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(settings, "proxy_url", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr(settings, "proxy_rotate_every", 120)
    monkeypatch.setattr(settings, "proxy_list", [])
    monkeypatch.setattr(settings, "free_proxies", False)
    monkeypatch.setattr(settings, "headless", False)
    av = NoCaptchaAvito()
    av.proxy = settings.proxy_url
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), av)
    asyncio.run(m.on_blocked(mon.Blocked("Доступ ограничен")))
    assert not asked and av.proxy == settings.proxy_url


def test_watchdog_backup_survives_healthcheck_failure(tmp_path, monkeypatch):
    import httpx
    from datetime import datetime
    from bot.watchdog import Watchdog

    class Down:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.ConnectError("down")

    import bot.watchdog as wd
    monkeypatch.setattr(wd.httpx, "AsyncClient", Down)
    monkeypatch.setattr(settings, "healthcheck_url", "https://hc.example/ping")
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.sqlite")
    store = Store(tmp_path / "t.sqlite")
    m = mon.Monitor(FakeBot(), store, FakeAvito())
    m.status["last_check"] = datetime.now()
    asyncio.run(Watchdog(m, store).tick(datetime.now()))
    assert list((tmp_path / "backup").glob("bot-*.sqlite3"))


def test_settings_tolerate_empty_numbers(monkeypatch):
    from bot.config import _float, _int
    monkeypatch.setenv("X_EMPTY", "")
    assert _int("X_EMPTY", 7) == 7 and _float("X_EMPTY", 0.5) == 0.5


def test_captcha_keeps_rotating_and_asks_admin_only_when_rotation_fails(tmp_path, monkeypatch):
    asked = []
    ips = iter(str(i) for i in range(100))
    same = {"on": False}

    class CaptchaAvito(FakeAvito):
        async def captcha_kind(self):
            return "interactive"

        async def set_proxy(self, proxy):
            self.proxy = proxy

        async def page_blocked(self):
            return False

    async def unsolved(self):
        asked.append(1)
        self.store.set_state("captcha_muted_until", mon.time.time() + 3600)
        return False

    async def fake_exit_ip(proxy=None):
        return "1.1.1.1" if same["on"] else next(ips)

    clock = iter(range(0, 10**6, 10))
    monkeypatch.setattr(mon.Monitor, "solve_by_admin", unsolved)
    monkeypatch.setattr(mon, "exit_ip", fake_exit_ip)
    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(mon.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(settings, "proxy_url", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr(settings, "proxy_rotate_url", "")
    monkeypatch.setattr(settings, "proxy_rotate_every", 120)
    monkeypatch.setattr(settings, "proxy_list", [])
    monkeypatch.setattr(settings, "free_proxies", False)
    av = CaptchaAvito()
    av.proxy = settings.proxy_url
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), av)
    for _ in range(6):
        asyncio.run(m.on_blocked(mon.Blocked("Доступ ограничен")))
    assert not asked
    same["on"] = True
    for _ in range(3):
        asyncio.run(m.on_blocked(mon.Blocked("Доступ ограничен")))
    assert len(asked) == 1 and av.proxy == settings.proxy_url
    assert mon.Monitor(FakeBot(), m.store, av)._captcha_muted()


def test_new_session_after_ip_block_rotation(tmp_path, monkeypatch):
    ips = iter(str(i) for i in range(10))
    resets = []

    class SessionAvito(FakeAvito):
        async def new_session(self):
            resets.append(1)

    async def fake_exit_ip(proxy=None):
        return next(ips)

    monkeypatch.setattr(mon, "exit_ip", fake_exit_ip)
    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(settings, "proxy_url", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr(settings, "proxy_rotate_url", "")
    monkeypatch.setattr(settings, "proxy_rotate_every", 120)
    av = SessionAvito()
    av.proxy = settings.proxy_url
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), av)
    assert asyncio.run(m.rotate_ip()) and resets == [1]
    assert asyncio.run(m.rotate_ip(link_lost=True)) and resets == [1]


def test_caption_has_no_damage_line(tmp_path):
    from bot.damage import DamageResult
    from bot.models import PhoneModel
    from bot.parser import Card
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), FakeAvito())
    card = Card(id="1", title="iPhone 13 Pro, 256 ГБ", price=40000, url="https://www.avito.ru/x_1")
    market = {"price": 42500, "median": 42500, "samples": 0, "source": mon.EVAL, "min": 40000, "max": 45000,
              "condition": "Хорошее", "wanted": "Хорошее"}
    card.price = 38000
    dmg = DamageResult(True, ("разбит экран/стекло",))
    caption = m.caption(card, "Краснодар", PhoneModel("13 Pro", 256), market, dmg, None, "")
    assert "Поломки" not in caption and "iPhone 13 Pro" in caption
    assert caption.startswith("🔥 НИЖЕ РЫНКА")
    assert "💰 38 000 ₽ (рынок Авито 40 000–45 000 ₽, средняя 42 500 ₽)" in caption
    assert "(рынок Авито 40 000–45 000 ₽, средняя 42 500 ₽)" in caption
    card.price = 43000
    near = {**market, "condition": "Хорошее", "wanted": "Отличное"}
    caption = m.caption(card, "Краснодар", PhoneModel("13 Pro", 256), near, dmg, None, "")
    assert caption.startswith("✅ В РЫНКЕ") and "средняя 42 500 ₽, по «Хорошее»)" in caption


def test_page_candidates_considered_before_next_page(tmp_path, monkeypatch):
    from bot.parser import Card
    events = []

    async def page(self, region, rname, p):
        events.append(f"page{p}")
        return [Card(id=f"{p}", title="iPhone 13, 128 ГБ", price=1000, url=f"https://www.avito.ru/{region}/x_{p}",
                     date="5 минут назад")]

    async def consider(self, card, region):
        events.append(f"consider{card.id}")
        return "нет выгоды"

    async def no_eval(*a, **k):
        return 0

    monkeypatch.setattr(mon.Monitor, "search_page", page)
    monkeypatch.setattr(mon.Monitor, "consider", consider)
    store = Store(tmp_path / "t.sqlite")
    store.set_state("bootstrapped", "1")
    m = mon.Monitor(FakeBot(), store, FakeAvito())
    monkeypatch.setattr(m.evaluation, "step", no_eval)
    asyncio.run(m.cycle(pages=[1, 2]))
    assert events == ["page1", "consider1", "page2", "consider2"]


def test_calculator_refreshes_only_at_night_once_filled(tmp_path, monkeypatch):
    calls = []

    async def page(self, region, rname, p):
        return []

    async def step(models, budget=15, patient=False):
        calls.append(budget)
        return 0

    monkeypatch.setattr(mon.Monitor, "search_page", page)
    store = Store(tmp_path / "t.sqlite")
    store.set_state("bootstrapped", "1")
    m = mon.Monitor(FakeBot(), store, FakeAvito())
    monkeypatch.setattr(m.evaluation, "step", step)
    async def run_cycle():
        await m.cycle(pages=[1])
        if m.eval_task:
            await m.eval_task

    monkeypatch.setattr(mon, "is_night", lambda: False)
    asyncio.run(run_cycle())
    assert calls == [settings.avito_eval_per_step]
    store.save_eval("13", 128, "Хорошее", 20000, 25000)
    m.eval_task = None
    asyncio.run(run_cycle())
    assert len(calls) == 1
    monkeypatch.setattr(mon, "is_night", lambda: True)
    asyncio.run(run_cycle())
    assert len(calls) == 2


def test_refresh_survives_empty_rate_limited_steps(tmp_path, monkeypatch):
    results = iter([8, 0, 0, 5, 0])
    pending = iter([True, True, False])
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), FakeAvito())

    async def step(models, budget=15, patient=False):
        return next(results)

    monkeypatch.setattr(m.evaluation, "step", step)
    monkeypatch.setattr(m.evaluation, "pending", lambda models: next(pending))
    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    assert asyncio.run(m.refresh_prices()) == 13 and not m.refreshing


def test_refresh_fill_mode_keeps_fresh_prices(tmp_path, monkeypatch):
    seen = []
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), FakeAvito())

    async def step(models, budget=15, patient=False):
        seen.append(m.evaluation.refresh_since)
        return 0

    monkeypatch.setattr(m.evaluation, "step", step)
    monkeypatch.setattr(m.evaluation, "pending", lambda models: False)
    asyncio.run(m.refresh_prices(force=False))
    asyncio.run(m.refresh_prices())
    assert seen[0] is None and seen[1] is not None


def test_link_rotation_reconnects_browser(tmp_path, monkeypatch):
    import httpx
    calls, restarts = [], []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)
            return httpx.Response(200)

    class RestartAvito(FakeAvito):
        async def restart(self):
            restarts.append(1)

        async def new_session(self):
            pass

    monkeypatch.setattr(mon.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(settings, "proxy_url", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr(settings, "proxy_rotate_url", "https://proxy.example/rotate?key=1")
    av = RestartAvito()
    av.proxy = settings.proxy_url
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), av)
    assert asyncio.run(m.rotate_ip()) and len(calls) == 1 and len(restarts) == 1
    assert asyncio.run(m.rotate_ip(link_lost=True)) and len(calls) == 1 and len(restarts) == 2


def test_fast_delay_day_jitter_night_and_slowdown(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "fast_json", False)
    monkeypatch.setattr(settings, "fast_interval_sec", 45)
    monkeypatch.setattr(settings, "fast_jitter_sec", 10)
    monkeypatch.setattr(settings, "night_fast_sec", 120)
    monkeypatch.setattr(settings, "block_slowdown_min", 10)
    monkeypatch.setattr(settings, "block_slowdown_factor", 2.0)
    monkeypatch.setattr(settings, "fast_turbo_sec", 30)
    monkeypatch.setattr(settings, "turbo_after_min", 20)
    monkeypatch.setattr(mon, "is_night", lambda: False)
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), FakeAvito())
    assert all(35 <= m.fast_delay() <= 55 for _ in range(50))
    m.started -= 21 * 60
    assert all(22.5 <= m.fast_delay() <= 37.5 for _ in range(50))
    m.last_block = mon.time.monotonic() - 15 * 60
    assert all(35 <= m.fast_delay() <= 55 for _ in range(50))
    m.last_block = mon.time.monotonic()
    assert all(70 <= m.fast_delay() <= 110 for _ in range(50))
    m.last_block = mon.time.monotonic() - 601
    assert m.fast_delay() <= 55
    monkeypatch.setattr(mon, "is_night", lambda: True)
    assert m.fast_delay() == 120


def test_night_hours_window(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(settings, "night_hours", (1, 5))
    assert mon.is_night(datetime(2026, 10, 16, 1, 0)) and mon.is_night(datetime(2026, 10, 16, 4, 59))
    assert not mon.is_night(datetime(2026, 10, 16, 5, 0)) and not mon.is_night(datetime(2026, 10, 16, 0, 59))


def test_publish_to_all_channels_and_reply_price_drop_everywhere(tmp_path, monkeypatch):
    class ChatBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.chats = []
            self.next_id = 100

        async def send_message(self, chat, text, **kw):
            self.chats.append((chat, text, kw.get("reply_to_message_id")))
            self.next_id += 1
            from types import SimpleNamespace
            return SimpleNamespace(message_id=self.next_id)

    monkeypatch.setattr(mon.asyncio, "sleep", _no_sleep)
    store = Store(tmp_path / "t.sqlite")
    assert store.add_channel(-1, -1) is False and store.add_channel(-2, -1) and store.channels(-1) == [-1, -2]
    fb = ChatBot()
    m = mon.Monitor(fb, store, FakeAvito())
    card = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))[2]
    asyncio.run(m.publish(card, mon.detect_phone(card.title, card.snippet), [], "caption", "s"))
    price, posts = store.get_sent(card.id)
    assert [c for c, _ in posts] == [-1, -2]
    cheaper = parse_search((FIX / "search_stavropol.html").read_text(encoding="utf-8"))[2]
    cheaper.price = price - 1000
    asyncio.run(m.report_price_drop(cheaper, price, posts))
    replies = [(c, r) for c, t, r in fb.chats if t.startswith("📉")]
    assert replies == posts and store.get_sent(card.id)[0] == cheaper.price
    assert store.remove_channel(-2, -1) and store.channels(-1) == [-1]


def test_newest_candidates_considered_first(tmp_path, monkeypatch):
    from bot.parser import Card
    order = []

    async def page(self, region, rname, p):
        return [Card(id=i, title="iPhone 13, 128 ГБ", price=1000, url=f"https://www.avito.ru/{region}/x_{i}", date=d)
                for i, d in (("a", "3 часа назад"), ("b", "2 минуты назад"), ("c", "40 минут назад"))]

    async def consider(self, card, region):
        order.append(card.id)
        return "нет выгоды"

    monkeypatch.setattr(mon.Monitor, "search_page", page)
    monkeypatch.setattr(mon.Monitor, "consider", consider)
    store = Store(tmp_path / "t.sqlite")
    store.set_state("bootstrapped", "1")
    store.save_eval("13", 128, "Хорошее", 1, 2)
    m = mon.Monitor(FakeBot(), store, FakeAvito())
    monkeypatch.setattr(mon, "is_night", lambda: False)
    asyncio.run(m.cycle(pages=[1]))
    assert order == ["b", "c", "a"]


def test_failed_link_rotation_retries_soon_without_long_pause(tmp_path, monkeypatch):
    import httpx
    slept, notes = [], []

    class FailClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return httpx.Response(200, json={"status": "error", "message": "busy"})

    async def fake_sleep(sec):
        slept.append(sec)

    class NoteBot(FakeBot):
        async def send_message(self, chat, text, **kw):
            notes.append(text)
            return Msg()

    monkeypatch.setattr(mon.httpx, "AsyncClient", FailClient)
    monkeypatch.setattr(mon.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(settings, "proxy_url", "http://u:p@1.2.3.4:8000")
    monkeypatch.setattr(settings, "proxy_rotate_url", "https://proxy.example/rotate?key=1")
    monkeypatch.setattr(settings, "captcha_relay", False)
    av = FakeAvito()
    av.proxy = settings.proxy_url
    m = mon.Monitor(NoteBot(), Store(tmp_path / "t.sqlite"), av)
    assert not asyncio.run(m.rotate_ip())
    asyncio.run(m.on_blocked(mon.Blocked("Доступ ограничен")))
    assert slept == [mon.ROTATE_RETRY_SEC, mon.ROTATE_RETRY_SEC] and not notes


def test_proxy_glitch_does_not_slow_down_polling(tmp_path, monkeypatch):
    async def no_rotation(self, link_lost=False):
        return True

    monkeypatch.setattr(mon.Monitor, "rotate_ip", no_rotation)
    av = FakeAvito()
    av.proxy = None
    m = mon.Monitor(FakeBot(), Store(tmp_path / "t.sqlite"), av)
    asyncio.run(m.on_blocked(mon.ProxyDown("прокси не отвечает")))
    assert m.last_block == float("-inf")
    asyncio.run(m.on_blocked(mon.Blocked("Доступ ограничен")))
    assert m.last_block > 0
