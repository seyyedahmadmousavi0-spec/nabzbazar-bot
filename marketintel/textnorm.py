"""Persian/Arabic text and numeric normalization.

Originals are never discarded by callers: functions here return *new* strings and
structured tokens so the raw text can be stored separately for auditing.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})

_LETTER_MAP = {
    ord("ي"): "ی", ord("ى"): "ی", ord("ې"): "ی", ord("ك"): "ک", ord("ة"): "ه",
    ord("٪"): "%", ord("٫"): ".", ord("٬"): ",", ord("،"): ",",
}
_STRIP_RE = re.compile("[\u200e\u200f\u202a-\u202e\u2066-\u2069\u0640\u064b-\u065f\u0670\ufeff]")
_WS_RE = re.compile(r"[ \t\u00a0\u200c\u200b]+")

SCALE_WORDS = {
    "هزار": Decimal(10) ** 3,
    "میلیون": Decimal(10) ** 6,
    "ملیون": Decimal(10) ** 6,
    "میلیارد": Decimal(10) ** 9,
    "بیلیون": Decimal(10) ** 9,
}
UNIT_WORDS = {
    "تومان": "IRT", "تومن": "IRT", "تومانی": "IRT", "ت": "IRT",
    "ریال": "IRR", "ریالی": "IRR",
    "دلار": "USD", "$": "USD", "usd": "USD",
}

_PERS = r"(?![\u0600-\u06FF])"
_NUM_RE = re.compile(
    r"(?<![\d.,])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d])"
    r"(?:\s*(?P<scale>هزار|میلیون|ملیون|میلیارد|بیلیون)" + _PERS + r")?"
    r"(?:\s*(?P<unit>تومانی|تومان|تومن|ریالی|ریال|دلار|\$|usd|ت)" + _PERS + r")?",
    re.IGNORECASE,
)
_PERCENT_AFTER = re.compile(r"\s*(%|درصد)")
_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_DATE_RE = re.compile(r"(?<!\d)(1[34]\d\d|20\d\d)/(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?!\d)")


def normalize_text(text: str) -> str:
    """Canonical form for matching: NFKC, Latin digits, ی/ک unified, separators unified."""
    s = unicodedata.normalize("NFKC", text or "")
    s = _STRIP_RE.sub("", s)
    s = s.translate(_DIGIT_MAP).translate(_LETTER_MAP)
    # Remove ZWNJ, zero-width spaces or whitespace inside numeric groupings like 262,‌900
    s = re.sub(r'(?<=\d)[,\.]\s*[\u200c\u200b]+\s*(?=\d)', ',', s)
    # Convert slash thousand separators like 5/250 to 5,250
    s = re.sub(r'(?<=\d)/(?=\d{3}(?!\d))', ',', s)
    return s


def normalize_for_match(text: str) -> str:
    s = normalize_text(text).replace("\u200c", " ").lower()
    return _WS_RE.sub(" ", s).strip()


def to_persian_digits(s: str) -> str:
    return s.translate({ord(str(i)): "۰۱۲۳۴۵۶۷۸۹"[i] for i in range(10)})


@dataclass
class NumberToken:
    start: int
    end: int
    raw: str
    value: Decimal              # after explicit scale word (e.g. "۲۵ میلیون" -> 25000000)
    base_value: Decimal         # number as written
    scale_word: str | None
    unit: str | None            # IRT / IRR / USD or None
    is_percent: bool = False
    flags: list[str] = field(default_factory=list)


def mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    chars = list(text)
    for a, b in spans:
        for i in range(a, min(b, len(chars))):
            chars[i] = " "
    return "".join(chars)


def find_aux_spans(text: str) -> tuple[list[tuple[int, int]], list[re.Match[str]], list[re.Match[str]]]:
    """Time and date spans that must not be read as prices."""
    times = list(_TIME_RE.finditer(text))
    dates = list(_DATE_RE.finditer(text))
    spans = [m.span() for m in times] + [m.span() for m in dates]
    return spans, times, dates


def extract_numbers(norm_text: str, ignore: list[tuple[int, int]] | None = None) -> list[NumberToken]:
    """Extract numeric tokens from *normalized* text, honouring explicit scale words and units."""
    aux, _, _ = find_aux_spans(norm_text)
    masked = mask_spans(norm_text, aux + (ignore or []))
    out: list[NumberToken] = []
    for m in _NUM_RE.finditer(masked):
        raw_num = m.group("num")
        flags: list[str] = []
        try:
            base = Decimal(raw_num.replace(",", ""))
        except InvalidOperation:  # pragma: no cover - regex guarantees digits
            continue
        # "61.200" style: a dot followed by exactly 3 digits is ambiguous (decimal vs thousands).
        if re.fullmatch(r"\d{1,3}\.\d{3}", raw_num):
            flags.append("ambiguous_separator")
        scale = m.group("scale")
        value = base * SCALE_WORDS[scale] if scale else base
        unit_raw = m.group("unit")
        unit = UNIT_WORDS.get(unit_raw.lower()) if unit_raw else None
        tail = masked[m.end():m.end() + 8]
        before = masked[max(0, m.start() - 1):m.start()]
        is_pct = (bool(_PERCENT_AFTER.match(tail)) or before == "%") and unit is None and scale is None
        out.append(NumberToken(m.start(), m.end(), norm_text[m.start():m.end()].strip(), value, base,
                               scale, unit, is_pct, flags))
    return out
