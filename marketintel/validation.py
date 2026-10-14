"""Configurable validation + explainable (heuristic, uncalibrated) confidence."""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import analytics
from .catalog import Catalog
from .parsing import ParsedItem
from .util import iso, parse_iso

METHOD_FACTOR = {"pipe_table": 1.0, "generic_line": 0.95, "car_line": 0.85, "deal_line": 0.8}

# code -> (severity, message_fa/en)
RULES: dict[str, tuple[str, str]] = {
    "missing_price": ("error", "قیمت یافت نشد / no price found"),
    "missing_unit": ("warn", "واحد قیمت مشخص نیست / unit missing"),
    "unit_not_in_price_token": ("info", "واحد از سرتیتر یا تنظیم منبع گرفته شده / unit taken from header/source default"),
    "range_price": ("warn", "قیمت به‌صورت بازه است / price given as a range"),
    "multiple_numbers": ("warn", "چند عدد در یک خط / multiple numbers on one line"),
    "ambiguous_separator": ("warn", "جداکننده‌ی عدد مبهم است / ambiguous separator"),
    "unexpected_unit": ("warn", "واحد با دارایی سازگار نیست / unit inconsistent with asset"),
    "implausible_value": ("warn", "مقدار خارج از بازه‌ی منطقی / outside sanity bounds"),
    "possible_scale_error": ("warn", "احتمال خطای مقیاس (×۱۰ⁿ) / possible scale error"),
    "unconfirmed_jump": ("warn", "جهش بزرگ بدون تأیید منبع دیگر / large jump, no independent confirmation"),
    "unusual_move_corroborated": ("info", "حرکت غیرعادی ولی هم‌خوان با منبع مستقل / unusual but corroborated"),
    "statistical_outlier": ("warn", "پرت آماری نسبت به تاریخچه / robust outlier vs history"),
    "stale": ("stale", "اطلاعات کهنه / stale observation"),
    "future_observation": ("warn", "زمان مشاهده در آینده / observation time in the future"),
    "missing_model": ("warn", "مدل خودرو مشخص نیست / vehicle model missing"),
    "missing_variant": ("warn", "تیپ/سال خودرو مشخص نیست / variant/year missing"),
    "discount_inconsistent": ("warn", "درصد تخفیف با قیمت‌ها نمی‌خواند / discount inconsistent with prices"),
}


@dataclass
class Verdict:
    status: str
    flags: list[dict[str, str]] = field(default_factory=list)
    confidence: float = 0.0
    confidence_detail: list[dict[str, Any]] = field(default_factory=list)


def _flag(code: str) -> dict[str, str]:
    sev, msg = RULES[code]
    return {"code": code, "severity": sev, "message": msg}


def validate(conn: sqlite3.Connection, item: ParsedItem, observed_at: datetime, now: datetime,
             source: sqlite3.Row, catalog: Catalog, settings: dict[str, Any],
             record_id: int | None = None) -> Verdict:
    codes: list[str] = [f for f in item.flags if f in RULES]
    asset = catalog.assets.get(item.asset_key or "")
    canon = item.amount_canonical
    cunit = item.canonical_unit

    if item.category == "deal" and "missing_price" in codes and item.extras.get("discount_pct_stated"):
        pass  # percent-only deal is incomplete but not an error; handled as review below
    if item.amount is None and "missing_price" not in codes:
        codes.append("missing_price")

    if asset and canon is not None and cunit is not None:
        if cunit != asset.expected_unit:
            codes.append("unexpected_unit")
        else:
            lo, hi = asset.bounds
            val = float(canon)
            if not lo <= val <= hi:
                codes.append("implausible_value")
                if any(lo <= val * (10 ** k) <= hi for k in (1, 2, 3)):
                    codes.append("possible_scale_error")
        # relative checks against recent history (comparable observations only)
        hist = analytics.history(conn, item.entity_key, since=observed_at - timedelta(hours=24),
                                 until=observed_at, limit=60, statuses=("valid",))
        hist = [h for h in hist if h["canonical_unit"] == cunit and h["id"] != record_id]
        if hist and "implausible_value" not in codes:
            vals = [h["amount_canonical"] for h in hist]
            med = statistics.median(vals)
            ratio = float(canon) / med if med else 1.0
            jump_limit = float(settings.get("unusual_jump_pct") or asset.max_jump_pct)
            jump_limit = min(jump_limit, asset.max_jump_pct)
            if any(abs(ratio / (10.0 ** k) - 1) < 0.05 for k in (-3, -2, -1, 1, 2, 3)):
                codes.append("possible_scale_error")
            elif abs(ratio - 1) * 100 > jump_limit:
                probe = {"entity_key": item.entity_key, "source_id": source["id"],
                         "amount_canonical": float(canon), "canonical_unit": cunit, "id": record_id or -1}
                if analytics.corroborating(conn, probe, now, tolerance_pct=max(3.0, jump_limit / 4),
                                           include_unconfirmed=True):
                    codes.append("unusual_move_corroborated")
                else:
                    codes.append("unconfirmed_jump")
            elif analytics.robust_outlier(vals, float(canon), min_n=int(settings.get("history_min_obs", 3)) + 2):
                codes.append("statistical_outlier")

    if "missing_model" in codes or "missing_variant" in codes:
        pass  # already flagged by parser for cars
    if observed_at > now + timedelta(minutes=10):
        codes.append("future_observation")

    stale_min = asset.stale_minutes if asset else int(
        (conn.execute("SELECT stale_minutes FROM categories WHERE key=?", (item.category,)).fetchone() or [360])[0])
    if (now - observed_at) > timedelta(minutes=stale_min):
        codes.append("stale")

    codes = list(dict.fromkeys(codes))
    flags = [_flag(c) for c in codes]
    sev = {f["severity"] for f in flags}
    if item.category == "deal" and codes == ["missing_price"] and item.extras.get("discount_pct_stated"):
        status = "needs_review"
    elif "error" in sev:
        status = "invalid"
    elif "stale" in sev:
        status = "stale"
    elif "warn" in sev:
        status = "needs_review"
    else:
        status = "valid"

    # explainable confidence: reliability x method, minus named penalties (NOT a probability)
    detail: list[dict[str, Any]] = []
    rel = float(source["reliability"])
    mf = METHOD_FACTOR.get(item.method, 0.8)
    conf = rel * mf
    detail.append({"factor": "source_reliability", "value": rel})
    detail.append({"factor": f"parse_method:{item.method}", "value": mf})
    for f in flags:
        delta = {"warn": -0.15, "error": -0.5, "stale": -0.2, "info": 0.0}[f["severity"]]
        if f["code"] == "unit_not_in_price_token":
            delta = -0.05
        if f["code"] == "unusual_move_corroborated":
            delta = 0.0
        if delta:
            conf += delta
            detail.append({"factor": f["code"], "delta": delta})
    if item.unit_rule.startswith("source_default"):
        conf -= 0.1
        detail.append({"factor": "unit_from_source_default", "delta": -0.1})
    conf = max(0.0, min(1.0, conf))
    detail.append({"factor": "final", "value": round(conf, 3), "note": "heuristic score, not a calibrated probability"})
    return Verdict(status, flags, round(conf, 3), detail)


def observed_iso(dt: datetime) -> str:
    return iso(dt)


__all__ = ["validate", "Verdict", "RULES", "parse_iso"]
