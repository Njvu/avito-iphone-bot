import asyncio

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

import bot.__main__ as ui
from bot.config import settings


@pytest.mark.parametrize("token, message", [("", ui.NO_TOKEN), ("123456:your-token-from-BotFather", ui.NO_TOKEN),
                                            ("abc", ui.BAD_TOKEN)])
def test_bad_or_missing_token_gives_clear_bilingual_message(monkeypatch, token, message):
    monkeypatch.setattr(settings, "bot_token", token)
    with pytest.raises(SystemExit) as e:
        ui.make_bot()
    assert str(e.value) == message and "BOT_TOKEN" in message and "@BotFather" in message


@pytest.mark.parametrize("error, expected", [(TelegramUnauthorizedError(None, "Unauthorized"), "Неверный BOT_TOKEN"),
                                             (TelegramNetworkError(None, "timeout"), "TG_PROXY")])
def test_telegram_check_explains_what_to_fix(error, expected):
    class Session:
        async def close(self):
            pass

    class Bot:
        session = Session()

        async def get_me(self):
            raise error

    with pytest.raises(SystemExit) as e:
        asyncio.run(ui.check_telegram(Bot()))
    assert expected in str(e.value)


def test_busy_port_disables_only_the_captcha_page(monkeypatch):
    async def busy(self):
        raise OSError(10048, "address in use")

    monkeypatch.setattr(settings, "remote_enabled", True)
    monkeypatch.setattr(ui.RemoteControl, "start", busy)
    assert asyncio.run(ui.start_remote(object())) is None
