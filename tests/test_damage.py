from bot.damage import inspect_damage, is_clean_for_market


def test_back_glass_confirmed():
    r = inspect_damage("Разбита задняя крышка, Face ID работает, iCloud чистый")
    assert r.accepted and r.damage == ("разбита задняя крышка",) and not r.warnings


def test_screen_and_back():
    r = inspect_damage("Треснуло стекло экрана, тач и изображение в норме. Задняя крышка разбита. Всё работает, айклауд чистый")
    assert r.accepted and len(r.damage) == 2


def test_rejects():
    for t in ["не работает Face ID", "полосы на экране", "был в воде", "не включается", "на запчасти",
              "не помню пароль от apple id", "iCloud заблокирован", "неизвестная деталь", "камера не работает",
              "Меняли экран, сломали плату, экран не реагирует. Помогу выйти из Айклауда"]:
        assert not inspect_damage(t).accepted, t


def test_protective_glass_is_whole():
    r = inspect_damage("Разбито защитное стекло, сам экран целый. Face ID работает, iCloud чистый")
    assert r.accepted and r.damage == () and "разбито только защитное стекло" in r.warnings


def test_scratches_whole_and_battery():
    r = inspect_damage("Отличное состояние, есть сколы и царапины на корпусе. АКБ 86%")
    assert r.accepted and r.damage == () and r.battery == 86


def test_unclear_and_replaced():
    r = inspect_damage("iPhone 13, разбито стекло. Меняли аккумулятор")
    assert r.accepted and any("уточнить" in w for w in r.warnings) and any("меняли" in w for w in r.warnings)
    assert not inspect_damage("iPhone 13, разбито стекло", unclear_mode="skip").accepted


def test_condition_param():
    assert not inspect_damage("Продаю", "Требуется ремонт").accepted
    assert inspect_damage("Продаю", "Отличное").accepted
    assert not inspect_damage("Продаю").accepted


def test_market_clean():
    assert is_clean_for_market("iPhone 14 Pro, без дефектов, оплата частями", "Отличное")
    assert not is_clean_for_market("разбит экран")
    assert not is_clean_for_market("что-то", "Требуется ремонт")


def test_real_avito_faceid_wording():
    t = ("В продаже айфон 12 мини на 128г. Не работает фейс и нужно поменять дисплей тк плохо реагирует на касания\n"
         "Не работают датчики или функции: Face ID")
    assert not inspect_damage(t).accepted
    assert not inspect_damage("Не работают датчики или функции: Камера").accepted


def test_only_screen_back_allowed():
    ok = ["Разбит экран, Face ID работает, iCloud чистый",
          "Сломана задняя крышка, всё остальное работает",
          "Экран сломан, тач и изображение в норме. Ничего не сломано кроме этого. Все работает",
          "Отличное состояние, ничего не глючит"]
    for t in ok:
        assert inspect_damage(t).accepted, t
    bad = ["Разбит экран, не работает вибро", "Перезагружается сам", "Аккумулятор вздулся",
           "Погнут корпус, разбита крышка", "Глючит сенсор", "Не ловит сеть иногда", "Неисправен микрофон",
           "Отличное состояние, не работает true tone"]
    for t in bad:
        assert not inspect_damage(t).accepted, t


def test_battery_without_percent():
    from bot.damage import battery_percent
    assert battery_percent("Акб 75\nНе работает") == 75
    assert battery_percent("Состояние аккумулятора: 77 %") == 77
    assert battery_percent("АКБ новый 2023 года") is None


def test_negations_not_damage():
    good = ["Экран без царапин и трещин, корпус без сколов. Face ID работает, iCloud чистый",
            "Телефон не битый, не вскрывался, ничего не менялось. Все работает",
            "Нет ни одной царапины, трещин нет. Отличное состояние",
            "Экран: Без дефектов\nКорпус: Без дефектов\nНе работают датчики или функции: нет\nВсе работает"]
    for t in good:
        r = inspect_damage(t)
        assert r.accepted and r.damage == () and not any("меняли" in w for w in r.warnings), (t, r)
    assert inspect_damage("Разбит экран, корпус без царапин, всё работает").damage == ("разбит экран/стекло",)


def test_faceid_spellings():
    text = ("Телефон в отлично состоянии! Как новый. Без вмятин, трещин, царапин и т.д. "
            "Все функции в том числе и фэйс айди, работают отлично! АКБ 92%")
    r = inspect_damage(text, "Отличное")
    assert r.accepted and r.faceid_ok and not r.damage
    assert not inspect_damage("iPhone 16 Pro Max, фэйс айди не работает", "Хорошее").accepted
    assert inspect_damage("Фейсайди работает, айклауд чистый, без дефектов", "Отличное").faceid_ok


def test_dead_board_rejected():
    assert inspect_damage("Отличное состояние, но плата сгорела").reason == "не включается/плата"


def test_foreign_or_hacked_icloud_rejected():
    for text in ("Телефон взломан машиниками, стоит их айклауд и пароль. Отличное состояние",
                 "Отличное состояние, на нём чужой Apple ID",
                 "Отличное состояние, айклауд бывшего владельца, пароль не знаю",
                 "Отличное состояние, стоит чужой icloud"):
        assert inspect_damage(text).reason == "чужой/взломанный iCloud", text
    ok = inspect_damage("Отличное состояние, айклауд чистый, выйду при покупке, всё работает")
    assert ok.accepted and ok.icloud_ok


def test_honest_seller_phrases_not_rejected():
    for text in ("Отличное состояние, я не мошенник, всё работает", "Отличное состояние, не взломан, всё работает"):
        assert inspect_damage(text).accepted, text
