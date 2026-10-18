import asyncio
from types import SimpleNamespace

import bot.__main__ as ui
from bot.db import Store


class FakeMessage:
    def __init__(self, chat_type, chat_id, origin=None):
        self.chat = SimpleNamespace(type=chat_type, id=chat_id)
        self.forward_origin = origin
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


def test_add_and_remove_here(tmp_path, monkeypatch):
    ui.store = Store(tmp_path / "t.sqlite")
    monkeypatch.setattr(ui.settings, "group_chat_id", -1)
    asyncio.run(ui.cmd_add_here(FakeMessage("supergroup", -2)))
    assert ui.channels() == [-1, -2]
    asyncio.run(ui.cmd_remove_here(FakeMessage("supergroup", -1)))
    assert ui.channels() == [-2]


def test_forwarded_channel_added(tmp_path, monkeypatch):
    ui.store = Store(tmp_path / "t.sqlite")
    monkeypatch.setattr(ui.settings, "group_chat_id", -1)
    monkeypatch.setattr(ui, "main_kb", lambda: None)
    origin = SimpleNamespace(type="channel", chat=SimpleNamespace(id=-100500, title="Находки"))
    m = FakeMessage("private", 5, origin)
    asyncio.run(ui.add_forwarded_channel(m))
    assert ui.channels() == [-1, -100500] and "Находки" in m.answers[-1]
