import asyncio
import html
import logging
import re
import statistics
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

from bot.avito import direct_ip_info
from bot.config import ROOT, settings
from bot.damage import DEFAULT_ALLOWED, DEFECTS
from bot.db import Store
from bot.monitor import Monitor
from bot.proxies import mask

log = logging.getLogger(__name__)
HTML = "HTML"
LOG = ROOT / "data" / "bot.log"
LIVE_KEY = "live_status"
NOISE = re.compile(r"aiogram\.event: Update id=|Метки Авито за цикл|Рынок за цикл")
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
SERVICE_USERS = {777000, 1087968824, 136817688}


@dataclass
class Context:
    store: Store
    monitor: Monitor
    status_text: Callable[[], str]
    stop: Callable[[], Awaitable[None]]


ctx: Context
router = Router()


def setup(store: Store, monitor: Monitor, status_text: Callable[[], str], stop: Callable[[], Awaitable[None]]):
    global ctx
    ctx = Context(store, monitor, status_text, stop)


def is_admin(uid: int | None) -> bool:
    return uid is not None and uid in ctx.store.admin_ids(settings.admin_ids)


def is_owner(uid: int | None) -> bool:
    return uid in settings.admin_ids


async def from_admin(m: Message) -> bool:
    return m.from_user is not None and m.sender_chat is None and is_admin(m.from_user.id)


async def callback_from_admin(c) -> bool:
    return is_admin(c.from_user.id)


router.message.filter(from_admin)
router.callback_query.filter(callback_from_admin)
B_FILTER = "🧰 Фильтр"


def avito_url(raw: str) -> str | None:
    u = urlsplit(raw.strip())
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not (host == "avito.ru" or host.endswith(".avito.ru")) or not u.path.startswith("/"):
        return None
    return f"https://{host}{u.path}"


def tail_lines(n: int, keep: Callable[[str], bool] = lambda line: True) -> list[str]:
    if not LOG.exists():
        return []
    with LOG.open(encoding="utf-8", errors="replace") as f:
        lines = f.readlines()[-max(n * 20, 2000):]
    return [line.rstrip() for line in lines if keep(line) and not NOISE.search(line)][-n:]


def since(minutes: int) -> Callable[[str], bool]:
    border = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")

    def keep(line: str) -> bool:
        m = STAMP.match(line)
        return bool(m) and m.group(1) >= border
    return keep


def short(line: str) -> str:
    return re.sub(r"^\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2}),\d+ \w+ [\w.]+: ", r"\1 ", line)


def speed_text() -> str:
    m = ctx.monitor
    checks, blocks, posts = m.count("check"), m.count("block"), m.count("post")
    every = f"~{3600 / checks:.0f} с" if checks else "—"
    lags = sorted(m.lags)
    fresh = (f"медиана {statistics.median(lags) / 60:.1f} мин, лучшая {lags[0] / 60:.1f} мин (по {len(lags)})"
             if lags else "нет данных")
    lines = ["<b>⚡ За последний час</b>",
             f"🔍 Проверок: {checks} (раз в {every}), полной загрузкой страницы: {m.count('html')}",
             f"⛔ Блоков: {blocks} · 🔄 смен IP по запросу: {m.count('rotation')}",
             f"🎯 Постов: {posts} · 🗑 снято после проверки: {m.count('removed')}",
             f"⏱ Свежесть (опубликовано → бот увидел): {fresh}"]
    recent = ctx.store.recent_sent(5)
    if recent:
        lines.append("<b>Последние посты</b>")
        lines += [f"• {sent[11:16]} iPhone {model} {storage} ГБ — {price:,} ₽".replace(",", " ")
                  + (f' <a href="{url}">↗</a>' if url else "")
                  for _, model, storage, price, sent, url in recent]
    return "\n".join(lines)


def live_text() -> str:
    return (f"{ctx.status_text()}\n\n{speed_text()}\n\n"
            f"<i>🔄 обновляется каждые {settings.live_status_sec} с · {datetime.now():%H:%M:%S}</i>")


def live_target() -> tuple[int, int, int | None] | None:
    raw = ctx.store.get_state(LIVE_KEY)
    if not raw:
        return None
    chat, message, thread = raw.split(":")
    return int(chat), int(message), int(thread) if thread else None


async def live_status_loop(bot):
    while True:
        await asyncio.sleep(settings.live_status_sec)
        target = live_target()
        if not target:
            continue
        chat, message, _ = target
        try:
            await bot.edit_message_text(live_text(), chat_id=chat, message_id=message, parse_mode=HTML,
                                        disable_web_page_preview=True)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                continue
            log.warning("Живой статус отключён: %s", e)
            ctx.store.set_state(LIVE_KEY, "")
        except Exception as e:
            log.warning("Не удалось обновить живой статус: %s", e)


@router.message(Command("status_here"))
async def cmd_status_here(m: Message):
    sent = await m.answer(live_text(), parse_mode=HTML, disable_web_page_preview=True)
    ctx.store.set_state(LIVE_KEY, f"{m.chat.id}:{sent.message_id}:{m.message_thread_id or ''}")
    try:
        await m.bot.pin_chat_message(m.chat.id, sent.message_id, disable_notification=True)
    except Exception as e:
        await m.answer(f"ℹ️ Не удалось закрепить сообщение ({html.escape(str(e))[:120]}). "
                       "Дайте боту право закреплять сообщения или закрепите вручную.", parse_mode=HTML)


@router.message(Command("status_off"))
async def cmd_status_off(m: Message):
    ctx.store.set_state(LIVE_KEY, "")
    await m.answer("⏹ Живой статус больше не обновляется.")


@router.message(Command("speed"))
async def cmd_speed(m: Message):
    await m.answer(speed_text(), parse_mode=HTML, disable_web_page_preview=True)


async def send_lines(m: Message, title: str, lines: list[str]):
    if not lines:
        return await m.answer(f"{title}: пусто")
    body = html.escape("\n".join(short(line) for line in lines))
    for i in range(0, len(body), 3500):
        await m.answer(f"<b>{title}</b>\n<pre>{body[i:i + 3500]}</pre>", parse_mode=HTML)


@router.message(Command("log"))
async def cmd_log(m: Message, command: CommandObject):
    n = int(command.args) if command.args and command.args.isdigit() else 30
    await send_lines(m, f"📜 Последние {min(n, 200)} строк лога", tail_lines(min(n, 200)))


@router.message(Command("logfile"))
async def cmd_logfile(m: Message):
    if LOG.exists():
        await m.answer_document(FSInputFile(LOG, filename="bot.log"))


@router.message(Command("errors"))
async def cmd_errors(m: Message, command: CommandObject):
    minutes = int(command.args) if command.args and command.args.isdigit() else 60
    lines = tail_lines(5000, lambda line: since(minutes)(line) and (" WARNING " in line or " ERROR " in line))
    kinds = Counter(re.sub(r"\d+", "#", short(line)[9:70]) for line in lines)
    summary = "\n".join(f"{n}× {html.escape(k)}" for k, n in kinds.most_common(10))
    await m.answer(f"<b>⚠️ Предупреждения и ошибки за {minutes} мин: {len(lines)}</b>\n{summary or 'нет'}",
                   parse_mode=HTML)
    await send_lines(m, "Последние", lines[-15:])


@router.message(Command("rotate"))
async def cmd_rotate(m: Message):
    await m.answer("🔄 Меняю IP — выполнится перед следующей проверкой…")
    try:
        rotated = await asyncio.wait_for(asyncio.shield(ctx.monitor.request_rotation()), 120)
        await m.answer("✅ IP сменён." if rotated else "⚠️ Сменить IP не удалось (или прокси без смены по ссылке).")
    except asyncio.TimeoutError:
        await m.answer("⏳ Смена IP ещё не выполнена — бот занят (блок/пауза). Статус: /status")


@router.message(Command("ip"))
async def cmd_ip(m: Message):
    ip = await direct_ip_info(ctx.monitor.avito.proxy)
    account = "есть" if await ctx.monitor.avito.logged_in() else "нет"
    await m.answer(f"🌐 Авито видит: <code>{html.escape(ip)}</code>\n"
                   f"🔌 Прокси: {html.escape(mask(ctx.monitor.avito.proxy))}\n👤 Вход в аккаунт Авито: {account}",
                   parse_mode=HTML)


@router.message(Command("screen"))
async def cmd_screen(m: Message):
    try:
        shot = await asyncio.wait_for(ctx.monitor.avito.page_screenshot(), 60)
    except Exception as e:
        return await m.answer(f"Не удалось сделать скриншот: {html.escape(str(e))[:200]}", parse_mode=HTML)
    await m.answer_photo(BufferedInputFile(shot, "screen.png"), caption="🖥 Браузер бота сейчас")


@router.message(Command("check"))
async def cmd_check(m: Message, command: CommandObject):
    url = avito_url(command.args or "")
    if not url:
        return await m.answer("Пришлите: /check https://www.avito.ru/…ссылка_на_объявление")
    await m.answer("🔎 Проверяю объявление…")
    try:
        lines = await asyncio.wait_for(ctx.monitor.explain(url), 90)
    except Exception as e:
        return await m.answer(f"Не удалось проверить: {html.escape(str(e))[:200]}", parse_mode=HTML)
    await m.answer(html.escape("\n".join(lines)), parse_mode=HTML, disable_web_page_preview=True)


@router.message(Command("restart"))
async def cmd_restart(m: Message):
    await m.answer("♻️ Перезапускаю бота — вернусь через ~30 с.")
    log.warning("Перезапуск по команде /restart от %s", m.from_user.id)
    asyncio.get_running_loop().call_later(1, lambda: asyncio.ensure_future(ctx.stop()))


def person(u) -> tuple[int | None, str]:
    if u is None or getattr(u, "is_bot", False) or u.id in SERVICE_USERS:
        return None, ""
    return u.id, u.full_name


def target_user(m: Message, args: str | None) -> tuple[int | None, str]:
    replied = m.reply_to_message
    if replied:
        origin = getattr(replied, "forward_origin", None)
        if origin is not None:
            return person(getattr(origin, "sender_user", None))
        if getattr(replied, "sender_chat", None):
            return None, ""
        return person(replied.from_user)
    token = (args or "").strip().split()[0] if args and args.strip() else ""
    return (int(token), "") if token.isdigit() and int(token) not in SERVICE_USERS else (None, "")


@router.message(Command("admins"))
async def cmd_admins(m: Message):
    extra = ctx.store.admins()
    lines = ["<b>👑 Владельцы</b> (из .env, управляют списком):"]
    lines += [f"• <code>{uid}</code>" for uid in sorted(settings.admin_ids)]
    lines.append("<b>🛡 Админы</b> (добавлены через чат):")
    lines += [f"• <code>{uid}</code> {html.escape(name)}" for uid, name in extra.items()] or ["• нет"]
    lines.append("\nДобавить: ответьте на сообщение человека командой /add_admin или /add_admin ID\n"
                 "Свой ID человек узнаёт командой /myid в личке с ботом.")
    await m.answer("\n".join(lines), parse_mode=HTML)


@router.message(Command("add_admin", "remove_admin"))
async def cmd_admin_edit(m: Message, command: CommandObject):
    if not is_owner(m.from_user.id):
        return await m.answer("⛔ Менять список админов могут только владельцы.")
    uid, name = target_user(m, command.args)
    if not uid:
        return await m.answer("Ответьте на сообщение человека этой командой или укажите его ID: "
                              f"/{command.command} 123456789")
    if command.command == "add_admin":
        added = ctx.store.add_admin(uid, name)
        await m.answer(f"✅ {html.escape(name) or uid} — админ." if added else "Уже админ.", parse_mode=HTML)
    elif uid in settings.admin_ids:
        await m.answer("Владельца из .env удалить через чат нельзя.")
    else:
        await m.answer("🗑 Админ удалён." if ctx.store.remove_admin(uid) else "Такого админа нет.")


def filter_text() -> str:
    allowed = ctx.store.allowed_defects(DEFAULT_ALLOWED)
    on = [label for key, label in DEFECTS.items() if key in allowed]
    return ("<b>🧰 Фильтр поломок</b>\n✅ — такие объявления публикуются (поломка указывается в посте), "
            "❌ — отсеиваются. Нажмите, чтобы переключить.\n\n"
            f"Сейчас пропускаются: {', '.join(on) if on else 'только целые'}.\n"
            "Магазины, чужие регионы и цены выше рынка отсеиваются всегда.")


def filter_kb() -> InlineKeyboardMarkup:
    allowed = ctx.store.allowed_defects(DEFAULT_ALLOWED)
    rows = [[InlineKeyboardButton(text=("✅ " if key in allowed else "❌ ") + label, callback_data=f"df:{key}")]
            for key, label in DEFECTS.items()]
    rows.append([InlineKeyboardButton(text="↩️ По умолчанию", callback_data="df:*reset")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("filter"))
@router.message(F.text == B_FILTER)
async def cmd_filter(m: Message):
    await m.answer(filter_text(), parse_mode=HTML, reply_markup=filter_kb())


@router.callback_query(F.data.startswith("df:"))
async def cb_filter(c: CallbackQuery):
    key = c.data[3:]
    allowed = set(ctx.store.allowed_defects(DEFAULT_ALLOWED))
    if key == "*reset":
        allowed = set(DEFAULT_ALLOWED)
    elif key in DEFECTS:
        allowed ^= {key}
    ctx.store.set_allowed_defects(allowed)
    log.info("Фильтр поломок изменён (%s): %s", c.from_user.id, ", ".join(sorted(allowed)) or "только целые")
    try:
        await c.message.edit_text(filter_text(), parse_mode=HTML, reply_markup=filter_kb())
    except TelegramBadRequest:
        pass
    await c.answer("Сохранено — действует со следующей проверки")


public_router = Router()


@public_router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Ваш ID: <code>{m.from_user.id}</code>", parse_mode=HTML)


DEBUG_HELP = """<b>🛠 Отладка и управление</b>
/status_here — живой статус в этом чате/теме (обновляется сам), /status_off — выключить
/speed — проверки, блоки, посты, свежесть за час и последние посты
/log 50 — последние строки лога · /logfile — файл лога · /errors 60 — ошибки за N минут
/ip — какой IP видит Авито и есть ли вход в аккаунт · /rotate — сменить IP
/screen — скриншот браузера бота · /filter — какие поломки пропускать (кнопки ✅/❌)
/check ссылка — пройдёт ли объявление и почему
/restart — перезапустить бота
/admins — список админов · /add_admin (ответом или ID) · /remove_admin — только владельцы
/myid — узнать свой ID (работает у всех)"""


@router.message(Command("debug"))
async def cmd_debug(m: Message):
    await m.answer(DEBUG_HELP, parse_mode=HTML)
