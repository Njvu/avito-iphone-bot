import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import bot.control as control
import bot.monitor as mon
from bot.config import settings
from bot.db import Store
from bot.parser import parse_item

FIX = Path(__file__).parent / "fixtures"


def _setup(tmp_path, monkeypatch, avito=None):
    monkeypatch.setattr(settings, "admin_ids", {1})
    store = Store(tmp_path / "t.sqlite")
    m = mon.Monitor(None, store, avito)
    control.setup(store, m, lambda: "STATUS", lambda: asyncio.sleep(0))
    return store, m


def test_admins_added_by_owner_only(tmp_path, monkeypatch):
    store, _ = _setup(tmp_path, monkeypatch)
    assert control.is_admin(1) and not control.is_admin(2) and control.is_owner(1)
    assert store.add_admin(2, "Вася: тест;") and not store.add_admin(2, "Вася")
    assert control.is_admin(2) and not control.is_owner(2)
    assert store.admins() == {2: "Вася"} and store.admin_ids({1}) == {1, 2}
    assert store.remove_admin(2) and not store.remove_admin(2) and not control.is_admin(2)


def test_target_user_from_reply_forward_or_id_rejects_service_accounts():
    user = SimpleNamespace(id=7, full_name="Петя", is_bot=False)
    reply = SimpleNamespace(reply_to_message=SimpleNamespace(from_user=user, forward_origin=None, sender_chat=None))
    assert control.target_user(reply, None) == (7, "Петя")
    fwd = SimpleNamespace(reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=1, full_name="Я", is_bot=False),
                                                           forward_origin=SimpleNamespace(sender_user=user),
                                                           sender_chat=None))
    assert control.target_user(fwd, None) == (7, "Петя")
    anon = SimpleNamespace(id=1087968824, full_name="Group", is_bot=True)
    for msg in (SimpleNamespace(from_user=anon, forward_origin=None, sender_chat=None),
                SimpleNamespace(from_user=user, forward_origin=None, sender_chat=SimpleNamespace(id=-100)),
                SimpleNamespace(from_user=user, forward_origin=SimpleNamespace(sender_chat=1), sender_chat=None)):
        assert control.target_user(SimpleNamespace(reply_to_message=msg), None) == (None, "")
    plain = SimpleNamespace(reply_to_message=None)
    assert control.target_user(plain, "12345") == (12345, "") and control.target_user(plain, "abc") == (None, "")
    assert control.target_user(plain, "777000") == (None, "") and control.target_user(plain, "-5") == (None, "")


def test_check_accepts_only_https_avito_links():
    assert control.avito_url("https://www.avito.ru/krasnodar/telefony/x_1?utm=1") == \
        "https://www.avito.ru/krasnodar/telefony/x_1"
    for bad in ("file:///C:/avito.ru/x", "http://www.avito.ru/x", "https://evil.com/avito.ru/x",
                "https://avito.ru.evil.com/x", "https://user@evil.com/?q=avito.ru/", "data:text/html,avito.ru/"):
        assert control.avito_url(bad) is None, bad


def test_speed_and_live_text_show_hour_counters(tmp_path, monkeypatch):
    store, m = _setup(tmp_path, monkeypatch)
    for kind in ("check", "check", "block", "post"):
        m.note(kind)
    m.lags.extend([60, 120, 180])
    store.save_listing("9", "https://www.avito.ru/krasnodar/x_9", "13", 128, 30000, "Краснодар", True)
    store.mark_sent("9", "13", 128, 30000, "s", [(-5, 1)])
    text = control.speed_text()
    assert "Проверок: 2" in text and "Блоков: 1" in text and "Постов: 1" in text
    assert "медиана 2.0 мин" in text and "iPhone 13 128 ГБ — 30 000 ₽" in text and "x_9" in text
    assert control.live_text().startswith("STATUS") and "каждые" in control.live_text()


def test_log_tail_filters_noise_and_time(tmp_path, monkeypatch):
    log = tmp_path / "bot.log"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(f"2020-01-01 00:00:00,000 WARNING bot.monitor: старое\n"
                   f"{now},000 INFO aiogram.event: Update id=1 is handled\n"
                   f"{now},000 WARNING bot.monitor: Блокировка Авито\n", encoding="utf-8")
    monkeypatch.setattr(control, "LOG", log)
    assert [control.short(x) for x in control.tail_lines(10)] == [
        "00:00:00 старое", f"{now[11:]} Блокировка Авито"]
    assert len(control.tail_lines(10, control.since(60))) == 1


def test_posted_lag_and_item_price():
    seen = datetime(2026, 10, 18, 12, 3, 30)
    assert mon.posted_lag("сегодня в 12:01", seen) == 150
    assert mon.posted_lag("вчера в 12:01", seen) is None and mon.posted_lag("сегодня в 13:00", seen) is None
    html = '<h1>iPhone</h1><span data-marker="item-view/item-price">10&nbsp;000₽</span>'
    assert parse_item(html).price == 10000 and parse_item("<h1>x</h1>").price is None


def test_explain_reports_verdict_without_side_effects(tmp_path, monkeypatch):
    class Avito:
        async def fetch(self, url, aux=False):
            return (FIX / "item_14pro.html").read_text(encoding="utf-8")

    store, m = _setup(tmp_path, monkeypatch, Avito())
    lines = asyncio.run(m.explain("https://www.avito.ru/krasnodar/x_1"))
    assert lines[-1].startswith(("✅", "❌")) and any("iPhone" in x for x in lines)
    assert not store.was_checked("1", 0) and store.shops_count() == 0


def test_rotation_request_is_served_by_monitor(tmp_path, monkeypatch):
    store, m = _setup(tmp_path, monkeypatch)

    async def rotate_ip(link_lost=False):
        return True

    monkeypatch.setattr(m, "rotate_ip", rotate_ip)

    async def run():
        wanted = m.request_rotation()
        assert m.request_rotation() is wanted
        await m.serve_rotation()
        return await wanted

    assert asyncio.run(run()) is True and m.count("rotation") == 1


def test_admin_filters_run_in_event_loop_not_in_thread():
    import inspect
    assert inspect.iscoroutinefunction(control.from_admin) and inspect.iscoroutinefunction(control.callback_from_admin)


def test_status_here_sends_pins_and_remembers_message(tmp_path, monkeypatch):
    store, _ = _setup(tmp_path, monkeypatch)
    calls = []

    class Bot:
        async def pin_chat_message(self, chat, message, disable_notification=False):
            calls.append(("pin", chat, message))

    class Msg:
        chat = SimpleNamespace(id=-100)
        message_thread_id = 5
        bot = Bot()

        async def answer(self, text, **kw):
            assert "message_thread_id" not in kw
            calls.append(("send", text[:6]))
            return SimpleNamespace(message_id=42)

    asyncio.run(control.cmd_status_here(Msg()))
    assert calls == [("send", "STATUS"), ("pin", -100, 42)] and control.live_target() == (-100, 42, 5)


def test_browser_pauses_search_and_sends_a_link(tmp_path, monkeypatch):
    store, m = _setup(tmp_path, monkeypatch)
    answers = []

    class Msg:
        def __init__(self, chat_type="private"):
            self.chat = SimpleNamespace(type=chat_type)

        async def answer(self, text, **kw):
            answers.append(text)

    m.remote = SimpleNamespace(open=lambda: ["http://1.2.3.4:8080/c/token"])
    asyncio.run(control.cmd_browser(Msg("supergroup")))
    assert "в личке" in answers[0] and store.get_state("paused") != "1"
    asyncio.run(control.cmd_browser(Msg()))
    assert store.get_state("paused") == "1" and "http://1.2.3.4:8080/c/token" in answers[1] and "SSH" in answers[1]
    m.remote = SimpleNamespace(open=lambda: ["https://x.trycloudflare.com/c/token"])
    asyncio.run(control.cmd_browser(Msg()))
    assert "SSH" not in answers[2]
    m.remote = None
    asyncio.run(control.cmd_browser(Msg()))
    assert "выключена" in answers[3]


def test_restart_button_asks_and_restarts_only_on_confirm(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    stopped, markups, edits = [], [], []

    async def stop():
        stopped.append(1)

    async def noop(*a, **kw):
        return None

    control.ctx.stop = stop

    class Msg:
        async def answer(self, text, reply_markup=None, **kw):
            markups.append(reply_markup)

        async def edit_text(self, text, **kw):
            edits.append(text)

    async def run():
        await control.cmd_restart_button(Msg())
        for data in ("rs:no", "rs:yes"):
            await control.cb_restart(SimpleNamespace(data=data, message=Msg(), from_user=SimpleNamespace(id=1), answer=noop))
        await asyncio.sleep(1.3)

    asyncio.run(run())
    assert markups[0].inline_keyboard[0][0].callback_data == "rs:yes"
    assert edits[0] == "Перезапуск отменён." and edits[1].startswith("♻️") and stopped == [1]


def test_only_owners_write_to_the_group_and_admins_as_the_bot(tmp_path, monkeypatch):
    store, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "group_chat_id", -100)
    store.add_admin(2, "Вася")
    store.add_admin(3, "Петя")
    sent, answers = [], []

    class FakeBot:
        async def send_message(self, chat, text):
            if chat == 3:
                raise RuntimeError("chat not found")
            sent.append(("text", chat, text))

        async def copy_message(self, chat, from_chat, message_id):
            sent.append(("copy", chat, message_id))

    class Msg:
        def __init__(self, uid, chat_type="private", reply=None):
            self.from_user = SimpleNamespace(id=uid)
            self.chat = SimpleNamespace(type=chat_type, id=uid)
            self.reply_to_message = reply
            self.bot = FakeBot()

        async def answer(self, text, **kw):
            answers.append(text)

    def cmd(args, name="say"):
        return SimpleNamespace(args=args, command=name)

    async def run():
        await control.cmd_say(Msg(2), cmd("привет"))
        await control.cmd_say(Msg(1, "supergroup"), cmd("привет"))
        await control.cmd_say(Msg(1), cmd(""))
        await control.cmd_say(Msg(1), cmd("Всем привет"))
        await control.cmd_say(Msg(1, reply=SimpleNamespace(message_id=55)), cmd(None))
        await control.cmd_say_admins(Msg(1), cmd("Собрание в 18:00", "say_admins"))

    asyncio.run(run())
    assert "только владельцы" in answers[0] and "только в личке" in answers[1] and "/say текст" in answers[2]
    assert sent == [("text", -100, "Всем привет"), ("copy", -100, 55), ("text", 2, "Собрание в 18:00")]
    assert "1 из 1" in answers[3] and "1 из 1" in answers[4] and "1 из 2" in answers[5] and "3" in answers[5]
