import asyncio
import html
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
                           CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

from bot import control
from bot.avito import Avito
from bot.captcha import NEW_IMAGE
from bot.remote import RemoteControl
from bot.tunnel import Tunnel
from bot.config import CITY_NAME, REGIONS, ROOT, settings
from bot.db import Store
from bot.models import ALL_MODELS, canonical_model
from bot.monitor import Monitor, money
from bot.parser import seller_profiles
from bot.proxies import mask
from bot.evaluation import EVAL_CONDITIONS, SHORT, eval_price
from bot.text import chunk_lines
from bot.watchdog import Watchdog

router = Router()
router.message.filter(control.from_admin)
router.callback_query.filter(control.callback_from_admin)
store: Store
monitor: Monitor
HTML = "HTML"

B_STATUS, B_MODELS, B_MARKET, B_REPAIR = "📊 Статус", "📱 Модели", "💰 Рынок", "🔧 Ремонт"
B_PRICES = "🔄 Цены Авито"
B_PAUSE, B_RESUME, B_HELP, B_STATS = "⏸ Пауза", "▶️ Запуск", "❓ Помощь", "📈 Аналитика"

HELP = f"""<b>🔎 Avito iPhone Bot</b>
Ищет б/у iPhone X–17 на Авито ({CITY_NAME}, частники) и публикует в группу
все подходящие: целые, с разбитым экраном и/или крышкой. Выгодные помечаются 🔥.

<b>Кнопки меню</b> (в личке):
📊 Статус — последняя проверка, блокировки, база цен
📱 Модели — включить/выключить модели кнопками
💰 Рынок — цены калькулятора Авито по модели (все состояния)
🔄 Цены Авито — обновить базу цен сейчас
📈 Аналитика — все модели: вилка цен по состояниям
🔧 Ремонт — цены ремонта (пока не учитываются в выгоде)
⏸ Пауза / ▶️ Запуск — остановить/продолжить мониторинг

<b>Команды</b>:
/market 13 Pro 256 — цены калькулятора Авито по модели и памяти
/prices — обновить базу цен калькулятора Авито сейчас (≈25 мин, мониторинг не останавливается)
/repair 13 Pro 9000 4000 — ремонт: экран, крышка
/add 13 Pro, /remove 13 Pro, /add all, /remove all
/add_here — в группе: публиковать находки и сюда; /remove_here — убрать группу
/channels — куда публикуются находки (канал — переслать боту пост из него)
/debug — отладка: живой статус в группе, логи, ошибки, скорость, смена IP, проверка объявления, админы"""


def main_kb() -> ReplyKeyboardMarkup:
    paused = store.get_state("paused") == "1"
    return ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=True, keyboard=[
        [KeyboardButton(text=B_STATUS), KeyboardButton(text=B_MODELS)],
        [KeyboardButton(text=B_MARKET), KeyboardButton(text=B_STATS)],
        [KeyboardButton(text=B_REPAIR), KeyboardButton(text=B_PRICES)],
        [KeyboardButton(text=B_RESUME if paused else B_PAUSE), KeyboardButton(text=control.B_FILTER)],
        [KeyboardButton(text=B_HELP)],
    ])


def private(m: Message) -> bool:
    return m.chat.type == "private"


async def reply(m: Message, text: str, markup=None):
    if markup is None and private(m):
        markup = main_kb()
    await m.answer(text, parse_mode=HTML, reply_markup=markup)


def split_model(args: str) -> tuple[str | None, list[str]]:
    tokens = (args or "").split()
    for n in range(len(tokens), 0, -1):
        m = canonical_model(" ".join(tokens[:n]))
        if m:
            return m, tokens[n:]
    return None, tokens


def parse_storage(tok: str) -> int | None:
    t = tok.lower().replace("гб", "").replace("gb", "")
    if t.endswith(("тб", "tb")):
        return int(t[:-2]) * 1024 if t[:-2].isdigit() else None
    if t.isdigit() and int(t) in (64, 128, 256, 512, 1024, 2048):
        return int(t)
    if t in ("1", "2"):
        return int(t) * 1024
    return None


def fmt_storage(gb: int) -> str:
    return f"{gb // 1024} ТБ" if gb >= 1024 else f"{gb} ГБ"


def models_kb() -> InlineKeyboardMarkup:
    tracked = set(store.tracked())
    rows, row = [], []
    for model in ALL_MODELS:
        row.append(InlineKeyboardButton(text=("✅ " if model in tracked else "▫️ ") + model, callback_data=f"tg:{model}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✅ Все", callback_data="tg:*all"),
                 InlineKeyboardButton(text="▫️ Ни одной", callback_data="tg:*none")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def models_text() -> str:
    return f"<b>📱 Отслеживаемые модели</b> ({len(store.tracked())} из {len(ALL_MODELS)})\nНажмите на модель, чтобы включить или выключить."


def thousands(n: int) -> str:
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def price_range(low: int, high: int) -> str:
    return f"{low:,}".replace(",", " ") + "–" + money(high)


def eval_lines(model: str, storage: int | None = None, full: bool = False) -> list[str]:
    by_storage: dict[int, dict[str, tuple[int, int]]] = {}
    for s, cond, low, high in store.evals_for(model):
        if not storage or s == storage:
            by_storage.setdefault(s, {})[cond] = (low, high)
    lines = []
    for s, conds in by_storage.items():
        if full:
            lines.append(f"<b>{fmt_storage(s)}</b>")
            lines += [f"  {c}: <b>{price_range(*conds[c])}</b> (ср. {money(eval_price(*conds[c], 'avg'))})"
                      for c in EVAL_CONDITIONS if c in conds]
        else:
            lines.append(f"• {fmt_storage(s)}: " + " · ".join(
                f"{SHORT[c]} {thousands(conds[c][0])}–{thousands(conds[c][1])}" for c in EVAL_CONDITIONS if c in conds))
    return lines


def market_text(model: str, storage: int | None = None) -> str:
    lines = eval_lines(model, storage, full=True)
    if not lines:
        return f"<b>💰 iPhone {model}</b>\nКалькулятор Авито ещё не оценил эту модель — оценки обновляются раз в сутки."
    return "\n".join([f"<b>💰 iPhone {model}</b> — калькулятор Авито, {html.escape(settings.avito_eval_city)}",
                      f"Вилка цен и средняя. Публикуем, если цена ≤ максимум − {money(settings.min_profit)}; ниже минимума — 🔥.", "", *lines])


def market_kb() -> InlineKeyboardMarkup:
    models = store.tracked()
    rows = [[InlineKeyboardButton(text=m, callback_data=f"mk:{m}") for m in models[i:i + 3]]
            for i in range(0, len(models), 3)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def repair_text() -> str:
    lines = ["<b>🔧 Стоимость ремонта</b> (экран / крышка)"]
    for model in store.tracked():
        r = store.get_repair(model)
        lines.append(f"• {model}: " + (f"{money(r[0])} / {money(r[1])}" if r else "не задано"))
    lines.append("\nЗадать: <code>/repair 13 Pro 9000 4000</code>")
    return "\n".join(lines)


def eval_updated_text() -> str:
    updated = store.evals_updated()
    return f"{datetime.fromisoformat(updated):%d.%m %H:%M}" if updated else "—"


def market_status() -> str:
    text = f"калькулятор Авито, вилок {store.evals_count()} (отл · хор), обновлено {eval_updated_text()}"
    since = monitor.evaluation.refresh_since
    if monitor.refreshing and since:
        text += f"\n⏳ Идёт обновление цен с {since:%H:%M}: обновлено {store.evals_since(since)} из ~{store.evals_count()}"
    elif monitor.refreshing or monitor.eval_task and not monitor.eval_task.done():
        text += "\n⏳ Идёт обновление цен"
    return text


def ago(d: datetime | None) -> str:
    if not d:
        return "—"
    sec = int((datetime.now() - d).total_seconds())
    return f"{d:%d.%m %H:%M:%S} ({sec} с назад)" if sec < 120 else f"{d:%d.%m %H:%M:%S} ({sec // 60} мин назад)"


def status_text() -> str:
    s = monitor.status
    t = lambda d: d.strftime("%d.%m %H:%M") if d else "—"
    paused = store.get_state("paused") == "1"
    lines = [
        "<b>📊 Статус</b>",
        "⏸ На паузе" if paused else "🟢 Мониторинг работает",
        f"🕒 Последняя проверка: {ago(s['last_check'])}",
        f"⏱ Ритм: {monitor.rhythm()}",
        f"⛔ Блокировок: {s['blocks']} (последняя: {t(s['last_block'])})",
        f"⚠️ Ошибка: {html.escape(s['last_error'] or 'нет')}",
        f"🎯 Постов: за час {store.sent_count(1)}, за сутки {store.sent_count(24)}",
        f"📣 Каналов для находок: {len(channels())} (/channels)",
        f"🌐 Соединение: {html.escape(mask(monitor.avito.proxy))}",
        f"💰 Рынок: {market_status()}",
        f"👤 Аккаунт Авито: {'вход есть' if s.get('account') else 'без входа'}",
        f"📱 Моделей: {len(store.tracked())} · 🤖 ИИ: {settings.ai_provider} (сбоев: {s['ai_fail']})",
    ]
    if s.get("captcha_wait"):
        lines.append("🧩 Жду решения капчи от админа (картинка в личке)")
    if s["pause_until"]:
        lines.append(f"⏳ Все IP заблокированы, пауза до {datetime.fromtimestamp(s['pause_until']):%H:%M}")
    return "\n".join(lines)


STATUS_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data="st")]])


@router.message(F.text, lambda m: monitor.captcha.is_reply(m))
async def captcha_answer(m: Message):
    monitor.captcha.answer(m.text)
    await m.answer("⏳ Ввожу символы на Авито…")


@router.callback_query(F.data == "cap:new")
async def cb_captcha_new(c: CallbackQuery):
    monitor.captcha.answer(NEW_IMAGE)
    await c.answer("Запрашиваю другую картинку…")


@router.message(Command("start", "help"))
@router.message(F.text == B_HELP)
async def cmd_help(m: Message):
    await reply(m, HELP)


def channels() -> list[int]:
    return store.channels(settings.group_chat_id)


@router.message(Command("id", "add_here"))
async def cmd_add_here(m: Message):
    if m.chat.type not in ("group", "supergroup"):
        return await reply(m, f"ID этого чата: <code>{m.chat.id}</code>\n"
                              "Канал добавляется пересылкой сюда любого поста из него.")
    added = store.add_channel(m.chat.id, settings.group_chat_id)
    note = "добавлена" if added else "уже в списке"
    await m.answer(f"✅ Группа {note} для находок (ID {m.chat.id}). Всего каналов: {len(channels())}")


@router.message(Command("remove_here"))
async def cmd_remove_here(m: Message):
    removed = store.remove_channel(m.chat.id, settings.group_chat_id)
    await m.answer("🗑 Группа убрана из рассылки находок." if removed else "Этой группы нет в списке.")


@router.message(F.forward_origin.type == "channel")
async def add_forwarded_channel(m: Message):
    chat = m.forward_origin.chat
    added = store.add_channel(chat.id, settings.group_chat_id)
    note = "добавлен" if added else "уже в списке"
    await reply(m, f"✅ Канал «{html.escape(chat.title or str(chat.id))}» {note} для находок.\n"
                   "Бот должен быть администратором канала с правом публикации.")


@router.message(Command("channels"))
async def cmd_channels(m: Message):
    lines = ["<b>📣 Куда публикуются находки</b>"]
    for chat_id in channels():
        try:
            title = (await m.bot.get_chat(chat_id)).title
        except Exception:
            title = "нет доступа"
        lines.append(f"• {html.escape(title or '')} — <code>{chat_id}</code>")
    lines.append("\nДобавить: /add_here в группе или переслать сюда пост из канала.\n"
                 "Убрать: /remove_here в группе или <code>/remove_chat ID</code> здесь.")
    await reply(m, "\n".join(lines))


@router.message(Command("block", "unblock"))
async def cmd_block(m: Message, command: CommandObject):
    profiles = seller_profiles((command.args or "").strip())
    if not profiles:
        return await reply(m, "Пришлите ссылку на профиль продавца: <code>/block https://www.avito.ru/brands/…</code>")
    if command.command == "block":
        for p in profiles:
            store.add_shop(p, "заблокирован админом")
        return await reply(m, f"⛔ Продавец заблокирован, его объявления больше не публикуются ({len(profiles)} ID).")
    removed = sum(store.remove_shop(p) for p in profiles)
    await reply(m, "✅ Продавец разблокирован." if removed else "Этого продавца нет в чёрном списке.")


@router.message(Command("remove_chat"))
async def cmd_remove_chat(m: Message, command: CommandObject):
    try:
        chat_id = int((command.args or "").strip())
    except ValueError:
        return await reply(m, "Формат: <code>/remove_chat -1001234567890</code>")
    removed = store.remove_channel(chat_id, settings.group_chat_id)
    await reply(m, "🗑 Убрано из рассылки." if removed else "Такого канала нет в списке.")


@router.my_chat_member()
async def on_added(e: ChatMemberUpdated):
    status = e.new_chat_member.status
    logging.info("Бот в чате %s (%s, %s): %s, действие от %s", e.chat.title, e.chat.id, e.chat.type,
                 status, e.from_user.id)
    if e.chat.type not in ("group", "supergroup") or not control.is_admin(e.from_user.id):
        return
    if status not in ("member", "administrator"):
        return
    if channels():
        await e.bot.send_message(e.chat.id, "ℹ️ Чтобы публиковать находки и сюда, напишите /add_here.")
        return
    store.add_channel(e.chat.id, settings.group_chat_id)
    note = "" if status == "administrator" else "\n⚠️ Сделайте бота администратором группы."
    await e.bot.send_message(e.chat.id, f"✅ Группа сохранена для находок (ID {e.chat.id}){note}")


@router.message(Command("list"))
@router.message(F.text == B_MODELS)
async def cmd_list(m: Message):
    await reply(m, models_text(), models_kb())


@router.callback_query(F.data.startswith("tg:"))
async def cb_toggle(c: CallbackQuery):
    key = c.data[3:]
    if key == "*all":
        for x in ALL_MODELS:
            store.add_tracked(x)
    elif key == "*none":
        for x in ALL_MODELS:
            store.remove_tracked(x)
    elif key in store.tracked():
        store.remove_tracked(key)
    elif key in ALL_MODELS:
        store.add_tracked(key)
    await c.message.edit_text(models_text(), parse_mode=HTML, reply_markup=models_kb())
    await c.answer("Сохранено")


@router.message(Command("add", "remove"))
async def cmd_add_remove(m: Message, command: CommandObject):
    add = command.command == "add"
    if (command.args or "").strip().lower() == "all":
        for x in ALL_MODELS:
            store.add_tracked(x) if add else store.remove_tracked(x)
        return await reply(m, "✅ Все модели " + ("добавлены" if add else "убраны"))
    model, _ = split_model(command.args)
    if not model:
        return await reply(m, "Не понял модель. Доступны: " + ", ".join(ALL_MODELS))
    store.add_tracked(model) if add else store.remove_tracked(model)
    await reply(m, f"✅ iPhone {model} " + ("добавлен" if add else "убран"))


@router.message(F.text == B_MARKET)
async def btn_market(m: Message):
    await reply(m, "<b>💰 Рынок</b>\nВыберите модель:", market_kb())


@router.callback_query(F.data.startswith("mk:"))
async def cb_market(c: CallbackQuery):
    await c.message.answer(market_text(c.data[3:]), parse_mode=HTML)
    await c.answer()


@router.message(Command("market"))
async def cmd_market(m: Message, command: CommandObject):
    model, rest = split_model(command.args)
    if not model:
        return await reply(m, "Формат: <code>/market 13 Pro</code> или <code>/market 13 Pro 256</code>")
    storage = parse_storage(rest[0]) if rest else None
    if rest and storage is None:
        return await reply(m, "Память: 64, 128, 256, 512, 1тб, 2тб")
    await reply(m, market_text(model, storage))


@router.message(Command("stats"))
@router.message(F.text == B_STATS)
async def btn_stats(m: Message):
    updated = store.evals_updated()
    if not updated:
        return await reply(m, "📈 Калькулятор Авито ещё не заполнен — оценки появятся в течение часа.")
    lines = [f"<b>📈 Рынок Авито</b> (калькулятор, {html.escape(settings.avito_eval_city)}, "
             f"обновлено {datetime.fromisoformat(updated):%d.%m %H:%M})",
             "вилка цен по состоянию, тыс. ₽: отл · хор", ""]
    for model in store.tracked():
        rows = eval_lines(model)
        if rows:
            lines += [f"<b>📱 iPhone {model}</b>", *rows, ""]
    for chunk in chunk_lines(lines):
        await reply(m, chunk)


@router.message(F.text == B_REPAIR)
async def btn_repair(m: Message):
    await reply(m, repair_text())


@router.message(Command("repair"))
async def cmd_repair(m: Message, command: CommandObject):
    model, rest = split_model(command.args)
    nums = [int("".join(ch for ch in x if ch.isdigit()) or 0) for x in rest]
    if not model:
        return await reply(m, repair_text())
    if len(nums) < 2:
        r = store.get_repair(model)
        return await reply(m, f"🔧 iPhone {model}: " + (f"экран {money(r[0])}, крышка {money(r[1])}" if r else "не задан"))
    store.set_repair(model, nums[0], nums[1])
    await reply(m, f"✅ Ремонт iPhone {model}: экран <b>{money(nums[0])}</b>, крышка <b>{money(nums[1])}</b>")


BACKGROUND: set[asyncio.Task] = set()
PRICE_CHATS: set[int] = set()


async def run_price_refresh(bot: Bot, chat_id: int):
    started = datetime.now()
    try:
        count = await monitor.refresh_prices()
        while count is None and monitor.refreshing:
            await asyncio.sleep(10)
        mins = max(1, round((datetime.now() - started).total_seconds() / 60))
        done = f"запросов {count}, " if count is not None else ""
        await bot.send_message(chat_id, f"✅ База цен обновлена за {mins} мин: {done}цен в базе {store.evals_count()}.")
    except Exception as e:
        logging.warning("Обновление базы цен для %s: %s", chat_id, e)
    finally:
        PRICE_CHATS.discard(chat_id)


@router.message(Command("prices"))
@router.message(F.text == B_PRICES)
async def cmd_prices(m: Message):
    busy = monitor.refreshing
    if m.chat.id not in PRICE_CHATS:
        PRICE_CHATS.add(m.chat.id)
        task = asyncio.create_task(run_price_refresh(m.bot, m.chat.id))
        BACKGROUND.add(task)
        task.add_done_callback(BACKGROUND.discard)
    if busy:
        return await reply(m, "⏳ База цен уже обновляется, дождитесь сообщения о завершении.")
    await reply(m, "🔄 Обновляю базу цен по калькулятору Авито: все модели, память и состояния.\n"
                   "Это около 25 минут, мониторинг идёт параллельно. Пришлю сообщение, когда закончу.")


@router.message(Command("pause", "resume"))
@router.message(F.text.in_({B_PAUSE, B_RESUME}))
async def cmd_pause(m: Message):
    pause = m.text in (B_PAUSE,) or (m.text or "").startswith("/pause")
    store.set_state("paused", "1" if pause else "0")
    await reply(m, "⏸ Мониторинг на паузе" if pause else "▶️ Мониторинг запущен")


@router.message(Command("status"))
@router.message(F.text == B_STATUS)
async def cmd_status(m: Message):
    await reply(m, status_text(), STATUS_KB)


@router.callback_query(F.data == "st")
async def cb_status(c: CallbackQuery):
    try:
        await c.message.edit_text(status_text(), parse_mode=HTML, reply_markup=STATUS_KB)
    except Exception:
        pass
    await c.answer("Обновлено")


service_router = Router()


@service_router.message(F.migrate_to_chat_id)
@service_router.message(F.migrate_from_chat_id)
async def on_migrated(m: Message):
    old, new = (m.chat.id, m.migrate_to_chat_id) if m.migrate_to_chat_id else (m.migrate_from_chat_id, m.chat.id)
    if store.replace_channel(old, new, settings.group_chat_id):
        logging.info("Группа %s стала супергруппой %s, рассылка перенесена", old, new)


debug_router = Router()


@debug_router.message()
async def unhandled(m: Message):
    logging.debug("Необработанное сообщение: chat=%s from=%s", m.chat.id, m.from_user and m.from_user.id)


async def setup_bot_profile(bot: Bot):
    private_cmds = [
        BotCommand(command="start", description="Меню и помощь"),
        BotCommand(command="status", description="Состояние мониторинга"),
        BotCommand(command="list", description="Отслеживаемые модели"),
        BotCommand(command="market", description="Рынок Авито: /market 13 Pro [256]"),
        BotCommand(command="stats", description="Аналитика: цены Авито по всем моделям и состояниям"),
        BotCommand(command="repair", description="Ремонт: /repair 13 Pro 9000 4000"),
        BotCommand(command="prices", description="Обновить базу цен Авито сейчас"),
        BotCommand(command="channels", description="Куда публикуются находки"),
        BotCommand(command="block", description="Заблокировать продавца: /block ссылка на профиль"),
        BotCommand(command="pause", description="Пауза мониторинга"),
        BotCommand(command="resume", description="Продолжить мониторинг"),
        BotCommand(command="debug", description="Отладка: логи, ошибки, скорость, IP, проверка объявления"),
        BotCommand(command="speed", description="Скорость и статистика за час"),
        BotCommand(command="errors", description="Ошибки за последний час"),
        BotCommand(command="check", description="Пройдёт ли объявление: /check ссылка"),
        BotCommand(command="filter", description="Фильтр поломок: что пропускать"),
        BotCommand(command="admins", description="Админы бота"),
        BotCommand(command="myid", description="Мой Telegram ID"),
    ]
    group_cmds = [BotCommand(command="add_here", description="Публиковать находки в эту группу"),
                  BotCommand(command="remove_here", description="Убрать группу из рассылки"),
                  BotCommand(command="status", description="Состояние мониторинга"),
                  BotCommand(command="status_here", description="Живой статус бота в этой группе/теме"),
                  BotCommand(command="status_off", description="Выключить живой статус"),
                  BotCommand(command="speed", description="Скорость и статистика за час"),
                  BotCommand(command="myid", description="Мой Telegram ID")]
    try:
        await bot.set_my_commands(private_cmds, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(group_cmds, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_short_description(f"Ищу б/у iPhone X–17 на Авито: {CITY_NAME}")
        await bot.set_my_description(
            f"🔎 Мониторю Авито ({CITY_NAME}) и присылаю б/у iPhone X–17: целые или только с разбитым "
            "экраном/крышкой, от частных продавцов. Управление — только для администраторов.")
    except Exception as e:
        logging.warning("Не удалось обновить профиль бота: %s", e)


NO_TOKEN = ("Не задан BOT_TOKEN в .env — получите токен у @BotFather и впишите его.\n"
            "BOT_TOKEN is not set in .env — get a token from @BotFather and put it there.")
BAD_TOKEN = ("Неверный BOT_TOKEN в .env — проверьте токен у @BotFather.\n"
             "BOT_TOKEN in .env is invalid — check the token with @BotFather.")
NO_TELEGRAM = ("Нет связи с Telegram (api.telegram.org): {error}\n"
               "Если Telegram заблокирован, укажите прокси TG_PROXY в .env (например socks5://127.0.0.1:10808).\n"
               "Cannot reach Telegram (api.telegram.org): {error}\n"
               "If Telegram is blocked in your network, set TG_PROXY in .env (e.g. socks5://127.0.0.1:10808).")


def make_bot() -> Bot:
    token = settings.bot_token
    if not token or "your-token" in token or "ваш-токен" in token:
        raise SystemExit(NO_TOKEN)
    session = AiohttpSession(proxy=settings.tg_proxy) if settings.tg_proxy else AiohttpSession()
    try:
        return Bot(token, session=session)
    except TokenValidationError:
        raise SystemExit(BAD_TOKEN) from None


async def check_telegram(bot: Bot):
    try:
        me = await asyncio.wait_for(bot.get_me(), 30)
    except TelegramUnauthorizedError:
        await bot.session.close()
        raise SystemExit(BAD_TOKEN) from None
    except (TelegramNetworkError, asyncio.TimeoutError, OSError) as e:
        await bot.session.close()
        raise SystemExit(NO_TELEGRAM.format(error=type(e).__name__)) from None
    logging.info("Telegram: бот @%s", me.username)


async def start_remote(avito: Avito) -> RemoteControl | None:
    if not settings.remote_enabled:
        return None
    remote = RemoteControl(avito, settings.remote_host, settings.remote_port, settings.public_url,
                           settings.captcha_wait_min + 5)
    try:
        await remote.start()
    except OSError as e:
        logging.warning("Порт %s занят — страница капчи отключена (смените REMOTE_PORT). "
                        "Port %s is busy — captcha page disabled (change REMOTE_PORT): %s",
                        settings.remote_port, settings.remote_port, e)
        return None
    return remote


async def keep_running(job, name: str):
    while True:
        try:
            await job()
        except Exception:
            logging.exception("%s остановился, перезапуск через 30 с", name)
        await asyncio.sleep(30)


async def main():
    global store, monitor
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(),
                                  RotatingFileHandler(ROOT / "data" / "bot.log", maxBytes=5_000_000,
                                                      backupCount=5, encoding="utf-8")])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bot = make_bot()
    await check_telegram(bot)
    store = Store(settings.db_path, tuple(REGIONS.values()), settings.market_reseller_listings)
    avito = Avito()
    await avito.start()
    remote = await start_remote(avito)
    tunnel = None
    if remote and settings.tunnel == "cloudflared":
        tunnel = Tunnel(settings.cloudflared_path, settings.remote_port, lambda url: setattr(remote, "tunnel_url", url))
        await tunnel.start()
    monitor = Monitor(bot, store, avito, remote)
    dp = Dispatcher()
    control.setup(store, monitor, status_text, dp.stop_polling)
    dp.include_router(control.router)
    dp.include_router(router)
    dp.include_router(control.public_router)
    dp.include_router(service_router)
    dp.include_router(debug_router)
    await setup_bot_profile(bot)
    tasks = [asyncio.create_task(keep_running(monitor.run, "Мониторинг")),
             asyncio.create_task(keep_running(Watchdog(monitor, store).run, "Сторож")),
             asyncio.create_task(keep_running(lambda: control.live_status_loop(bot), "Живой статус"))]
    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        if tunnel:
            await tunnel.stop()
        if remote:
            await remote.stop()
        await avito.close()


if __name__ == "__main__":
    (ROOT / "data").mkdir(exist_ok=True)
    asyncio.run(main())
