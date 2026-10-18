import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

import bot.monitor as mon
from bot.damage import inspect_damage
from bot.db import Store
from bot.models import reject_reason


def test_icloud_not_linked_is_not_locked():
    for text in ("Отличное состояние, всё работает. iCloud не привязан, Face ID работает",
                 "Отличное состояние, iCloud чистый, ни к чему не привязан",
                 "Отличное состояние, айклауд чистый, блок зарядки в комплекте",
                 "Отличное состояние, без блокировок, iCloud чистый",
                 "Отличное состояние, iCloud не заблокирован"):
        assert inspect_damage(text).accepted, text
    for text in ("Отличное состояние, iCloud привязан, не могу выйти", "Отличное состояние, iCloud заблокирован",
                 "Отличное состояние, блокировка iCloud", "Отличное состояние, iCloud не отвязан"):
        assert inspect_damage(text).reason == "заблокирован iCloud", text


def test_missing_box_is_not_broken_faceid():
    for text in ("Отличное состояние, Face ID работает, коробки нет",
                 "Отличное состояние, Face ID работает, зарядки нет, только телефон"):
        assert inspect_damage(text).accepted, text
    for text in ("Отличное состояние, Face ID нет", "Отличное состояние, нет Face ID", "Отличное состояние, Face ID: нет"):
        assert inspect_damage(text).reason == "не работает Face ID", text


def test_no_faults_wording_accepted():
    for text in ("Телефон в отличном состоянии, без неисправностей", "Неисправностей нет, всё работает",
                 "Никаких неисправностей, Face ID работает", "Неисправностей не обнаружено, отличное состояние",
                 "Нет никаких царапин, отличное состояние"):
        r = inspect_damage(text, "Отличное")
        assert r.accepted and not r.damage, text
    r = inspect_damage("На экране трещин нет, всё работает", "Отличное")
    assert r.accepted and r.damage == ()
    assert not inspect_damage("Отличное состояние, неисправность камеры").accepted


def test_negation_does_not_hide_other_faults():
    assert not inspect_damage("Не работает камера и сломан динамик. Отличное состояние").accepted
    assert not inspect_damage("Не включается и разбит экран").accepted
    assert not inspect_damage("Не помню пароль и царапины на корпусе").accepted
    assert not inspect_damage("Нет face id и царапин").accepted


def test_unlocked_is_not_locked():
    title = "iPhone 13, 128 ГБ, SIM + eSIM"
    assert reject_reason(title, "Unlocked, работает с любой симкой") is None
    assert reject_reason(title, "Телефон locked на оператора") == "лочка/R-SIM"


def test_private_seller_wording_not_shop():
    for text in ("Продаю свой телефон, не магазин", "Магазинам и перекупам не беспокоить",
                 "Скупщикам и магазинам просьба не беспокоить", "Есть магазинная гарантия"):
        assert not mon.is_shop_text(text), text
    for text in ("Наш магазин предлагает", "Магазин техники Apple", "Есть гарантия от продавца"):
        assert mon.is_shop_text(text), text


class _Msg:
    message_id = 5


def _post(tmp_path, monkeypatch, bot, photos=(b"x",)):
    sleeps = []

    async def sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr(mon.asyncio, "sleep", sleep)
    m = mon.Monitor(bot, Store(tmp_path / "t.sqlite"), None)
    return asyncio.run(m._post(1, list(photos), "caption")), sleeps


def test_post_gives_up_at_once_when_bot_is_kicked(tmp_path, monkeypatch):
    class Kicked:
        calls = 0

        async def send_media_group(self, chat, media):
            Kicked.calls += 1
            raise TelegramForbiddenError(None, "bot was kicked")

    (ids, error), sleeps = _post(tmp_path, monkeypatch, Kicked())
    assert ids == [] and isinstance(error, TelegramForbiddenError) and Kicked.calls == 1 and sleeps == []


def test_post_bad_photo_falls_back_to_text_without_waiting(tmp_path, monkeypatch):
    class BadPhoto:
        sent = []

        async def send_media_group(self, chat, media):
            raise TelegramBadRequest(None, "IMAGE_PROCESS_FAILED")

        async def send_message(self, chat, text, **kw):
            BadPhoto.sent.append(text)
            return _Msg()

    (ids, error), sleeps = _post(tmp_path, monkeypatch, BadPhoto())
    assert ids == [5] and error is None and BadPhoto.sent == ["caption"] and sleeps == []


def test_post_waits_flood_limit_and_no_sleep_after_last_try(tmp_path, monkeypatch):
    class Flood:
        calls = 0

        async def send_message(self, chat, text, **kw):
            Flood.calls += 1
            if Flood.calls == 1:
                raise TelegramRetryAfter(None, "flood", 7)
            return _Msg()

    (ids, _), sleeps = _post(tmp_path, monkeypatch, Flood(), photos=())
    assert ids == [5] and sleeps == [7]

    class Down:
        async def send_message(self, chat, text, **kw):
            raise RuntimeError("network")

    (ids, error), sleeps = _post(tmp_path, monkeypatch, Down(), photos=())
    assert ids == [] and isinstance(error, RuntimeError) and sleeps == [10, 10]


def test_unlocked_wording_is_not_a_lock():
    assert reject_reason("iPhone 13, 128 ГБ", "Разлоченный, работает с любой симкой") is None
    assert reject_reason("iPhone 13, 128 ГБ", "Залочен под оператора") == "лочка/R-SIM"


def test_cracked_back_glass_is_not_a_cracked_screen():
    dmg = inspect_damage("Разбито заднее стекло, всё работает", "Хорошее", "warn", {"back"})
    assert dmg.accepted and dmg.damage
    assert not inspect_damage("Разбит экран, всё работает", "Хорошее", "warn", {"back"}).accepted


def test_private_seller_phrases_are_not_shop():
    for text in ("Гарантия 8 мес от Apple, всё работает", "Обслуживался в сервисном центре, батарея менялась"):
        assert not mon.SHOP.search(text.lower()), text
    assert mon.SHOP.search("гарантия 3 мес, рассрочка")


def test_price_estimator_skips_a_failing_combination(tmp_path):
    from bot.evaluation import PREDICTION, AvitoEvaluation, RateLimited

    class Flaky(AvitoEvaluation):
        calls = 0

        async def _call(self, path, body=None, patient=False):
            if path == PREDICTION:
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise RuntimeError("500")
                return {"isPredicted": True, "min": 10000, "max": 12000}
            raise AssertionError(path)

        async def location_id(self):
            return 1

        async def _model_ids(self):
            return {"13": 1}

        async def _model_options(self, model, model_id):
            return {128: 1, 256: 2}, 0

    store = Store(tmp_path / "t.sqlite")
    ev = Flaky(None, store, "Краснодар")
    assert asyncio.run(ev.step(["13"], budget=10)) == 3
    assert issubclass(RateLimited, RuntimeError)
