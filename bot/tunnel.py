import asyncio
import logging
import re
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

ERROR_EVERY = 600
URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")


def parse_url(line: str) -> str | None:
    m = URL_RE.search(line)
    return m.group(0) if m else None


class Tunnel:
    def __init__(self, exe: str, port: int, on_url: Callable[[str], None]):
        self.exe, self.port, self.on_url = exe, port, on_url
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self.url: str | None = None
        self._errors = 0
        self._error_logged = float("-inf")

    async def start(self, wait: float = 30):
        ready = asyncio.Event()
        self._task = asyncio.create_task(self._supervise(ready))
        try:
            await asyncio.wait_for(ready.wait(), wait)
        except TimeoutError:
            log.warning("Туннель не дал адрес за %s с, капча — по локальной ссылке", wait)

    async def _supervise(self, ready: asyncio.Event):
        while True:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    self.exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", f"http://127.0.0.1:{self.port}",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            except OSError as e:
                log.warning("Не удалось запустить туннель %s: %s", self.exe, e)
                ready.set()
                return
            async for raw in self._proc.stderr:
                line = raw.decode(errors="replace").strip()
                if " ERR " in line:
                    self._log_error(line)
                url = parse_url(line)
                if url and url != self.url:
                    self.url = url
                    self.on_url(url)
                    log.info("Туннель для капчи: %s", url)
                    ready.set()
            await self._proc.wait()
            log.warning("Туннель остановился (код %s), перезапуск через 10 с", self._proc.returncode)
            await asyncio.sleep(10)

    def _log_error(self, line: str):
        self._errors += 1
        now = time.monotonic()
        if now - self._error_logged >= ERROR_EVERY:
            log.warning("Туннель: обрывы связи с Cloudflare (%s за последнее время), последний: %s", self._errors, line[-160:])
            self._errors, self._error_logged = 0, now

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            await self._proc.wait()
