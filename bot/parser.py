import re
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

BASE = "https://www.avito.ru"


@dataclass
class Card:
    id: str
    title: str
    price: int | None
    url: str
    snippet: str = ""
    condition: str = ""
    location: str = ""
    delivery: bool = False
    seller: str = ""
    seller_url: str = ""
    photos: list[str] = field(default_factory=list)
    badges: list[str] = field(default_factory=list)
    date: str = ""
    published: float | None = None

    @property
    def age_hours(self) -> float | None:
        return parse_age(self.date)

    @property
    def below_market(self) -> bool:
        return self.avito_mark == "below"

    @property
    def avito_mark(self) -> str:
        text = " ".join(self.badges).lower()
        if "ниже рыночн" in text:
            return "below"
        if "выше рыночн" in text:
            return "above"
        if "рыночная цена" in text:
            return "market"
        return ""


@dataclass
class ItemPage:
    title: str = ""
    description: str = ""
    params: dict[str, str] = field(default_factory=dict)
    photos: list[str] = field(default_factory=list)
    seller: str = ""
    seller_url: str = ""
    seller_label: str = ""
    posted: str = ""
    price: int | None = None


_UNITS = {"минут": 1 / 60, "час": 1, "дн": 24, "день": 24, "недел": 168, "месяц": 720, "год": 8760, "лет": 8760}
_MONTHS = ("январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр")


def parse_age(text: str, now: datetime | None = None) -> float | None:
    t = (text or "").lower().strip()
    if not t:
        return None
    if "только что" in t or "секунд" in t:
        return 0.0
    day = 0 if t.startswith("сегодня") else 1 if t.startswith("вчера") else None
    if day is not None:
        clock = re.search(r"(\d{1,2}):(\d{2})", t)
        if not clock:
            return 6.0 if day == 0 else 24.0
        now = now or datetime.now()
        posted = now.replace(hour=int(clock.group(1)), minute=int(clock.group(2)), second=0, microsecond=0)
        return max(0.0, (now - posted).total_seconds() / 3600 + 24 * day)
    m = re.match(r"(\d+)?\s*([а-я]+)\s+назад", t)
    if m:
        for unit, hours in _UNITS.items():
            if m.group(2).startswith(unit):
                return int(m.group(1) or 1) * hours
    m = re.match(r"(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?(?:\D+(\d{1,2}):(\d{2}))?", t)
    if m:
        for i, name in sorted(enumerate(_MONTHS, 1), key=lambda x: -len(x[1])):
            if m.group(2).startswith(name):
                now = now or datetime.now()
                hour, minute = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
                try:
                    d = datetime(int(m.group(3) or now.year), i, int(m.group(1)), hour, minute)
                    if not m.group(3) and d - now > timedelta(days=1):
                        d = d.replace(year=now.year - 1)
                except ValueError:
                    return None
                return max(0.0, (now - d).total_seconds() / 3600)
    return None


def _text(node: Node | None) -> str:
    return " ".join(node.text(separator=" ", strip=True).split()) if node else ""


def _clean_url(href: str) -> str:
    return urljoin(BASE, href.split("?")[0]) if href else ""


_PROFILE = re.compile(r"/(brands|user)/([A-Za-z0-9_-]+)")


def seller_profiles(url: str) -> list[str]:
    path = urlsplit(url or "").path
    found = [f"{BASE}/{kind}/{ident}" for kind, ident in _PROFILE.findall(path)]
    seller_id = parse_qs(urlsplit(url or "").query).get("sellerId", [""])[0]
    if seller_id and found:
        found.append(f"{BASE}/{found[0].split('/')[3]}/{seller_id}")
    return list(dict.fromkeys(found))


def is_blocked(title: str, html: str = "") -> bool:
    t = (title or "").lower()
    head = (html or "")[:20000].lower()
    return "доступ ограничен" in t or "проблема с ip" in head or "firewall-container" in head


_CLOSED = re.compile(r'data-marker="item-view/closed|снято с публикации|товар продан', re.I)


def is_closed(html: str) -> bool:
    body = HTMLParser(html).css_first("main")
    return bool(_CLOSED.search(body.html if body else html[:200000]))


def parse_search(html: str) -> list[Card]:
    tree = HTMLParser(html)
    cards, seen = [], set()
    for node in tree.css('[data-marker="item"]'):
        link = node.css_first('a[data-marker="item-title"]') or node.css_first('a[itemprop="url"]')
        if not link:
            continue
        url = _clean_url(link.attributes.get("href") or "")
        cid = node.attributes.get("data-item-id") or (re.search(r"_(\d+)$", url) or [None, url])[1]
        if not url or cid in seen:
            continue
        seen.add(cid)
        price = None
        meta = node.css_first('meta[itemprop="price"]')
        raw = meta.attributes.get("content") if meta else _text(node.css_first('[data-marker="item-price"]'))
        digits = re.sub(r"\D", "", raw or "")
        if digits:
            price = int(digits)
        paras = [p for p in node.css("p") if not p.attributes.get("data-marker")
                 and not p.css_first("[data-marker]")]
        texts = [_text(p) for p in paras]
        snippet = max(texts, key=len, default="")
        seller_a = node.css_first('a[href^="/brands/"]') or node.css_first('a[href*="/user/"]')
        cards.append(Card(
            id=str(cid),
            title=_text(link),
            price=price,
            url=url,
            snippet=snippet,
            condition=_text(node.css_first('[data-marker="item-specific-params"]')),
            location=_text(node.css_first('[data-marker="item-location"]')),
            delivery=any("доставка" in t.lower() for t in texts),
            seller=_text(seller_a.css_first("p")) if seller_a else "",
            seller_url=_clean_url(seller_a.attributes.get("href", "")) if seller_a else "",
            photos=[i.attributes.get("src") for i in node.css('img[itemprop="image"]') if i.attributes.get("src")],
            badges=[_text(b) for b in node.css('[data-marker^="iva-item/"]') if _text(b)],
            date=_text(node.css_first('[data-marker="item-date"]')),
        ))
    return cards


_IMG_SET = re.compile(r'\{[^{}]*?"1280x960"[^{}]*\}')
_URL = re.compile(r'"(\d{2,4}x\d{2,4})"\s*:\s*"(https://[^"]+)"')


def _img_key(url: str) -> str:
    m = re.search(r"/image/1/([^/?]{12,30})", url)
    return m.group(1)[:24] if m else url


def hires_photos(html: str, gallery: list[str]) -> list[str]:
    text = html.replace('\\"', '"').replace("\\/", "/")
    big = {}
    for m in _IMG_SET.finditer(text):
        urls = dict(_URL.findall(m.group(0)))
        if "1280x960" in urls:
            for u in urls.values():
                big.setdefault(_img_key(u), urls["1280x960"])
    out = []
    for u in gallery:
        hi = big.get(_img_key(u), u)
        if hi not in out:
            out.append(hi)
    if not out:
        for hi in big.values():
            if hi not in out:
                out.append(hi)
    return out[:10]


_POSTED = re.compile(r"(сегодня|вчера|\d{1,2}\s+[а-яё]+(?:\s+\d{4})?)\s+в\s+\d{1,2}:\d{2}", re.I)


def item_posted(tree) -> str:
    for marker in ("item-view/item-date", "item-view/item-id"):
        m = _POSTED.search(_text(tree.css_first(f'[data-marker="{marker}"]')))
        if m:
            return m.group(0)
    return ""


def parse_item(html: str) -> ItemPage:
    tree = HTMLParser(html)
    params = {}
    for li in tree.css('[data-marker="item-view/item-params"] li'):
        key_node = li.css_first("span")
        full = _text(li)
        key = _text(key_node).rstrip(": ").strip() if key_node else ""
        if key:
            params[key] = full[len(_text(key_node)):].strip(": ").strip()
    gallery = tree.css_first('[data-marker="item-view/main-gallery"]')
    photos = []
    if gallery:
        for img in gallery.css("img"):
            src = img.attributes.get("src") or ""
            if src.startswith("http") and src not in photos:
                photos.append(src)
    seller_link = tree.css_first('[data-marker="seller-info/name"] a') or tree.css_first('a[data-marker="seller-link/link"]')
    return ItemPage(
        title=_text(tree.css_first("h1")),
        description=(tree.css_first('[data-marker="item-view/item-description"]') or tree.css_first('[itemprop="description"]')).text(separator="\n", strip=True)
        if (tree.css_first('[data-marker="item-view/item-description"]') or tree.css_first('[itemprop="description"]')) else "",
        params=params,
        photos=hires_photos(html, photos),
        seller=_text(tree.css_first('[data-marker="seller-info/name"]')),
        seller_url=_clean_url(seller_link.attributes.get("href", "")) if seller_link else "",
        seller_label=_text(tree.css_first('[data-marker="seller-info/label"]')),
        posted=item_posted(tree),
        price=int(digits) if (digits := re.sub(r"\D", "", _text(tree.css_first('[data-marker="item-view/item-price"]'))))
        else None,
    )


def count_seller_phones(html: str) -> int:
    tree = HTMLParser(html)
    links = set()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").split("?")[0]
        if "/telefony/" in href and re.search(r"_\d{6,}$", href):
            links.add(href)
    return len(links)
