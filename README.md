# Avito iPhone Deal Finder

[Русская версия](README.ru.md)

![tests](https://github.com/Njvu/avito-iphone-bot/actions/workflows/tests.yml/badge.svg)

A Telegram bot that watches the [Avito](https://www.avito.ru) classifieds marketplace around the clock (Krasnodar by
default, private sellers only) for used iPhones X through 17, Air and 16e. It filters out broken, locked and shop
listings, prices every listing against Avito's own price estimator and posts the good deals to a Telegram group —
usually within **seconds** of the listing appearing in Avito search.

## Contents

- [Features](#features)
- [Results](#results)
- [Architecture](#architecture)
- [Running on Windows — step by step](#running-on-windows--step-by-step)
- [First run: what to expect](#first-run-what-to-expect)
- [Avito account](#avito-account-optional-recommended)
- [Running on a Linux server](#running-on-a-linux-server-ubuntu)
- [Telegram controls](#telegram-controls)
- [Configuration (.env)](#configuration-env)
- [Blocks, proxies and captchas](#blocks-proxies-and-captchas)
- [Files and data](#files-and-data)
- [Troubleshooting](#troubleshooting)
- [Tests](#tests)

## Features

**Search**
- The first search page sorted by date is read every ~4 s (randomised 3–5 s) through the same lightweight JSON
  request the website itself uses: ≈0.4 s instead of ≈8 s for a full page load. Falls back to page loads if the
  JSON request is unavailable.
- Every 10 minutes a full pass over pages 1–5 (also JSON, ~5 s) catches anything missed.
- The same rhythm runs around the clock; at night (1:00–5:00) the price base is refreshed in the background.
- Only listings younger than 24 h; posted and rejected listings are never opened again.

**Filters**
- Model and storage are detected from free text (Latin/Cyrillic spellings, typos, "13 про макс", "256гб").
- By default only four conditions are posted: intact, cracked screen glass, cracked back glass, or both.
  Rejected: Face ID, camera, network, touch, display lines, water damage, "for parts", iCloud/Apple ID lock,
  non-original parts, refurbished, eSIM-only, carrier-locked. A cracked screen protector is not damage.
- Negations are understood: "not water damaged", "not locked", "Face ID works", "never repaired".
- Which defects to accept is switchable from Telegram (`/filter`, 16 toggles); an accepted defect is shown in the
  post as "⚠️ поломка: …".
- Shops are filtered by wording ("our store", warranty, instalments, trade-in), by Avito's "business seller" badge
  and by a blacklist: a seller once identified as a shop is never opened again (`/block`, `/unblock`).
- The same listing from the same seller (model, storage, price) is posted at most once a day.

**Pricing**
- The market price is the min–max range of Avito's price estimator for the model, storage and condition.
  "New"/"Excellent" listings are compared with "Excellent", all others with "Good".
- A listing is posted when its price is at most "range maximum − 1,000 ₽" (`MIN_PROFIT`). Below the minimum —
  "🔥 НИЖЕ РЫНКА" (below market), inside the range — "✅ В РЫНКЕ" (at market).
- The base (~200 ranges) is refreshed every night in small batches; manually — the "🔄 Цены Авито" button or `/prices`.

**Posting**
- Instant post: a deal goes to the group in 1–3 s straight from the search card (up to 4 photos).
- Right after that the bot opens the listing and runs the full checks: if it fails (shop, iCloud, damage, removed)
  the post is deleted; if it passes, the caption gets the description and publication time and the photos are
  replaced with Avito's maximum quality (1280×960).
- If the price of a posted listing drops, the bot replies to the post with "📉 Цена снижена" (price dropped).
- Can post to several groups and channels at once.

**Reliability**
- On a block or captcha the bot rotates the mobile proxy IP and carries on; after a block it checks 1.5× less often
  for 5 minutes. Admins get a captcha only if rotation fails: a screenshot plus a link to the bot's live browser,
  solvable from a phone.
- Watchdog: admins are notified if Avito could not be checked for 30 minutes and again when it recovers, and also
  when the Avito login is lost.
- Daily database backup (`data/backup`, last 7 kept), rotating log (`data/bot.log`, 5 × 5 MB).
- `run.bat` and the systemd service restart the bot after a crash.

**Telegram controls** — a button menu, a live status message that updates itself every 10 s, logs, error
summaries, speed statistics, a "would this listing pass and why" checker, IP rotation, browser screenshots,
remote restart and chat-managed admins. Full list [below](#telegram-controls).

Example post (the bot writes in Russian):

```
🔥 НИЖЕ РЫНКА
📱 iPhone 15 Pro, 256 ГБ
💰 36 000 ₽ (рынок Авито 38 000–44 000 ₽, средняя 41 000 ₽)
✅ Face ID
🔋 АКБ 86%
📝 «start of the seller's description»
🕒 сегодня в 12:58
📍 Краснодар, district · частное лицо
🔗 link
```

## Results

| Metric | Before | After |
|---|---|---|
| First-page check | full page load every 45 s | JSON list every ~4 s |
| Seen in search → posted to the group | 25–40 s | 1–3 s (instant post) |
| Published on Avito → seen by the bot | 3–5 min | ~1–1.5 min (the rest is Avito's own indexing delay) |
| Full pass over 5 pages | ~1.5 min | ~5 s |

The numbers come from the bot's own measurements: every verified listing logs its publication time next to the
moment the bot first saw it, and a 40-minute "source race" compared several data sources side by side.

## Architecture

```mermaid
flowchart LR
    A[Avito search JSON<br/>every ~4 s] --> P[Parser<br/>catalog.py / parser.py]
    P --> F[Filters<br/>models.py / damage.py]
    F --> M[Market price<br/>Avito estimator ranges]
    M -->|deal| I[Instant post<br/>Telegram]
    I --> V[Verify listing page]
    V -->|fails| D[Delete post]
    V -->|passes| E[Edit post: description,<br/>time, HD photos]
    S[(SQLite)] --- F
    S --- M
```

| Module | Responsibility |
|---|---|
| `bot/monitor.py` | Main loop: rhythm, blocks and IP rotation, candidate pipeline, instant posts and verification |
| `bot/avito.py` | Playwright (Chromium) session: page loads, the JSON list request, cookies and the account login |
| `bot/catalog.py`, `bot/parser.py` | Parsing of the JSON list, search pages and listing pages |
| `bot/models.py`, `bot/damage.py`, `bot/text.py` | Model/storage detection, damage, lock and shop filters |
| `bot/evaluation.py` | Avito price estimator client and nightly refresh of the price ranges |
| `bot/db.py` | SQLite storage: listings, posts, price ranges, settings, admins |
| `bot/__main__.py` | Telegram bot (aiogram 3): menus, commands, startup |
| `bot/control.py` | Live status, debug commands, admins |
| `bot/remote.py`, `bot/captcha.py`, `bot/tunnel.py` | A web page to solve a captcha in the bot's browser from a phone |
| `bot/watchdog.py` | Watchdog, database backup, healthcheck ping |
| `bot/proxies.py`, `bot/config.py` | Proxies and settings from `.env` |

**Stack:** Python 3.12+, asyncio, aiogram 3, Playwright, SQLite, selectolax, httpx, pytest, GitHub Actions.

## Running on Windows — step by step

### 1. What you need

- **Python 3.12 or newer** — [python.org/downloads](https://www.python.org/downloads/). Tick
  **"Add python.exe to PATH"** in the installer.
- **A bot token** — send `/newbot` to [@BotFather](https://t.me/BotFather), pick a name, copy the token
  (`123456:ABC…`).
- **A Telegram group** for the finds. Add the bot and make it an admin (post and delete messages).
- **Recommended: a Russian mobile proxy** (HTTP, with a "change IP" link). A home IP works too but gets blocked more
  often; datacenter IPs are blocked almost at once.
- If Telegram is not reachable directly in your network — a VPN client with a local proxy (e.g. v2rayN in
  "system proxy" mode, address like `socks5://127.0.0.1:10808`).

### 2. Download

**Code → Download ZIP** on the repository page and unpack it (e.g. to `D:\avito-iphone-bot`), or:

```bat
git clone https://github.com/Njvu/avito-iphone-bot.git
```

### 3. Enter the token and proxy

Double-click **`setup_env.bat`**. On the first run it creates the Python environment and downloads Chromium
(a few minutes), then asks for:

1. the bot token;
2. the proxy — the provider's `host:port:login:password` format or `http://login:password@host:port`
   (Enter — no proxy);
3. the proxy's "change IP" link (Enter — none).

The answers are saved to `.env` in the project folder.

### 4. Other settings

Open `.env` in Notepad and check:

- `ADMIN_IDS` — your Telegram ID (send `/myid` to the bot once it runs; can be added later);
- `GROUP_CHAT_ID` — the group ID (optional: sending `/add_here` in the group is enough);
- `TG_PROXY` — the local proxy address if Telegram only works through a VPN;
- `CITY` and `CITY_NAME` — the city (see [Another city](#another-city));
- `HEADLESS=false` shows the bot's browser window (handy on a desktop), `true` runs without a window.

Every other option is documented in [`.env.example`](.env.example) and in [Configuration](#configuration-env).

### 5. Start

Double-click **`run.bat`** and keep its window open — that is the running bot. If the bot stops, `run.bat` restarts
it after 15 seconds. To stop: `Ctrl+C` in the window, then `Y`. Do not start `run.bat` twice.

### 6. Connect the group and admins

1. Send `/myid` to the bot, put the number into `ADMIN_IDS` in `.env`, restart `run.bat`.
2. Send `/add_here` in the group — finds will be posted there. For a channel: make the bot a channel admin and
   forward any channel post to the bot.
3. `/start` in a private chat with the bot opens the control menu.

## First run: what to expect

1. The bot checks the Telegram token and connection first and stops with a clear message if something is wrong
   (wrong token, Telegram blocked — set `TG_PROXY`).
2. **No posts for the first 30–60 minutes:** the bot fills its price base from Avito's price estimator
   (~200 ranges, in small batches to avoid blocks). `/status` shows the progress; after that, deals are posted
   within seconds.
3. The internal city ID for the fast JSON requests is looked up (log line "ID города … на Авито: …").

## Avito account (optional, recommended)

Logged-in visitors see new listings sooner (in our measurements ~1–1.5 min after publication instead of 3–5 min).
Use a spare account.

1. Stop the bot (`Ctrl+C` in the `run.bat` window).
2. Run **`open_browser.bat`** — the bot's browser opens.
3. Log in to Avito and close the window. The session is kept in `data/browser`.
4. Start `run.bat` again. The log says "Авито: вход в аккаунт — есть" (logged in).

With `HEADLESS=false` you can also log in directly in the running bot's window. The bot tells admins in Telegram if
the session is lost. `open_browser.bat` is also the way to solve a captcha by hand. On a server (no window) log in
through `/browser` in Telegram.

## Running on a Linux server (Ubuntu)

A VPS with Ubuntu 22.04/24.04, 2 vCPU / 4 GB is enough. From a datacenter IP Avito works only through a mobile proxy.

```bash
git clone https://github.com/Njvu/avito-iphone-bot.git
cd avito-iphone-bot
sudo bash deploy/setup.sh
sudo nano /opt/avito-iphone-bot/.env
sudo systemctl restart avito-bot
```

`deploy/setup.sh` copies the project to `/opt/avito-iphone-bot`, creates a system user, installs dependencies and
Chromium, registers a systemd service (`deploy/avito-bot.service`) that starts the bot on boot and restarts it on
failure, and opens ports 22 and 8080.

In the server's `.env`: `HEADLESS=true`, `TG_PROXY=` (empty), `PUBLIC_URL=http://SERVER_IP:8080` (captcha link),
`PROXY_URL`, `PROXY_ROTATE_URL`. The service runs on Moscow time (`TZ=Europe/Moscow`); `setup.sh` does not enable a
firewall (on one that is already active it only opens port 8080).

Moving from a PC: stop the bot on the PC and copy the project folder with `.env` and `data/bot.sqlite3` to the server
before running `setup.sh`. A Windows browser profile cannot be read on Linux, so log in to Avito again on the server:
send `/browser` to the bot — it pauses the search and replies with a link to its live browser (valid 15 minutes, opens
on one device). Log in to Avito on that page from a phone or PC and send `/resume`. The page is plain HTTP, so it is safer to type the
password through an SSH tunnel: `ssh -L 8080:127.0.0.1:8080 root@SERVER_IP`, then open the same link with the address
replaced by `http://127.0.0.1:8080`.

Useful commands:

```bash
sudo systemctl status avito-bot
sudo journalctl -u avito-bot -f
tail -f /opt/avito-iphone-bot/data/bot.log
```

## Telegram controls

All commands are admin-only (except `/myid`). The interface is in Russian. `/start` in a private chat opens the menu:

| Button | What it does |
|---|---|
| 📊 Статус | Last check, rhythm, blocks, posts per hour/day, price base, Avito login |
| 📱 Модели | Turn tracking of individual models on or off |
| 💰 Рынок | Avito price ranges for a model (by storage and condition) |
| 📈 Аналитика | Price ranges for all models |
| 🔄 Цены Авито | Refresh the price base now (in the background, search keeps running) |
| 🔧 Ремонт | Repair price table (reference only) |
| 🧰 Фильтр | Which defects to accept or reject |
| ⏸ Пауза / ▶️ Запуск | Pause and resume the search |
| 🔁 Рестарт | Restart the bot (with confirmation, ~30 s) |

Commands:

| Command | What it does |
|---|---|
| `/status`, `/stats`, `/list` | State, price analytics, tracked models |
| `/add 13 Pro`, `/remove 13 Pro`, `/add all`, `/remove all` | Turn models on/off |
| `/market 13 Pro 256` | Estimator ranges for a model |
| `/prices` | Refresh the price base now |
| `/repair 13 Pro 9000 4000` | Repair prices (screen, back) |
| `/filter` | Defect filter |
| `/pause`, `/resume` | Pause and resume |
| `/add_here`, `/remove_here` | (in a group) post finds here / stop posting here |
| `/channels`, `/remove_chat ID` | Where finds are posted, remove a group/channel |
| `/block <url>`, `/unblock <url>` | Seller blacklist (seller profile link) |
| `/status_here`, `/status_off` | Live status in a group/topic/private chat: pinned and updated every 10 s |
| `/speed` | Checks, blocks, posts and freshness over the last hour, latest posts |
| `/check <url>` | Whether a listing would be posted and why not (nothing is posted) |
| `/log 50`, `/logfile`, `/errors 60` | Log tail, the whole log file, warnings and errors over N minutes |
| `/ip`, `/rotate`, `/screen` | IP seen by Avito and login state; change IP; screenshot of the bot's browser |
| `/browser` | Link to the bot's live browser for 15 min (search paused): log in to Avito or solve a captcha, then `/resume` |
| `/restart` | Restart the bot (`run.bat`/systemd bring it back in ~15 s) |
| `/admins`, `/add_admin`, `/remove_admin` | Admins: reply to a message, forward one, or pass an ID. Only owners from `ADMIN_IDS` can change the list |
| `/say text`, `/say_admins text` | Owners only: write as the bot to the group / to every admin privately; as a reply — forward a photo, video or file |
| `/myid` | Show your Telegram ID (anyone) |
| `/debug` | List of debug commands |

## Configuration (.env)

Restart the bot after changing `.env`. Every option with a comment — [`.env.example`](.env.example).

| Option | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | Bot token from @BotFather |
| `ADMIN_IDS` | — | Owner IDs, comma-separated |
| `GROUP_CHAT_ID` | — | Group for the finds (or `/add_here`) |
| `TG_PROXY` | empty | Proxy for Telegram only, e.g. `socks5://127.0.0.1:10808` |
| `CITY`, `CITY_NAME` | `krasnodar`, `Краснодар` | City: Avito URL slug and Russian name |
| `PROXY_URL` | empty | Proxy for Avito (`host:port:login:password` or `http://…`) |
| `PROXY_ROTATE_URL` | empty | The proxy's "change IP" link |
| `PROXY_LIST` | empty | Fallback proxies, comma-separated |
| `HEADLESS` | `true` | `false` shows the browser window |
| `FAST_JSON_SEC` | `4` | First-page check interval, seconds |
| `CHECK_INTERVAL_MIN/MAX` | `10` | Full pass interval, minutes |
| `DEEP_PAGES` | `4` | Pages beyond the first in a full pass |
| `NIGHT_HOURS`, `NIGHT_FAST_SEC` | `1-5`, `0` | Night hours (price refresh) and an optional slower night interval (0 = same as day) |
| `MAX_AGE_HOURS` | `24` | Maximum listing age |
| `MAX_POST_DELAY_SEC` | `0` | Never post a deal later than N seconds after its publication on Avito (0 = no limit) |
| `MIN_PROFIT` | `1000` | How many ₽ below the range maximum the price must be |
| `INSTANT_ALL` | `true` | Post before the listing page is verified |
| `MAX_PHOTOS` | `4` | Photos per post |
| `UNCLEAR_MODE` | `warn` | Condition not described: `warn` — post with a note, `skip` — don't post |
| `LIVE_STATUS_SEC` | `10` | Live status refresh interval |
| `REMOTE_PORT`, `PUBLIC_URL` | `8080`, empty | Captcha page and its public address |
| `TUNNEL` | empty | `cloudflared` — temporary public address for the captcha page |
| `AI_PROVIDER` | `none` | Optional AI check of the description and photos: `openai` (any compatible API), `ollama`, `claude` |
| `HEALTHCHECK_URL` | empty | External monitoring URL (healthchecks.io) |

### Another city

Set `CITY` to the city's slug from the Avito URL (`avito.ru/moskva` → `moskva`) and `CITY_NAME` to its Russian name
(`Москва`). The internal city ID is looked up automatically on the first run, so search speed is the same.

## Blocks, proxies and captchas

- **A mobile proxy** works best: on a block the bot calls `PROXY_ROTATE_URL`, gets a new IP within seconds and
  carries on. Rotation clears cookies and anti-bot marks but keeps the Avito login.
- Occasional blocks and IP changes are normal; the bot slows down for 5 minutes after a block.
- **Captcha:** if rotation fails, admins get a screenshot and a one-time link to a page where the captcha can be
  solved in the bot's live browser from a phone (on the home Wi-Fi, via `PUBLIC_URL` on a server, or through
  `TUNNEL=cloudflared`, which needs `tools/cloudflared.exe`).
- Telegram and Avito take different routes: Telegram through `TG_PROXY` (if set), Avito through `PROXY_URL` or
  directly. A VPN in TUN mode routes Avito through itself too — use "system proxy" mode.

## Files and data

| Path | What it is |
|---|---|
| `run.bat` | Starts the bot with auto-restart (installs everything on the first run via `install.bat`) |
| `setup_env.bat` | Helper: token, proxy, "change IP" link → `.env` |
| `open_browser.bat` | The bot's browser window: Avito login or captcha |
| `install.bat` | Python check, `.venv`, dependencies and Chromium |
| `.env` | Your settings and secrets (never committed) |
| `data/bot.sqlite3` | Database: listings, posts, prices, settings |
| `data/backup/` | Daily database backups (last 7) |
| `data/bot.log` | Log |
| `data/browser/` | The bot's browser profile (cookies, Avito login) |
| `prices.txt` | Starting model prices |
| `deploy/` | Linux server install (systemd) |
| `docs/MANUAL_RU.txt` | Detailed operator manual (Russian) |

## Troubleshooting

| Symptom | What to do |
|---|---|
| "Python not found" when running a `.bat` | Install Python 3.12+ with "Add python.exe to PATH", run again |
| The bot says the token is invalid | Check `BOT_TOKEN` in `.env` (no spaces or quotes) |
| "Telegram is unreachable" | Turn on the VPN and set `TG_PROXY`, e.g. `socks5://127.0.0.1:10808` |
| No posts in the first hour | Expected: the price base is being filled, see `/status` |
| No posts at all | `/status` (are checks running), `/errors 60`, `/check <url>` on a matching listing |
| Many "Блокировка Авито" (Avito block) lines in a row | Use a mobile proxy with IP rotation; `/rotate` changes the IP by hand |
| "вход в аккаунт — нет" (not logged in) | Stop the bot and log in again with `open_browser.bat` |
| Port 8080 is busy | The bot runs without the captcha page; change `REMOTE_PORT` |
| Nothing is posted to the group | The bot must be a group admin; `/channels` shows where it posts |

The log is `data/bot.log`, or `/log 50` in Telegram.

## Tests

```bash
pip install -r requirements.txt
python -m pytest -q
```

160+ tests cover parsing (search pages, JSON list, listing pages), model and damage detection, pricing rules,
the instant-post/verify/delete flow, block handling and IP rotation, Telegram controls and storage. They run on
every push via GitHub Actions (Python 3.12 and 3.14) and never read your `.env`.

## Disclaimer

This is a personal, educational project. Automated access may be against Avito's terms of service — use it
responsibly, at a low request rate and at your own risk. The project is not affiliated with Avito.

## License

[MIT](LICENSE)
