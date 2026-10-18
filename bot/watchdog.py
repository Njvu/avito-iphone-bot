import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from bot.config import settings

log = logging.getLogger(__name__)


def backup_db(db: sqlite3.Connection, folder: Path, keep: int = 7) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"bot-{datetime.now():%Y%m%d}.sqlite3"
    with sqlite3.connect(target) as dst:
        db.backup(dst)
    for old in sorted(folder.glob("bot-*.sqlite3"))[:-keep]:
        old.unlink()
    return target


def is_stale(status: dict, now: datetime, minutes: int) -> bool:
    if status.get("captcha_wait") or (status.get("pause_until") or 0) > now.timestamp():
        return False
    last = status.get("last_check")
    return last is None or now - last > timedelta(minutes=minutes)


class Watchdog:
    def __init__(self, monitor, store):
        self.monitor, self.store = monitor, store
        self.started = datetime.now()
        self.alerted = False
        self.backup_day = None

    async def run(self):
        while True:
            await asyncio.sleep(300)
            try:
                await self.tick(datetime.now())
            except Exception as e:
                log.warning("Сторож: %s", e)

    async def tick(self, now: datetime):
        status = self.monitor.status
        paused = self.store.get_state("paused") == "1"
        warm = now - self.started > timedelta(minutes=settings.stale_minutes)
        stale = not paused and warm and is_stale(status, now, settings.stale_minutes)
        if stale and not self.alerted:
            self.alerted = True
            await self.monitor.notify_admins(
                f"🚨 Бот не проверял Авито больше {settings.stale_minutes} мин.\n"
                f"Последняя ошибка: {status.get('last_error') or 'нет'}")
        elif not stale and self.alerted:
            self.alerted = False
            await self.monitor.notify_admins("✅ Мониторинг Авито снова работает.")
        if settings.healthcheck_url and not stale:
            try:
                async with httpx.AsyncClient(timeout=15, trust_env=False) as c:
                    await c.get(settings.healthcheck_url)
            except httpx.HTTPError as e:
                log.warning("Сторож: пинг мониторинга не прошёл: %s", e)
        if self.backup_day != now.date():
            self.backup_day = now.date()
            log.info("Копия базы: %s", backup_db(self.store.db, settings.db_path.parent / "backup"))
