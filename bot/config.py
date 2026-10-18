import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if not os.getenv("BOT_SKIP_DOTENV"):
    load_dotenv(ROOT / ".env")
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

SEARCH_PATH = "telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc"
CITY = os.getenv("CITY", "").strip() or "krasnodar"
CITY_NAME = os.getenv("CITY_NAME", "").strip() or "Краснодар"
REGIONS = {CITY: CITY_NAME}


def proxy_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw or "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, login, password = parts
        return f"http://{login}:{password}@{host}:{port}"
    return f"http://{raw}"


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, "").strip() or default)


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, "").strip() or default)


def _hours(name: str, default: str) -> tuple[int, int]:
    lo, hi = (os.getenv(name, "").strip() or default).split("-")
    return int(lo), int(hi)


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    group_chat_id: int = _int("GROUP_CHAT_ID", 0)
    admin_ids: set[int] = field(default_factory=lambda: {
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    })

    check_interval_min: int = max(3, _int("CHECK_INTERVAL_MIN", 3))
    check_interval_max: int = max(3, _int("CHECK_INTERVAL_MAX", 5))
    block_pause_min: int = _int("BLOCK_PAUSE_MIN", 10)
    block_pause_max: int = _int("BLOCK_PAUSE_MAX", 15)
    fast_interval_sec: int = _int("FAST_INTERVAL_SEC", 45)
    fast_jitter_sec: int = _int("FAST_JITTER_SEC", 10)
    night_fast_sec: int = _int("NIGHT_FAST_SEC", 120)
    fast_turbo_sec: int = _int("FAST_TURBO_SEC", 30)
    turbo_after_min: int = _int("TURBO_AFTER_MIN", 20)
    block_slowdown_min: int = _int("BLOCK_SLOWDOWN_MIN", 10)
    block_slowdown_factor: float = _float("BLOCK_SLOWDOWN_FACTOR", 2.0)
    fast_json: bool = _bool("FAST_JSON", True)
    fast_json_sec: int = _int("FAST_JSON_SEC", 7)
    instant_below_min: bool = _bool("INSTANT_BELOW_MIN", True)
    instant_all: bool = _bool("INSTANT_ALL", True)
    model_sweep: int = _int("MODEL_SWEEP", 0)
    live_status_sec: int = max(5, _int("LIVE_STATUS_SEC", 10))
    avito_eval: bool = _bool("AVITO_EVAL", True)
    market_source: str = os.getenv("MARKET_SOURCE", "eval").strip().lower()
    avito_eval_city: str = os.getenv("AVITO_EVAL_CITY", "").strip() or CITY_NAME
    avito_eval_basis: str = os.getenv("AVITO_EVAL_BASIS", "avg").strip().lower()
    avito_eval_markup: int = _int("AVITO_EVAL_MARKUP", 0)
    avito_eval_per_step: int = _int("AVITO_EVAL_PER_STEP", 40)
    night_hours: tuple[int, int] = field(default_factory=lambda: _hours("NIGHT_HOURS", "1-5"))
    deep_pages: int = _int("DEEP_PAGES", 2)
    max_age_hours: int = _int("MAX_AGE_HOURS", 24)
    max_photos: int = _int("MAX_PHOTOS", 4)
    proxy_list: list[str] = field(default_factory=lambda: [
        proxy_url(x) for x in os.getenv("PROXY_LIST", "").split(",") if x.strip()])
    free_proxies: bool = _bool("FREE_PROXIES", True)
    captcha_relay: bool = _bool("CAPTCHA_RELAY", True)
    captcha_wait_min: int = _int("CAPTCHA_WAIT_MIN", 10)
    captcha_cooldown_min: int = _int("CAPTCHA_COOLDOWN_MIN", 60)
    remote_enabled: bool = _bool("REMOTE_ENABLED", True)
    remote_host: str = os.getenv("REMOTE_HOST", "0.0.0.0")
    remote_port: int = _int("REMOTE_PORT", 8080)
    public_url: str = os.getenv("PUBLIC_URL", "").strip()
    tunnel: str = os.getenv("TUNNEL", "").strip().lower()
    cloudflared_path: str = os.getenv("CLOUDFLARED_PATH", "").strip() or str(ROOT / "tools" / "cloudflared.exe")
    bootstrap_pages: int = _int("BOOTSTRAP_PAGES", 5)
    headless: bool = _bool("HEADLESS", True)
    light_pages: bool = _bool("LIGHT_PAGES", False)
    proxy_url: str = proxy_url(os.getenv("PROXY_URL", ""))
    proxy_rotate_url: str = os.getenv("PROXY_ROTATE_URL", "").strip()
    proxy_rotate_wait: int = _int("PROXY_ROTATE_WAIT", 15)
    proxy_rotate_every: int = _int("PROXY_ROTATE_EVERY", 0)
    tg_proxy: str = os.getenv("TG_PROXY", "").strip()
    reseller_max_phones: int = _int("RESELLER_MAX_PHONES", 0)
    market_reseller_listings: int = _int("MARKET_RESELLER_LISTINGS", 3)

    deal_threshold: float = _float("DEAL_THRESHOLD", 0.85)
    min_profit: int = _int("MIN_PROFIT", 1000)
    min_profit_broken: int = _int("MIN_PROFIT_BROKEN", 2500)
    base_prices_first: bool = _bool("BASE_PRICES_FIRST", True)
    market_discount: float = _float("MARKET_DISCOUNT", 0.9)
    market_window_hours: int = _int("MARKET_WINDOW_HOURS", 6)
    market_min_samples: int = _int("MARKET_MIN_SAMPLES", 10)
    market_publish_samples: int = _int("MARKET_PUBLISH_SAMPLES", 20)
    unclear_mode: str = os.getenv("UNCLEAR_MODE", "warn")

    ai_provider: str = os.getenv("AI_PROVIDER", "ollama")
    ai_fallback: str = os.getenv("AI_FALLBACK", "none")
    ai_base_url: str = os.getenv("AI_BASE_URL", "").strip()
    ai_api_key: str = os.getenv("AI_API_KEY", "").strip()
    ai_models: list[str] = field(default_factory=lambda: [
        x.strip() for x in os.getenv("AI_MODEL", "gemini-3.5-flash").split(",") if x.strip()])
    ai_proxy: str = os.getenv("AI_PROXY", "").strip()
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
    ai_max_photos: int = _int("AI_MAX_PHOTOS", 3)

    stale_minutes: int = _int("STALE_MINUTES", 30)
    healthcheck_url: str = os.getenv("HEALTHCHECK_URL", "").strip()

    db_path: Path = ROOT / os.getenv("DB_PATH", "data/bot.sqlite3")


settings = Settings()
