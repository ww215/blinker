"""
Shared US geography constants used by parse_data.py and generate_maps.py.
"""
import json
import re
from pathlib import Path

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "VI": "US Virgin Islands", "GU": "Guam",
    "MP": "Northern Mariana Islands",
}

# FIPS state code -> USPS abbreviation, for matching the counties GeoJSON
FIPS_TO_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
    "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD",
    "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE",
    "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY", "72": "PR",
}

# States with no meaningful position on the shared CONUS backdrop map (they
# get their own solo/zoomed image instead).
SOLO_STATES = {"AK", "HI", "PR"}

MAPDATA_DIR = Path(__file__).parent / "mapdata"


def load_county_names_by_state() -> dict[str, set[str]]:
    """Returns {state_abbr: {county_name, ...}} from the bundled counties GeoJSON."""
    path = MAPDATA_DIR / "us-counties.json"
    if not path.exists():
        return {}
    counties = json.load(open(path, encoding="utf-8"))["features"]
    result: dict[str, set[str]] = {}
    for feat in counties:
        abbr = FIPS_TO_ABBR.get(feat["properties"]["STATE"])
        if not abbr:
            continue
        result.setdefault(abbr, set()).add(feat["properties"]["NAME"])
    return result


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def extract_counties(description: str, state_abbr: str, county_names_by_state: dict[str, set[str]]) -> list[str]:
    """Best-effort extraction of explicitly-named counties from a raw area
    code description, cross-checked against real county names for that
    state so we only report ones we're confident about (no guessing)."""
    names = county_names_by_state.get(state_abbr, set())
    if not names:
        return []
    found = set()
    for m in re.finditer(r"([A-Z][A-Za-z.\' -]+?)\s+[Cc]ounty", description):
        cand = m.group(1).strip()
        if cand in names:
            found.add(cand)
    for m in re.finditer(r"[Cc]ounties of ([^()]+?)(?:\.|;|\(|$)", description):
        for part in re.split(r",| and ", m.group(1)):
            cand = part.strip()
            if cand in names:
                found.add(cand)
    return sorted(found)
