import asyncio

from playwright.async_api import async_playwright

from bot.avito import launch_context, search_url
from bot.config import REGIONS, settings


async def main():
    async with async_playwright() as pw:
        ctx = await launch_context(pw, settings.proxy_url, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(search_url(next(iter(REGIONS))))
        print("Войдите в аккаунт Авито или решите капчу в открытом окне, затем закройте окно.\n"
              "Log in to Avito or solve a captcha in the opened window, then close it.")
        await page.wait_for_event("close", timeout=0)
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
