"""
One-time (offline) generator for small, low-resolution "highlighted state" /
"highlighted county" PNGs used by the bot. Not a runtime dependency of
bot.py \u2014 only needs to be re-run if mapdata/ changes.

Usage:
    python generate_maps.py

Outputs:
    assets/maps/states/{STATE_ABBR}.png
    assets/maps/counties/{AREA_CODE}.png
"""
import json
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import data as datamod
import us_geo

ROOT = Path(__file__).parent
STATES_GEOJSON = ROOT / "mapdata" / "us-states.json"
COUNTIES_GEOJSON = ROOT / "mapdata" / "us-counties.json"
OUT_STATES = ROOT / "assets" / "maps" / "states"
OUT_COUNTIES = ROOT / "assets" / "maps" / "counties"

# deliberately small + low-res, per request
IMG_W, IMG_H = 320, 200
DPI = 70

HIGHLIGHT_COLOR = "#e8562c"
HIGHLIGHT_EDGE = "#ffffff"
BASE_COLOR = "#3a3f47"
BASE_EDGE = "#5b6270"
COUNTY_HIGHLIGHT = "#ffcf3f"

FIPS_TO_ABBR = us_geo.FIPS_TO_ABBR
SOLO_STATES = us_geo.SOLO_STATES


def polygons_from_geom(geom):
    t = geom["type"]
    if t == "Polygon":
        yield geom["coordinates"][0]
    elif t == "MultiPolygon":
        for poly in geom["coordinates"]:
            yield poly[0]


def bbox_of_rings(rings, pad_frac=0.12):
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    px = (x1 - x0) * pad_frac or 0.5
    py = (y1 - y0) * pad_frac or 0.5
    return x0 - px, x1 + px, y0 - py, y1 + py


def new_fig():
    fig, ax = plt.subplots(figsize=(IMG_W / DPI, IMG_H / DPI), dpi=DPI)
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")
    ax.axis("off")
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)
    return fig, ax


def save(fig, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI, transparent=True)
    plt.close(fig)


def slugify(name: str) -> str:
    return us_geo.slugify(name)


def main():
    states = json.load(open(STATES_GEOJSON, encoding="utf-8"))["features"]
    counties = json.load(open(COUNTIES_GEOJSON, encoding="utf-8"))["features"]

    name_to_abbr = {}
    for abbr, name in us_geo.US_STATES.items():
        name_to_abbr[name] = abbr

    # --- CONUS backdrop (48 states + DC), computed once ---
    conus_rings = []
    state_rings_by_name = {}
    for feat in states:
        name = feat["properties"]["name"]
        abbr = name_to_abbr.get(name)
        rings = list(polygons_from_geom(feat["geometry"]))
        state_rings_by_name[name] = rings
        if abbr and abbr not in SOLO_STATES:
            conus_rings.extend(rings)
    conus_bbox = bbox_of_rings(conus_rings, pad_frac=0.03)

    print(f"Generating {len(states)} state maps...")
    for feat in states:
        name = feat["properties"]["name"]
        abbr = name_to_abbr.get(name)
        if not abbr:
            continue
        out_path = OUT_STATES / f"{abbr}.png"

        if abbr in SOLO_STATES:
            fig, ax = new_fig()
            rings = state_rings_by_name[name]
            for ring in rings:
                xs = [p[0] for p in ring]
                ys = [p[1] for p in ring]
                ax.fill(xs, ys, facecolor=HIGHLIGHT_COLOR, edgecolor=HIGHLIGHT_EDGE, linewidth=0.6)
            x0, x1, y0, y1 = bbox_of_rings(rings, pad_frac=0.15)
            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)
            ax.set_aspect(1.3)
            save(fig, out_path)
        else:
            fig, ax = new_fig()
            for other in states:
                oname = other["properties"]["name"]
                oabbr = name_to_abbr.get(oname)
                if not oabbr or oabbr in SOLO_STATES:
                    continue
                is_target = oabbr == abbr
                color = HIGHLIGHT_COLOR if is_target else BASE_COLOR
                edge = HIGHLIGHT_EDGE if is_target else BASE_EDGE
                zorder = 2 if is_target else 1
                for ring in state_rings_by_name[oname]:
                    xs = [p[0] for p in ring]
                    ys = [p[1] for p in ring]
                    ax.fill(xs, ys, facecolor=color, edgecolor=edge, linewidth=0.3, zorder=zorder)
            x0, x1, y0, y1 = conus_bbox
            ax.set_xlim(x0, x1)
            ax.set_ylim(y0, y1)
            ax.set_aspect(1.3)
            save(fig, out_path)

    print(f"Saved state maps to {OUT_STATES}")

    # --- county maps: only for (state, county) pairs actually referenced ---
    county_rings_by_state = {}
    for feat in counties:
        abbr = FIPS_TO_ABBR.get(feat["properties"]["STATE"])
        if not abbr:
            continue
        county_rings_by_state.setdefault(abbr, {})[feat["properties"]["NAME"]] = list(
            polygons_from_geom(feat["geometry"])
        )

    # --- county maps: one image per area code, highlighting every county
    # that area code's cities fall in (not just a single county) ---
    needed_by_npa: dict[str, list[str]] = {}
    for r in datamod.RECORDS:
        if r.get("counties"):
            needed_by_npa[r["area_code"]] = r["subdivision"], r["counties"]

    print(f"Generating {len(needed_by_npa)} county maps (one per area code)...")
    skipped = 0
    for npa, (abbr, county_list) in sorted(needed_by_npa.items()):
        state_counties = county_rings_by_state.get(abbr, {})
        target_names = [c for c in county_list if c in state_counties]
        if not target_names:
            skipped += 1
            continue

        out_path = OUT_COUNTIES / f"{npa}.png"
        fig, ax = new_fig()
        for cname, rings in state_counties.items():
            is_target = cname in target_names
            color = COUNTY_HIGHLIGHT if is_target else BASE_COLOR
            edge = HIGHLIGHT_EDGE if is_target else BASE_EDGE
            zorder = 2 if is_target else 1
            for ring in rings:
                xs = [p[0] for p in ring]
                ys = [p[1] for p in ring]
                ax.fill(xs, ys, facecolor=color, edgecolor=edge, linewidth=0.25, zorder=zorder)

        all_rings = [ring for rings in state_counties.values() for ring in rings]
        target_rings = [ring for name in target_names for ring in state_counties[name]]
        # Zoom to the combined extent of the highlighted counties (with
        # generous padding), capped to the state's own extent.
        x0, x1, y0, y1 = bbox_of_rings(target_rings, pad_frac=0.6)
        sx0, sx1, sy0, sy1 = bbox_of_rings(all_rings, pad_frac=0.04)
        x0, x1 = max(x0, sx0), min(x1, sx1)
        y0, y1 = max(y0, sy0), min(y1, sy1)
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        ax.set_aspect(1.3)
        save(fig, out_path)

    if skipped:
        print(f"  [skip] {skipped} area code(s) had counties not found in the county geojson")
    print(f"Saved county maps to {OUT_COUNTIES}")


if __name__ == "__main__":
    main()
