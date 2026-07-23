# Area Code Trivia Bot

A Discord bot that teaches you North American (and Caribbean) telephone area codes:

- **Random trivia** — posts a random "did you know" fact about an area code to a chosen
  channel, at a random interval between 1 second and 3 hours.
- **`/trivia`** — instantly posts one random trivia fact (doesn't affect the random schedule).
- **`/setchannel`** — pick which channel the random trivia gets posted to.
- **`/quiz`** — an interactive quiz question about area codes.

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

Examples:
- `/quiz mode:"area code → place" country:US subdivisions:"NY, NJ, CT" answer_mode:typed`
- `/quiz mode:"place → area code" country:Canada answer_mode:buttons num_options:6`

The bot prefers non-overlay area codes (the original code for a region) when
picking questions, since overlay codes serve the exact same area as another
code and would create ambiguous/duplicate answers.

## Data

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
