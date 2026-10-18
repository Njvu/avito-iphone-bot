from pathlib import Path

from bot.parser import count_seller_phones, is_blocked, parse_item, parse_search

FIX = Path(__file__).parent / "fixtures"


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_search_real_markup():
    cards = parse_search(read("search_stavropol.html"))
    assert [c.id for c in cards] == ["8408773533", "8457945601", "8382185244"]
    a, b, c = cards
    assert a.title == "iPhone 12, 128 ГБ, SIM + eSIM" and a.price == 4000
    assert a.url == "https://www.avito.ru/essentuki/telefony/iphone_12_128_gb_sim_esim_8408773533"
    assert a.condition == "Требуется ремонт" and a.delivery and "плату" in a.snippet
    assert a.photos == ["https://90.img.avito.st/image/1/1.K6j5"]
    assert b.seller == "Продавец 2" and b.seller_url.endswith("/brands/6978eaf7895016a55994d96872637e6b")
    assert b.location == "Пятигорск" and b.price == 39900
    assert c.price == 48000 and c.snippet == "Продаю 15 про."


def test_parse_item_page():
    item = parse_item(read("item_14pro.html"))
    assert item.params["Экран"] == "Без дефектов"
    assert item.params["Состояние"] == "Удовлетворительное"
    assert item.params["Работа устройства"] == "Включается"
    assert len(item.photos) == 2 and item.seller == "Продавец 4" and item.seller_label == "Частное лицо"
    assert "256 ГБ" in item.description


def test_seller_and_block():
    assert count_seller_phones(read("seller.html")) == 2
    assert is_blocked("Доступ ограничен: проблема с IP")
    assert not is_blocked("Смартфоны Apple", "<script>captcha</script>")


def test_hires_photos_from_json():
    from bot.parser import hires_photos
    import json
    inner = json.dumps({"urls": {"1280x960": "https://c60.img.avito.st/image/1/1.eVaTU7a41b-l5Fey7QJBVpbz17kt8leppf_BIG",
                                 "640x480": "https://c60.img.avito.st/image/1/1.eVaTU7a41b-l5Fey7QJBVpbz17kt8leppf_MID"}})
    html = '<script>x="' + inner.replace('"', '\\"').replace("/", "\\/") + '"</script>'
    gal = ["https://80.img.avito.st/image/1/1.eVaTU7a41b-l5Fey7QJBVpbz17kt8leppf_Xv.sig"]
    assert hires_photos(html, gal) == ["https://c60.img.avito.st/image/1/1.eVaTU7a41b-l5Fey7QJBVpbz17kt8leppf_BIG"]
    assert hires_photos("", gal) == gal


def test_user_agent_has_no_headless_marker():
    from bot.avito import user_agent
    ua = user_agent("153.0.8010.12", "linux")
    assert "Headless" not in ua and "Chrome/153.0.0.0" in ua and "Linux x86_64" in ua
    assert "Windows NT 10.0" in user_agent("153.0.1", "win32")


def test_seller_profiles_from_links():
    from bot.parser import seller_profiles
    url = ("https://www.avito.ru/brands/seller8cd4/all/telefony?gdlkerfdnwq=101&page_from=from_item_card"
           "&iid=8407929503&sellerId=48530d06de8eec54bf3080c38df069ed")
    assert seller_profiles(url) == ["https://www.avito.ru/brands/seller8cd4",
                                    "https://www.avito.ru/brands/48530d06de8eec54bf3080c38df069ed"]
    assert seller_profiles("https://www.avito.ru/user/6367c4/profile") == ["https://www.avito.ru/user/6367c4"]
    assert seller_profiles("https://www.avito.ru/krasnodar/telefony/iphone_8407929503") == []


def test_parse_age_today_and_yesterday_with_time():
    from datetime import datetime
    from bot.parser import parse_age
    now = datetime(2026, 10, 17, 16, 30)
    assert parse_age("Сегодня, 15:57", now) == (33 / 60)
    assert abs(parse_age("Вчера, 16:30", now) - 24.0) < 1e-9
    assert parse_age("сегодня", now) == 6.0


def test_item_posted_time_from_item_id_line():
    from bot.parser import parse_item
    html = ('<html><h1>iPhone</h1><span data-marker="item-view/item-id">№&nbsp;8441166525 · сегодня в 03:28 · '
            '<span data-marker="item-view/total-views">36&nbsp;просмотров</span></span></html>')
    assert parse_item(html).posted == "сегодня в 03:28"
    old = '<span data-marker="item-view/item-id">№ 1 · 15 октября в 12:58 · 3 просмотра</span>'
    assert parse_item(old).posted == "15 октября в 12:58"
    assert parse_item("<h1>x</h1>").posted == ""
