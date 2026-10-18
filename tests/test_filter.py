import asyncio
from types import SimpleNamespace

import bot.control as control
import bot.monitor as mon
from bot.config import settings
from bot.damage import BAD, DEFAULT_ALLOWED, DEFECTS, REASON_KEYS, blocked, inspect_damage
from bot.db import Store
from bot.models import reject_reason


def test_every_rejection_reason_has_a_switch():
    assert all(r in REASON_KEYS for _, r in BAD if r != "__icloud__")
    assert set(REASON_KEYS.values()) <= set(DEFECTS) and DEFAULT_ALLOWED <= set(DEFECTS)


def test_default_filter_keeps_current_behaviour():
    assert not inspect_damage("Не работает Face ID, экран целый").accepted
    ok = inspect_damage("Разбит экран, всё работает, Face ID работает, iCloud чистый")
    assert ok.accepted and ok.damage == ("разбит экран/стекло",)


def test_allowed_defect_passes_with_warning_and_disallowed_screen_rejects():
    allowed = DEFAULT_ALLOWED | {"faceid"}
    dmg = inspect_damage("Не работает Face ID, в остальном всё отлично", "Хорошее", "warn", allowed)
    assert dmg.accepted and "не работает Face ID" in dmg.damage
    assert any("поломка: не работает Face ID" in w for w in dmg.warnings)
    no_screen = frozenset({"back", "unclear"})
    assert not inspect_damage("Разбит экран, всё работает", "Хорошее", "warn", no_screen).accepted
    assert inspect_damage("Утопленник, не включается", "", "warn", DEFAULT_ALLOWED | {"water"}).reason == \
        "не включается/плата"


def test_reject_reasons_follow_switches():
    reason = reject_reason("iPhone 13, 128 ГБ", "залочен под оператора")
    assert reason == "лочка/R-SIM" and blocked(reason) and not blocked(reason, DEFAULT_ALLOWED | {"lock"})
    assert not blocked(None) and blocked("что-то новое", DEFAULT_ALLOWED | set(DEFECTS))


def test_filter_saved_in_store_and_used_by_monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids", {1})
    store = Store(tmp_path / "t.sqlite")
    m = mon.Monitor(None, store, None)
    control.setup(store, m, lambda: "", lambda: asyncio.sleep(0))
    assert m.allowed() == DEFAULT_ALLOWED
    edits, answers = [], []

    class Msg:
        async def edit_text(self, text, **kw):
            edits.append(kw["reply_markup"])

    async def answer(text):
        answers.append(text)

    cb = SimpleNamespace(data="df:faceid", from_user=SimpleNamespace(id=1), message=Msg(), answer=answer)
    asyncio.run(control.cb_filter(cb))
    assert "faceid" in m.allowed() and answers
    buttons = [b.text for row in edits[-1].inline_keyboard for b in row]
    assert "✅ Не работает Face ID" in buttons and "❌ Утопленник" in buttons
    cb.data = "df:*reset"
    asyncio.run(control.cb_filter(cb))
    assert m.allowed() == DEFAULT_ALLOWED
    store.set_allowed_defects(set())
    assert m.allowed() == frozenset() and "только целые" in control.filter_text()


def test_allowed_module_with_several_phrasings_is_not_caught_as_other_fault():
    allowed = DEFAULT_ALLOWED | {"module"}
    dmg = inspect_damage("Не работает камера, камера сломана, остальное отлично", "Хорошее", "warn", allowed)
    assert dmg.accepted and {REASON_KEYS[d] for d in dmg.damage} == {"module"}
