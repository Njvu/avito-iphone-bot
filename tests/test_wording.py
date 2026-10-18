from datetime import datetime

from bot.damage import inspect_damage, market_dirt
from bot.models import detect_phone, reject_reason
from bot.parser import parse_age
from bot.tunnel import parse_url

TITLE = "iPhone 13, 128 ГБ, SIM + eSIM"


def test_broken_faceid_slang_rejected():
    for text in ("Разбит экран, без фейса", "Хорошее состояние, нет фейса", "Хорошее состояние, фейса нет",
                 "Хорошее состояние, фейс отвалился", "Хорошее состояние, отвалился фейс", "Хорошее состояние, фейс не пашет",
                 "Хорошее состояние, без face id", "Хорошее состояние, проблемы с Face ID"):
        assert inspect_damage(text, "Хорошее").reason == "не работает Face ID", text
    for text in ("Отличное состояние, Face ID работает проблем нет", "Отличное состояние, проблем с Face ID нет",
                 "Отличное состояние, интерфейс не тормозит", "Отличное состояние, фейс айди работает, нет царапин",
                 "Отличное состояние, никогда не было проблем с Face ID"):
        assert inspect_damage(text, "Отличное").accepted, text


def test_common_positive_phrases_not_rejected():
    for text in ("Отличное состояние. Не утопленник, не вскрывался, не ремонтировался",
                 "Отличное состояние, не был в воде, все работает", "Отличное состояние, в воде не был",
                 "Отличное состояние. Экран без полос и пятен, всё работает", "Отличное состояние, дисплей без выгораний",
                 "Отличное состояние, нет выгорания экрана", "Отличное состояние, полос и пятен на экране нет",
                 "Отличное состояние, тач не глючит", "Отличное состояние, тач не тупит", "Отличное состояние, камера не глючит",
                 "Отличное состояние. Мошенникам не беспокоить", "Отличное состояние, Apple ID свой, не мошенник",
                 "Отличное состояние, вышел из всех своих аккаунтов", "Отличное состояние, никаких аккаунтов не привязано",
                 "Отличное состояние, iCloud чистый, в комплекте блок и кабель", "Отличное состояние, без блокировки активации",
                 "Отличное состояние, блокировки активации нет", "Отличное состояние. Я не работаю с перекупами"):
        assert inspect_damage(text, "Отличное").accepted, text
        assert not market_dirt(text, "Отличное"), text


def test_real_faults_still_rejected():
    for text, reason in (("Хорошее состояние, был в воде", "утопленник"), ("Хорошее состояние, попала вода", "утопленник"),
                         ("Хорошее состояние, на экране полосы", "повреждён дисплей"),
                         ("Хорошее состояние, экран выгорел", "повреждён дисплей"),
                         ("Хорошее состояние, тач не реагирует", "повреждён дисплей"),
                         ("Хорошее состояние, взломан мошенниками", "чужой/взломанный iCloud"),
                         ("Хорошее состояние, активационная блокировка", "заблокирован iCloud"),
                         ("Хорошее состояние, привязан к аккаунту, пароль забыл", "заблокирован iCloud"),
                         ("Хорошее состояние, iCloud на блоке", "заблокирован iCloud"),
                         ("Хорошее состояние, стоит блок айклауд", "заблокирован iCloud"),
                         ("Хорошее состояние, разбита камера", "разбита камера"),
                         ("Хорошее состояние, разбито заднее стекло и стекло камеры", "разбита камера"),
                         ("Хорошее состояние, проблемы с тачем", "неисправный модуль")):
        assert inspect_damage(text, "Хорошее").reason == reason, text
    for text in ("Хорошее состояние, нет коробки сломан динамик", "Хорошее состояние, без зарядки и сломана кнопка"):
        assert not inspect_damage(text, "Хорошее").accepted, text
    assert inspect_damage("Хорошее состояние, разбита задняя крышка, камеры целые", "Хорошее").damage == ("разбита задняя крышка",)
    assert inspect_damage("Хорошее состояние, без нареканий", "Хорошее").faceid_ok


def test_reject_reason_negations():
    for text in ("Телефон не заблочен, всё работает", "Никаких r-sim и лочек", "Оригинал, не копия",
                 "Телефон оригинальный, не реплика", "Не на запчасти, полностью рабочий"):
        assert reject_reason(TITLE, text) is None, text
    assert reject_reason(TITLE, "Телефон заблочен") == "лочка/R-SIM"
    assert reject_reason(TITLE, "Продаю на запчасти, копия") == "не телефон/запчасти"


def test_untracked_iphone_title_not_taken_from_description():
    assert detect_phone("iPhone SE (2022), 128 ГБ", "Рассмотрю обмен на iPhone 13 с вашей доплатой") is None
    assert detect_phone("iPhone 8 Plus, 64 ГБ", "Обмен на iPhone 11") is None
    assert detect_phone("iPhone 7, 128 ГБ", "Как iPhone 12 почти") is None
    assert detect_phone("Телефон", "iPhone 13 Pro 256 ГБ").model == "13 Pro"
    assert detect_phone("iPhone XS Мах, 256 ГБ").model == "XS Max"


def test_parse_age_dates_with_year_and_time():
    now = datetime(2026, 10, 17, 10, 0)
    assert parse_age("16 октября 23:15", now) == 10.75
    assert parse_age("16 октября в 23:15", now) == 10.75
    assert parse_age("17 октября 2025", now) > 24 * 300
    assert parse_age("17 октября 10:05", now) == 0.0
    assert parse_age("15 октября", now) == 58.0
    assert parse_age("2 года назад", now) == 2 * 8760


def test_tunnel_ignores_api_host_in_errors():
    line = ('2026-10-17T06:31:54Z ERR Failed to request quick Tunnel error="Post \\"https://api.trycloudflare.com/tunnel\\": '
            'dial tcp: lookup api.trycloudflare.com: no such host"')
    assert parse_url(line) is None
    assert parse_url("INF |  https://quiet-river-bold-moon.trycloudflare.com  |") == "https://quiet-river-bold-moon.trycloudflare.com"
