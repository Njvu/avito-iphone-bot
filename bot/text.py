import re

_LAT2CYR = str.maketrans("aceopxyktmh", "асеорхуктмн")
_CYR2LAT = str.maketrans("асеорхуктмн", "aceopxyktmh")
_CYR = re.compile(r"[а-яё]")
_LAT = re.compile(r"[a-z]")
_WORD = re.compile(r"[a-zа-яё]+")
_LATIN_WORDS = {"iphone", "pro", "max", "plus", "mini", "air", "face", "id", "icloud", "esim", "sim",
                "gb", "tb", "apple", "promax", "trade", "in"}


def _fix_word(m: re.Match) -> str:
    w = m.group(0)
    c, l = len(_CYR.findall(w)), len(_LAT.findall(w))
    if c and l:
        lat = w.translate(_CYR2LAT)
        if lat in _LATIN_WORDS:
            return lat
        return w.translate(_LAT2CYR) if c >= l else w.translate(_CYR2LAT)
    return w


def normalize(text: str) -> str:
    s = (text or "").lower().replace("ё", "е").replace("\xa0", " ")
    s = _WORD.sub(_fix_word, s)
    s = s.replace("ё", "е")
    return re.sub(r"[ \t]+", " ", s)


def chunk_lines(lines: list[str], limit: int = 3800) -> list[str]:
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    return chunks + [cur] if cur else chunks
