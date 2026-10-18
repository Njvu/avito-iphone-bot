import logging
import secrets
import socket
import time

from aiohttp import web

from bot.avito import Avito

log = logging.getLogger(__name__)

COOKIE = "rc_session"

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Капча Авито</title>
<style>
body{margin:0;font:15px system-ui,sans-serif;background:#111;color:#eee}
header{padding:8px 12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
#s{display:block;width:100%;touch-action:none;user-select:none;-webkit-user-select:none;background:#222}
input{flex:1;min-width:120px;padding:8px;font-size:16px}
button{padding:8px 12px;font-size:15px}
#st{width:100%;color:#9c9}
</style></head><body>
<header>
<input id="t" placeholder="Текст для ввода"><button id="send">Ввести</button><button id="enter">Enter</button>
<div id="st">Нажимайте и тяните прямо по картинке — это браузер бота.</div>
</header>
<img id="s" draggable="false">
<script>
const base = location.pathname.replace(/\\/$/, "");
const img = document.getElementById("s"), st = document.getElementById("st");
let busy = false, down = false, last = 0;
function refresh() {
  if (busy) return;
  busy = true;
  fetch(base + "/shot.jpg?" + Date.now(), {cache: "no-store"}).then(r => {
    if (r.ok) return r.blob().then(b => { const old = img.src; img.src = URL.createObjectURL(b); if (old) URL.revokeObjectURL(old); });
    if (r.status !== 503) { st.textContent = "Сессия закрыта: капча решена или ссылка устарела."; clearInterval(timer); }
  }).catch(() => {}).finally(() => { busy = false; });
}
const timer = setInterval(refresh, 700);
refresh();
function pos(e) {
  const r = img.getBoundingClientRect();
  return {x: (e.clientX - r.left) * img.naturalWidth / r.width, y: (e.clientY - r.top) * img.naturalHeight / r.height};
}
function send(body) {
  return fetch(base + "/input", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
}
img.addEventListener("pointerdown", e => { e.preventDefault(); down = true; img.setPointerCapture(e.pointerId); send({type: "down", ...pos(e)}); });
img.addEventListener("pointermove", e => {
  if (!down) return;
  const now = Date.now();
  if (now - last < 40) return;
  last = now;
  send({type: "move", ...pos(e)});
});
img.addEventListener("pointerup", e => { if (!down) return; down = false; send({type: "up", ...pos(e)}).then(refresh); });
document.getElementById("send").onclick = () => { const t = document.getElementById("t"); send({type: "text", text: t.value}); t.value = ""; };
document.getElementById("enter").onclick = () => send({type: "key", key: "Enter"});
</script></body></html>"""


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class RemoteControl:
    def __init__(self, avito: Avito, host: str, port: int, public_url: str = "", ttl_min: int = 15):
        self.avito, self.host, self.port, self.ttl = avito, host, port, ttl_min * 60
        self.urls = [u.strip().rstrip("/") for u in public_url.split(",") if u.strip()] or [f"http://{local_ip()}:{port}"]
        self.tunnel_url: str | None = None
        self.token: str | None = None
        self.client: str | None = None
        self.expires = 0.0
        self._runner: web.AppRunner | None = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/c/{token}", self.page)
        app.router.add_get("/c/{token}/shot.jpg", self.shot)
        app.router.add_post("/c/{token}/input", self.input)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("Страница решения капчи: %s", ", ".join(f"{u}/c/…" for u in self.urls))

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()

    def open(self) -> list[str]:
        self.token = secrets.token_urlsafe(18)
        self.client = None
        self.expires = time.time() + self.ttl
        return [f"{u}/c/{self.token}" for u in ([self.tunnel_url] if self.tunnel_url else []) + self.urls]

    def close(self):
        self.token = None
        self.client = None

    def _check(self, request: web.Request, first: bool = False) -> str | None:
        token = request.match_info["token"]
        if not self.token or time.time() > self.expires or not secrets.compare_digest(token, self.token):
            raise web.HTTPNotFound()
        if first and self.client is None:
            self.client = secrets.token_urlsafe(18)
            return self.client
        if not self.client or not secrets.compare_digest(request.cookies.get(COOKIE, ""), self.client):
            raise web.HTTPNotFound()
        return None

    async def page(self, request: web.Request):
        session = self._check(request, first=True)
        resp = web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})
        if session:
            resp.set_cookie(COOKIE, session, path=f"/c/{request.match_info['token']}", httponly=True, samesite="Strict")
        return resp

    async def shot(self, request: web.Request):
        self._check(request)
        try:
            body = await self.avito.remote_screenshot()
        except Exception:
            return web.Response(status=503, headers={"Cache-Control": "no-store"})
        return web.Response(body=body, content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def input(self, request: web.Request):
        self._check(request)
        data = await request.json()
        kind = data.get("type")
        if kind in ("down", "move", "up"):
            await self.avito.remote_mouse(kind, float(data["x"]), float(data["y"]))
        elif kind == "text":
            await self.avito.remote_type(str(data.get("text", ""))[:100])
        elif kind == "key" and data.get("key") in ("Enter", "Backspace", "Tab"):
            await self.avito.remote_key(data["key"])
        return web.json_response({"ok": True})
