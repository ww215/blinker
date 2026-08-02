"""
Parses data/raw.txt into data/area_codes.json

NOTE: for now this keeps United States entries only (Canada/Mexico/Caribbean
rows in raw.txt are parsed but then dropped) \u2014 easy to re-enable later by
removing the `country == "US"` filter below.

Each output record:
{
  "area_code": "212",
  "country": "US",
  "country_name": "United States",
  "subdivision": "NY",              # 2-letter US state code
  "subdivision_name": "New York",   # full state name
  "city": "New York City",          # best-effort primary city/place name
  "counties": ["New York"],         # best-effort, only when explicitly named in the source text
  "is_overlay": false,
  "timezone": "-5"
}
"""
import json
import re

import us_geo

US_STATES = us_geo.US_STATES

CA_PROVINCES = {
    "ON": "Ontario", "QC": "Quebec", "BC": "British Columbia", "AB": "Alberta",
    "MB": "Manitoba", "SK": "Saskatchewan", "NB": "New Brunswick", "NS": "Nova Scotia",
    "NL": "Newfoundland and Labrador", "YT": "Yukon", "PE": "Prince Edward Island",
    "NT": "Northwest Territories", "NU": "Nunavut",
}

COUNTRY_NAMES = {
    "US": "United States",
    "CA": "Canada",
    "MX": "Mexico",
}

INCLUDE_COUNTRIES = {"US"}  # temporarily US-only; widen this set to bring others back


def clean_desc(desc: str) -> str:
    # drop trailing parenthetical notes like "(see split 973, overlay 551)"
    desc = re.sub(r"\([^)]*\)", "", desc)
    desc = desc.strip(" .;")
    return desc


def is_overlay_row(desc: str) -> bool:
    return bool(re.search(r"overlaid on|overlay\b", desc, re.IGNORECASE)) and \
        bool(re.search(r"overlaid on \d", desc, re.IGNORECASE))


def extract_city(desc: str, region: str) -> str:
    d = clean_desc(desc)
    # Remove leading directional / regional qualifiers before a colon, e.g.
    # "N New Jersey: Jersey City, Hackensack" -> "Jersey City, Hackensack"
    # "California: Oakland, East Bay" -> "Oakland, East Bay"
    parts = d.split(":", 1)
    if len(parts) == 2:
        tail = parts[1].strip()
        if tail:
            d = tail
    # take just the first place name before a comma/semicolon
    first = re.split(r"[,;]", d)[0].strip()
    if not first:
        first = d.strip()
    return first if first else region


def main():
    county_names_by_state = us_geo.load_county_names_by_state()

    records = []
    with open("data/raw.txt", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            fields = re.split(r"\t+", line)
            if len(fields) < 4:
                continue
            code_field, region, tz, desc = fields[0], fields[1], fields[2], fields[3]

            if region == "MX" or code_field.startswith("52 "):
                country = "MX"
                subdivision = None
                subdivision_name = None
                area_code = code_field.split()[-1]
                city = "Mexico City"
            elif region in US_STATES:
                country = "US"
                subdivision = region
                subdivision_name = US_STATES[region]
                area_code = code_field
                city = extract_city(desc, subdivision_name)
            elif region in CA_PROVINCES:
                country = "CA"
                subdivision = region
                subdivision_name = CA_PROVINCES[region]
                area_code = code_field
                city = extract_city(desc, subdivision_name)
            else:
                # region == "--" or unknown: country determined from description text
                country = None
                subdivision = None
                subdivision_name = None
                area_code = code_field
                city = clean_desc(desc)
                country_name_guess = city
                country = country_name_guess  # store full name as pseudo-code; fixed below

            if country is None:
                continue

            overlay = is_overlay_row(desc)
            counties = (
                us_geo.extract_counties(desc, subdivision, county_names_by_state)
                if country == "US" and subdivision
                else []
            )

            rec = {
                "area_code": area_code,
                "country": country if country in ("US", "CA", "MX") else None,
                "country_name": COUNTRY_NAMES.get(country, country if isinstance(country, str) else None),
                "subdivision": subdivision,
                "subdivision_name": subdivision_name,
                "city": city,
                "counties": counties,
                "is_overlay": overlay,
                "timezone": tz,
                "raw_description": desc,
            }
            records.append(rec)

    fixed = []
    for r in records:
        if r["country"] is None:
            name = r["country_name"]
            fixed.append({**r, "country": name, "city": name})
        else:
            fixed.append(r)

    fixed = [r for r in fixed if r["country"] in INCLUDE_COUNTRIES]

    with open("data/area_codes.json", "w", encoding="utf-8") as f:
        json.dump(fixed, f, indent=2, ensure_ascii=False)

    _write_embedded_module(fixed)

    print(f"Parsed {len(fixed)} area code records (countries included: {sorted(INCLUDE_COUNTRIES)})")
    with_counties = sum(1 for r in fixed if r["counties"])
    print(f"Records with a confidently-matched county: {with_counties}/{len(fixed)}")


def _write_embedded_module(records: list[dict]) -> None:
    """Bakes the dataset into area_codes_data.py (gzip+base64) so the bot
    works even without the data/ directory (e.g. minimal Docker images)."""
    import base64
    import gzip

    raw = json.dumps(records, ensure_ascii=False)
    b64 = base64.b64encode(gzip.compress(raw.encode("utf-8"))).decode("ascii")

    with open("area_codes_data.py", "w", encoding="utf-8") as out:
        out.write('"""\n')
        out.write("Auto-generated by parse_data.py. Do not edit by hand.\n")
        out.write("Contains the full area-code dataset, gzip-compressed and base64-encoded,\n")
        out.write("so the bot works even if the data/ directory does not ship with the\n")
        out.write("deployment image (e.g. a Dockerfile that only COPYs bot.py).\n")
        out.write('"""\n\n')
        out.write("AREA_CODES_B64 = (\n")
        chunk = 100
        for i in range(0, len(b64), chunk):
            out.write(f'    "{b64[i:i+chunk]}"\n')
        out.write(")\n")


if __name__ == "__main__":
    main()
