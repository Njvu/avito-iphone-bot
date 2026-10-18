import asyncio

import httpx

from bot.remote import RemoteControl


class FakeAvito:
    def __init__(self):
        self.events = []

    async def remote_screenshot(self):
        return b"\xff\xd8\xff jpeg"

    async def remote_mouse(self, kind, x, y):
        self.events.append((kind, x, y))

    async def remote_type(self, text):
        self.events.append(("text", text))

    async def remote_key(self, key):
        self.events.append(("key", key))


def test_remote_session():
    async def run():
        fa = FakeAvito()
        rc = RemoteControl(fa, "127.0.0.1", 18765, "http://127.0.0.1:18765")
        await rc.start()
        try:
            async with httpx.AsyncClient(trust_env=False) as c:
                assert (await c.get("http://127.0.0.1:18765/c/nope")).status_code == 404
                link = rc.open()[0]
                assert (await c.get(link)).status_code == 200
                assert (await c.get(link + "/shot.jpg")).content.startswith(b"\xff\xd8\xff")
                await c.post(link + "/input", json={"type": "down", "x": 10, "y": 20})
                await c.post(link + "/input", json={"type": "text", "text": "ab12"})
                await c.post(link + "/input", json={"type": "key", "key": "Delete"})
                async with httpx.AsyncClient(trust_env=False) as other:
                    assert (await other.get(link)).status_code == 404
                    assert (await other.get(link + "/shot.jpg")).status_code == 404
                assert (await c.get(link)).status_code == 200
                rc.close()
                assert (await c.get(link)).status_code == 404
        finally:
            await rc.stop()
        assert fa.events == [("down", 10.0, 20.0), ("text", "ab12")]

    asyncio.run(run())
