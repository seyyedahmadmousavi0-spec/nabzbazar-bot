"""Loads the configurable asset/car dictionaries (marketintel/data/*.json, overridable by env dir)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .textnorm import normalize_for_match

DATA_DIR = Path(os.environ.get("MI_DATA_DIR", Path(__file__).parent / "data"))
_B = r"(?<![\u0600-\u06FFa-z0-9])"
_A = r"(?![\u0600-\u06FFa-z0-9])"


def alias_regex(alias: str) -> re.Pattern[str]:
    return re.compile(_B + re.escape(normalize_for_match(alias)) + _A)


@dataclass(frozen=True)
class Asset:
    key: str
    category: str
    name_fa: str
    aliases: tuple[str, ...]
    unit_of_measure: str | None
    bounds: tuple[float, float]
    max_jump_pct: float
    stale_minutes: int

    @property
    def expected_unit(self) -> str:
        if self.key in ("gold_ounce", "oil_brent"):
            return "USD"
        return "USD" if self.category in ("crypto", "energy") else "IRR"


class Catalog:
    def __init__(self, assets_path: Path | None = None, cars_path: Path | None = None):
        a = json.loads((assets_path or DATA_DIR / "assets.json").read_text(encoding="utf-8"))
        c = json.loads((cars_path or DATA_DIR / "cars.json").read_text(encoding="utf-8"))
        self.assets: dict[str, Asset] = {}
        self._alias: list[tuple[re.Pattern[str], Asset, str]] = []
        for x in a["assets"]:
            asset = Asset(x["key"], x["category"], x["name_fa"], tuple(x["aliases"]), x.get("unit_of_measure"),
                          (float(x["bounds"][0]), float(x["bounds"][1])), float(x.get("max_jump_pct", 15)),
                          int(x.get("stale_minutes", 360)))
            self.assets[asset.key] = asset
            for al in asset.aliases:
                self._alias.append((alias_regex(al), asset, al))
        self.categories: list[dict] = a["categories"]
        self.brands: dict = c["brands"]
        self.cities: list[str] = [normalize_for_match(x) for x in c["cities"]]
        self._brand_alias = [(alias_regex(al), key) for key, b in self.brands.items() for al in b["aliases"]]
        self._model_alias = {
            key: [(alias_regex(al), mk) for mk, als in b["models"].items() for al in als]
            for key, b in self.brands.items()
        }

    def find_assets(self, w: str) -> list[tuple[int, int, Asset]]:
        """All non-overlapping alias hits (longest alias wins on overlap)."""
        hits = []
        for rx, asset, al in self._alias:
            for m in rx.finditer(w):
                hits.append((m.start(), m.end(), asset, len(al)))
        hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
        taken: list[tuple[int, int, Asset]] = []
        for s, e, asset, _ in hits:
            if all(e <= ts or s >= te for ts, te, _ in taken):
                taken.append((s, e, asset))
        return sorted(taken, key=lambda t: t[0])

    def find_brand(self, w: str) -> tuple[int, int, str] | None:
        best = None
        for rx, key in self._brand_alias:
            m = rx.search(w)
            if m and (best is None or (m.end() - m.start()) > (best[1] - best[0])):
                best = (m.start(), m.end(), key)
        return best

    def find_model(self, brand_key: str, w: str) -> tuple[int, int, str] | None:
        for rx, mk in self._model_alias.get(brand_key, []):
            m = rx.search(w)
            if m:
                return m.start(), m.end(), mk
        return None

    def find_city(self, w: str) -> str | None:
        for c in self.cities:
            if re.search(_B + re.escape(c) + _A, w):
                return c
        return None


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    return Catalog()
