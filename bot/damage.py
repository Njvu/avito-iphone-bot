import re
from dataclasses import dataclass, field

from bot.text import normalize

_FACE = r"(?:face\s*id|ф[еэ]йс\s*(?:айди|ид)\b|ф[еэ]йсайди)"
_FACE_ANY = r"(?:\bф[еэ]йс\w*(?:\s*(?:айди|ид)\b)?|face\s*id|faceid)"
_FACE_DEAD = r"(?:не работа\w*|нерабоч\w*|не пашет|отвал\w*|отлетел\w*|слетел\w*)"
_CLOUD = r"(?:icloud|айклауд|айклоуд|apple\s*id|эпл\s*айди|учетн\w* запис\w*|аккаунт\w*)"
_FRAUD = r"(?<!не )(?<!не с )(?:мошенник|машиник)(?!ам\b|ов\b)"

BAD = [
    (r"не работают датчики или функции|(не работа\w*|нерабоч\w*|сломан\w*)\s+(ф[еэ]йс|face)|ф[еэ]йс\s*(айди|ид)?\s*не работа", "не работает Face ID/датчики"),
    (r"плохо реагирует|не реагирует на (касан|нажат)|тач (глючит|плохо)|сенсор (глючит|плохо)", "проблемы с тачем"),
    (rf"{_FACE}.{{0,30}}(не работает|нерабоч|не функционир|отвал|слетел|не настраива)|{_FACE}[^,.;!?\n]{{0,30}}\bнет\b|"
     rf"(не работает|нерабоч|нет)\s*{_FACE}|\b(без|нет)\s+{_FACE_ANY}|{_FACE_ANY}\s*[-:]?\s*(нет\b|{_FACE_DEAD})|"
     rf"{_FACE_DEAD}\s+(только\s+)?{_FACE_ANY}|проблем\w*\s+с\s+{_FACE_ANY}", "не работает Face ID"),
    (r"проблем\w*\s+с\s+(тач|сенсор|камер|динамик|микрофон|сет[ьи]|связ|зарядк|кнопк|wi-?fi|блютуз|bluetooth|модем|звук)",
     "неисправный модуль"),
    (r"(разбит|трещин|треснул|лопнул|побит|раскол|сломан)\w*\s+(на\s+)?((стекл|линз|объектив)\w*\s+)?(блок\w*\s+)?камер|"
     r"((стекл|линз|объектив)\w*\s+)?камер\w*\s+(разбит|трещин|треснул|лопнул|побит|раскол|сломан)|"
     r"(разбит|трещин|треснул|лопнул|побит|раскол)\w*[^,.;!?\n]{0,25}(стекл|линз|объектив)\w*\s+камер(ы|ах)\b", "разбита камера"),
    (r"(не работает|нерабоч|не ловит|глючит).{0,25}(камер|тач|сенсор|динамик|сеть|связь|кнопк|микрофон|вспышк|модем|wi-?fi|блютуз|bluetooth|зарядк)", "неисправный модуль"),
    (r"(камер|тач|сенсор|динамик|сеть|связь|кнопк|микрофон|модем|wi-?fi).{0,20}(не работает|нерабоч|не ловит|сломан|глючит)", "неисправный модуль"),
    (r"(полос|пятн|засвет)\w*.{0,20}(экран|диспле)|(экран|диспле)\w*.{0,20}(полос|пятн|засвет)|битые пиксел|черн(ый|ое) экран|не показывает|экран не реагирует|не реагирует на нажат|тач не|выгор", "повреждён дисплей"),
    (r"утоп|после воды|был в воде|попала? вода|окисл", "утопленник"),
    (r"не включается|не грузится|не загружается|вечная загрузка|бутлуп|не заряжается|сломал\w* плат|\bплат[аы]\b.{0,20}(сгорел|сломан|умерл|неисправ)|после ремонта не", "не включается/плата"),
    (r"на запчаст|донор|под восстановление|требует ремонта платы", "на запчасти"),
    (r"icloud|айклауд|айклоуд|apple\s*id|эпл\s*айди|учетн.{0,10}запис", "__icloud__"),
    (r"не помню пароль|забыл пароль|забыла пароль|пароль\w*[^,.;!?\n]{0,20}(не помн\w*|забыл\w*)|"
     r"(?<!без )(?<!нет )(?<!никаких )(блокировк[^,.;!?\n]{0,15}активац|активационн\w*\s+блокировк)(?!\w*\s+(нет|не было|отсутству\w*)\b)|"
     r"режим пропажи", "заблокирован iCloud"),
    (rf"{_CLOUD}.{{0,25}}(чуж|не мо[йяе]|не наш|левы|бывш|{_FRAUD})|\b(чуж\w*|их|левы\w*|бывш\w*)\s+{_CLOUD}|"
     r"стоит\s+(их|чуж\w*|не мой|не моя|левы\w*)|пароль.{0,20}(не знаю|неизвест)|(не знаю|неизвест\w*).{0,15}пароль|"
     rf"(?<!не )(?<!без )взлома\w*|{_FRAUD}", "чужой/взломанный iCloud"),
    (r"неоригинальн|не оригинал|неизвестн.{0,5}детал|копи[яю] экрана|китайск.{0,10}(экран|диспле)|oled копи|incell", "неоригинальные детали"),
]
_ICLOUD_OK = r"(icloud|айклауд|айклоуд|apple\s*id).{0,30}(чист|свобод|не привяз|отвяз|выйд|выход|сброшен|без привяз)|(чист|свобод|отвязан).{0,20}(icloud|айклауд|apple\s*id)"
_LOCK = r"(?<!не )(?<!без )(?<!никаких )(?<!за)(?<!раз)(?:(?:за)?блок(?=ир|ан|нут)(?!\w*\s+нет\b)|\b(?:на|в)\s+блоке\b(?!\s+(?:от\s+)?(?:зарядк|питани)))"
_ICLOUD_BAD = (rf"(icloud|айклауд|айклоуд|apple\s*id).{{0,40}}({_LOCK}|(?<!не )(?<!не)привязан|не отвязан|не могу выйти|"
               rf"не выйти|помогу выйти)|{_LOCK}.{{0,20}}(icloud|айклауд|apple\s*id)|"
               r"(icloud|айклауд|айклоуд|apple\s*id)\w*[ \t]*[-:]?[ \t]*(на[ \t]+)?блок\b(?![ \t]+(от[ \t]+)?(зарядк|питани))|"
               r"(?<!без )(?<!нет )\bблок[ \t]+(на[ \t]+)?(icloud|айклауд|айклоуд|apple\s*id)")
_FACEID_OK = rf"{_FACE}.{{0,25}}(работа(ет|ют)|исправ|в норме|ок\b|есть)|(работает|исправен|рабочий).{{0,15}}{_FACE}"
_ALL_OK = r"все работает|всё работает|все функции[^.!?\n]{0,40}работают|полностью исправ|полностью рабоч|без нарекани"

_PROT = r"(защитн\w*\s*стекл\w*|пленк\w*|бронестекл\w*|стекл\w*\s*защитн\w*)"
_PROT_BROKEN = rf"{_PROT}.{{0,30}}(разбит|трещин|треснул|лопнул|побит)|(разбит|трещин|треснул|лопнул|побит)\w*.{{0,20}}{_PROT}"
_CRACK = r"(разбит|трещин|треснул|лопнул|побит|раскол|паутин|сетк[аи] трещ|сломан)"
_GAP = r"[^,.;!?\n]{0,25}"
_SCREEN = rf"{_CRACK}\w*{_GAP}(экран|стекл|диспле|модул)|(экран|стекл|диспле)\w*{_GAP}{_CRACK}"
_BACK = rf"{_CRACK}\w*{_GAP}(крышк|задн|корпус)|(крышк|задн\w* стекл|корпус)\w*{_GAP}{_CRACK}"
_ALLOWED_PART = r"(экран\w*|стекл\w*|диспле\w*|модул\w*|крышк\w*|задн\w*|корпус\w*)"
_ALLOWED_PHRASE = rf"{_ALLOWED_PART}\s*(\w+\s*){{0,2}}{_CRACK}\w*|{_CRACK}\w*\s*(\w+\s*){{0,2}}{_ALLOWED_PART}"
_OTHER_FAULT = (r"(?<!не )(?<!ни )\b(неисправ\w*|сломан\w*|глючит|глюки|барахлит|перезагружа\w*|греется|"
                r"вздул\w*|вздут\w*|погнут\w*|выключается сам\w*|отключается сам\w*|не видит|не ловит|"
                r"не работа(?!ю\b|ем\b)\w*|нерабоч\w*|не функционир\w*|не заряжа\w*|дефект \w+ (платы|модуля))")
_REPLACED = r"(менял\w*|замен\w*|поменял\w*|ставил\w*).{0,25}(экран|диспле|модул|акб|аккумулятор|батаре|крышк|корпус)"
_GOOD = r"без_дефектов|идеал|отличн\w* состояни|хорош\w* состояни|без дефектов|без поломок|без нарекани|не битый|не бит\b|все работает|всё работает|как нов|полностью исправ|состояние нового"
_BATTERY = re.compile(r"(?:акб|аккумулятор\w*|батаре\w*|емкост\w*)[^\d]{0,25}(\d{2,3})(?!\d)\s*(?:%|проц\w*)?")
_PARAM_GOOD = {"новое", "отличное", "хорошее", "удовлетворительное", "как новое"}

DEFECTS = {
    "screen": "Разбит экран / стекло",
    "back": "Разбита задняя крышка",
    "unclear": "Состояние не описано (с пометкой ⚠️)",
    "faceid": "Не работает Face ID",
    "display": "Дисплей: полосы, пятна, тач",
    "module": "Камера, динамик, сеть, кнопки",
    "water": "Утопленник",
    "dead": "Не включается / плата",
    "parts": "На запчасти / донор",
    "icloud": "iCloud заблокирован или чужой",
    "nonorig": "Неоригинальные детали",
    "repair": "«Требуется ремонт» без описания",
    "lock": "Лочка / R-SIM",
    "refurb": "Восстановленный / обменка",
    "esim": "Только eSIM",
    "other": "Прочие неисправности",
}
DEFAULT_ALLOWED = frozenset({"screen", "back", "unclear"})
REASON_KEYS = {
    "не работает Face ID/датчики": "faceid", "не работает Face ID": "faceid", "проблемы с тачем": "display",
    "повреждён дисплей": "display", "неисправный модуль": "module", "разбита камера": "module",
    "утопленник": "water", "не включается/плата": "dead", "на запчасти": "parts", "не телефон/запчасти": "parts",
    "заблокирован iCloud": "icloud", "чужой/взломанный iCloud": "icloud", "неоригинальные детали": "nonorig",
    "лочка/R-SIM": "lock", "восстановленный/обменка": "refurb", "только eSIM": "esim",
}


def blocked(reason: str | None, allowed=DEFAULT_ALLOWED) -> bool:
    return bool(reason) and REASON_KEYS.get(reason) not in allowed


@dataclass
class DamageResult:
    accepted: bool
    damage: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    reason: str = ""
    battery: int | None = None
    faceid_ok: bool = False
    icloud_ok: bool = False


_PROBLEM = (r"\bне\b|\bнет\b|сломан|глюч|барахл|проблем|плох|неисправ|дефект|отказ|умер|сгорел|замен|менял|"
            r"копи|неориг|утоп|вод[аеуы]|влаг|окисл|полос|пятн|засвет|пиксел|запчаст|донор|восстановл|обменк|"
            r"заблок|блокир|лоч|r-?sim|рсим|esim|магазин|гаранти|рассроч|trade")
_FLAG_TOPICS = [
    (rf"{_FACE}|ф[еэ]йс|датчик", rf"{_FACE}|ф[еэ]йс|датчик"),
    (r"icloud|айклауд|apple\s*id|аккаунт|пароль|активац", r"icloud|айклауд|айклоуд|apple\s*id|пароль|активац|учетн"),
    (r"включ|загруж|плат|заряж|питан", r"включ|загруж|плат[аеуы]?\b|заряж|питан"),
    (r"утоп|вод|влаг|окисл", r"утоп|вод[аеуы]|влаг|окисл"),
    (r"оригин|детал|копи|замен|менял", r"оригин|детал|копи|замен|менял|ставил"),
    (r"полос|пятн|засвет|пиксел|диспле|изображ|тач|сенсор|реагир",
     r"полос|пятн|засвет|пиксел|диспле|изображ|тач|сенсор|реагир"),
    (r"камер|динамик|микрофон|сет[ьи]|связ|кнопк|вспышк|wi-?fi|блютуз|bluetooth|модем|звук|вибр",
     r"камер|динамик|микрофон|сет[ьи]\b|связ|кнопк|вспышк|wi-?fi|блютуз|bluetooth|модем|звук|вибр"),
    (r"запчаст|донор", r"запчаст|донор"),
    (r"восстанов|обменк|реф", r"восстановл|обменк|\bреф\b|refurb"),
    (r"esim|sim|сим|оператор|лоч", r"esim|r-?sim|рсим|оператор|лоч"),
    (r"магазин|салон", r"магазин|салон|рассроч|кредит"),
]


def _phrases(text: str) -> list[str]:
    return [p for p in re.split(r"[.!?;\n]+", strip_negations(normalize(text))) if p.strip()]


def flag_confirmed(flag: str, text: str) -> bool:
    f = normalize(flag)
    phrases = _phrases(text)
    for topic, evidence in _FLAG_TOPICS:
        if re.search(topic, f) and any(re.search(evidence, p) and re.search(_PROBLEM, p) for p in phrases):
            return True
    return False


def battery_percent(text: str) -> int | None:
    m = _BATTERY.search(normalize(text))
    if m and 50 <= int(m.group(1)) <= 100:
        return int(m.group(1))
    return None


_NEG_WORDS = (r"(царапин\w*|трещин\w*|скол\w*|разбит\w*|бит\w*|сломан\w*|дефект\w*|потертост\w*|поломок|повреждени\w*|"
              r"вмятин\w*|битост\w*|неисправност\w*)")
_NEG_NOUNS = r"(царапин|трещин|сколов|дефектов|повреждений|неисправностей|поломок|вмятин|потертостей)\b"
_GAP_WORD = (r"(?!(?:работа|включа|выключа|ловит|заряжа|загружа|грузит|видит|реагир|функционир|показыва|настраива|"
             r"помн|зна|face|id\b|ф[еэ]йс|айди|icloud|айклауд|apple|камер|динамик|микрофон|кнопк|тач|сенсор|звук|"
             r"сет[ьи]\b|связ|коробк|зарядк|зарядн|кабел|провод|шнур|чехл|чехол|наушник|блок|документ|чек|комплект|упаковк))\w+")
_SCREEN_FLAW = r"(полос\w*|пятен|пятн\w*|засвет\w*|выгора\w*|выгорел\w*|(битых\s+)?пиксел\w*)"
_NEGATIONS = [
    rf"\b(без|нет|ни одной|ни одного|ни единой|не|ни|никак\w*)\s+((и|или|,|{_GAP_WORD})\s+){{0,3}}{_NEG_WORDS}"
    rf"(\s*(и|или|,)\s*{_NEG_WORDS})*",
    rf"\b{_NEG_NOUNS}(\s*(и|или|,)\s*{_NEG_NOUNS})*(\s+{_GAP_WORD}){{0,2}}\s+(нет|не было|не обнаружено|не выявлено|отсутству\w*)\b",
    r"\b(не|ни разу не|ничего не|никогда не)\s+(менял\w*|вскрыв\w*|ремонтир\w*|падал\w*|тонул\w*|бит\w*|разбит\w*|"
    r"утоп\w*|топил\w*|(по)?был\w*\s+в\s+вод\w*|попада?л\w*\s+(в\s+)?вод\w*|на\s+запчаст\w*|донор\w*|"
    r"глюч\w*|тупит|тормоз\w*|лага\w*|залипа\w*|висн\w*|зависа\w*|завис\b|зависл\w*|выгор\w*)",
    r"\bв\s+вод\w*\s+(не|ни разу не|никогда не)\s+(по)?был\w*",
    rf"\b(без|нет|никак\w*|ни одного|ни одной|ни единого)\s+((и|или|,|битых|битого|видимых)\s+){{0,2}}{_SCREEN_FLAW}"
    rf"(\s*(и|или|,)\s*{_SCREEN_FLAW})*",
    rf"\b{_SCREEN_FLAW}(\s*(и|или|,)\s*{_SCREEN_FLAW})*(\s+на\s+(экран\w*|диспле\w*))?\s+(нет|не было|не обнаружено|отсутству\w*)\b",
    r"\b(нет|без|никаких|не было|не возникало)\s+(каких-?либо\s+|никаких\s+)?(проблем|претензий)\b",
    r"\b(проблем|нарекани\w*|вопросов|претензий)\b(\s+[\w-]+){0,4}?\s+(нет|не было|не возникало|не обнаружено)\b",
    r"не работают датчики или функции:?\s*(нет|все работают|отсутствуют|-)",
    r"(экран|корпус|дисплей|стекло|крышка)\s*:?\s*(без дефектов|идеальн\w*|как нов\w*|отличн\w*)",
    r"(кабел\w*|зарядк\w*|провод\w*|блок\w*|чехл?\w*|коробк\w*|наушник\w*|адаптер\w*)\s*(\w+\s*){0,2}(не ?оригинал\w*|неоригинал\w*)",
]


def strip_negations(s: str) -> str:
    for rx in _NEGATIONS:
        s = re.sub(rx, " без_дефектов ", s)
    return s


def inspect_damage(text: str, condition: str = "", unclear_mode: str = "warn",
                   allowed=DEFAULT_ALLOWED) -> DamageResult:
    s = strip_negations(normalize(text))
    cond = normalize(condition).strip()
    battery = battery_percent(s)
    icloud_ok = bool(re.search(_ICLOUD_OK, s))
    faceid_ok = bool(re.search(_FACEID_OK, s)) or bool(re.search(_ALL_OK, s))
    extra = []
    rest = s
    if re.search(_ICLOUD_BAD, s):
        if "icloud" not in allowed:
            return DamageResult(False, reason="заблокирован iCloud", battery=battery)
        extra.append("заблокирован iCloud")
        rest = re.sub(_ICLOUD_BAD, " ", rest)
    for pattern, reason in BAD:
        if reason == "__icloud__" or not re.search(pattern, s):
            continue
        if REASON_KEYS[reason] not in allowed:
            return DamageResult(False, reason=reason, battery=battery)
        if reason not in extra:
            extra.append(reason)
        rest = re.sub(pattern, " ", rest)

    other = re.search(_OTHER_FAULT, re.sub(_ALLOWED_PHRASE, " ", rest))
    if other:
        if "other" not in allowed:
            return DamageResult(False, reason=f"другая неисправность: «{other.group(0)}»", battery=battery)
        extra.append(f"неисправность «{other.group(0)}»")

    warnings = [f"поломка: {x}" for x in extra]
    prot = bool(re.search(_PROT_BROKEN, s))
    cleaned = re.sub(_PROT_BROKEN, " ", s)
    cleaned = re.sub(_PROT + r"\w*", " ", cleaned)
    damage = []
    if re.search(_SCREEN, cleaned):
        if "screen" not in allowed:
            return DamageResult(False, reason="разбит экран (выключено в фильтре)", battery=battery)
        damage.append("разбит экран/стекло")
    if re.search(_BACK, cleaned):
        if "back" not in allowed:
            return DamageResult(False, reason="разбита крышка (выключено в фильтре)", battery=battery)
        damage.append("разбита задняя крышка")
    if prot and not damage:
        warnings.append("разбито только защитное стекло")
    if re.search(_REPLACED, s):
        warnings.append("ранее меняли детали — уточнить оригинальность")

    damage += extra
    if not damage:
        if cond == "требуется ремонт" and "repair" not in allowed:
            return DamageResult(False, reason="требуется ремонт, поломка не указана", battery=battery)
        if not (re.search(_GOOD, s) or cond in _PARAM_GOOD or prot):
            return DamageResult(False, reason="состояние не подтверждено", battery=battery)

    if not (faceid_ok and icloud_ok):
        if unclear_mode == "skip":
            return DamageResult(False, tuple(damage), warnings, "Face ID/iCloud не подтверждены", battery, faceid_ok, icloud_ok)
        warnings.append("уточнить у продавца: " + ", ".join(
            x for x, ok in (("Face ID", faceid_ok), ("iCloud", icloud_ok)) if not ok))
    return DamageResult(True, tuple(damage), warnings, "подходит", battery, faceid_ok, icloud_ok)


def market_dirt(text: str, condition: str = "") -> str:
    s = strip_negations(normalize(text))
    if normalize(condition).strip() == "требуется ремонт":
        return "требуется ремонт"
    cleaned = re.sub(_PROT_BROKEN, " ", s)
    if re.search(_SCREEN, cleaned):
        return "разбит экран"
    if re.search(_BACK, cleaned):
        return "разбита крышка"
    if re.search(_ICLOUD_BAD, s):
        return "заблокирован iCloud"
    return next((r for p, r in BAD if r != "__icloud__" and re.search(p, s)), "")


def is_clean_for_market(text: str, condition: str = "") -> bool:
    return not market_dirt(text, condition)
