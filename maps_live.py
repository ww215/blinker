"""
Runtime map renderer.

Old approach: `generate_maps.py` pre-baked ~265+52 separate static PNG
files to disk (one per area code / state) and bot.py just read whichever
file it needed.

New approach (this module): all state + county boundary geometry is loaded
into memory ONCE, when the bot process starts (see `_MapData`). For every
state we keep exactly one persistent matplotlib map already populated with
every county/state shape as a real object (a `Polygon` patch) — nothing is
redrawn from scratch. When the bot needs to show a map, it just:

  1. re-colors the relevant patches on that one already-loaded map
     (highlight vs. base color),
  2. zooms the view to the right extent,
  3. takes a "screenshot" of it (renders straight to PNG bytes in memory,
     no disk file involved),

then that PNG is attached to the Discord message. So there's truly one map
per state living in memory for the whole life of the bot, not hundreds of
separate pre-generated images.

bot.py runs these renders in a worker thread (asyncio.to_thread) so the
CPU-bound savefig() call doesn't block the gateway's event loop. That means
two renders CAN now happen concurrently (e.g. two /quiz calls for the same
state, or the background trivia loop firing mid-render), so every public
render function below is serialized behind `_RENDER_LOCK` \u2014 without it,
two threads recoloring/rezooming the same shared Figure at once would
produce corrupted or wrong-colored maps.
"""
import io
import json
import threading
from pathlib import Path
from typing import Optional

# Guards every persistent Figure/patch mutation in this module (construction
# and re-coloring alike). Renders are cheap (small, low-res images) so
# serializing them costs little, and it's the only thing that makes it safe
# to call these from multiple threads at once.
_RENDER_LOCK = threading.Lock()

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Polygon

import us_geo

ROOT = Path(__file__).parent
STATES_GEOJSON = ROOT / "mapdata" / "us-states.json"
COUNTIES_GEOJSON = ROOT / "mapdata" / "us-counties.json"

# deliberately small + low-res, per request
IMG_W, IMG_H = 320, 200
DPI = 70

HIGHLIGHT_COLOR = "#e8562c"
HIGHLIGHT_EDGE = "#ffffff"
BASE_COLOR = "#3a3f47"
BASE_EDGE = "#5b6270"
COUNTY_HIGHLIGHT = "#e6231c"

FIPS_TO_ABBR = us_geo.FIPS_TO_ABBR
SOLO_STATES = us_geo.SOLO_STATES


def _polygons_from_geom(geom):
    t = geom["type"]
    if t == "Polygon":
        yield geom["coordinates"][0]
    elif t == "MultiPolygon":
        for poly in geom["coordinates"]:
            yield poly[0]


def _bbox_of_rings(rings, pad_frac=0.12):
    xs = [p[0] for ring in rings for p in ring]
    ys = [p[1] for ring in rings for p in ring]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    px = (x1 - x0) * pad_frac or 0.5
    py = (y1 - y0) * pad_frac or 0.5
    return x0 - px, x1 + px, y0 - py, y1 + py


def _new_fig():
    fig = Figure(figsize=(IMG_W / DPI, IMG_H / DPI), dpi=DPI)
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0, 0, 1, 1))
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")
    ax.axis("off")
    return fig, ax


def _render_png_bytes(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, dpi=DPI, transparent=True, format="png")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Geometry, loaded once for the whole process.
# ---------------------------------------------------------------------------

class _MapData:
    def __init__(self):
        states = json.load(open(STATES_GEOJSON, encoding="utf-8"))["features"]
        counties = json.load(open(COUNTIES_GEOJSON, encoding="utf-8"))["features"]
        name_to_abbr = {name: abbr for abbr, name in us_geo.US_STATES.items()}

        self.state_rings_by_abbr: dict[str, list] = {}
        conus_rings = []
        for feat in states:
            name = feat["properties"]["name"]
            abbr = name_to_abbr.get(name)
            if not abbr:
                continue
            rings = list(_polygons_from_geom(feat["geometry"]))
            self.state_rings_by_abbr[abbr] = rings
            if abbr not in SOLO_STATES:
                conus_rings.extend(rings)
        self.conus_bbox = _bbox_of_rings(conus_rings, pad_frac=0.03)

        self.county_rings_by_state: dict[str, dict[str, list]] = {}
        for feat in counties:
            abbr = FIPS_TO_ABBR.get(feat["properties"]["STATE"])
            if not abbr:
                continue
            self.county_rings_by_state.setdefault(abbr, {})[feat["properties"]["NAME"]] = list(
                _polygons_from_geom(feat["geometry"])
            )


_data: Optional[_MapData] = None


def _get_data() -> _MapData:
    global _data
    if _data is None:
        _data = _MapData()
    return _data


# ---------------------------------------------------------------------------
# ONE persistent CONUS map (48 states + DC), used by the area-code track.
# Built once on first use, then just re-colored + re-screenshotted per call.
# ---------------------------------------------------------------------------

class _ConusMap:
    def __init__(self):
        d = _get_data()
        self.fig, self.ax = _new_fig()
        self.patches_by_abbr: dict[str, list[Polygon]] = {}
        for abbr, rings in d.state_rings_by_abbr.items():
            if abbr in SOLO_STATES:
                continue
            plist = []
            for ring in rings:
                patch = Polygon(ring, closed=True, facecolor=BASE_COLOR,
                                 edgecolor=BASE_EDGE, linewidth=0.3, zorder=1)
                self.ax.add_patch(patch)
                plist.append(patch)
            self.patches_by_abbr[abbr] = plist
        x0, x1, y0, y1 = d.conus_bbox
        self.ax.set_xlim(x0, x1)
        self.ax.set_ylim(y0, y1)
        self.ax.set_aspect(1.3)

    def render(self, target_abbr: str) -> bytes:
        for abbr, plist in self.patches_by_abbr.items():
            is_target = abbr == target_abbr
            color = HIGHLIGHT_COLOR if is_target else BASE_COLOR
            edge = HIGHLIGHT_EDGE if is_target else BASE_EDGE
            zorder = 2 if is_target else 1
            for patch in plist:
                patch.set_facecolor(color)
                patch.set_edgecolor(edge)
                patch.set_zorder(zorder)
        return _render_png_bytes(self.fig)


_conus_map: Optional[_ConusMap] = None


def _get_conus_map() -> _ConusMap:
    global _conus_map
    if _conus_map is None:
        _conus_map = _ConusMap()
    return _conus_map


# ---------------------------------------------------------------------------
# ONE persistent map per state's counties, used by the county track. Built
# lazily (first time that state is needed) and cached for the rest of the
# process's life.
# ---------------------------------------------------------------------------

class _CountyMap:
    def __init__(self, abbr: str):
        d = _get_data()
        state_counties = d.county_rings_by_state.get(abbr, {})
        self.fig, self.ax = _new_fig()
        self.patches_by_county: dict[str, list[Polygon]] = {}
        for cname, rings in state_counties.items():
            plist = []
            for ring in rings:
                patch = Polygon(ring, closed=True, facecolor=BASE_COLOR,
                                 edgecolor=BASE_EDGE, linewidth=0.25, zorder=1)
                self.ax.add_patch(patch)
                plist.append(patch)
            self.patches_by_county[cname] = plist

        all_rings = [ring for rings in state_counties.values() for ring in rings]
        if all_rings:
            self.sx0, self.sx1, self.sy0, self.sy1 = _bbox_of_rings(all_rings, pad_frac=0.04)
        else:
            self.sx0 = self.sx1 = self.sy0 = self.sy1 = 0
        self.ax.set_xlim(self.sx0, self.sx1)
        self.ax.set_ylim(self.sy0, self.sy1)
        self.ax.set_aspect(1.3)
        self.state_counties = state_counties

    def render(self, target_names: list[str]) -> Optional[bytes]:
        target_names = [c for c in target_names if c in self.state_counties]
        if not target_names:
            return None

        for cname, plist in self.patches_by_county.items():
            is_target = cname in target_names
            color = COUNTY_HIGHLIGHT if is_target else BASE_COLOR
            edge = HIGHLIGHT_EDGE if is_target else BASE_EDGE
            zorder = 2 if is_target else 1
            for patch in plist:
                patch.set_facecolor(color)
                patch.set_edgecolor(edge)
                patch.set_zorder(zorder)

        target_rings = [ring for name in target_names for ring in self.state_counties[name]]
        x0, x1, y0, y1 = _bbox_of_rings(target_rings, pad_frac=0.6)
        x0, x1 = max(x0, self.sx0), min(x1, self.sx1)
        y0, y1 = max(y0, self.sy0), min(y1, self.sy1)
        self.ax.set_xlim(x0, x1)
        self.ax.set_ylim(y0, y1)
        return _render_png_bytes(self.fig)


_county_maps_by_state: dict[str, _CountyMap] = {}


def _get_county_map(abbr: str) -> Optional[_CountyMap]:
    if abbr not in _county_maps_by_state:
        d = _get_data()
        if abbr not in d.county_rings_by_state:
            return None
        _county_maps_by_state[abbr] = _CountyMap(abbr)
    return _county_maps_by_state[abbr]


# ---------------------------------------------------------------------------
# Solo states (AK, HI, PR) \u2014 always fully highlighted, nothing to zoom
# to besides themselves, so the render is identical every time. Cached
# after the first render.
# ---------------------------------------------------------------------------

_solo_state_bytes: dict[str, bytes] = {}


def _render_solo_state(abbr: str) -> Optional[bytes]:
    if abbr in _solo_state_bytes:
        return _solo_state_bytes[abbr]
    d = _get_data()
    rings = d.state_rings_by_abbr.get(abbr)
    if not rings:
        return None
    fig, ax = _new_fig()
    for ring in rings:
        ax.add_patch(Polygon(ring, closed=True, facecolor=HIGHLIGHT_COLOR,
                              edgecolor=HIGHLIGHT_EDGE, linewidth=0.6))
    x0, x1, y0, y1 = _bbox_of_rings(rings, pad_frac=0.15)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect(1.3)
    png_bytes = _render_png_bytes(fig)
    _solo_state_bytes[abbr] = png_bytes
    return png_bytes


# ---------------------------------------------------------------------------
# Public API used by bot.py
# ---------------------------------------------------------------------------

def render_state_png(abbr: str) -> Optional[bytes]:
    """US map with `abbr` highlighted (or a solo zoomed map for AK/HI/PR)."""
    if not abbr:
        return None
    with _RENDER_LOCK:
        if abbr in SOLO_STATES:
            return _render_solo_state(abbr)
        if abbr not in _get_data().state_rings_by_abbr:
            return None
        return _get_conus_map().render(abbr)


def render_county_png(abbr: str, county_names: list[str]) -> Optional[bytes]:
    """State map zoomed to + highlighting every county in `county_names`.
    Returns None if the state/counties aren't found (caller should fall
    back to `render_state_png`)."""
    if not abbr or not county_names:
        return None
    with _RENDER_LOCK:
        county_map = _get_county_map(abbr)
        if county_map is None:
            return None
        return county_map.render(county_names)
