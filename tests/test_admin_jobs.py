import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import bot.__main__ as ui
from bot.db import Store


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeMessage:
    def __init__(self, chat_id, bot=None, chat_type="private", **fields):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.bot = bot
        self.answers = []
        self.migrate_to_chat_id = fields.get("migrate_to_chat_id")
        self.migrate_from_chat_id = fields.get("migrate_from_chat_id")

    async def answer(self, text, **kw):
        self.answers.append(text)


class FakeMonitor:
    def __init__(self, refreshing, count=None):
        self.refreshing = refreshing
        self.count = count
        self.calls = 0

    async def refresh_prices(self):
        self.calls += 1
        return None if self.refreshing else self.count


def test_evals_stale_after_two_days(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    assert store.evals_stale(["13"])
    store.save_eval("13", 128, "Хорошее", 40000, 45000)
    store.save_eval("14", 128, "Хорошее", 50000, 55000)
    assert not store.evals_stale(["13", "14"]) and not store.evals_stale([])
    old = (datetime.now() - timedelta(hours=49)).isoformat()
    store.db.execute("UPDATE avito_eval SET updated_at=? WHERE model='14'", (old,))
    store.db.commit()
    assert store.get_eval("14", 128, "Хорошее") is None
    assert store.evals_stale(["13", "14"]) and not store.evals_stale(["13"])


def test_replace_channel(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.add_channel(-2, -1)
    assert store.replace_channel(-1, -1001, -1) and store.channels(-1) == [-1001, -2]
    assert store.replace_channel(-2, -1001, -1) and store.channels(-1) == [-1001]
    assert not store.replace_channel(-5, -1005, -1) and store.channels(-1) == [-1001]


def test_group_migration_moves_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "store", Store(tmp_path / "t.sqlite"), raising=False)
    monkeypatch.setattr(ui.settings, "group_chat_id", -1)
    ui.store.add_channel(-7, -1)
    asyncio.run(ui.on_migrated(FakeMessage(-1, chat_type="group", migrate_to_chat_id=-1001)))
    asyncio.run(ui.on_migrated(FakeMessage(-1007, chat_type="supergroup", migrate_from_chat_id=-7)))
    assert ui.channels() == [-1001, -1007]
    asyncio.run(ui.on_migrated(FakeMessage(-1001, chat_type="supergroup", migrate_from_chat_id=-1)))
    assert ui.channels() == [-1001, -1007]


def test_prices_while_refresh_running_reports_when_done(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.sqlite")
    store.save_eval("13", 128, "Хорошее", 40000, 45000)
    fm = FakeMonitor(refreshing=True)
    monkeypatch.setattr(ui, "store", store, raising=False)
    monkeypatch.setattr(ui, "monitor", fm, raising=False)
    monkeypatch.setattr(ui, "main_kb", lambda: None)

    async def finish_refresh(_):
        fm.refreshing = False

    monkeypatch.setattr(ui.asyncio, "sleep", finish_refresh)
    bot = FakeBot()
    m = FakeMessage(5, bot)

    async def run():
        await ui.cmd_prices(m)
        await ui.cmd_prices(m)
        await asyncio.gather(*ui.BACKGROUND)

    asyncio.run(run())
    assert fm.calls == 1 and len(bot.sent) == 1 and not ui.PRICE_CHATS
    assert bot.sent[0][0] == 5 and "цен в базе 1" in bot.sent[0][1] and "запросов" not in bot.sent[0][1]
    assert all(a.startswith("⏳") for a in m.answers)


def test_prices_started_here_reports_requests(tmp_path, monkeypatch):
    fm = FakeMonitor(refreshing=False, count=3)
    monkeypatch.setattr(ui, "store", Store(tmp_path / "t.sqlite"), raising=False)
    monkeypatch.setattr(ui, "monitor", fm, raising=False)
    monkeypatch.setattr(ui, "main_kb", lambda: None)
    bot = FakeBot()
    m = FakeMessage(5, bot)

    async def run():
        await ui.cmd_prices(m)
        await asyncio.gather(*ui.BACKGROUND)

    asyncio.run(run())
    assert m.answers[0].startswith("🔄") and "запросов 3" in bot.sent[0][1] and not ui.PRICE_CHATS


def test_keep_running_restarts_crashed_job(monkeypatch):
    class Stop(BaseException):
        pass

    calls = []

    async def job():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        raise Stop

    async def no_wait(_):
        pass

    monkeypatch.setattr(ui.asyncio, "sleep", no_wait)
    with pytest.raises(Stop):
        asyncio.run(ui.keep_running(job, "Мониторинг"))
    assert len(calls) == 2
