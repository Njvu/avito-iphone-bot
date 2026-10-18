import re
import sys

from bot.config import ROOT, proxy_url

ENV = ROOT / ".env"


def set_value(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        return re.sub(rf"^{key}=.*$", lambda _: line, text, flags=re.M)
    return text.rstrip("\n") + f"\n{line}\n"


def main():
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else (ROOT / ".env.example").read_text(encoding="utf-8")
    print("Вставьте значение и нажмите Enter. Пустая строка — оставить как есть.\n"
          "Paste a value and press Enter. Empty line keeps the current value.\n")
    answers = {
        "BOT_TOKEN": input("1) Токен бота от @BotFather / bot token: ").strip(),
        "PROXY_URL": proxy_url(input("2) Прокси (host:port:login:password или http://…) / proxy: ")),
        "PROXY_ROTATE_URL": input("3) Ссылка смены IP / IP rotation link: ").strip(),
    }
    for key, value in answers.items():
        if value:
            text = set_value(text, key, value)
    ENV.write_text(text, encoding="utf-8")
    print(f"\nСохранено в {ENV}. Бот подхватит настройки при следующем запуске.\nSaved to {ENV}.")


if __name__ == "__main__":
    sys.exit(main())
