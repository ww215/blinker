# Area Code & County Trivia Bot

A Discord bot that teaches you US telephone area codes **and** the counties
they cover \u2014 as two completely separate tracks, each with their own
trivia and quiz commands (Canada/Mexico/Caribbean support exists in the data
pipeline but is switched off for now \u2014 see "US-only for now" below).

- **Random trivia** — posts a random fact to a chosen channel at a random
  interval between 1 second and 3 hours. Each time it fires, it randomly
  picks **either** an area-code fact **or** a county fact.
- **Area code track:** `/trivia`, `/quiz`, `/quizhistory`, `/recenttrivia`
- **County track:** `/countytrivia`, `/countyquiz`, `/countyquizhistory`, `/countyrecenttrivia`
- **`/setchannel`** — pick which channel the random trivia gets posted to (applies to both tracks).
- Every trivia/quiz message includes a small, low-res map: the area-code
  track always shows the state; the county track zooms in and highlights
  the specific county/counties.

## Setup

1. **Create a Discord application & bot**
   - Go to https://discord.com/developers/applications → New Application.
   - Bot tab → Add Bot → copy the token.
   - Under "Privileged Gateway Intents", enable **Message Content Intent**
     (needed so the bot can read your typed quiz answers).
   - OAuth2 → URL Generator → scopes: `bot`, `applications.commands`.
     Bot permissions: `Send Messages`, `Embed Links`, `Read Message History`.
     Use the generated URL to invite the bot to your server.

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set your token**
   ```bash
   cp .env.example .env   # then edit .env, or just export the variable
   export DISCORD_TOKEN=your-bot-token-here
   ```

4. **Run the bot**
   ```bash
   python bot.py
   ```

Slash commands sync automatically on startup (can take up to a few minutes to
appear the very first time, depending on Discord's cache).

## Commands

### `/setchannel [channel]`
Sets the channel where random trivia messages will be posted (requires
"Manage Server" permission). Defaults to the channel the command is run in.
Applies to **both** tracks \u2014 each scheduled post randomly picks area
code or county.

### Area code track

**`/trivia`** — immediately posts one random area-code trivia fact.

**`/quiz`** — interactive area-code quiz. Parameters:

| Parameter      | Required | Description |
|----------------|----------|-------------|
| `mode`         | yes | **"Bot shows an area code → you guess the place"** or **"Bot shows a place → you guess the area code"** |
| `country`      | yes | Country to quiz on (currently US-only, see below) |
| `answer_mode`  | yes | **"Type your answer in chat"** or **"Pick from multiple-choice buttons"** |
| `subdivisions` | no  | Comma-separated states to restrict to, e.g. `NY, CA, TX` (autocompletes) |
| `num_options`  | no  | Number of multiple-choice buttons (2–8, default 4). Buttons mode only. |

**`/quizhistory`** — same as `/quiz`, but pulls only from area codes actually
sent as trivia in this server recently. Parameters: `mode`, `answer_mode`, `num_options`.

**`/recenttrivia [count]`** — lists the most recently sent **area-code**
trivia facts (default 10, max 25), newest first.

### County track

**`/countytrivia`** — immediately posts one random county trivia fact.

**`/countyquiz`** — interactive county quiz. Parameters:

| Parameter      | Required | Description |
|----------------|----------|-------------|
| `mode`         | yes | **"Bot shows an area code → you guess the county"** or **"Bot shows a county → you guess the area code"** |
| `answer_mode`  | yes | **"Type your answer in chat"** or **"Pick from multiple-choice buttons"** |
| `subdivisions` | no  | Comma-separated states to restrict to, e.g. `NY, CA, TX` (autocompletes) |
| `num_options`  | no  | Number of multiple-choice buttons (2–8, default 4). Buttons mode only. |

**`/countyquizhistory`** — same as `/countyquiz`, but pulls only from
counties actually sent as trivia in this server recently.

**`/countyrecenttrivia [count]`** — lists the most recently sent **county**
trivia facts (default 10, max 25), newest first.

### Both quiz commands work the same way

Examples (type these plainly into Discord's slash command fields — no
quote marks needed, Discord's own field separates each parameter):
- `/quiz mode:Bot shows an area code -> you guess the place country:US subdivisions:NY, NJ, CT answer_mode:Type your answer in chat`
- `/countyquiz mode:Bot shows a county -> you guess the area code answer_mode:Pick from multiple-choice buttons num_options:6`

(If you do paste a value wrapped in quotes by habit, e.g. `"NY, NJ, CT"`,
the bot strips stray quote characters automatically so it still works.)

Every quiz keeps going after each question \u2014 do nothing for 3 seconds
and a new question (same settings) fires automatically. Send `.` in that
window to stop, or append `.` to a typed answer (e.g. `907.`) to answer and
stop in one message. Correct-answer streaks are tracked per player and shown
(with a ping) on every result.

## Maps

Every trivia fact and quiz question comes with a small (320x200, low-res —
deliberately, per request) PNG map:
- **Area code track:** always the US with the relevant **state** highlighted.
- **County track:** zoomed into that state, highlighting **every county the
  area code actually touches** (not just one). This isn't guessed from a
  city name \u2014 it's built by taking every city NANPA lists for that area
  code (`mapdata/us-area-code-cities.csv`) and testing each one's real
  coordinates against actual county polygons (point-in-polygon), so an area
  code spanning 6 counties shows all 6, highlighted together. Coverage:
  **265 of 275 area codes (96%)**; the rest (rare/overlay/territory codes
  not in that source data) fall back to the plain state map.
- Alaska, Hawaii, and Puerto Rico get their own solo zoomed state map since
  they're nowhere near the mainland US map.

Maps are **pre-generated, static files** in `assets/maps/` (not rendered at
runtime), so the bot itself has no extra dependency (matplotlib is only
needed to *build* them). If you edit `data/raw.txt` and want to regenerate
everything from scratch:

```bash
pip install matplotlib
python build_county_coverage.py   # (re)builds mapdata/area_code_counties.json
python parse_data.py              # rebuilds data/area_codes.json with counties
python generate_maps.py           # rebuilds assets/maps/*.png
```

Data sources (all bundled in `mapdata/`, all public):
- `us-states.json`, `us-counties.json` \u2014 state/county boundary GeoJSON.
- `us-area-code-cities.csv` \u2014 NANPA-derived city list per area code
  (from the `ravisorg/Area-Code-Geolocation-Database` GitHub project), used
  to figure out which counties an area code actually reaches.

## US-only for now

Per request, Canada/Mexico/Caribbean entries are currently filtered out —
`parse_data.py` still parses them from `raw.txt`, they're just dropped by
`INCLUDE_COUNTRIES = {"US"}` before writing the final dataset. To bring them
back later, widen that set and re-run `parse_data.py` (note: `generate_maps.py`
would need non-US map sources added before those countries could show maps
too).

`data/raw.txt` is the source area-code table (US, Canada, Mexico, plus a
handful of Caribbean countries/territories that use the NANP). Non-geographic
entries (toll-free, emergency, reserved, government, etc.) were excluded.

`parse_data.py` turns `data/raw.txt` into `data/area_codes.json`, the file
the bot actually reads at runtime. Re-run it after editing `raw.txt`:

```bash
python parse_data.py
```

## Notes / possible extensions

- Currently one bot process serves any number of servers; each server gets
  its own independent random-trivia timer once `/setchannel` is used there.
- `data/settings.json` stores each server's configured trivia channel — back
  it up if you care about the config.
- City names are extracted heuristically from the original free-text
  descriptions, so a few are approximate (e.g. a whole county/region name
  instead of a single city). Feel free to hand-edit `data/area_codes.json`
  for any entries you want cleaned up — it won't be overwritten unless you
  rerun `parse_data.py`.
