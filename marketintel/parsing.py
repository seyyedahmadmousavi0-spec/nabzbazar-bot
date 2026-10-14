"""Layered message parsing: deterministic generic parser + pluggable source-specific parsers.

Design rules: never guess a missing unit silently. Every unit decision is recorded in
`unit_rule`; anything uncertain is carried as a flag for the validation layer.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .catalog import Catalog, default_catalog
from .jalali import jalali_to_gregorian
from .textnorm import NumberToken, extract_numbers, find_aux_spans, normalize_for_match
from .util import UTC, tz

_URL_RE = re.compile(r"https?://[^\s<>\"')]+")
_HEADER_UNIT = re.compile(r"(?:به|واحد|قیمت ها به|قیمت)\s*:?\s*(تومان|تومن|ریال)(?![\u0600-\u06FF])")
_RANGE_SEP = re.compile(r"^\s*(?:تا|الی|-|–|~)\s*$")
_BULLET = re.compile(r"^[\s•\-–*#>🔹🔸🟢🔴⚪️▪️◾️💰💵📌✅❌⭐️]+")
_CODE_RE = re.compile(r"کد(?: تخفیف)?\s*:?\s*([a-z0-9_-]{3,})", re.I)
_EXPIRE_H = re.compile(r"تا\s*(\d{1,3})\s*ساعت دیگر")
_YEAR_RE = re.compile(r"(?:مدل\s*)?(?<!\d)(1[34]\d\d|20[12]\d)(?!\d)")
_TIP_RE = re.compile(r"تیپ\s*(\d)")
_KM_RE = re.compile(r"کارکرد\s*([\d,]+)\s*(?:کیلومتر|km)?")
_VARIANT_WORDS = ("اتوماتیک", "دنده ای", "دستی", "پلاس", "فول", "الیت", "کراس", "توربو")
UNIT_NAMES = {"IRT": "toman", "IRR": "rial", "USD": "usd"}


@dataclass
class ParsedItem:
    category: str
    asset_key: str | None = None
    asset_name: str | None = None
    brand: str | None = None
    model: str | None = None
    variant: str | None = None
    model_year: int | None = None
    side: str | None = None
    amount: Decimal | None = None
    price_low: Decimal | None = None
    price_high: Decimal | None = None
    stated_unit: str | None = None
    unit_rule: str = "none"
    unit_of_measure: str | None = None
    price_type: str = "quoted_market"
    location: str | None = None
    observed_at: datetime | None = None
    observed_source: str = "message_date"
    flags: list[str] = field(default_factory=list)
    method: str = "generic_line"
    snippet: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def amount_canonical(self) -> Decimal | None:
        if self.amount is None or self.stated_unit is None:
            return None
        return self.amount * 10 if self.stated_unit == "IRT" else self.amount

    @property
    def canonical_unit(self) -> str | None:
        return {"IRT": "IRR", "IRR": "IRR", "USD": "USD"}.get(self.stated_unit or "")

    @property
    def entity_key(self) -> str:
        c = self.category
        if c == "car":
            parts = [c, self.brand or "?", self.model or "?", self.variant or "-", str(self.model_year or "?"),
                     self.location or "-"]
            return ":".join(parts)
        if c == "deal":
            name = normalize_for_match(self.asset_name or "")
            h = hashlib.sha1(name.encode()).hexdigest()[:10]
            return f"deal:{h}:{self.extras.get('store') or '-'}"
        return ":".join([c, self.asset_key or "?", self.side or "-", self.unit_of_measure or "-"])


@dataclass
class ParseResult:
    items: list[ParsedItem]
    status: str        # parsed | partial | unparsed
    note: str = ""
    urls: list[str] = field(default_factory=list)


@dataclass
class SourceCtx:
    default_unit: str | None = None       # IRT/IRR configured on the source
    categories: tuple[str, ...] = ()
    timezone: str = "Asia/Tehran"
    message_date: datetime | None = None


# ---------------------------------------------------------------------------------
def _stated_observation(w: str, ctx: SourceCtx) -> tuple[datetime | None, str]:
    if ctx.message_date is None:
        return None, "message_date"
    zone = tz(ctx.timezone)
    md = ctx.message_date.astimezone(zone)
    _, times, dates = find_aux_spans(w)
    day = md.date()
    stated_date = False
    if dates:
        y, m, d = (int(x) for x in dates[0].groups())
        try:
            if y < 1700:
                y, m, d = jalali_to_gregorian(y, m, d)
            day = datetime(y, m, d).date()
            stated_date = True
        except ValueError:
            pass
    if times:
        hh, mm = int(times[0].group(1)), int(times[0].group(2))
        obs = datetime(day.year, day.month, day.day, hh, mm, tzinfo=zone)
        if not stated_date and obs > md + timedelta(minutes=5):
            obs -= timedelta(days=1)
        return obs.astimezone(UTC), "stated_time"
    if stated_date and day != md.date():
        return datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(UTC), "stated_date"
    return None, "message_date"


def _resolve_unit(tok_unit: str | None, header: str | None, ctx: SourceCtx) -> tuple[str | None, str]:
    if tok_unit:
        return tok_unit, f"stated:{UNIT_NAMES[tok_unit]}"
    if header:
        return header, f"message_header:{UNIT_NAMES[header]}"
    if ctx.default_unit:
        return ctx.default_unit, f"source_default:{UNIT_NAMES.get(ctx.default_unit, ctx.default_unit)}"
    return None, "none"


def _header_unit(w: str) -> str | None:
    m = _HEADER_UNIT.search(w)
    if not m:
        return None
    return "IRR" if m.group(1) == "ریال" else "IRT"


def _price_tokens(tokens: list[NumberToken]) -> list[NumberToken]:
    return [t for t in tokens if not t.is_percent]


def _pick_price(seg_tokens: list[NumberToken], seg: str) -> tuple[NumberToken | None, NumberToken | None, list[str]]:
    """Return (primary token, range partner or None, flags)."""
    toks = _price_tokens(seg_tokens)
    if not toks:
        return None, None, ["missing_price"]
    first = toks[0]
    flags = list(first.flags)
    if len(toks) >= 2 and _RANGE_SEP.match(seg[first.end:toks[1].start]):
        return first, toks[1], flags + ["range_price"]
    if len(toks) >= 2:
        flags.append("multiple_numbers")
    return first, None, flags


def _build_market_items(w: str, assets: list, ctx: SourceCtx, header: str | None,
                        line_obs: tuple[datetime | None, str]) -> list[ParsedItem]:
    items: list[ParsedItem] = []
    bounds = [a[0] for a in assets] + [len(w)]
    for i, (s, e, asset) in enumerate(assets):
        seg_start, seg_end = s, bounds[i + 1]
        seg = w[seg_start:seg_end]
        toks = extract_numbers(seg, ignore=[(0, e - s)])
        sides = [(m.start(), "buy" if m.group(0) == "خرید" else "sell")
                 for m in re.finditer(r"خرید|فروش", seg)]
        groups: list[tuple[str | None, list[NumberToken], str]] = []
        if len(sides) == 1:
            groups.append((sides[0][1], toks, seg))
        elif len(sides) > 1:
            if toks and sides[0][0] > toks[0].start:
                for j, (pos, side) in enumerate(sides):
                    prev_pos = sides[j - 1][0] if j > 0 else 0
                    gt = [t for t in toks if prev_pos <= t.start < pos]
                    groups.append((side, gt, seg))
            else:
                for j, (pos, side) in enumerate(sides):
                    end = sides[j + 1][0] if j + 1 < len(sides) else len(seg)
                    gt = [t for t in toks if pos <= t.start < end]
                    groups.append((side, gt, seg))
        else:
            groups.append((None, toks, seg))
        for side, gt, _ in groups:
            primary, partner, flags = _pick_price(gt, seg)
            it = ParsedItem(category=asset.category, asset_key=asset.key, asset_name=asset.name_fa, side=side,
                            unit_of_measure=asset.unit_of_measure, snippet=seg.strip()[:300], flags=flags)
            if primary is not None:
                unit_tok = primary.unit or (partner.unit if partner else None)
                it.stated_unit, it.unit_rule = _resolve_unit(unit_tok, header, ctx)
                if partner is not None:
                    lo, hi = sorted([primary.value, partner.value])
                    it.price_low, it.price_high = lo, hi
                    it.amount = (lo + hi) / 2
                else:
                    it.amount = primary.value
                if primary.scale_word:
                    it.extras["scale_word"] = primary.scale_word
                it.extras["raw_price"] = primary.raw
                pct_toks = [t for t in gt if t.is_percent] or [t for t in toks if t.is_percent]
                if pct_toks:
                    try:
                        pct_val = float(pct_toks[0].value)
                        p_start, p_end = pct_toks[0].start, pct_toks[0].end
                        around = seg[max(0, p_start - 6):min(len(seg), p_end + 6)]
                        if any(c in around for c in ("↓", "🔻", "-", "▼")) or ("🔴" in seg and "🟢" not in seg):
                            if pct_val > 0:
                                pct_val = -pct_val
                        elif any(c in around for c in ("↑", "🔼", "+", "▲")) or ("🟢" in seg and "🔴" not in seg):
                            if pct_val < 0:
                                pct_val = abs(pct_val)
                        it.extras["change_pct"] = round(pct_val, 2)
                    except Exception:
                        pass
                if it.stated_unit is None:
                    it.flags.append("missing_unit")
                elif it.unit_rule.startswith("source_default") or it.unit_rule.startswith("message_header"):
                    it.flags.append("unit_not_in_price_token")
            it.observed_at, it.observed_source = line_obs
            items.append(it)
    return items


def _build_car_item(w: str, cat: Catalog, ctx: SourceCtx, header: str | None,
                    line_obs: tuple[datetime | None, str]) -> ParsedItem | None:
    b = cat.find_brand(w)
    if not b:
        return None
    bs, be, bkey = b
    ignore = [(bs, be)]
    model = cat.find_model(bkey, w)
    mkey = None
    if model:
        mkey = model[2]
        ignore.append((model[0], model[1]))
    it = ParsedItem(category="car", brand=bkey, model=mkey, price_type="asking_price", method="car_line",
                    snippet=w[:300], asset_name=cat.brands[bkey]["name_fa"])
    if (ym := _YEAR_RE.search(w)):
        it.model_year = int(ym.group(1))
        ignore.append(ym.span())
    if (tm := _TIP_RE.search(w)):
        it.variant = f"tip{tm.group(1)}"
        ignore.append(tm.span())
    for word in _VARIANT_WORDS:
        if word in w:
            it.variant = f"{it.variant}+{word}" if it.variant else word
    if (km := _KM_RE.search(w)):
        it.extras["mileage_km"] = int(km.group(1).replace(",", ""))
        ignore.append(km.span())
    it.location = cat.find_city(w)
    it.extras["condition"] = "new" if re.search(r"(?<![\u0600-\u06FF])صفر(?![\u0600-\u06FF])", w) else "used_or_unspecified"
    toks = [t for t in extract_numbers(w, ignore=ignore) if not t.is_percent]
    priced = [t for t in toks if t.unit or t.scale_word]
    primary = priced[0] if priced else None
    if primary is None:
        it.flags.append("missing_price")
    else:
        it.amount = primary.value
        it.stated_unit, it.unit_rule = _resolve_unit(primary.unit, header, ctx)
        it.extras["raw_price"] = primary.raw
        if it.stated_unit is None:
            it.flags.append("missing_unit")
        elif primary.unit is None:
            it.flags.append("unit_not_in_price_token")
    if it.model is None:
        it.flags.append("missing_model")
    if it.variant is None and it.model_year is None:
        it.flags.append("missing_variant")
    it.observed_at, it.observed_source = line_obs
    return it


def _build_deal_item(w: str, ctx: SourceCtx, header: str | None, urls: list[str],
                     line_obs: tuple[datetime | None, str]) -> ParsedItem | None:
    if "تخفیف" not in w and "حراج" not in w:
        return None
    toks = extract_numbers(w)
    pct = [t for t in toks if t.is_percent]
    prices = [t for t in toks if not t.is_percent and (t.unit or t.scale_word or t.value >= 1000)]
    first_num = min([t.start for t in toks], default=len(w))
    name = re.split(r"تخفیف|حراج|کد", w[:max(first_num, 1)] if first_num else w)[0]
    name = _BULLET.sub("", name).strip(" :-–|")
    if not name:
        name = re.sub(r"\s+", " ", w[:60]).strip()
    it = ParsedItem(category="deal", asset_name=name, price_type="seller_stated", method="deal_line",
                    snippet=w[:300])
    if pct:
        it.extras["discount_pct_stated"] = float(pct[0].value)
    if prices:
        vals = sorted(prices, key=lambda t: t.value)
        new_t, old_t = vals[0], vals[-1]
        unit_tok = next((t.unit for t in prices if t.unit), None)
        it.stated_unit, it.unit_rule = _resolve_unit(unit_tok, header, ctx)
        it.amount = new_t.value
        if len(vals) >= 2:
            it.extras["old_price"] = float(old_t.value)
            calc = float((old_t.value - new_t.value) / old_t.value * 100) if old_t.value else 0
            it.extras["discount_pct_computed"] = round(calc, 1)
            if pct and abs(calc - float(pct[0].value)) > 3:
                it.flags.append("discount_inconsistent")
        if it.stated_unit is None:
            it.flags.append("missing_unit")
    else:
        it.flags.append("missing_price")
    if (cm := _CODE_RE.search(w)):
        it.extras["coupon_code"] = cm.group(1).upper()
    if (em := _EXPIRE_H.search(w)):
        it.extras["expires_in_hours"] = int(em.group(1))
    if urls:
        it.extras["urls"] = urls[:3]
    it.observed_at, it.observed_source = line_obs
    return it


# ---------------------------------------------------------------------------------
def parse_generic(text: str, ctx: SourceCtx, cat: Catalog | None = None) -> ParseResult:
    cat = cat or default_catalog()
    urls = _URL_RE.findall(text or "")
    w_all = normalize_for_match(text)
    if not w_all:
        return ParseResult([], "unparsed", "empty text")
    header = _header_unit(w_all)
    msg_obs = _stated_observation(w_all, ctx)
    allowed = set(ctx.categories) if ctx.categories else None
    items: list[ParsedItem] = []
    skipped = 0
    for raw_line in re.split(r"[\n\r]+", text):
        w = normalize_for_match(_BULLET.sub("", raw_line))
        if not w:
            continue
        line_obs = _stated_observation(w, ctx)
        if line_obs[0] is None:
            line_obs = msg_obs
        assets = [h for h in cat.find_assets(w)]
        # an alias that is consumed as a unit ("... 67,000 دلار") is not an asset mention
        toks = extract_numbers(w)
        assets = [h for h in assets if not any(t.start <= h[0] < t.end for t in toks)]
        line_items: list[ParsedItem] = []
        if assets:
            line_items = _build_market_items(w, assets, ctx, header, line_obs)
        elif (car := _build_car_item(w, cat, ctx, header, line_obs)) is not None:
            line_items = [car]
        elif (deal := _build_deal_item(w, ctx, header, urls, line_obs)) is not None:
            line_items = [deal]
        else:
            if items and items[-1].category == "car" and "missing_price" in items[-1].flags and toks:
                priced = [t for t in toks if t.unit or t.scale_word or t.value >= 100_000_000]
                if priced:
                    it_car = items[-1]
                    it_car.amount = priced[0].value
                    it_car.stated_unit, it_car.unit_rule = _resolve_unit(priced[0].unit, header, ctx)
                    it_car.extras["raw_price"] = priced[0].raw
                    it_car.flags.remove("missing_price")
                    if it_car.stated_unit is None:
                        it_car.flags.append("missing_unit")
                    continue
            if toks:
                skipped += 1
            continue
        for it in line_items:
            if allowed is not None and it.category not in allowed:
                skipped += 1
                continue
            items.append(it)
    if not items:
        return ParseResult([], "unparsed", "no recognizable asset/car/deal lines", urls)
    status = "parsed"
    note = ""
    if skipped:
        status, note = "partial", f"{skipped} line(s) with numbers were not recognized"
    return ParseResult(items, status, note, urls)


def parse_pipe_table(text: str, ctx: SourceCtx, cat: Catalog | None = None) -> ParseResult:
    """Example source-specific parser for a recurring template: `name | buy | sell` per line."""
    cat = cat or default_catalog()
    header = _header_unit(normalize_for_match(text))
    obs = _stated_observation(normalize_for_match(text), ctx)
    items: list[ParsedItem] = []
    bad = 0
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [normalize_for_match(c) for c in line.split("|")]
        if len(cells) != 3:
            bad += 1
            continue
        hits = cat.find_assets(cells[0])
        if not hits:
            bad += 1
            continue
        asset = hits[0][2]
        for side, cell in (("buy", cells[1]), ("sell", cells[2])):
            toks = _price_tokens(extract_numbers(cell))
            it = ParsedItem(category=asset.category, asset_key=asset.key, asset_name=asset.name_fa, side=side,
                            unit_of_measure=asset.unit_of_measure, method="pipe_table", snippet=line.strip()[:300])
            if toks:
                it.amount = toks[0].value
                it.stated_unit, it.unit_rule = _resolve_unit(toks[0].unit, header, ctx)
                it.extras["raw_price"] = toks[0].raw
                if it.stated_unit is None:
                    it.flags.append("missing_unit")
            else:
                it.flags.append("missing_price")
            it.observed_at, it.observed_source = obs
            items.append(it)
    if not items:
        return ParseResult([], "unparsed", "pipe_table: no rows")
    return ParseResult(items, "partial" if bad else "parsed", f"{bad} bad row(s)" if bad else "")


PARSERS: dict[str, Callable[..., ParseResult]] = {"generic": parse_generic, "pipe_table": parse_pipe_table}


def register_parser(name: str, fn: Callable[..., ParseResult]) -> None:
    PARSERS[name] = fn


def parse_message(text: str, parser: str, ctx: SourceCtx, cat: Catalog | None = None) -> ParseResult:
    fn = PARSERS.get(parser, parse_generic)
    try:
        return fn(text, ctx, cat)
    except Exception as exc:  # parsers must never crash the pipeline
        return ParseResult([], "unparsed", f"parser_error:{type(exc).__name__}")
