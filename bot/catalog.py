from urllib.parse import urlencode

from bot.parser import BASE, Card

CATALOG_PATH = "/web/1/js/items"
CATALOG_PARAMS = {"categoryId": 84, "params[110618]": 469735, "params[110680]": 458500,
                  "verticalCategoryId": 4, "rootCategoryId": 6}
MODEL_PARAM = "params[110617][]"
MODEL_IDS = {"X": 464597, "XR": 465227, "XS": 467691, "XS Max": 459308, "11": 468690, "11 Pro": 461016,
             "11 Pro Max": 468037, "12 mini": 491622, "12": 491645, "12 Pro": 491594, "12 Pro Max": 491590,
             "13 mini": 1642360, "13": 1642358, "13 Pro": 1642359, "13 Pro Max": 1642361, "14": 17605542,
             "14 Plus": 17605549, "14 Pro": 17605543, "14 Pro Max": 17605548, "15": 18720777, "15 Plus": 18720870,
             "15 Pro": 18720713, "15 Pro Max": 18720875, "16e": 22313350, "16": 22121571, "16 Plus": 22121572,
             "16 Pro": 22121573, "16 Pro Max": 22121574, "17": 22446489, "17 Pro": 22446491, "17 Pro Max": 22446492,
             "Air": 22446490}


def catalog_path(location: int, page: int = 1, model: str | None = None) -> str:
    query = {**CATALOG_PARAMS, "locationId": location, "user": 1, "cd": 0, "s": 104, "p": page,
             "localPriority": 0, "updateListOnly": "true"}
    if model:
        query[MODEL_PARAM] = MODEL_IDS[model]
    return f"{CATALOG_PATH}?{urlencode(query)}"


def _payloads(item: dict, step: str, component: str) -> list[dict]:
    return [c.get("payload") or {} for c in (item.get("iva") or {}).get(step) or []
            if (c.get("componentData") or {}).get("component") == component]


def _payload(item: dict, step: str, component: str) -> dict:
    found = _payloads(item, step, component)
    return found[0] if found else {}


def _link(path: str) -> str:
    return f"{BASE}{path.split('?')[0]}" if path else ""


def _side(size: str) -> int:
    width, _, height = size.partition("x")
    return int(width) * int(height) if width.isdigit() and height.isdigit() else 0


def _plain(text: str | None) -> str:
    return " ".join((text or "").split())


def parse_catalog(data: dict) -> list[Card]:
    cards, seen = [], set()
    for item in ((data or {}).get("catalog") or {}).get("items") or []:
        cid = str(item.get("id") or "")
        if item.get("type") != "item" or not cid or not item.get("urlPath") or cid in seen:
            continue
        seen.add(cid)
        price = (item.get("priceDetailed") or {}).get("value")
        profile = _payload(item, "UserInfoStep", "seller-info").get("profile") or {}
        geo = _payload(item, "GeoStep", "geo").get("geoForItems") or {}
        description = _payload(item, "DescriptionStep", "description").get("description") or item.get("description")
        cards.append(Card(
            id=cid,
            title=_plain(item.get("title")),
            price=int(price) if price else None,
            url=_link(item["urlPath"]),
            snippet=_plain(description),
            condition=_plain(_payload(item, "AutoParamsStep", "auto-params").get("text")),
            location=", ".join(r["content"] for r in geo.get("geoReferences") or [] if r.get("content")),
            delivery=bool(_payloads(item, "PriceStep", "delivery") or _payloads(item, "GeoStep", "delivery")),
            seller=_plain(profile.get("title")),
            seller_url=_link(profile.get("link") or (item.get("userLogo") or {}).get("link") or ""),
            photos=[max(img.items(), key=lambda kv: _side(kv[0]))[1] for img in item.get("images") or [] if img],
            badges=[b["title"] for p in _payloads(item, "BadgeBarStep", "badgebar")
                    for b in p.get("badges") or [] if b.get("title")],
            date=_plain(_payload(item, "DateInfoStep", "date-info").get("relative")),
        ))
    return cards
