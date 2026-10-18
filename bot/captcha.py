import asyncio
import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)

NEW_IMAGE = "__new__"
KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Другая картинка", callback_data="cap:new")]])


def captcha_caption(kind: str | None, links: list[str] | None) -> str:
    lines = ["🧩 Авито просит капчу."]
    if links:
        lines.append("Откройте и решите прямо в браузере бота (подойдёт любая ссылка):\n" + "\n".join(links))
        lines.append("Или решите в окне Chromium бота на компьютере.")
    if kind == "text":
        lines.append("Или ответьте на это сообщение символами с картинки (reply).")
    return "\n".join(lines)


class CaptchaRelay:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.messages: set[tuple[int, int]] = set()
        self.answers: asyncio.Queue[str] = asyncio.Queue()

    async def ask(self, image: bytes, admins: set[int], caption: str, with_new: bool = True):
        self.drain()
        for uid in admins:
            try:
                msg = await self.bot.send_photo(uid, BufferedInputFile(image, "captcha.png"), caption=caption,
                                                reply_markup=KB if with_new else None)
                self.messages.add((uid, msg.message_id))
            except Exception as e:
                log.warning("Не удалось отправить капчу админу %s: %s", uid, e)

    async def notify(self, admins: set[int], text: str, image: bytes | None = None):
        for uid in admins:
            try:
                if image:
                    await self.bot.send_photo(uid, BufferedInputFile(image, "page.png"), caption=text)
                else:
                    await self.bot.send_message(uid, text)
            except Exception as e:
                log.warning("Не удалось написать админу %s: %s", uid, e)

    def is_reply(self, m: Message) -> bool:
        r = m.reply_to_message
        return bool(r and (m.chat.id, r.message_id) in self.messages)

    def answer(self, text: str):
        self.answers.put_nowait(text.strip())

    def drain(self):
        while not self.answers.empty():
            self.answers.get_nowait()

    async def wait(self, minutes: float) -> str | None:
        try:
            return await asyncio.wait_for(self.answers.get(), minutes * 60)
        except asyncio.TimeoutError:
            return None

    def close(self):
        self.messages.clear()
        self.drain()
