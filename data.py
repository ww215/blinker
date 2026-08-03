"""
Loads and queries the area code dataset (data/area_codes.json).
"""
import base64
import gzip
import json
import random
from pathlib import Path
from typing import Optional

import us_geo
from area_codes_data import AREA_CODES_B64

DATA_PATH = Path(__file__).parent / "data" / "area_codes.json"


def _load_records() -> list[dict]:
    # Prefer the external JSON file if present (e.g. you hand-edited it or
    # regenerated it with parse_data.py). Falls back to the dataset baked
    # into area_codes_data.py, which always ships with the code and doesn't
    # depend on a data/ directory being present in the deployment image.
    if DATA_PATH.exists():
        try:
            with open(DATA_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    raw = gzip.decompress(base64.b64decode(AREA_CODES_B64)).decode("utf-8")
    return json.loads(raw)


RECORDS: list[dict] = _load_records()

# Countries that have subdivisions we track (US states, Canadian provinces)
COUNTRIES_WITH_SUBDIVISIONS = {"US", "CA"}


def country_display_name(country_key: str) -> str:
    for r in RECORDS:
        if r["country"] == country_key:
            return r["country_name"] or country_key
    return country_key


def get_countries() -> list[tuple[str, str]]:
    """Returns list of (key, display_name), sorted by display name."""
    seen = {}
    for r in RECORDS:
        seen[r["country"]] = r["country_name"] or r["country"]
    return sorted(seen.items(), key=lambda kv: kv[1])


def get_subdivisions(country_key: str) -> list[tuple[str, str]]:
    """Returns list of (code, name) subdivisions for a country, if any."""
    seen = {}
    for r in RECORDS:
        if r["country"] == country_key and r["subdivision"]:
            seen[r["subdivision"]] = r["subdivision_name"]
    return sorted(seen.items(), key=lambda kv: kv[0])


def location_label(r: dict) -> str:
    """Human readable location string for a record, e.g. 'New York City, NY'."""
    if r["subdivision"]:
        return f"{r['city']}, {r['subdivision']}"
    return f"{r['city']}, {r['country_name']}"


QUOTE_CHARS = "\"'\u201c\u201d\u2018\u2019"


def clean_token(s: str) -> str:
    """Strips whitespace and stray straight/curly quote characters from a
    user-typed field, so pasting an example like '"NY, NJ, CT"' (quotes and
    all) still matches correctly instead of silently breaking on the first
    and last items."""
    return s.strip().strip(QUOTE_CHARS).strip()


def filter_records(
    country: Optional[str] = None,
    subdivisions: Optional[list[str]] = None,
    prefer_non_overlay: bool = True,
) -> list[dict]:
    pool = RECORDS
    if country:
        country = clean_token(country)
        pool = [r for r in pool if r["country"] == country]
    if subdivisions:
        subs = {clean_token(s).upper() for s in subdivisions if clean_token(s)}
        pool = [r for r in pool if r["subdivision"] and r["subdivision"].upper() in subs]

    if prefer_non_overlay:
        non_overlay = [r for r in pool if not r["is_overlay"]]
        if non_overlay:
            pool = non_overlay

    return pool


def pick_random_record(
    country: Optional[str] = None,
    subdivisions: Optional[list[str]] = None,
    exclude_area_code: Optional[str] = None,
    exclude_area_codes: Optional[set] = None,
) -> Optional[dict]:
    """`exclude_area_codes` (a set built up over a whole quiz session) is
    preferred when given \u2014 it avoids repeating ANY record already seen
    this session, not just the immediately previous one. Falls back to
    `exclude_area_code` (single value) for backwards compatibility. If
    excluding the whole set would empty the pool, the exclusion is dropped
    for this pick so the quiz starts a fresh lap instead of stalling."""
    pool = filter_records(country, subdivisions)
    if not pool:
        return None
    if exclude_area_codes and len(pool) > 1:
        narrowed = [r for r in pool if r["area_code"] not in exclude_area_codes]
        if narrowed:
            pool = narrowed
    elif exclude_area_code and len(pool) > 1:
        narrowed = [r for r in pool if r["area_code"] != exclude_area_code]
        if narrowed:
            pool = narrowed
    return random.choice(pool)


def pick_distractors(correct: dict, pool: list[dict], value_fn, count: int) -> list[str]:
    """Picks `count` distractor values (distinct from the correct one) from pool."""
    correct_value = value_fn(correct)
    candidates = list({value_fn(r) for r in pool if value_fn(r) != correct_value})
    random.shuffle(candidates)
    return candidates[:count]


# ---------------------------------------------------------------------------
# County pool \u2014 completely separate from the area-code pool above.
# Flattened one row per (area_code, county) pair, e.g. area code 315 touching
# 4 counties yields 4 separate county-pool rows all sharing that area code.
# ---------------------------------------------------------------------------

def get_county_records(subdivisions: Optional[list[str]] = None) -> list[dict]:
    pool = filter_records(country="US", subdivisions=subdivisions)
    out = []
    for r in pool:
        for county in r.get("counties") or []:
            out.append({
                "area_code": r["area_code"],
                "county": county,
                "subdivision": r["subdivision"],
                "subdivision_name": r["subdivision_name"],
                "country": r["country"],
                "country_name": r["country_name"],
            })
    return out


def county_label(cr: dict) -> str:
    return f"{cr['county']} County, {cr['subdivision']}"


def all_counties_for_area_code(area_code: str) -> list[str]:
    """Every county a given area code touches (not just the single one a
    flattened county-pool row happens to carry) \u2014 used for the map, which
    always highlights the full set at once."""
    for r in RECORDS:
        if r["area_code"] == area_code:
            return r.get("counties") or []
    return []


def pick_random_county_record(
    subdivisions: Optional[list[str]] = None,
    exclude_keys: Optional[set] = None,
) -> Optional[dict]:
    """Picks a random COUNTY, not a random (area_code, county) row.

    get_county_records() flattens one row per area code that touches a
    county, so a county covered by many overlapping area codes (e.g. Los
    Angeles County, served by 8+ codes) would otherwise show up far more
    often than a county served by just one \u2014 every county should be
    equally likely to come up here, regardless of how many area codes
    happen to reach it. We group the flattened rows back up by county and
    pick among those groups instead, then pick one of that county's
    covering area codes at random just to have a concrete record to
    return (it doesn't affect what's asked/graded \u2014 see
    build_county_question / grade_county_answer, neither of which touch
    area_code).

    `exclude_keys` is a set of (county, subdivision) tuples already seen
    this quiz session. If excluding all of them would empty the pool, the
    exclusion is dropped for this pick so a session starts a fresh lap
    instead of stalling once every county's been shown once.
    """
    pool = get_county_records(subdivisions)
    if not pool:
        return None

    groups: dict[tuple, list[dict]] = {}
    for r in pool:
        groups.setdefault((r["county"], r["subdivision"]), []).append(r)

    group_keys = list(groups.keys())
    if exclude_keys and len(group_keys) > 1:
        narrowed = [k for k in group_keys if k not in exclude_keys]
        if narrowed:
            group_keys = narrowed

    chosen_key = random.choice(group_keys)
    return random.choice(groups[chosen_key])


# ---------------------------------------------------------------------------
# Full US county pool \u2014 used ONLY by the "guess the county from the map"
# quiz (/countyquiz). Deliberately has NOTHING to do with the area-code
# dataset above: it's built straight from the counties GeoJSON (the same
# geometry the map is rendered from), so every real US county is in the
# pool exactly once and has an identical chance of coming up, regardless
# of how many (or how few, or zero) area codes our area-code dataset
# happens to associate with it.
# ---------------------------------------------------------------------------

_ALL_US_COUNTIES: Optional[list[dict]] = None


def _load_all_us_counties() -> list[dict]:
    global _ALL_US_COUNTIES
    if _ALL_US_COUNTIES is None:
        by_state = us_geo.load_county_names_by_state()
        out = []
        for abbr, names in by_state.items():
            state_name = us_geo.US_STATES.get(abbr, abbr)
            for county in sorted(names):
                out.append({
                    "county": county,
                    "subdivision": abbr,
                    "subdivision_name": state_name,
                    "country": "US",
                    "country_name": "United States",
                })
        _ALL_US_COUNTIES = out
    return _ALL_US_COUNTIES


def get_all_us_counties(subdivisions: Optional[list[str]] = None) -> list[dict]:
    pool = _load_all_us_counties()
    if subdivisions:
        subs = {clean_token(s).upper() for s in subdivisions if clean_token(s)}
        pool = [r for r in pool if r["subdivision"] in subs]
    return pool


def pick_random_us_county(
    subdivisions: Optional[list[str]] = None,
    exclude_keys: Optional[set] = None,
) -> Optional[dict]:
    """Picks a random real US county, with NO connection to area codes at
    all \u2014 every county in the (filtered) pool has an identical chance.

    `exclude_keys` is a set of (county, subdivision) tuples already seen
    this quiz session; if excluding all of them would empty the pool, the
    exclusion is dropped for this pick so a session starts a fresh lap
    instead of stalling once every county's been shown once."""
    pool = get_all_us_counties(subdivisions)
    if not pool:
        return None
    if exclude_keys and len(pool) > 1:
        narrowed = [r for r in pool if (r["county"], r["subdivision"]) not in exclude_keys]
        if narrowed:
            pool = narrowed
    return random.choice(pool)
