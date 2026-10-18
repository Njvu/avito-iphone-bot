import re
from dataclasses import dataclass

from bot.text import normalize

ALL_MODELS = [
    "X", "XR", "XS", "XS Max",
    "11", "11 Pro", "11 Pro Max",
    "12 mini", "12", "12 Pro", "12 Pro Max",
    "13 mini", "13", "13 Pro", "13 Pro Max",
    "14", "14 Plus", "14 Pro", "14 Pro Max",
    "15", "15 Plus", "15 Pro", "15 Pro Max",
    "16e", "16", "16 Plus", "16 Pro", "16 Pro Max",
    "17", "Air", "17 Pro", "17 Pro Max",
]
_VALID = {m.lower(): m for m in ALL_MODELS}

_MODEL_RE = re.compile(
    r"(?:iphone|айфон)\s*(?:(air)\b|([xх]s\s*max|[xх]s\s*мах|[xх]r\b|[xх]s\b|[xх]\b|10\b)|(1[1-7])(?!\d)\s*"
    r"(e\b|pro\s*max|promax|pro|plus|mini|air)?)"
)
_X = {"xsmax": "XS Max", "xsмax": "XS Max", "xr": "XR", "xs": "XS", "x": "X", "10": "X"}
_X_LAT = str.maketrans("хма", "xma")
_OTHER_IPHONE = re.compile(r"(?:iphone|айфон)\s*(?:se\b|[4-9](?!\d))")
_RU_VARIANTS = [(re.compile(rf"\b{ru}\b"), en) for ru, en in
                (("промакс", "pro max"), ("про", "pro"), ("макс", "max"), ("плюс", "plus"), ("мини", "mini"),
                 ("эйр", "air"), ("айр", "air"), ("иксэс", "xs"), ("икс", "x"))]
_MEM_RE = re.compile(r"(?<!\d)(64|128|256|512)\s*(?:гб|gb|g\b)|(?<!\d)([12])\s*(?:тб|tb)\b")

_NOT_PHONE = re.compile(
    r"\b(куплю|выкуп|скупка|чехол|чехлы|защитное стекло|стекло на|коробка от|коробку|"
    r"запчаст|донор|ремонт телефонов|копия|реплика|муляж|аналог|китай)"
)
_REJECT = [
    (re.compile(r"r-?sim|рсим|р-сим|(?<!раз)лочен|залочен|(?<!un)locked|под оператор|turbo ?sim"), "лочка/R-SIM"),
    (re.compile(r"восстановл|\bреф\b|refurb|обменк|обменн|\bcpo\b"), "восстановленный/обменка"),
]


@dataclass(frozen=True)
class PhoneModel:
    model: str
    storage_gb: int


def canonical_model(raw: str) -> str | None:
    s = normalize(raw).replace("iphone", "").replace("айфон", "").strip()
    s = re.sub(r"\s+", " ", s).replace("promax", "pro max")
    if s in ("17 air",):
        s = "air"
    return _VALID.get(s)


def _en_variants(s: str) -> str:
    for rx, en in _RU_VARIANTS:
        s = rx.sub(en, s)
    return s


def _find_model(s: str) -> str | None:
    s = _en_variants(s)
    for m in _MODEL_RE.finditer(s):
        if m.group(1):
            return "Air"
        if m.group(2):
            return _X.get(re.sub(r"\s+", "", m.group(2)).translate(_X_LAT))
        num, var = m.group(3), (m.group(4) or "").replace("promax", "pro max")
        var = re.sub(r"\s+", " ", var)
        if var == "air":
            return "Air"
        key = f"{num}{var}" if var == "e" else f"{num} {var}".strip()
        if key.lower() in _VALID:
            return _VALID[key.lower()]
    return None


def _find_storage(s: str) -> int | None:
    m = _MEM_RE.search(s)
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else int(m.group(2)) * 1024


def detect_phone(title: str, text: str = "") -> PhoneModel | None:
    t, d = normalize(title), normalize(text)
    if _NOT_PHONE.search(t):
        return None
    model = _find_model(t)
    if not model and _OTHER_IPHONE.search(_en_variants(t)):
        return None
    model = model or _find_model(d)
    storage = _find_storage(t) or _find_storage(d) or 0
    if not model:
        return None
    return PhoneModel(model, storage)


_NOT_REJECT = re.compile(
    r"\b(не|ни разу не|никогда не|без|нет|никак\w*)\s+(\w+\s+){0,2}"
    r"(r-?sim\w*|рсим\w*|р-сим\w*|лочен\w*|лочк\w*|залочен\w*|заблочен\w*|locked|восстановл\w*|реф\w*|refurb\w*|обменк\w*|"
    r"обменн\w*|cpo)"
    r"|\b(не|ни)\s+(на\s+)?(копи\w*|реплик\w*|муляж\w*|запчаст\w*|донор\w*)"
)


def reject_reason(title: str, text: str = "") -> str | None:
    t = normalize(title)
    sim_part = t.split(",")[-1].strip() if "," in t else ""
    if sim_part == "esim":
        return "только eSIM"
    full = _NOT_REJECT.sub(" ", t + "\n" + normalize(text))
    if _NOT_PHONE.search(full) and re.search(r"на запчаст|донор|копия|реплика|муляж", full):
        return "не телефон/запчасти"
    for rx, reason in _REJECT:
        if rx.search(full):
            return reason
    if re.search(r"только esim|esim only|нет слота под сим|без слота", full):
        return "только eSIM"
    return None
