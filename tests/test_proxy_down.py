import asyncio
import time

import bot.monitor as mon
from bot.avito import Blocked, ProxyDown
from bot.db import Store


class Avito:
    proxy = None


def test_repeated_proxy_down_backs_off_and_rotates(tmp_path, monkeypatch):
    sleeps, rotations = [], []

    async def sleep(sec):
        sleeps.append(sec)

    async def rotate_ip(link_lost=False):
        rotations.append(link_lost)
        return True

    monkeypatch.setattr(mon.asyncio, "sleep", sleep)
    m = mon.Monitor(None, Store(tmp_path / "t.sqlite"), Avito())
    monkeypatch.setattr(m, "rotate_ip", rotate_ip)

    async def run():
        for _ in range(4):
            await m.on_blocked(ProxyDown("net::ERR_TUNNEL_CONNECTION_FAILED"))
        await m.on_blocked(Blocked("Доступ ограничен"))

    asyncio.run(run())
    assert rotations == [True, False, False, False, False]
    assert sleeps == [30, 60, 120]
    assert m.proxy_down == 0 and m.last_block > float("-inf")


def test_fast_checks_wait_for_scheduled_deep_pass(tmp_path, monkeypatch):
    m = mon.Monitor(None, Store(tmp_path / "t.sqlite"), Avito())
    calls = []

    async def cycle(pages=None, light=False):
        calls.append((pages, light))

    monkeypatch.setattr(m, "cycle", cycle)
    monkeypatch.setattr(mon.settings, "fast_json_sec", 0.1)
    monkeypatch.setattr(mon, "is_night", lambda: False)
    m.next_deep = time.monotonic() + 0.35
    asyncio.run(m.wait_next())
    assert calls and all(c == ([1], True) for c in calls)
