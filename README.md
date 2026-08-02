# Area Code Trivia Bot

A Discord bot that teaches you US telephone area codes (Canada/Mexico/Caribbean
support exists in the data pipeline but is switched off for now \u2014 see
"US-only for now" below):

- **Random trivia** — posts a random "did you know" fact about an area code
  (with a small map!) to a chosen channel, at a random interval between
  1 second and 3 hours.
- **`/trivia`** — instantly posts one random trivia fact (doesn't affect the random schedule).
- **`/setchannel`** — pick which channel the random trivia gets posted to.
- **`/quiz`** — an interactive, continuing quiz on area codes.
- **`/quizhistory`** — quiz yourself on the area codes this server has actually seen recently.
- **`/recenttrivia`** — list what's been sent lately.
- Every trivia/quiz message includes a small, low-res map highlighting the
  relevant state (and county, when we can confidently tell which one from
  the source data).

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

### `/trivia`
Immediately posts one random area-code trivia fact to the current channel.

### `/quiz`
Starts an interactive quiz question. Parameters:

| Parameter      | Required | Description |
|----------------|----------|-------------|
| `mode`         | yes | **"Bot shows an area code → you guess the place"** or **"Bot shows a place → you guess the area code"** |
| `country`      | yes | Country to quiz on (autocompletes as you type: US, Canada, Mexico, Bahamas, Jamaica, ...) |
| `answer_mode`  | yes | **"Type your answer in chat"** or **"Pick from multiple-choice buttons"** |
| `subdivisions` | no  | Comma-separated states/provinces to restrict to, e.g. `NY, CA, TX` (autocompletes; 2-letter codes for US states / Canadian provinces) |
| `num_options`  | no  | Number of multiple-choice buttons to show (2–8, default 4). Only used when `answer_mode` is buttons. |

Examples (type these plainly into Discord's slash command fields — no
quote marks needed, Discord's own field separates each parameter):
- `/quiz mode:Bot shows an area code -> you guess the place country:US subdivisions:NY, NJ, CT answer_mode:Type your answer in chat`
- `/quiz mode:Bot shows a place -> you guess the area code country:Canada answer_mode:Pick from multiple-choice buttons num_options:6`

(If you do paste a value wrapped in quotes by habit, e.g. `"NY, NJ, CT"`,
the bot strips stray quote characters automatically so it still works.)

### `/quizhistory`
Same as `/quiz`, but pulls questions only from the area codes that have
actually been sent as trivia in this server recently (auto trivia + `/trivia`
both feed this history). Great for "quiz me on what you've already taught
me." Parameters: `mode`, `answer_mode`, `num_options` — no country/subdivision
filter, since it's scoped to your server's own history already.

### `/recenttrivia [count]`
Lists the most recently sent trivia facts in this server (default 10, max 25),
newest first, with a "how long ago" timestamp.

## Maps

Every trivia fact and quiz question comes with a small (320x200, low-res —
deliberately, per request) PNG map:
- Normally it's the US highlighted with the relevant **state** colored in.
- For the ~20 area codes where the original source text explicitly names a
  **county** (e.g. "Nassau County, Long Island"), the map instead zooms into
  that state and highlights the matched county too. This is intentionally
  conservative: we only highlight a county when it's *named* in the data, we
  never guess one from a city name, so most questions will just show the
  state.
- Alaska, Hawaii, and Puerto Rico get their own solo zoomed map since they're
  nowhere near the mainland US map.

Maps are **pre-generated, static files** in `assets/maps/` (not rendered at
runtime), so the bot itself has no extra dependency (matplotlib is only
needed to *build* them). If you edit `data/raw.txt` and re-run
`parse_data.py`, also re-run:

```bash
pip install matplotlib
python generate_maps.py
```

This regenerates `assets/maps/states/*.png` and `assets/maps/counties/*.png`
from `mapdata/us-states.json` and `mapdata/us-counties.json` (bundled;
sourced from public GeoJSON on GitHub).

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
