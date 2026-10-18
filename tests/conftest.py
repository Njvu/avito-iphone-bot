import os

import pytest

os.environ["BOT_SKIP_DOTENV"] = "1"


@pytest.fixture(autouse=True)
def _test_settings(monkeypatch):
    from bot.config import settings
    monkeypatch.setattr(settings, "group_chat_id", -1001234567890)
    monkeypatch.setattr(settings, "ai_provider", "none")
