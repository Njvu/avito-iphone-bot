import base64
import json
import logging
import re
import time

import httpx

from bot.config import settings

log = logging.getLogger(__name__)

PROMPT = """Ты проверяешь объявление о продаже б/у iPhone на Авито. Отвечай СТРОГО одним JSON без пояснений.

Правила. Подходят ТОЛЬКО: телефон без поломок; разбит экран (стекло), но изображение и тач работают;
разбита задняя крышка; экран + крышка вместе. Разбитое защитное стекло/плёнка — это НЕ поломка.
Не подходят: не работает Face ID, камера, сеть, динамик, кнопки, тач; полосы/пятна/засветы на дисплее
(повреждён сам дисплей, а не только стекло); утопленник; не включается; «на запчасти»; заблокирован
iCloud/Apple ID, «не помню пароль»; неоригинальные детали; «неизвестная деталь»; восстановленный/обменка;
только eSIM; лочка под оператора/R-SIM; магазин (перекупщик-частник — подходит).
ВАЖНО: разбитая задняя крышка или разбитое стекло экрана сами по себе — это suitable: true.
В red_flags пиши только проблемы, о которых ПРЯМО сказано в тексте объявления или явно видно на фото.
Не переписывай список правил в red_flags. Если проблем нет — red_flags: [].
Если про Face ID или iCloud в объявлении ничего не сказано — "unknown".

Формат:
{"model": "13 Pro", "storage_gb": 256, "damage": ["экран"|"крышка"], "suitable": true,
 "faceid": "ok|broken|unknown", "icloud": "clean|locked|unknown",
 "red_flags": ["..."], "verdict": "кратко по-русски, 1 предложение"}

Объявление:
"""


class AIError(Exception):
    pass


DOWN_FOR = 600
_down_until: dict[str, float] = {}


def _mark_down(model: str):
    _down_until[model] = time.monotonic() + DOWN_FOR


def available_models() -> list[str]:
    now = time.monotonic()
    return [m for m in settings.ai_models if _down_until.get(m, 0) <= now]


def _parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and "suitable" in data else None


def _b64(images: list[bytes]) -> list[str]:
    return [base64.b64encode(i).decode() for i in images]


def _check(r: httpx.Response) -> httpx.Response:
    if r.status_code >= 400:
        raise AIError(f"{r.status_code} {r.text[:300]}")
    return r


async def _openai(text: str, images: list[bytes]) -> dict | None:
    content = [{"type": "text", "text": PROMPT + text}]
    content += [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in _b64(images)]
    headers = {"Authorization": f"Bearer {settings.ai_api_key}"}
    url = settings.ai_base_url.rstrip("/") + "/chat/completions"
    error = None
    async with httpx.AsyncClient(timeout=45, trust_env=False, proxy=settings.ai_proxy or None) as c:
        for model in available_models():
            payload = {"model": model, "temperature": 0, "max_tokens": 2000,
                       "messages": [{"role": "user", "content": content}]}
            try:
                r = await c.post(url, json=payload, headers=headers)
            except httpx.HTTPError as e:
                error = AIError(f"{model}: {type(e).__name__}")
                _mark_down(model)
                log.info("ИИ %s не ответил (%s), пробую следующую модель", model, type(e).__name__)
                continue
            if r.status_code >= 500 or r.status_code == 429:
                error = AIError(f"{model}: {r.status_code} {r.text[:200]}")
                _mark_down(model)
                log.info("ИИ %s недоступен (%s), пробую следующую модель", model, r.status_code)
                continue
            return _parse(_check(r).json()["choices"][0]["message"].get("content") or "")
    raise error or AIError("все модели временно недоступны")


async def _ollama(text: str, images: list[bytes]) -> dict | None:
    payload = {
        "model": settings.ollama_model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": 8192},
        "messages": [{"role": "user", "content": PROMPT + text, "images": _b64(images)}],
    }
    async with httpx.AsyncClient(timeout=180, trust_env=False) as c:
        r = _check(await c.post(settings.ollama_url.rstrip("/") + "/api/chat", json=payload))
        return _parse(r.json()["message"]["content"])


async def _claude(text: str, images: list[bytes]) -> dict | None:
    import anthropic

    kwargs = {}
    if settings.tg_proxy:
        kwargs["http_client"] = anthropic.DefaultAsyncHttpxClient(proxy=settings.tg_proxy)
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None, **kwargs)
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}}
               for b in _b64(images)]
    content.append({"type": "text", "text": PROMPT + text})
    resp = await client.messages.create(
        model=settings.claude_model, max_tokens=2000, output_config={"effort": "low"},
        messages=[{"role": "user", "content": content}])
    if resp.stop_reason == "refusal":
        return None
    return _parse("".join(b.text for b in resp.content if b.type == "text"))


PROVIDERS = {"openai": _openai, "ollama": _ollama, "claude": _claude}


def providers() -> list[str]:
    chain = [settings.ai_provider.lower(), settings.ai_fallback.lower()]
    return [p for i, p in enumerate(chain) if p in PROVIDERS and p not in chain[:i]]


async def analyze(text: str, images: list[bytes]) -> dict | None:
    for name in providers():
        try:
            result = await PROVIDERS[name](text[:6000], images)
        except Exception as e:
            log.warning("ИИ %s недоступен: %s", name, e)
            continue
        if result is not None:
            return result
        log.warning("ИИ %s вернул непонятный ответ", name)
    return None
