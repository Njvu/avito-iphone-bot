import bot.__main__ as ui
from bot.db import Store


def test_market_views_show_ranges(tmp_path):
    ui.store = Store(tmp_path / "t.sqlite")
    ui.store.save_eval("13", 128, "Хорошее", 40000, 45000)
    ui.store.save_eval("13", 128, "Отличное", 42500, 47000)
    assert ui.eval_lines("13") == ["• 128 ГБ: отл 42,5–47 · хор 40–45"]
    full = ui.eval_lines("13", full=True)
    assert "  Хорошее: <b>40 000–45 000 ₽</b> (ср. 42 500 ₽)" in full


def test_status_shows_live_rhythm_and_refresh_progress(tmp_path, monkeypatch):
    import time
    from datetime import datetime, timedelta
    import bot.monitor as mon
    from bot.config import settings
    monkeypatch.setattr(settings, "fast_json", True)
    monkeypatch.setattr(settings, "fast_json_sec", 15)
    monkeypatch.setattr(settings, "model_sweep", 1)
    monkeypatch.setattr(mon, "is_night", lambda: False)
    ui.store = Store(tmp_path / "t.sqlite")
    ui.store.save_eval("13", 128, "Хорошее", 40000, 45000)
    from types import SimpleNamespace
    m = mon.Monitor(None, ui.store, SimpleNamespace(proxy=None))
    m.next_deep = time.monotonic() + 300
    m.last_block = time.monotonic() - 60
    m.status["last_check"] = datetime.now() - timedelta(seconds=5)
    m.refreshing = True
    m.evaluation.refresh_since = datetime.now() - timedelta(minutes=1)
    monkeypatch.setattr(ui, "monitor", m, raising=False)
    monkeypatch.setattr(ui, "channels", lambda: [1], raising=False)
    text = ui.status_text()
    assert "1-я стр. раз в ~15 с (JSON), после блока ×2 ещё 9 мин" in text
    assert "полный проход 5 стр." in text and "(через 5 мин)" in text and "с назад)" in text
    assert "Идёт обновление цен с" in text and "обновлено 1 из ~1" in text


def test_market_button_text_still_works_next_to_status(tmp_path):
    ui.store = Store(tmp_path / "t.sqlite")
    ui.store.save_eval("13", 128, "Хорошее", 40000, 45000)
    assert "40 000–45 000" in ui.market_text("13") and callable(ui.market_status)
