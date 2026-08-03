"""
Builds mapdata/area_code_counties.json: {area_code: [county_name, ...]}

For every area code, takes every city NANPA lists for it (mapdata/us-area-
code-cities.csv, lat/lng per city) and tests each point against real county
polygons (mapdata/us-counties.json) to find which county it falls in. The
union of counties across all of an area code's cities is a solid, genuinely
comprehensive "which counties does this area code touch" answer \u2014 far
better coverage than only trusting counties explicitly named in raw.txt's
free-text descriptions.

Run this before parse_data.py (parse_data.py reads its output).
"""
import csv
import json
import statistics
from pathlib import Path

from matplotlib.path import Path as MplPath

import us_geo

ROOT = Path(__file__).parent
CITIES_CSV = ROOT / "mapdata" / "us-area-code-cities.csv"
COUNTIES_GEOJSON = ROOT / "mapdata" / "us-counties.json"
OUT_PATH = ROOT / "mapdata" / "area_code_counties.json"

STATE_NAME_TO_ABBR = {name: abbr for abbr, name in us_geo.US_STATES.items()}


def reject_outliers(points: list[tuple[str, float, float]]) -> list[tuple[str, float, float]]:
    """points: [(city, lat, lng), ...] for a single area code. Drops points
    that are wildly far from the group's median position \u2014 catches bad
    geocodes in the source CSV (e.g. a city coordinate hundreds of miles off)
    without a fixed distance threshold, since some real area codes (Alaska's
    907 covers the whole state, spanning far more than a typical NPA) are
    legitimately huge. Only kicks in with enough points to get a stable
    estimate; small groups are trusted as-is."""
    if len(points) < 5:
        return points
    med_lat = statistics.median(p[1] for p in points)
    med_lng = statistics.median(p[2] for p in points)
    dists = [((lat - med_lat) ** 2 + (lng - med_lng) ** 2) ** 0.5 for _, lat, lng in points]
    med_dist = statistics.median(dists)
    bound = max(med_dist * 5, 2.0)
    return [p for p, d in zip(points, dists) if d <= bound]


def load_county_paths_by_state():
    counties = json.load(open(COUNTIES_GEOJSON, encoding="utf-8"))["features"]
    by_state: dict[str, list[tuple[str, list[MplPath]]]] = {}
    for feat in counties:
        abbr = us_geo.FIPS_TO_ABBR.get(feat["properties"]["STATE"])
        if not abbr:
            continue
        name = feat["properties"]["NAME"]
        geom = feat["geometry"]
        rings = geom["coordinates"] if geom["type"] == "Polygon" else [p[0] for p in geom["coordinates"]]
        # geom["type"] == "Polygon" -> coordinates = [ring]; MultiPolygon -> [[ring], [ring], ...]
        if geom["type"] == "Polygon":
            ring_list = [geom["coordinates"][0]]
        else:
            ring_list = [poly[0] for poly in geom["coordinates"]]
        paths = [MplPath(ring) for ring in ring_list]
        by_state.setdefault(abbr, []).append((name, paths))
    return by_state


def find_county(lng: float, lat: float, county_paths: list[tuple[str, list[MplPath]]]) -> str | None:
    for name, paths in county_paths:
        for p in paths:
            if p.contains_point((lng, lat)):
                return name
    return None


def main():
    county_paths_by_state = load_county_paths_by_state()

    points_by_npa: dict[str, list[tuple[str, float, float, str]]] = {}
    with open(CITIES_CSV, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 6:
                continue
            npa, city, state_name, country, lat_s, lng_s = row[:6]
            if country.strip() != "US":
                continue
            abbr = STATE_NAME_TO_ABBR.get(state_name.strip())
            if not abbr:
                continue
            try:
                lat, lng = float(lat_s), float(lng_s)
            except ValueError:
                continue
            points_by_npa.setdefault(npa, []).append((city, lat, lng, abbr))

    result: dict[str, set] = {}
    total_points = 0
    unmatched_points = 0
    dropped_outliers = 0

    for npa, entries in points_by_npa.items():
        cleaned = reject_outliers([(c, lat, lng) for c, lat, lng, _ in entries])
        dropped_outliers += len(entries) - len(cleaned)
        cleaned_cities = {c for c, _, _ in cleaned}
        for city, lat, lng, abbr in entries:
            if city not in cleaned_cities:
                continue
            total_points += 1
            county_paths = county_paths_by_state.get(abbr, [])
            county = find_county(lng, lat, county_paths)
            if county:
                result.setdefault(npa, set()).add(county)
            else:
                unmatched_points += 1

    out = {npa: sorted(counties) for npa, counties in sorted(result.items())}
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"Dropped {dropped_outliers} outlier city coordinates before matching")
    print(f"Matched {total_points - unmatched_points}/{total_points} remaining city points to a county")
    print(f"Area codes with at least one county: {len(out)}")
    print(f"Saved to {OUT_PATH}")


if __name__ == "__main__":
    main()
